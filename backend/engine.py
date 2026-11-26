import json
import logging
import os
import threading
import time
import tomllib
from pathlib import Path

import tantivy
from tantivy import Filter, TextAnalyzerBuilder, Tokenizer

from settings import INDEX_PATH_CHECK_ATTEMPTS, INDEX_PATH_CHECK_DELAY_MS

logger = logging.getLogger("oeil-engine")
_STATS_TTL_SECONDS = 300       # cache stats legeres (num_docs, segments) : 5 min
_INDEX_SIZE_TTL_SECONDS = 3600  # cache taille index (os.walk) : 1 h
_RELOAD_INTERVAL_SECONDS = 30.0  # throttle reload() : au plus 1 fois toutes les 30 s
_LOCK_FILENAMES = (".tantivy-meta.lock", ".tantivy-writer.lock")
# Codes acceptes par l'indexeur Rust -> noms attendus par tantivy-py.
_TANTIVY_PY_LANGUAGES = {
    "fr": "french",
    "en": "english",
    "es": "spanish",
    "de": "german",
    "it": "italian",
    "pt": "portuguese",
}


class SearchQueryError(ValueError):
    """Erreur de requete utilisateur (parse query, syntaxe, etc.)."""


class IndexAccessError(RuntimeError):
    """Erreur runtime de l'index (reload/search/lecture docs)."""


class IndexOpenError(RuntimeError):
    """Erreur d'ouverture de l'index avec diagnostic operable."""


class SearchEngine:
    """Wrapper autour de tantivy-py pour lire l'index cree par le Rust."""

    def __init__(self, index_path: str):
        init_started_at = time.time()
        if not self._wait_for_index_path(index_path):
            raise FileNotFoundError(f"Index introuvable: {index_path}")

        self.index_path = index_path
        open_started_at = time.time()
        self.index = self._open_index_with_recovery()
        open_elapsed = time.time() - open_started_at
        self.schema = self.index.schema
        self._reload_lock = threading.Lock()
        self._stats_lock = threading.RLock()
        self._last_reload_at = 0.0
        self._reload_in_progress = False
        self._cached_stats: dict | None = None
        self._cached_stats_at = 0.0
        self._cached_index_size: int = 0
        self._cached_index_size_at = 0.0
        self._index_size_refreshing = False

        tokenizer_started_at = time.time()
        analyzer = self._build_query_analyzer()
        self.index.register_tokenizer("custom_tech", analyzer)
        tokenizer_elapsed = time.time() - tokenizer_started_at

        reload_started_at = time.time()
        self.index.reload()
        reload_elapsed = time.time() - reload_started_at
        self._last_reload_at = time.monotonic()
        total_elapsed = time.time() - init_started_at
        logger.info(
            "SearchEngine init '%s': open=%.2fs tokenizer=%.2fs reload=%.2fs total=%.2fs",
            self.index_path,
            open_elapsed,
            tokenizer_elapsed,
            reload_elapsed,
            total_elapsed,
        )

    @staticmethod
    def _wait_for_index_path(index_path: str) -> bool:
        for attempt in range(1, INDEX_PATH_CHECK_ATTEMPTS + 1):
            if os.path.isdir(index_path):
                if attempt > 1:
                    logger.warning(
                        "Chemin index revenu disponible apres %s tentatives: %s",
                        attempt,
                        index_path,
                    )
                return True
            if attempt >= INDEX_PATH_CHECK_ATTEMPTS:
                break
            if INDEX_PATH_CHECK_DELAY_MS > 0:
                time.sleep(INDEX_PATH_CHECK_DELAY_MS / 1000)
        return False

    def _lock_paths(self) -> list[Path]:
        base = Path(self.index_path)
        return [base / filename for filename in _LOCK_FILENAMES]

    @staticmethod
    def _is_lock_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "lockfile" in message or "failed to acquire lock" in message

    @staticmethod
    def _is_zstd_unsupported_error(exc: Exception) -> bool:
        return "zstd-compression" in str(exc)

    def _collect_lock_details(self) -> list[dict]:
        details = []
        for lock_path in self._lock_paths():
            if not lock_path.exists():
                continue
            try:
                stat = lock_path.stat()
                age_seconds = max(0.0, time.time() - stat.st_mtime)
            except OSError:
                age_seconds = None
            details.append({"path": lock_path, "age_seconds": age_seconds})
        return details

    @staticmethod
    def _format_lock_details(lock_details: list[dict]) -> str:
        if not lock_details:
            return "aucun lockfile detecte"
        parts = []
        for item in lock_details:
            lock_path = item["path"]
            age_seconds = item["age_seconds"]
            if age_seconds is None:
                parts.append(f"{lock_path.name} (age inconnu)")
            else:
                parts.append(f"{lock_path.name} ({age_seconds:.0f}s)")
        return ", ".join(parts)

    def _open_index_with_recovery(self):
        try:
            return tantivy.Index.open(self.index_path)
        except Exception as exc:
            if self._is_zstd_unsupported_error(exc):
                # Le message de tantivy recopie tout meta.json : inutile ici.
                raise IndexOpenError(
                    f"Index '{self.index_path}' compresse en zstd : le module tantivy "
                    "installe (wheel PyPI) ne sait pas le lire. Backend arrete, lancez "
                    r"'.\build_tantivy_zstd.ps1 -Install' a la racine du projet."
                ) from None
            if not self._is_lock_error(exc):
                raise IndexOpenError(f"Impossible d'ouvrir l'index '{self.index_path}': {exc}") from exc

            lock_details = self._collect_lock_details()
            logger.warning(
                "Ouverture index bloquee par lock pour '%s': %s",
                self.index_path,
                self._format_lock_details(lock_details),
            )
            raise IndexOpenError(
                f"Index verrouille '{self.index_path}': "
                f"{self._format_lock_details(lock_details)}. "
                "Le lock n'a pas ete supprime automatiquement; arretez le writer "
                "concerne avant toute intervention manuelle."
            ) from exc

    def _load_index_config(self) -> dict:
        config_path = Path(self.index_path) / ".schema_config.toml"
        if not config_path.is_file():
            return {}
        with open(config_path, "rb") as fh:
            return tomllib.load(fh)

    @staticmethod
    def _tantivy_py_language(language: str) -> str:
        """Traduit le code de l'indexeur Rust (fr, en...) en nom tantivy-py.

        Reproduit parse_language() de indexer/src/schema.rs, y compris le
        repli sur l'anglais pour un code inconnu.
        """
        return _TANTIVY_PY_LANGUAGES.get(language.strip().lower(), "english")

    def _build_query_analyzer(self):
        """Construit l'analyseur des requetes, identique a celui de l'indexeur.

        Aucun filtre n'est ignore silencieusement: un analyseur different de
        celui utilise a l'indexation ferait manquer des resultats sans erreur.
        """
        config = self._load_index_config()
        max_word_length = int(config.get("max_word_length", 250))
        use_stemming = bool(config.get("use_stemming", False))
        use_stop_words = bool(config.get("use_stop_words", False))
        language = self._tantivy_py_language(str(config.get("language", "en")))

        # Meme ordre que l'indexeur Rust: remove_long, lowercase, stemmer, stopword.
        builder = TextAnalyzerBuilder(Tokenizer.simple())
        builder = builder.filter(Filter.remove_long(max_word_length))
        builder = builder.filter(Filter.lowercase())
        if use_stemming:
            builder = builder.filter(Filter.stemmer(language))
        if use_stop_words:
            builder = builder.filter(Filter.stopword(language))
        return builder.build()

    @staticmethod
    def _value_or_call(value):
        return value() if callable(value) else value

    def _compute_index_size(self) -> int:
        index_size_bytes = 0
        try:
            for root, _dirs, files in os.walk(self.index_path):
                for fname in files:
                    index_size_bytes += os.path.getsize(os.path.join(root, fname))
        except Exception:
            logger.exception("Erreur calcul taille index pour %s", self.index_path)
        return index_size_bytes

    @staticmethod
    def _format_size(index_size_bytes: int) -> str:
        if index_size_bytes >= 1_073_741_824:
            return f"{index_size_bytes / 1_073_741_824:.1f} Go"
        if index_size_bytes >= 1_048_576:
            return f"{index_size_bytes / 1_048_576:.0f} Mo"
        return f"{index_size_bytes / 1024:.0f} Ko"

    def _load_segment_details(self) -> list[dict]:
        segment_details = []
        meta_path = Path(self.index_path) / "meta.json"
        if not meta_path.exists():
            return segment_details

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            segments = meta.get("segments", [])
            for seg in segments:
                max_doc = seg.get("max_doc", 0)
                deletes = seg.get("deletes", {})
                num_deleted = deletes.get("num_deleted_docs", 0) if isinstance(deletes, dict) else 0
                segment_details.append({
                    "id": seg.get("segment_id", "")[:8],
                    "docs": max_doc - num_deleted,
                })

            segment_details.sort(key=lambda s: s["docs"], reverse=True)
        except Exception:
            logger.exception("Erreur lecture meta.json pour %s", self.index_path)

        return segment_details

    def _maybe_reload(self) -> None:
        """Reload throttle et non-bloquant.

        Au plus un reload toutes les _RELOAD_INTERVAL_SECONDS secondes.
        Si un autre thread est deja en cours de reload, retour immediat :
        les appelants continuent avec le searcher courant plutot que
        d'attendre. Le reload lui-meme s'execute hors du lock pour qu'un
        reload lent (disque idle, mmap) ne bloque jamais les recherches.
        """
        now = time.monotonic()
        with self._reload_lock:
            if self._reload_in_progress:
                return
            if now - self._last_reload_at < _RELOAD_INTERVAL_SECONDS:
                return
            self._reload_in_progress = True
            self._last_reload_at = now
        try:
            t0 = time.perf_counter()
            self.index.reload()
            elapsed = time.perf_counter() - t0
            if elapsed > 1.0:
                logger.warning(
                    "reload() a pris %.2fs pour '%s'",
                    elapsed, self.index_path,
                )
        except Exception:
            logger.exception("reload() echoue pour '%s'", self.index_path)
        finally:
            with self._reload_lock:
                self._reload_in_progress = False

    def search(self, query: str, limit: int = 20, offset: int = 0) -> dict:
        self._maybe_reload()
        try:
            searcher = self.index.searcher()
        except Exception as exc:
            raise IndexAccessError(f"Index indisponible '{self.index_path}': {exc}") from exc

        try:
            parsed = self.index.parse_query(query, ["content", "filename"])
        except Exception as exc:
            raise SearchQueryError(str(exc)) from exc

        try:
            results = searcher.search(parsed, limit + offset)
        except Exception as exc:
            raise IndexAccessError(f"Recherche impossible sur '{self.index_path}': {exc}") from exc
        raw_hits = results.hits if hasattr(results, "hits") else results

        hits = []
        for score, doc_address in raw_hits[offset:]:
            try:
                doc = searcher.doc(doc_address)
            except Exception as exc:
                raise IndexAccessError(
                    f"Lecture resultat impossible sur '{self.index_path}': {exc}"
                ) from exc
            # Fallback: si content_raw est absent (ancien index sans index_raw_content=true),
            # on utilise content (version sanitisee). Les snippets seront moins fideles
            # au contenu original mais restent exploitables.
            raw = doc.get_first("content_raw") or doc.get_first("content") or ""
            hit = {
                "score": score,
                "source": doc.get_first("source"),
                "filename": doc.get_first("filename"),
                "type": doc.get_first("type"),
                "content_raw": raw,
            }
            hits.append(hit)

        return {
            "query": query,
            "total": results.count if hasattr(results, "count") else len(raw_hits),
            "limit": limit,
            "offset": offset,
            "hits": hits,
        }

    def _refresh_index_size_background(self):
        """Recalcule index_size en arriere-plan (thread daemon)."""
        try:
            t0 = time.time()
            size = self._compute_index_size()
            elapsed = time.time() - t0
            logger.info(
                "index_size recalcule pour '%s': %s en %.1fs",
                self.index_path, self._format_size(size), elapsed,
            )
            with self._get_stats_lock():
                self._cached_index_size = size
                self._cached_index_size_at = time.time()
                if self._cached_stats is not None:
                    self._cached_stats["index_size_bytes"] = size
                    self._cached_stats["index_size"] = self._format_size(size)
                    self._cached_stats["index_size_pending"] = False
        except Exception:
            logger.exception("Echec du calcul asynchrone de index_size pour '%s'", self.index_path)
        finally:
            with self._get_stats_lock():
                self._index_size_refreshing = False

    def _get_stats_lock(self):
        """Retourne le verrou cache, y compris pour les doubles de tests minimaux."""
        lock = getattr(self, "_stats_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._stats_lock = lock
        return lock

    def _get_index_size(self, now: float) -> tuple[int, bool, bool]:
        """Retourne la taille de l'index avec stale-while-revalidate.

        Si le TTL est depasse et qu'une valeur precedente existe, retourne
        la valeur perimee immediatement et lance un recalcul en arriere-plan.
        Au premier appel, lance aussi le calcul en arriere-plan et retourne
        temporairement une taille nulle marquee comme en cours de calcul.
        """
        with self._get_stats_lock():
            if self._cached_index_size_at and now - self._cached_index_size_at < _INDEX_SIZE_TTL_SECONDS:
                return self._cached_index_size, False, False

        # TTL depasse — si on a deja une valeur, retourner stale + recalcul async
        with self._get_stats_lock():
            if self._cached_index_size_at:
                stale_value = self._cached_index_size
                if not self._index_size_refreshing:
                    self._index_size_refreshing = True
                    threading.Thread(
                        target=self._refresh_index_size_background, daemon=True,
                    ).start()
                    logger.debug("index_size stale pour '%s', recalcul en arriere-plan", self.index_path)
                return stale_value, True, False

        # Premier appel: lancer le parcours du disque sans bloquer la route.
        with self._get_stats_lock():
            if not self._index_size_refreshing:
                self._index_size_refreshing = True
                threading.Thread(
                    target=self._refresh_index_size_background,
                    name="index-size-refresh",
                    daemon=True,
                ).start()
            return 0, False, True

    def stats(self) -> dict:
        now = time.time()
        with self._get_stats_lock():
            if self._cached_stats and now - self._cached_stats_at < _STATS_TTL_SECONDS:
                age = now - self._cached_stats_at
                logger.debug("stats cache hit pour '%s' (age: %.0fs)", self.index_path, age)
                return dict(self._cached_stats)

        self._maybe_reload()
        try:
            searcher = self.index.searcher()
            num_docs = self._value_or_call(searcher.num_docs)
            num_segments = self._value_or_call(searcher.num_segments)
        except Exception as exc:
            raise IndexAccessError(f"Stats indisponibles sur '{self.index_path}': {exc}") from exc

        index_size_bytes, index_size_is_stale, index_size_pending = self._get_index_size(now)
        segment_details = self._load_segment_details()

        payload = {
            "total_docs": num_docs,
            "num_segments": num_segments,
            "index_size_bytes": index_size_bytes,
            "index_size": "Calcul..." if index_size_pending else self._format_size(index_size_bytes),
            "index_size_pending": index_size_pending,
            "segments": segment_details[:50],
        }
        # Ne pas figer dans le cache court un payload contenant une taille stale:
        # le prochain appel doit pouvoir voir la valeur fraiche calculee en fond.
        if not index_size_is_stale and not index_size_pending:
            with self._get_stats_lock():
                self._cached_stats = dict(payload)
                self._cached_stats_at = now
        return payload
