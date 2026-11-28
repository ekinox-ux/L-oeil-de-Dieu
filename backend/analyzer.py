"""
Analyse des fichiers JSONL sources pour extraire des statistiques detaillees.
Emails, telephones, domaines. Les resultats sont caches dans un fichier JSON.

Peut etre lance comme worker autonome:
    python analyzer.py <source_dir> [--no-resume]
"""

import hashlib
import json
import logging
import logging.handlers
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

try:
    import ijson
except ImportError:
    ijson = None

from settings import (
    ANALYZER_DB_BATCH_SIZE,
    ANALYZER_MAX_DOMAIN_ENTRIES,
    ANALYZER_MAX_EMAIL_ENTRIES,
    ANALYZER_MAX_LINE_BYTES,
    ANALYZER_MAX_PHONE_ENTRIES,
    ANALYZER_SAVE_EVERY,
    CACHE_DIR,
)

_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 Mo par fichier
_LOG_BACKUP_COUNT = 3               # garde 3 rotations (analyzer.log.1 a .3)

# Dans l'API, les messages remontent au logger racine (log uvicorn). Seul le
# worker ecrit analyzer.log: un unique processus detient donc le fichier, ce
# qui permet la rotation sous Windows, et l'import par les tests n'ecrit rien.
_logger = logging.getLogger("analyzer")
_logger.setLevel(logging.INFO)


def configure_worker_logging() -> None:
    """Active le journal fichier + console du worker d'analyse (idempotent)."""
    if any(getattr(handler, "_oeil_worker", False) for handler in _logger.handlers):
        return
    log_path = CACHE_DIR / "analyzer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        handler._oeil_worker = True
        _logger.addHandler(handler)
    _logger.propagate = False

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", re.ASCII)
PHONE_REGEX = re.compile(r"\b(?:0[1-9](?:[\s.-]?\d{2}){4}|\+33[\s.-]?\d(?:[\s.-]?\d{2}){4})\b")

PERSO_DOMAINS = {
    "gmail.com", "hotmail.com", "hotmail.fr", "yahoo.com", "yahoo.fr",
    "outlook.com", "outlook.fr", "live.fr", "live.com", "aol.com",
    "icloud.com", "me.com", "msn.com", "wanadoo.fr", "laposte.net",
    "orange.fr", "free.fr", "sfr.fr", "bbox.fr", "numericable.fr",
    "protonmail.com", "gmx.fr", "gmx.com", "mail.com", "ymail.com",
}


def _legacy_artifact_name(source_dir: str) -> str:
    safe_name = source_dir.replace("\\", "_").replace("/", "_").replace(":", "")
    return safe_name


def _artifact_name(source_dir: str) -> str:
    normalized = os.path.normcase(os.path.normpath(source_dir))
    digest = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    leaf = Path(source_dir.rstrip("\\/")).name or "source"
    label = re.sub(r"[^a-zA-Z0-9._-]+", "_", leaf).strip("._-")[:48] or "source"
    return f"{label}_{digest}"


def _artifact_paths(prefix: str, suffix: str, source_dir: str) -> tuple[Path, Path]:
    current = CACHE_DIR / f"{prefix}_{_artifact_name(source_dir)}{suffix}"
    legacy = CACHE_DIR / f"{prefix}_{_legacy_artifact_name(source_dir)}{suffix}"
    return current, legacy


def _prefer_existing_artifact(prefix: str, suffix: str, source_dir: str) -> Path:
    current, legacy = _artifact_paths(prefix, suffix, source_dir)
    if current.exists() or not legacy.exists():
        return current
    return legacy


def _get_lock_path(source_dir: str) -> Path:
    return _prefer_existing_artifact("analyzer", ".lock", source_dir)


def _acquire_lock(source_dir: str) -> bool:
    """Cree le lockfile contenant le source_dir. Retourne False si deja present.
    owner_pid (le backend) puis worker_pid (le worker, renseigne a son
    demarrage) permettent de reconnaitre un lock dont le processus est mort."""
    lock_path = _get_lock_path(source_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(lock_path, "x", encoding="utf-8") as f:
            json.dump({
                "source_dir": source_dir,
                "owner_pid": os.getpid(),
                "worker_pid": None,
                "created_at": time.time(),
            }, f)
        return True
    except FileExistsError:
        return False


def _release_lock(source_dir: str):
    """Supprime le lockfile (best effort)."""
    try:
        for path in set(_artifact_paths("analyzer", ".lock", source_dir)):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _mark_worker_lock(source_dir: str) -> None:
    """Associe le PID du worker au lock puis entretient son heartbeat."""
    lock_path = _get_lock_path(source_dir)
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        data["worker_pid"] = os.getpid()
        data["heartbeat_at"] = time.time()
        tmp_path = lock_path.with_suffix(".lock.tmp")
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        tmp_path.replace(lock_path)
    except (OSError, json.JSONDecodeError):
        _logger.exception("Impossible de mettre a jour le lock analyseur %s", lock_path)


def _touch_lock(source_dir: str) -> None:
    """Met a jour l'activite sans reecrire le lock a chaque lot."""
    try:
        os.utime(_get_lock_path(source_dir), None)
    except OSError:
        pass


_LOCK_MAX_AGE_SECONDS = 86400  # 24h — au-dela, un lock est considere potentiellement orphelin
# Delai laisse au worker pour inscrire son PID apres le lancement.
_LOCK_STARTUP_GRACE_SECONDS = 120


def _pid_is_running(pid) -> bool | None:
    """True si le processus existe, False s'il est termine, None si inconnu.

    Un PID recycle par un autre processus passe pour vivant : le lock est
    alors garde et seule la regle des 24 h s'applique, jamais l'inverse.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(process_query_limited_information, 0, pid)
        if not handle:
            return True if ctypes.get_last_error() == error_access_denied else False
        try:
            exit_code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _lock_process_is_dead(data: dict, lock_age: float) -> bool:
    """Le processus qui tient le lock est-il certainement termine ?

    Le worker inscrit son PID des son demarrage. Tant qu'il ne l'a pas fait,
    le lock appartient au backend qui l'a lance ; on lui laisse un court delai
    avant de conclure que le worker n'a jamais demarre.
    """
    worker_pid = data.get("worker_pid")
    if worker_pid is not None:
        return _pid_is_running(worker_pid) is False
    if lock_age < _LOCK_STARTUP_GRACE_SECONDS:
        return False
    return _pid_is_running(data.get("owner_pid")) is False


def _lock_age(entry: Path, now: float) -> float | None:
    """Age du lock, ou None s'il vient d'etre supprime (fin du worker)."""
    try:
        return now - entry.stat().st_mtime
    except OSError:
        return None


def _lock_has_recent_progress(source_dir: str, now: float) -> bool:
    mtime = _most_recent_progress_mtime(source_dir)
    if mtime is None:
        return False
    return (now - mtime) < _LOCK_MAX_AGE_SECONDS


def _iter_analyzer_locks():
    try:
        for entry in CACHE_DIR.iterdir():
            if entry.name.startswith("analyzer_") and entry.name.endswith(".lock"):
                yield entry
    except OSError:
        return


def _find_active_lock() -> str | None:
    """Scanne CACHE_DIR pour trouver un lockfile actif.
    Retourne le source_dir contenu dans le lock, ou None.

    Un lockfile dont le worker est mort est supprime aussitot. Un lockfile
    trop ancien (> 24h) l'est aussi, sauf si un fichier progress associe a ete
    modifie recemment (signe d'activite). Un lockfile illisible, corrompu ou
    supprime pendant la lecture est ignore (pas de faux positif)."""
    now = time.time()
    for entry in _iter_analyzer_locks():
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            source = data.get("source_dir")
            if not source:
                continue
        except (json.JSONDecodeError, OSError):
            continue

        lock_age = _lock_age(entry, now)
        if lock_age is None:
            continue

        if _lock_process_is_dead(data, lock_age):
            _logger.warning("Lock orphelin supprime (processus termine) pour %s", source)
            try:
                entry.unlink()
            except OSError:
                pass
            continue

        if lock_age <= _LOCK_MAX_AGE_SECONDS:
            return source

        if _lock_has_recent_progress(source, now):
            mtime = _most_recent_progress_mtime(source) or now
            progress_age = now - mtime
            _logger.info(
                "Lock ancien (%.0fh) mais progress recent (%.0fh) pour %s, conserve",
                lock_age / 3600, progress_age / 3600, source,
            )
            return source

        _logger.warning(
            "Lock orphelin supprime (age: %.0fh) pour %s", lock_age / 3600, source,
        )
        try:
            entry.unlink()
        except OSError:
            pass
    return None


def cleanup_stale_analyzer_locks(source_dir: str | None = None) -> dict:
    """Supprime uniquement les lockfiles d'analyseur consideres orphelins."""
    now = time.time()
    removed = []
    kept = []

    for entry in _iter_analyzer_locks():
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            source = data.get("source_dir")
            if not source:
                continue
        except (json.JSONDecodeError, OSError):
            continue

        if source_dir and source != source_dir:
            continue

        lock_age = _lock_age(entry, now)
        if lock_age is None:
            continue  # deja supprime par le worker

        process_dead = _lock_process_is_dead(data, lock_age)
        if not process_dead and (
            lock_age <= _LOCK_MAX_AGE_SECONDS or _lock_has_recent_progress(source, now)
        ):
            kept.append(source)
            continue

        try:
            entry.unlink()
            removed.append(source)
        except OSError:
            kept.append(source)

    return {
        "removed": removed,
        "kept": kept,
    }


def _find_latest_cached_source() -> str | None:
    """Retourne le source_dir du cache d'analyse le plus recent, s'il existe."""
    latest_source = None
    latest_mtime = -1.0
    try:
        for entry in CACHE_DIR.iterdir():
            if entry.name.startswith("analysis_") and entry.name.endswith(".json"):
                try:
                    data = json.loads(entry.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                source_dir = data.get("source_dir", "")
                if not source_dir:
                    continue
                mtime = entry.stat().st_mtime
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_source = source_dir
    except OSError:
        pass
    return latest_source


def get_status() -> dict:
    """Construit le statut a partir des fichiers persistants (lockfile, progress, cache).
    Ne depend d'aucune variable memoire volatile — fonctionne apres restart backend."""
    source_dir = _find_active_lock()
    running = source_dir is not None
    if not source_dir:
        source_dir = _find_latest_cached_source()

    status = {
        "running": running,
        "progress": 0,
        "total_files": 0,
        "current_file": "",
        "last_run": None,
        "emails_found": 0,
        "phones_found": 0,
    }

    # Si une analyse est en cours, utiliser ce source_dir
    # Sinon, chercher le dernier cache disponible
    if source_dir:
        progress = _load_progress_summary(source_dir)
        if progress:
            processed = progress.get("processed_files", [])
            status["progress"] = progress.get("processed_count", len(processed))
            status["current_file"] = progress.get("current_file", "")
            status["emails_found"] = progress.get("total_emails_found", 0)
            status["phones_found"] = progress.get("total_phones_found", 0)

        cache = load_cache(source_dir)
        if cache:
            status["total_files"] = cache.get("total_files", 0)
            status["last_run"] = cache.get("scan_date")
            if not running:
                status["progress"] = cache.get("files_processed", 0)
                status["emails_found"] = cache.get("emails", {}).get("total_occurrences", 0)
                status["phones_found"] = cache.get("phones", {}).get("total_occurrences", 0)

    return status


def get_cache_path(source_dir: str) -> Path:
    return _prefer_existing_artifact("analysis", ".json", source_dir)


def _get_progress_path(source_dir: str) -> Path:
    """Chemin du fichier progress au format actuel (SQLite)."""
    return _prefer_existing_artifact("progress", ".sqlite", source_dir)


def _get_legacy_progress_json_path(source_dir: str) -> Path:
    """Chemin du fichier progress au format legacy JSON (conserve pour migration)."""
    return _prefer_existing_artifact("progress", ".json", source_dir)


def _most_recent_progress_mtime(source_dir: str) -> float | None:
    """Retourne le mtime le plus recent parmi SQLite + JSON legacy, ou None."""
    mtimes = []
    for path in (_get_progress_path(source_dir), _get_legacy_progress_json_path(source_dir)):
        if path.exists():
            try:
                mtimes.append(path.stat().st_mtime)
            except OSError:
                pass
    return max(mtimes) if mtimes else None


def load_cache(source_dir: str) -> dict | None:
    cache_path = get_cache_path(source_dir)
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _watchdog_worker(proc: subprocess.Popen, source_dir: str):
    """Attend la fin du subprocess et libere le lock s'il reste orphelin."""
    try:
        proc.wait()
    except Exception:
        pass
    lock_path = _get_lock_path(source_dir)
    if lock_path.exists():
        _logger.warning(
            "Watchdog: worker PID %s termine (code=%s) mais lock encore present, nettoyage",
            proc.pid,
            proc.returncode,
        )
        _release_lock(source_dir)


def run_analysis(source_dir: str, resume: bool = True):
    """Lance l'analyse dans un process separe.

    Le lock est acquis ici (cote parent) avant le Popen pour garantir
    la serialisation. Le worker enfant herite du lock et le supprime en finally.
    Un thread watchdog surveille le process et libere le lock si le child
    crash sans l'avoir fait lui-meme.
    Retourne False si une analyse est deja en cours.
    """
    if not _acquire_lock(source_dir):
        return False

    args = [sys.executable, os.path.abspath(__file__), source_dir]
    if not resume:
        args.append("--no-resume")

    try:
        _logger.info("Lancement worker analyse: %s", " ".join(args))
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        _logger.error("Echec lancement worker analyse: %s", exc)
        _release_lock(source_dir)
        return False

    threading.Thread(
        target=_watchdog_worker, args=(proc, source_dir), daemon=True,
    ).start()

    return True


def _trim_counter(counter: Counter, max_size: int):
    if max_size > 0 and len(counter) > max_size:
        top = counter.most_common(max_size)
        counter.clear()
        counter.update(dict(top))


def _build_result(
    source_dir: str,
    jsonl_files: list[str],
    email_counter: Counter,
    domain_counter: Counter,
    phone_counter: Counter,
    total_lines: int,
    total_emails_found: int,
    total_phones_found: int,
    unique_emails_seen: int,
    unique_phones_seen: int,
    files_by_folder: Counter,
    elapsed: float,
    done_files: int,
):
    perso_count = sum(count for domain, count in domain_counter.items() if domain in PERSO_DOMAINS)
    pro_count = sum(count for domain, count in domain_counter.items() if domain not in PERSO_DOMAINS)

    tld_counts = Counter()
    for domain, count in domain_counter.items():
        tld = "." + domain.rsplit(".", 1)[-1]
        tld_counts[tld] += count

    return {
        "scan_date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scan_duration_seconds": round(elapsed, 1),
        "source_dir": source_dir,
        "total_files": len(jsonl_files),
        "files_processed": done_files,
        "total_lines": total_lines,
        "complete": done_files >= len(jsonl_files),
        "limits": {
            "email_counter_limit": ANALYZER_MAX_EMAIL_ENTRIES,
            "phone_counter_limit": ANALYZER_MAX_PHONE_ENTRIES,
        },
        "emails": {
            "unique_count": unique_emails_seen,
            "total_occurrences": total_emails_found,
            "perso_count": perso_count,
            "pro_count": pro_count,
            "top_emails": [{"email": email, "count": count} for email, count in email_counter.most_common(100)],
            "top_domains": [{"domain": domain, "count": count} for domain, count in domain_counter.most_common(30)],
            "tld_distribution": [{"tld": tld, "count": count} for tld, count in tld_counts.most_common(10)],
        },
        "phones": {
            "unique_count": unique_phones_seen,
            "total_occurrences": total_phones_found,
            "top_phones": [{"phone": phone, "count": count} for phone, count in phone_counter.most_common(50)],
        },
        "sources": [{"name": name, "files": count} for name, count in files_by_folder.most_common(30)],
    }


def _save_cache(result: dict, source_dir: str):
    cache_path = get_cache_path(source_dir)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    tmp_path.replace(cache_path)


_PROGRESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_files (
    path TEXT PRIMARY KEY
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS counter_emails (
    key TEXT PRIMARY KEY, count INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS counter_domains (
    key TEXT PRIMARY KEY, count INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS counter_phones (
    key TEXT PRIMARY KEY, count INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS counter_folders (
    key TEXT PRIMARY KEY, count INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seen_emails (
    key TEXT PRIMARY KEY
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seen_phones (
    key TEXT PRIMARY KEY
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS failed_files (
    path TEXT PRIMARY KEY, error TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS totals (
    key TEXT PRIMARY KEY, value INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
) WITHOUT ROWID;
"""


def _connect_progress(path: Path, *, wal: bool = True) -> sqlite3.Connection:
    """Ouvre la progression durable, sans recopier la base complete.

    Si la base est illisible, la connexion est fermee avant de propager
    l'erreur: sous Windows, un handle oublie bloquerait le remplacement ou la
    mise en quarantaine du fichier corrompu.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(f"PRAGMA journal_mode={'WAL' if wal else 'DELETE'}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.executescript(_PROGRESS_SCHEMA)
    except BaseException:
        conn.close()
        raise
    return conn


def _quarantine_corrupt_progress(path: Path) -> Path:
    """Met de cote une progression illisible (une seule quarantaine conservee)."""
    quarantine = path.with_name(path.name + ".corrupt")
    for suffix in ("", "-wal", "-shm"):
        Path(f"{quarantine}{suffix}").unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.replace(Path(f"{quarantine}{suffix}"))
    path.replace(quarantine)
    return quarantine


def _save_progress(
    source_dir: str,
    processed_files: list[str],
    email_counter: Counter,
    domain_counter: Counter,
    phone_counter: Counter,
    seen_emails: set[str],
    seen_phones: set[str],
    files_by_folder: Counter,
    total_lines: int,
    total_emails_found: int,
    total_phones_found: int,
    current_file: str = "",
    failed_files: dict[str, str] | None = None,
):
    """Remplace un snapshot dans une transaction SQLite atomique."""
    progress_path = _get_progress_path(source_dir)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect_progress(progress_path)
    try:
        with conn:
            for table in (
                "processed_files", "counter_emails", "counter_domains",
                "counter_phones", "counter_folders", "seen_emails",
                "seen_phones", "failed_files", "totals", "metadata",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.executemany(
                "INSERT OR IGNORE INTO processed_files(path) VALUES(?)",
                ((p,) for p in processed_files),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO counter_emails(key, count) VALUES(?, ?)",
                email_counter.items(),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO counter_domains(key, count) VALUES(?, ?)",
                domain_counter.items(),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO counter_phones(key, count) VALUES(?, ?)",
                phone_counter.items(),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO counter_folders(key, count) VALUES(?, ?)",
                files_by_folder.items(),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO seen_emails(key) VALUES(?)",
                ((e,) for e in seen_emails),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO seen_phones(key) VALUES(?)",
                ((p,) for p in seen_phones),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO failed_files(path, error) VALUES(?, ?)",
                (failed_files or {}).items(),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO totals(key, value) VALUES(?, ?)",
                [
                    ("total_lines", total_lines),
                    ("total_emails_found", total_emails_found),
                    ("total_phones_found", total_phones_found),
                    ("unique_emails_seen", len(seen_emails)),
                    ("unique_phones_seen", len(seen_phones)),
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('current_file', ?)",
                (current_file,),
            )
    finally:
        conn.close()


def _load_progress_from_sqlite(sqlite_path: Path) -> dict:
    """Recharge le dict progress depuis le fichier SQLite.

    Si les tables seen_* sont vides mais que les counters contiennent des
    entrees, on omet la cle `seen` correspondante pour que
    `_restore_seen_values` fallback sur les cles des counters. Ca evite de
    perdre le comptage d'uniques si une ancienne migration a rate le seen.
    """
    conn = _connect_progress(sqlite_path)
    try:
        processed_files = [row[0] for row in conn.execute(
            "SELECT path FROM processed_files"
        )]
        counters = {
            "emails": dict(conn.execute("SELECT key, count FROM counter_emails")),
            "domains": dict(conn.execute("SELECT key, count FROM counter_domains")),
            "phones": dict(conn.execute("SELECT key, count FROM counter_phones")),
            "folders": dict(conn.execute("SELECT key, count FROM counter_folders")),
        }
        seen_emails = [row[0] for row in conn.execute("SELECT key FROM seen_emails")]
        seen_phones = [row[0] for row in conn.execute("SELECT key FROM seen_phones")]
        totals = dict(conn.execute("SELECT key, value FROM totals"))
        failed_files = dict(conn.execute("SELECT path, error FROM failed_files"))
        metadata = dict(conn.execute("SELECT key, value FROM metadata"))
    finally:
        conn.close()

    seen_dict: dict = {}
    if seen_emails or not counters["emails"]:
        seen_dict["emails"] = seen_emails
    if seen_phones or not counters["phones"]:
        seen_dict["phones"] = seen_phones

    return {
        "processed_files": processed_files,
        "counters": counters,
        "seen": seen_dict,
        "unique_emails_seen": totals.get(
            "unique_emails_seen", len(seen_emails) or len(counters["emails"]),
        ),
        "unique_phones_seen": totals.get(
            "unique_phones_seen", len(seen_phones) or len(counters["phones"]),
        ),
        "total_lines": totals.get("total_lines", 0),
        "total_emails_found": totals.get("total_emails_found", 0),
        "total_phones_found": totals.get("total_phones_found", 0),
        "current_file": metadata.get("current_file", ""),
        "failed_files": failed_files,
    }


def _load_progress_summary(source_dir: str) -> dict | None:
    """Lit uniquement les champs necessaires au statut HTTP."""
    sqlite_path = _get_progress_path(source_dir)
    if sqlite_path.exists():
        try:
            conn = _connect_progress(sqlite_path)
            try:
                processed_count = conn.execute(
                    "SELECT COUNT(*) FROM processed_files"
                ).fetchone()[0]
                totals = dict(conn.execute("SELECT key, value FROM totals"))
                metadata = dict(conn.execute("SELECT key, value FROM metadata"))
                return {
                    "processed_count": processed_count,
                    "total_emails_found": totals.get("total_emails_found", 0),
                    "total_phones_found": totals.get("total_phones_found", 0),
                    "current_file": metadata.get("current_file", ""),
                }
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            _logger.exception("Lecture du resume de progression impossible: %s", sqlite_path)

    return _load_progress_from_json_legacy(_get_legacy_progress_json_path(source_dir))


def _load_progress_from_json_legacy(json_path: Path) -> dict | None:
    """Lit un ancien progress JSON (format historique)."""
    if not json_path.exists():
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if isinstance(data, list):
        return {"processed_files": data}
    return data if isinstance(data, dict) else None


def _json_root_is_dict(path: Path) -> bool:
    """Inspecte le premier caractere non-blanc du fichier pour detecter un dict.

    Permet de savoir si un progress JSON legacy est au format structure
    (dict avec counters/seen/...) ou au format tres ancien (liste racine).
    """
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1)
                if not chunk:
                    return False
                if chunk in (b" ", b"\t", b"\n", b"\r"):
                    continue
                return chunk == b"{"
    except OSError:
        return False


def _migrate_json_to_sqlite(json_path: Path, sqlite_path: Path) -> None:
    """Migre un fichier progress JSON (potentiellement tres gros) vers SQLite.

    Utilise ijson en streaming pour ne pas charger le JSON entier en RAM.
    Le JSON original est archive en .json.migrated apres succes.
    """
    if ijson is None:
        raise RuntimeError(
            "ijson requis pour migrer le progress JSON legacy. "
            "Installer via: venv/Scripts/pip install ijson"
        )

    t0 = time.time()
    _logger.info("Migration progress JSON -> SQLite: %s", json_path)

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = sqlite_path.with_suffix(".sqlite.tmp")
    tmp_path.unlink(missing_ok=True)

    conn = _connect_progress(tmp_path, wal=False)
    try:
        _migrate_processed_files(conn, json_path)
        for kind in ("emails", "domains", "phones", "folders"):
            _migrate_counters(conn, json_path, kind)
        for kind in ("emails", "phones"):
            _migrate_seen(conn, json_path, kind)
        _migrate_totals(conn, json_path)
    finally:
        conn.close()

    tmp_path.replace(sqlite_path)

    backup_path = json_path.with_suffix(".json.migrated")
    try:
        if backup_path.exists():
            backup_path.unlink()
        json_path.replace(backup_path)
        _logger.info("Legacy JSON archive: %s", backup_path)
    except OSError:
        _logger.exception("Echec archivage du JSON legacy %s", json_path)

    elapsed = time.time() - t0
    try:
        size_mb = sqlite_path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0
    _logger.info("Migration terminee en %.1fs, SQLite: %.1f Mo", elapsed, size_mb)


def _iter_batches(iterable, size: int):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _migrate_processed_files(conn: sqlite3.Connection, json_path: Path) -> None:
    with open(json_path, "rb") as fh:
        items = ((p,) for p in ijson.items(fh, "processed_files.item"))
        for batch in _iter_batches(items, 50000):
            conn.executemany(
                "INSERT OR IGNORE INTO processed_files(path) VALUES(?)", batch
            )
    conn.commit()


def _migrate_counters(conn: sqlite3.Connection, json_path: Path, kind: str) -> None:
    table = f"counter_{kind}"
    with open(json_path, "rb") as fh:
        pairs = ijson.kvitems(fh, f"counters.{kind}")
        for batch in _iter_batches(pairs, 50000):
            conn.executemany(
                f"INSERT OR REPLACE INTO {table}(key, count) VALUES(?, ?)", batch
            )
    conn.commit()


def _migrate_seen(conn: sqlite3.Connection, json_path: Path, kind: str) -> None:
    table = f"seen_{kind}"
    with open(json_path, "rb") as fh:
        items = ((v,) for v in ijson.items(fh, f"seen.{kind}.item"))
        for batch in _iter_batches(items, 50000):
            conn.executemany(f"INSERT OR IGNORE INTO {table}(key) VALUES(?)", batch)
    conn.commit()


_TOTAL_KEYS = {
    "total_lines", "total_emails_found", "total_phones_found",
    "unique_emails_seen", "unique_phones_seen",
}


def _migrate_totals(conn: sqlite3.Connection, json_path: Path) -> None:
    with open(json_path, "rb") as fh:
        for prefix, event, value in ijson.parse(fh):
            if event == "number" and prefix in _TOTAL_KEYS:
                conn.execute(
                    "INSERT OR REPLACE INTO totals(key, value) VALUES(?, ?)",
                    (prefix, int(value)),
                )
    conn.commit()


def _load_progress(source_dir: str) -> dict | None:
    """Charge le progress. Priorite: SQLite, sinon JSON legacy (avec migration).

    Lit d'abord le fichier SQLite. Si absent mais un JSON legacy existe, tente
    une migration JSON -> SQLite. Si la migration echoue (ijson absent, JSON
    corrompu), lit directement le JSON (lecture memoire). Retourne None si
    aucun progress.
    """
    sqlite_path = _get_progress_path(source_dir)
    json_path = _get_legacy_progress_json_path(source_dir)

    if sqlite_path.exists():
        try:
            return _load_progress_from_sqlite(sqlite_path)
        except sqlite3.DatabaseError:
            _logger.exception(
                "SQLite progress corrompu: %s, fallback JSON legacy", sqlite_path
            )

    if json_path.exists():
        if _json_root_is_dict(json_path):
            try:
                _migrate_json_to_sqlite(json_path, sqlite_path)
                return _load_progress_from_sqlite(sqlite_path)
            except Exception:
                _logger.exception(
                    "Migration JSON->SQLite echouee pour %s, lecture JSON directe",
                    json_path,
                )
        return _load_progress_from_json_legacy(json_path)

    return None


def _restore_seen_values(progress: dict, counters: dict) -> tuple[set[str], set[str]]:
    seen = progress.get("seen", {})
    emails = seen.get("emails")
    phones = seen.get("phones")

    if isinstance(emails, list):
        seen_emails = {str(value) for value in emails}
    else:
        seen_emails = set(counters.get("emails", {}).keys())

    if isinstance(phones, list):
        seen_phones = {str(value) for value in phones}
    else:
        seen_phones = set(counters.get("phones", {}).keys())

    return seen_emails, seen_phones


def _iter_bounded_text_lines(filepath: str):
    """Lit un JSONL sans jamais allouer une ligne au-dela du plafond configure."""
    with open(filepath, "rb") as stream:
        line_number = 0
        while True:
            raw = stream.readline(ANALYZER_MAX_LINE_BYTES + 1)
            if not raw:
                return
            line_number += 1
            if len(raw) > ANALYZER_MAX_LINE_BYTES:
                while raw and not raw.endswith(b"\n"):
                    raw = stream.readline(ANALYZER_MAX_LINE_BYTES + 1)
                raise ValueError(
                    f"ligne {line_number} superieure a {ANALYZER_MAX_LINE_BYTES} octets"
                )
            yield raw.decode("utf-8", errors="replace")


def _increment_counter_rows(conn: sqlite3.Connection, table: str, values: Counter) -> None:
    if not values:
        return
    conn.executemany(
        f"""INSERT INTO {table}(key, count) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET count = count + excluded.count""",
        values.items(),
    )


def _trim_counter_table(conn: sqlite3.Connection, table: str, limit: int) -> None:
    if limit <= 0:
        return
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if count <= limit:
        return
    conn.execute("DROP TABLE IF EXISTS temp.keep_counter_keys")
    conn.execute("CREATE TEMP TABLE keep_counter_keys(key TEXT PRIMARY KEY) WITHOUT ROWID")
    conn.execute(
        f"INSERT INTO keep_counter_keys SELECT key FROM {table} "
        "ORDER BY count DESC, key LIMIT ?",
        (limit,),
    )
    conn.execute(
        f"DELETE FROM {table} WHERE NOT EXISTS "
        f"(SELECT 1 FROM keep_counter_keys WHERE keep_counter_keys.key = {table}.key)"
    )
    conn.execute("DROP TABLE keep_counter_keys")


def _reset_progress(conn: sqlite3.Connection) -> None:
    with conn:
        for table in (
            "processed_files", "counter_emails", "counter_domains",
            "counter_phones", "counter_folders", "seen_emails",
            "seen_phones", "failed_files", "totals", "metadata",
        ):
            conn.execute(f"DELETE FROM {table}")


def _flush_analysis_batch(
    conn: sqlite3.Connection,
    emails: Counter,
    domains: Counter,
    phones: Counter,
) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO seen_emails(key) VALUES(?)",
        ((value,) for value in emails),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO seen_phones(key) VALUES(?)",
        ((value,) for value in phones),
    )
    _increment_counter_rows(conn, "counter_emails", emails)
    _increment_counter_rows(conn, "counter_domains", domains)
    _increment_counter_rows(conn, "counter_phones", phones)
    emails.clear()
    domains.clear()
    phones.clear()


def _analysis_snapshot(
    conn: sqlite3.Connection,
    source_dir: str,
    jsonl_files: list[str],
    elapsed: float,
) -> dict:
    totals = dict(conn.execute("SELECT key, value FROM totals"))
    email_counter = Counter(dict(conn.execute(
        "SELECT key, count FROM counter_emails ORDER BY count DESC, key LIMIT ?",
        (ANALYZER_MAX_EMAIL_ENTRIES,),
    )))
    domain_counter = Counter(dict(conn.execute(
        "SELECT key, count FROM counter_domains ORDER BY count DESC, key LIMIT ?",
        (ANALYZER_MAX_DOMAIN_ENTRIES,),
    )))
    phone_counter = Counter(dict(conn.execute(
        "SELECT key, count FROM counter_phones ORDER BY count DESC, key LIMIT ?",
        (ANALYZER_MAX_PHONE_ENTRIES,),
    )))
    files_by_folder = Counter(dict(conn.execute(
        "SELECT key, count FROM counter_folders"
    )))
    unique_emails = conn.execute("SELECT COUNT(*) FROM seen_emails").fetchone()[0]
    unique_phones = conn.execute("SELECT COUNT(*) FROM seen_phones").fetchone()[0]
    done_files = conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0]
    failed_files = [
        {"path": path, "error": error}
        for path, error in conn.execute("SELECT path, error FROM failed_files ORDER BY path")
    ]

    result = _build_result(
        source_dir,
        jsonl_files,
        email_counter,
        domain_counter,
        phone_counter,
        totals.get("total_lines", 0),
        totals.get("total_emails_found", 0),
        totals.get("total_phones_found", 0),
        unique_emails,
        unique_phones,
        files_by_folder,
        elapsed,
        done_files,
    )
    result["failed_files"] = failed_files
    # Les progressions anterieures a ces totaux n'en disposent pas: on garde
    # alors l'estimation calculee par _build_result depuis les domaines.
    if "perso_emails_found" in totals and "pro_emails_found" in totals:
        result["emails"]["perso_count"] = totals["perso_emails_found"]
        result["emails"]["pro_count"] = totals["pro_emails_found"]
    return result


def _record_failed_file(conn: sqlite3.Connection, filepath: str, exc: Exception) -> None:
    message = f"{type(exc).__name__}: {exc}"[:2000]
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO failed_files(path, error) VALUES(?, ?)",
            (filepath, message),
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('current_file', '')"
        )


def _remove_completed_progress(source_dir: str) -> None:
    progress_path = _get_progress_path(source_dir)
    paths = [progress_path, _get_legacy_progress_json_path(source_dir)]
    paths.extend(Path(f"{progress_path}{suffix}") for suffix in ("-wal", "-shm"))
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _logger.exception("Impossible de supprimer la progression terminee %s", path)


def _open_progress_for_analysis(progress_path: Path) -> sqlite3.Connection:
    """Ouvre la progression; une base corrompue est mise de cote puis recreee."""
    try:
        return _connect_progress(progress_path)
    except sqlite3.DatabaseError:
        quarantine = _quarantine_corrupt_progress(progress_path)
        _logger.exception(
            "Progression illisible mise de cote dans %s; reprise depuis zero", quarantine,
        )
        return _connect_progress(progress_path)


def _do_analysis(source_dir: str, resume: bool):
    start = time.time()
    # source_dir est la cle des artefacts (cache, progression, lock): il reste
    # tel que configure. Seul le parcours disque developpe ~ et %VAR%.
    walk_root = os.path.expandvars(os.path.expanduser(source_dir))
    jsonl_files = sorted(
        os.path.join(root, filename)
        for root, _, files in os.walk(walk_root)
        for filename in files
        if filename.lower().endswith(".jsonl")
    )

    # Declenche au besoin la migration historique JSON -> SQLite avant
    # d'ouvrir la connexion incrementale.
    if resume and not _get_progress_path(source_dir).exists():
        legacy = _load_progress(source_dir)
        if legacy and not _get_progress_path(source_dir).exists():
            counters = legacy.get("counters", {})
            seen_emails, seen_phones = _restore_seen_values(legacy, counters)
            _save_progress(
                source_dir,
                legacy.get("processed_files", []),
                Counter(counters.get("emails", {})),
                Counter(counters.get("domains", {})),
                Counter(counters.get("phones", {})),
                seen_emails,
                seen_phones,
                Counter(counters.get("folders", {})),
                legacy.get("total_lines", 0),
                legacy.get("total_emails_found", 0),
                legacy.get("total_phones_found", 0),
            )

    progress_path = _get_progress_path(source_dir)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open_progress_for_analysis(progress_path)
    completed = False
    final_result = None
    try:
        if not resume:
            _reset_progress(conn)

        with conn:
            _trim_counter_table(conn, "counter_emails", ANALYZER_MAX_EMAIL_ENTRIES)
            _trim_counter_table(conn, "counter_domains", ANALYZER_MAX_DOMAIN_ENTRIES)
            _trim_counter_table(conn, "counter_phones", ANALYZER_MAX_PHONE_ENTRIES)

        already_done = {
            row[0] for row in conn.execute("SELECT path FROM processed_files")
        }
        files_to_process = [path for path in jsonl_files if path not in already_done]
        _logger.info(
            "Demarrage: %s fichiers JSONL (%s deja traites, %s restants)",
            len(jsonl_files), len(already_done), len(files_to_process),
        )

        for attempt, filepath in enumerate(files_to_process, start=1):
            _touch_lock(source_dir)
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('current_file', ?)",
                    (filepath,),
                )

            email_batch: Counter = Counter()
            domain_batch: Counter = Counter()
            phone_batch: Counter = Counter()
            file_lines = 0
            file_emails = 0
            file_phones = 0
            file_perso = 0
            file_pro = 0
            batch_entries = 0

            try:
                file_size = os.path.getsize(filepath)
                _logger.info(
                    "[%s/%s] %s (%.1f MB)",
                    len(already_done) + attempt,
                    len(jsonl_files),
                    os.path.basename(filepath),
                    file_size / (1024 * 1024),
                )
                conn.execute("BEGIN IMMEDIATE")
                for line in _iter_bounded_text_lines(filepath):
                    file_lines += 1
                    if file_lines % 10_000 == 0:
                        _touch_lock(source_dir)
                    for email in EMAIL_REGEX.findall(line):
                        email_lower = email.lower()
                        domain = email_lower.split("@", 1)[1]
                        email_batch[email_lower] += 1
                        domain_batch[domain] += 1
                        file_emails += 1
                        if domain in PERSO_DOMAINS:
                            file_perso += 1
                        else:
                            file_pro += 1
                        batch_entries += 1

                    for phone in PHONE_REGEX.findall(line):
                        phone_batch[re.sub(r"[\s.-]", "", phone)] += 1
                        file_phones += 1
                        batch_entries += 1

                    if batch_entries >= ANALYZER_DB_BATCH_SIZE:
                        _flush_analysis_batch(
                            conn, email_batch, domain_batch, phone_batch,
                        )
                        batch_entries = 0
                        _touch_lock(source_dir)

                _flush_analysis_batch(conn, email_batch, domain_batch, phone_batch)
                folder_name = os.path.basename(os.path.dirname(filepath))
                _increment_counter_rows(
                    conn, "counter_folders", Counter({folder_name: 1}),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO processed_files(path) VALUES(?)",
                    (filepath,),
                )
                conn.execute("DELETE FROM failed_files WHERE path = ?", (filepath,))
                for key, value in (
                    ("total_lines", file_lines),
                    ("total_emails_found", file_emails),
                    ("total_phones_found", file_phones),
                    ("perso_emails_found", file_perso),
                    ("pro_emails_found", file_pro),
                ):
                    conn.execute(
                        """INSERT INTO totals(key, value) VALUES(?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = value + excluded.value""",
                        (key, value),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('current_file', '')"
                )
                conn.commit()
                already_done.add(filepath)
            except Exception as exc:
                conn.rollback()
                _record_failed_file(conn, filepath, exc)
                _logger.exception("Fichier non valide, progression annulee pour %s", filepath)

            if attempt % ANALYZER_SAVE_EVERY == 0:
                with conn:
                    _trim_counter_table(conn, "counter_emails", ANALYZER_MAX_EMAIL_ENTRIES)
                    _trim_counter_table(conn, "counter_domains", ANALYZER_MAX_DOMAIN_ENTRIES)
                    _trim_counter_table(conn, "counter_phones", ANALYZER_MAX_PHONE_ENTRIES)
                result = _analysis_snapshot(
                    conn, source_dir, jsonl_files, time.time() - start,
                )
                _save_cache(result, source_dir)
                _logger.info(
                    "%s/%s fichiers examines, %s valides",
                    attempt, len(files_to_process), result["files_processed"],
                )
            elif attempt % 50 == 0:
                _logger.info("%s/%s fichiers examines", attempt, len(files_to_process))

        with conn:
            _trim_counter_table(conn, "counter_emails", ANALYZER_MAX_EMAIL_ENTRIES)
            _trim_counter_table(conn, "counter_domains", ANALYZER_MAX_DOMAIN_ENTRIES)
            _trim_counter_table(conn, "counter_phones", ANALYZER_MAX_PHONE_ENTRIES)
        final_result = _analysis_snapshot(
            conn, source_dir, jsonl_files, time.time() - start,
        )
        _save_cache(final_result, source_dir)
        completed = bool(final_result["complete"] and not final_result["failed_files"])
    finally:
        conn.close()

    if completed:
        _remove_completed_progress(source_dir)

    elapsed = time.time() - start
    _logger.info(
        "Analyse %s en %.0fs: %s fichiers valides, %s en erreur",
        "terminee" if completed else "incomplete",
        elapsed,
        final_result["files_processed"] if final_result else 0,
        len(final_result["failed_files"]) if final_result else 0,
    )
    _logger.info("Cache sauvegarde: %s", get_cache_path(source_dir))


# ---- Mode worker (lance par subprocess depuis l'API) ----
# Le lock a deja ete acquis par le parent (run_analysis).
# Le worker est responsable uniquement du cleanup en finally.

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyzer.py <source_dir> [--no-resume]", file=sys.stderr)
        sys.exit(1)

    source_dir = sys.argv[1]
    resume = "--no-resume" not in sys.argv

    configure_worker_logging()
    _mark_worker_lock(source_dir)
    _logger.info("Worker analyse demarre (PID %s) pour %s", os.getpid(), source_dir)
    try:
        _do_analysis(source_dir, resume)
    except Exception as exc:
        _logger.exception("Erreur fatale worker: %s", exc)
        sys.exit(1)
    finally:
        _release_lock(source_dir)
        _logger.info("Worker analyse termine pour %s", source_dir)
