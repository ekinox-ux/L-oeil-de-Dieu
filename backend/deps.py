"""Dependances partagees : auth, validation, gestion des index, rate limiting."""

import json
import hmac
import logging
import random
import re
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from engine import SearchEngine
from settings import (
    ALLOW_LOCALHOST_NO_AUTH,
    ALLOWED_ORIGINS,
    ALLOWED_ROOTS,
    API_KEY,
    INDEXES_CONFIG,
    INDEX_PATH_FAILURE_THRESHOLD,
    INDEX_PATH_RECHECK_SECONDS,
    INDEX_MAX_RETRY_DELAY_SECONDS,
    INDEX_RETRY_DELAY_SECONDS,
    resolve_runtime_path,
)

logger = logging.getLogger("oeil-api")

# ---- Registre des index avec etat runtime ----
# Statuts possibles:
#   ready   — index operationnel, engine disponible
#   loading — chargement en cours, engine=None
#   error   — echec de chargement, engine=None, error contient le message
#
# Tous les index sont enregistres avec status="loading" puis charges en
# arriere-plan dans un thread daemon.
indexes: dict[str, dict] = {}
_indexes_lock = threading.RLock()
_loader_thread: threading.Thread | None = None
_loader_thread_lock = threading.Lock()

ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_ID_LENGTH = 64
LOCAL_CLIENT_HEADER = "X-Oeil-Local-Client"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def rate_limit_key(request: Request) -> str:
    """Quota par adresse ET par type de client.

    Le bot et le frontend tournent sur la meme machine: avec la seule adresse
    IP, ils se partageraient un quota unique. L'authentification est verifiee
    avant le limiteur, changer d'en-tete ne permet donc pas de contourner.
    """
    if request.headers.get("X-API-Key"):
        client_type = "api-key"
    elif request.headers.get(LOCAL_CLIENT_HEADER):
        client_type = "local-ui"
    else:
        client_type = "anonymous"
    return f"{get_remote_address(request)}:{client_type}"


limiter = Limiter(key_func=rate_limit_key)


def _retry_delay_for_count(retry_count: int) -> int:
    base = INDEX_RETRY_DELAY_SECONDS * (2 ** max(0, retry_count - 1))
    capped = min(base, INDEX_MAX_RETRY_DELAY_SECONDS)
    jitter = random.uniform(0, capped * 0.15)
    return int(capped + jitter)


def _build_index_record(
    idx_id: str,
    cfg: dict,
    *,
    engine=None,
    status: str,
    error: str = "",
    retry_count: int = 0,
    last_error_at: float = 0.0,
    next_retry_at: float = 0.0,
    retry_delay_seconds: int = 0,
    last_ready_at: float = 0.0,
    autoload: bool = True,
    last_path_check_at: float = 0.0,
    last_path_ok_at: float = 0.0,
    path_failure_count: int = 0,
) -> dict:
    idx_path = str(resolve_runtime_path(cfg["path"]))
    source_path = cfg.get("source", "")
    return {
        "engine": engine,
        "name": cfg.get("name", idx_id),
        "path": idx_path,
        "source": source_path,
        "status": status,
        "error": error,
        "retry_count": retry_count,
        "last_error_at": last_error_at,
        "next_retry_at": next_retry_at,
        "retry_delay_seconds": retry_delay_seconds,
        "last_ready_at": last_ready_at,
        "autoload": autoload,
        "last_path_check_at": last_path_check_at,
        "last_path_ok_at": last_path_ok_at,
        "path_failure_count": path_failure_count,
    }


def _mark_index_loading(idx_id: str):
    with _indexes_lock:
        current = indexes.get(idx_id)
        if not current or current.get("status") == "ready":
            return None
        current["status"] = "loading"
        current["next_retry_at"] = 0.0
        current["retry_delay_seconds"] = 0
        current.setdefault("last_error_at", 0.0)
        current.setdefault("retry_count", 0)
        return dict(current)


def _mark_index_load_failed(idx_id: str, cfg: dict, exc: Exception):
    now = time.time()
    with _indexes_lock:
        current = indexes.get(idx_id, {})
        retry_count = int(current.get("retry_count", 0)) + 1
        retry_delay_seconds = _retry_delay_for_count(retry_count)
        indexes[idx_id] = _build_index_record(
            idx_id,
            cfg,
            engine=None,
            status="error",
            error=str(exc),
            retry_count=retry_count,
            last_error_at=now,
            next_retry_at=now + retry_delay_seconds,
            retry_delay_seconds=retry_delay_seconds,
            last_ready_at=float(current.get("last_ready_at", 0.0) or 0.0),
            autoload=bool(current.get("autoload", True)),
            last_path_check_at=float(current.get("last_path_check_at", 0.0) or 0.0),
            last_path_ok_at=float(current.get("last_path_ok_at", 0.0) or 0.0),
            path_failure_count=int(current.get("path_failure_count", 0) or 0),
        )
    logger.error(
        "Erreur chargement index [%s]: %s (retry #%s dans %ss)",
        idx_id,
        exc,
        retry_count,
        retry_delay_seconds,
    )


def _index_unavailable_detail(cfg: dict) -> str:
    name = cfg["name"]
    status = cfg.get("status", "ready")
    if status == "loading":
        next_retry_at = float(cfg.get("next_retry_at", 0.0) or 0.0)
        if next_retry_at:
            wait_seconds = max(1, int(round(next_retry_at - time.time())))
            return (
                f"L'index '{name}' est en cours de chargement. "
                f"Nouvelle tentative dans environ {wait_seconds}s."
            )
        return f"L'index '{name}' est en cours de chargement. Reessayez dans quelques instants."

    if status == "error":
        error = cfg.get("error", "erreur inconnue")
        retry_count = int(cfg.get("retry_count", 0) or 0)
        next_retry_at = float(cfg.get("next_retry_at", 0.0) or 0.0)
        if next_retry_at:
            wait_seconds = max(1, int(round(next_retry_at - time.time())))
            return (
                f"L'index '{name}' est en erreur: {error}. "
                f"Retry #{retry_count} dans environ {wait_seconds}s."
            )
        return f"L'index '{name}' est en erreur: {error}"

    return f"L'index '{name}' est indisponible."


def _indexes_need_attention() -> bool:
    with _indexes_lock:
        return any(
            cfg.get("autoload", True)
            and cfg.get("engine") is None
            and cfg.get("status", "ready") in {"loading", "error"}
            for cfg in indexes.values()
        )


def _mark_path_checked(idx_id: str, *, ok: bool):
    now = time.time()
    with _indexes_lock:
        cfg = indexes.get(idx_id)
        if not cfg:
            return
        cfg["last_path_check_at"] = now
        if ok:
            cfg["last_path_ok_at"] = now
            cfg["path_failure_count"] = 0


def _increment_path_failure(idx_id: str) -> int:
    now = time.time()
    with _indexes_lock:
        cfg = indexes.get(idx_id)
        if not cfg:
            return 0
        cfg["last_path_check_at"] = now
        cfg["path_failure_count"] = int(cfg.get("path_failure_count", 0) or 0) + 1
        return int(cfg["path_failure_count"])


_PATH_CHECK_TIMEOUT_SECONDS = 2
_PATH_CHECK_SLOTS = threading.BoundedSemaphore(2)


def _is_dir_with_timeout(path: str) -> bool:
    """Verifie is_dir() avec un timeout pour eviter de bloquer les requetes HTTP
    quand un volume est lent a repondre (disque externe, partage reseau)."""
    if not _PATH_CHECK_SLOTS.acquire(blocking=False):
        logger.warning("Verification de chemin differee: deux controles sont deja bloques")
        return False

    result = [False]

    def _check():
        try:
            result[0] = resolve_runtime_path(path).is_dir()
        except OSError:
            result[0] = False
        finally:
            _PATH_CHECK_SLOTS.release()

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout=_PATH_CHECK_TIMEOUT_SECONDS)
    if t.is_alive():
        logger.warning("Timeout (%ss) sur is_dir pour %s", _PATH_CHECK_TIMEOUT_SECONDS, path)
        return False
    return result[0]


def _ready_index_path_missing(idx_id: str, cfg: dict) -> bool:
    path = str(cfg.get("path", ""))
    if not path:
        failure_count = _increment_path_failure(idx_id)
        if failure_count < INDEX_PATH_FAILURE_THRESHOLD:
            logger.warning(
                "Index [%s] sans chemin detecte (%s/%s) — attente avant bascule en erreur",
                idx_id,
                failure_count,
                INDEX_PATH_FAILURE_THRESHOLD,
            )
            return False
        return True

    if _is_dir_with_timeout(path):
        _mark_path_checked(idx_id, ok=True)
        return False

    failure_count = _increment_path_failure(idx_id)
    if failure_count < INDEX_PATH_FAILURE_THRESHOLD:
        logger.warning(
            "Index [%s] chemin indisponible %s (%s/%s) — verification differee",
            idx_id,
            path,
            failure_count,
            INDEX_PATH_FAILURE_THRESHOLD,
        )
        return False

    logger.error(
        "Index [%s] perdu au runtime apres %s echecs consecutifs: %s",
        idx_id,
        failure_count,
        path,
    )
    _mark_index_load_failed(idx_id, cfg, FileNotFoundError(f"Index introuvable: {path}"))
    return True


def refresh_ready_indexes(force: bool = False):
    to_check: list[tuple[str, dict]] = []
    now = time.time()
    with _indexes_lock:
        for idx_id, cfg in indexes.items():
            if cfg.get("status", "ready") != "ready" or cfg.get("engine") is None:
                continue
            last_checked = float(cfg.get("last_path_check_at", 0.0) or 0.0)
            if not force and last_checked and now - last_checked < INDEX_PATH_RECHECK_SECONDS:
                continue
            to_check.append((idx_id, dict(cfg)))

    changed = False
    for idx_id, cfg in to_check:
        if _ready_index_path_missing(idx_id, cfg):
            changed = True

    if changed:
        _ensure_index_loader_running()


def _next_loader_wake_in() -> int | None:
    now = time.time()
    wake_in = None
    with _indexes_lock:
        for cfg in indexes.values():
            if not cfg.get("autoload", True):
                continue
            if cfg.get("engine") is not None:
                continue
            status = cfg.get("status", "ready")
            if status == "loading":
                return 1
            if status == "error":
                next_retry_at = float(cfg.get("next_retry_at", 0.0) or 0.0)
                if not next_retry_at:
                    return 1
                wait_seconds = max(1, int(round(next_retry_at - now)))
                wake_in = wait_seconds if wake_in is None else min(wake_in, wait_seconds)
    return wake_in


def verify_api_key(request: Request):
    client_host = request.client.host if request.client else ""
    api_key = request.headers.get("X-API-Key", "")
    if api_key and API_KEY and hmac.compare_digest(api_key.encode(), API_KEY.encode()):
        return

    # Le bypass local est reserve au frontend officiel. L'en-tete personnalise
    # force un preflight CORS dans un navigateur cross-origin : une page tierce
    # ne peut donc pas declencher silencieusement des appels vers localhost.
    if ALLOW_LOCALHOST_NO_AUTH and client_host in _LOOPBACK_HOSTS:
        local_client = request.headers.get(LOCAL_CLIENT_HEADER, "")
        origin = request.headers.get("Origin", "").rstrip("/")
        allowed_origins = {item.rstrip("/") for item in ALLOWED_ORIGINS}
        origin_allowed = not origin or "*" in allowed_origins or origin in allowed_origins
        if hmac.compare_digest(local_client.encode(), b"1") and origin_allowed:
            return

    raise HTTPException(403, "Acces refuse.")


def get_indexes_snapshot() -> dict[str, dict]:
    """Retourne un instantane coherent du registre sans exposer une iteration concurrente."""
    with _indexes_lock:
        return {idx_id: dict(cfg) for idx_id, cfg in indexes.items()}


def get_index_id_for_config(config: dict) -> str | None:
    """Retrouve l'identifiant d'une configuration retournee par get_engine()."""
    with _indexes_lock:
        for idx_id, current in indexes.items():
            if current is config:
                return idx_id
    return None


def validate_id(value: str, name: str = "id"):
    if not value or len(value) > MAX_ID_LENGTH or not ID_REGEX.match(value):
        raise HTTPException(400, f"Parametre '{name}' invalide.")


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        normalized_path = resolve_runtime_path(str(path))
        normalized_root = resolve_runtime_path(str(root))
        normalized_path.relative_to(normalized_root)
        return True
    except (ValueError, OSError):
        return False


def validate_path_safe(path_str: str) -> Path:
    resolved = resolve_runtime_path(path_str)
    if not resolved.is_dir():
        raise HTTPException(404, "Chemin introuvable.")

    if ALLOWED_ROOTS:
        for root in ALLOWED_ROOTS:
            if _is_under_root(resolved, root):
                return resolved
        raise HTTPException(403, "Chemin hors des racines autorisees.")

    blocked = [
        Path("/etc"), Path("/root"), Path("/boot"), Path("/proc"),
        Path("/sys"), Path("/dev"), Path("C:/Windows"),
        Path("C:/Program Files"), Path("C:/Program Files (x86)"),
        Path("C:/ProgramData"),
    ]
    for root in blocked:
        if _is_under_root(resolved, root):
            raise HTTPException(403, "Acces refuse a ce chemin.")
    return resolved


def get_engine(index_id: str | None) -> tuple[SearchEngine, dict]:
    with _indexes_lock:
        if not indexes:
            raise HTTPException(503, "Aucun index charge.")

    if index_id is None:
        refresh_ready_indexes()
        # Premier index ready
        with _indexes_lock:
            for first_cfg in indexes.values():
                if first_cfg.get("status", "ready") == "ready":
                    return first_cfg["engine"], first_cfg
        _ensure_index_loader_running()
        raise HTTPException(503, "Aucun index pret.")

    validate_id(index_id, "index")
    with _indexes_lock:
        if index_id not in indexes:
            raise HTTPException(404, "Index inconnu.")

    refresh_ready_indexes()
    with _indexes_lock:
        cfg = indexes[index_id]
        status = cfg.get("status", "ready")

    if status == "loading":
        _ensure_index_loader_running()
        raise HTTPException(409, _index_unavailable_detail(cfg))

    if status == "error":
        _ensure_index_loader_running()
        raise HTTPException(409, _index_unavailable_detail(cfg))

    return cfg["engine"], cfg


def _load_single_index(idx_id: str, cfg: dict):
    """Charge un index et met a jour son statut dans le registre."""
    try:
        engine = SearchEngine(str(resolve_runtime_path(cfg["path"])))
        now = time.time()
        with _indexes_lock:
            indexes[idx_id] = _build_index_record(
                idx_id,
                cfg,
                engine=engine,
                status="ready",
                last_ready_at=now,
                last_path_check_at=now,
                last_path_ok_at=now,
            )
        logger.info(
            "Index charge: [%s] %s -> %s",
            idx_id,
            cfg.get("name", idx_id),
            str(resolve_runtime_path(cfg["path"])),
        )
    except Exception as exc:
        _mark_index_load_failed(idx_id, cfg, exc)


def _pending_index_configs() -> list[tuple[str, dict]]:
    pending = []
    now = time.time()
    with _indexes_lock:
        for idx_id, cfg in indexes.items():
            if not cfg.get("autoload", True):
                continue
            status = cfg.get("status", "ready")
            next_retry_at = float(cfg.get("next_retry_at", 0.0) or 0.0)
            should_retry = status == "loading" or (status == "error" and now >= next_retry_at)
            if should_retry and cfg.get("engine") is None:
                pending.append((idx_id, {
                    "id": idx_id,
                    "name": cfg.get("name", idx_id),
                    "path": cfg["path"],
                    "source": cfg.get("source", ""),
                }))
    return pending


def _background_index_loader():
    global _loader_thread
    logger.info("Worker de chargement des index lance")
    try:
        while True:
            pending = _pending_index_configs()
            if pending:
                logger.info(
                    "Indexes en attente: %s",
                    ", ".join(f"{idx_id}" for idx_id, _cfg in pending),
                )
                for idx_id, cfg in pending:
                    current = _mark_index_loading(idx_id)
                    if not current:
                        continue
                    logger.info("Tentative de chargement index [%s]...", idx_id)
                    _load_single_index(idx_id, cfg)

            if not _indexes_need_attention():
                logger.info("Tous les index configures sont prets")
                return

            wake_in = _next_loader_wake_in()
            if wake_in is None:
                logger.info("Aucun retry planifie pour le moment, arret du loader")
                return

            logger.info("Indexes encore indisponibles — prochaine tentative dans %ss", wake_in)
            time.sleep(wake_in)
    finally:
        with _loader_thread_lock:
            _loader_thread = None


def _ensure_index_loader_running():
    global _loader_thread
    if not _indexes_need_attention():
        return
    with _loader_thread_lock:
        if not _indexes_need_attention():
            return
        if _loader_thread and _loader_thread.is_alive():
            return
        _loader_thread = threading.Thread(target=_background_index_loader, daemon=True)
        _loader_thread.start()


def mark_index_error(index_id: str, exc: Exception):
    with _indexes_lock:
        cfg = indexes.get(index_id)
    if not cfg:
        return
    _mark_index_load_failed(index_id, cfg, exc)
    logger.error("Index [%s] marque en erreur runtime: %s", index_id, exc)
    _ensure_index_loader_running()


def request_index_retry(index_id: str) -> dict:
    validate_id(index_id, "index")
    with _indexes_lock:
        cfg = indexes.get(index_id)
        if not cfg:
            raise HTTPException(404, "Index inconnu.")
        if cfg.get("status") == "ready":
            return dict(cfg)
        cfg["status"] = "loading"
        cfg["next_retry_at"] = 0.0
        cfg["retry_delay_seconds"] = 0
        cfg["engine"] = None
        cfg["autoload"] = True
        current = dict(cfg)
    logger.info("Retry manuel demande pour l'index [%s]", index_id)
    _ensure_index_loader_running()
    return current


def load_indexes_config():
    if not INDEXES_CONFIG.is_file():
        logger.warning("Fichier de config introuvable: %s", INDEXES_CONFIG)
        return

    with open(INDEXES_CONFIG, "r", encoding="utf-8") as f:
        configs = json.load(f)

    if not configs:
        return

    # Tous les index sont charges en arriere-plan pour un startup instantane.
    # Le backend repond immediatement (health: 0 index) et les index
    # deviennent disponibles au fur et a mesure.
    with _indexes_lock:
        for cfg in configs:
            idx_id = cfg["id"]
            indexes[idx_id] = _build_index_record(
                idx_id,
                cfg,
                engine=None,
                status="loading",
                autoload=True,
            )
            logger.info("Index enregistre: [%s] %s (chargement en arriere-plan)", idx_id, cfg.get("name", idx_id))

    _ensure_index_loader_running()


def save_indexes_config():
    # Sauvegarde tous les index connus, quel que soit leur statut runtime.
    # Le statut est un etat ephemere; la config doit rester complete.
    with _indexes_lock:
        configs = [
            {"id": idx_id, "name": cfg["name"], "path": cfg["path"], "source": cfg.get("source", "")}
            for idx_id, cfg in indexes.items()
        ]
    INDEXES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = INDEXES_CONFIG.with_suffix(INDEXES_CONFIG.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)
    tmp_path.replace(INDEXES_CONFIG)
