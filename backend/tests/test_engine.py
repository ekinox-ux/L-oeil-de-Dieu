"""Tests unitaires pour la logique de cache de SearchEngine."""

import os
import sys
import time
import threading
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import engine as engine_mod
from engine import IndexOpenError, SearchEngine


class FakeSearcher:
    def __init__(self, num_docs=100, num_segments=3):
        self.num_docs = num_docs
        self.num_segments = num_segments


class FakeIndex:
    def __init__(self, num_docs=100, num_segments=3):
        self._num_docs = num_docs
        self._num_segments = num_segments
        self.schema = {}

    def open(self, path):
        return self

    def reload(self):
        pass

    def register_tokenizer(self, name, analyzer):
        pass

    def searcher(self):
        return FakeSearcher(self._num_docs, self._num_segments)


def _make_engine(tmp_path, num_docs=100, num_segments=3):
    """Cree un SearchEngine avec un faux index tantivy."""
    index_dir = tmp_path / "idx"
    index_dir.mkdir(exist_ok=True)
    # Fichier bidon pour que _compute_index_size trouve quelque chose
    (index_dir / "data.bin").write_bytes(b"x" * 1024)

    fake_index = FakeIndex(num_docs, num_segments)

    with patch.object(SearchEngine, "__init__", lambda self, path: None):
        eng = SearchEngine.__new__(SearchEngine)

    eng.index_path = str(index_dir)
    eng.index = fake_index
    eng.schema = {}
    eng._reload_lock = threading.Lock()
    eng._last_reload_at = 0.0
    eng._reload_in_progress = False
    eng._cached_stats = None
    eng._cached_stats_at = 0.0
    eng._cached_index_size = 0
    eng._cached_index_size_at = 0.0
    eng._index_size_refreshing = False
    return eng


def _wait_for_size_refresh(eng, timeout=1.0):
    deadline = time.monotonic() + timeout
    while eng._index_size_refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not eng._index_size_refreshing


def test_stats_returns_cached_within_stats_ttl(tmp_path):
    """Deux appels < 5 min retournent le meme resultat sans recalcul."""
    eng = _make_engine(tmp_path)

    with patch.object(eng, "_compute_index_size", return_value=2048) as mock_size:
        result1 = eng.stats()
        _wait_for_size_refresh(eng)
        result2 = eng.stats()

    assert result1["total_docs"] == 100
    assert result2["total_docs"] == 100
    # _compute_index_size appele une seule fois (cache stats couvre tout)
    assert mock_size.call_count == 1


def test_stats_refreshes_docs_after_stats_ttl_but_keeps_index_size(tmp_path):
    """Apres expiration du TTL stats (5 min), num_docs est recalcule
    mais index_size reste en cache (TTL 1h)."""
    eng = _make_engine(tmp_path, num_docs=100)

    with patch.object(eng, "_compute_index_size", return_value=2048) as mock_size:
        pending = eng.stats()
        assert pending["index_size_pending"] is True
        _wait_for_size_refresh(eng)
        result1 = eng.stats()
        assert result1["total_docs"] == 100
        assert mock_size.call_count == 1

        # Simuler expiration du cache stats (5 min)
        eng._cached_stats_at -= engine_mod._STATS_TTL_SECONDS + 1
        # Changer num_docs dans le faux index
        eng.index._num_docs = 200

        result2 = eng.stats()
        assert result2["total_docs"] == 200
        # index_size pas recalcule (TTL 1h pas expire)
        assert mock_size.call_count == 1
        assert result2["index_size_bytes"] == 2048


def test_index_size_refreshes_after_long_ttl(tmp_path):
    """Apres expiration du TTL index_size (1h), la valeur stale est retournee
    immediatement et _compute_index_size est rappele en arriere-plan."""
    eng = _make_engine(tmp_path)

    call_sizes = [1024, 4096]
    with patch.object(eng, "_compute_index_size", side_effect=call_sizes) as mock_size:
        eng.stats()
        _wait_for_size_refresh(eng)
        result1 = eng.stats()
        assert result1["index_size_bytes"] == 1024

        # Expirer les deux caches
        eng._cached_stats_at -= engine_mod._STATS_TTL_SECONDS + 1
        eng._cached_index_size_at -= engine_mod._INDEX_SIZE_TTL_SECONDS + 1

        # Stale-while-revalidate: retourne l'ancienne valeur immediatement
        result2 = eng.stats()
        assert result2["index_size_bytes"] == 1024  # stale value

        # Attendre que le thread de recalcul termine
        time.sleep(0.1)
        assert mock_size.call_count == 2
        assert eng._cached_index_size == 4096

        # Le prochain appel stats retourne la nouvelle valeur sans attendre
        # l'expiration du cache stats (invalidation par le refresh async)
        result3 = eng.stats()
        assert result3["index_size_bytes"] == 4096


def test_index_size_last_known_value_survives_stats_refresh(tmp_path):
    """La derniere valeur connue de index_size est reutilisee tant que son TTL
    n'est pas expire, meme si les stats legeres sont recalculees plusieurs fois."""
    eng = _make_engine(tmp_path)

    with patch.object(eng, "_compute_index_size", return_value=5000) as mock_size:
        eng.stats()
        _wait_for_size_refresh(eng)
        eng.stats()
        assert mock_size.call_count == 1

        # 3 expirations du cache stats, aucune du cache index_size
        for _ in range(3):
            eng._cached_stats_at -= engine_mod._STATS_TTL_SECONDS + 1
            result = eng.stats()
            assert result["index_size_bytes"] == 5000

        # Toujours 1 seul appel a _compute_index_size
        assert mock_size.call_count == 1


def test_stale_while_revalidate_prevents_concurrent_refresh(tmp_path):
    """Deux appels rapproches apres expiration ne lancent qu'un seul recalcul."""
    eng = _make_engine(tmp_path)

    call_sizes = [1024, 8192]
    with patch.object(eng, "_compute_index_size", side_effect=call_sizes) as mock_size:
        eng.stats()
        _wait_for_size_refresh(eng)
        eng.stats()
        assert mock_size.call_count == 1

        # Expirer les deux caches
        eng._cached_stats_at -= engine_mod._STATS_TTL_SECONDS + 1
        eng._cached_index_size_at -= engine_mod._INDEX_SIZE_TTL_SECONDS + 1

        # Premier appel apres expiration: retourne stale, lance thread
        result1 = eng.stats()
        assert result1["index_size_bytes"] == 1024

        # Deuxieme appel pendant le refresh: pas de second thread
        eng._cached_stats_at -= engine_mod._STATS_TTL_SECONDS + 1
        result2 = eng.stats()
        assert result2["index_size_bytes"] in (1024, 8192)  # stale ou fresh

        time.sleep(0.1)
        # Un seul recalcul en arriere-plan
        assert mock_size.call_count == 2


def test_first_call_starts_index_size_in_background(tmp_path):
    """Le premier appel retourne sans attendre le parcours complet du disque."""
    eng = _make_engine(tmp_path)

    with patch.object(eng, "_compute_index_size", return_value=8192) as mock_size:
        result = eng.stats()
        _wait_for_size_refresh(eng)
        ready = eng.stats()

    assert result["total_docs"] == 100
    assert result["num_segments"] == 3
    assert result["index_size_bytes"] == 0
    assert result["index_size"] == "Calcul..."
    assert result["index_size_pending"] is True
    assert ready["index_size_bytes"] == 8192
    assert ready["index_size"] == "8 Ko"
    assert ready["index_size_pending"] is False
    assert mock_size.call_count == 1


def test_open_index_never_removes_lockfiles_automatically(tmp_path, monkeypatch):
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    lock_path = index_dir / ".tantivy-meta.lock"
    lock_path.write_text("", encoding="utf-8")
    old_time = time.time() - 120
    os.utime(lock_path, (old_time, old_time))

    attempts = {"count": 0}

    def fake_open(path):
        attempts["count"] += 1
        raise ValueError("Failed to acquire Lockfile: PermissionDenied")

    monkeypatch.setattr(engine_mod.tantivy.Index, "open", fake_open)
    with pytest.raises(IndexOpenError) as exc_info:
        SearchEngine(str(index_dir))

    assert "pas ete supprime automatiquement" in str(exc_info.value)
    assert attempts["count"] == 1
    assert lock_path.exists()


def test_open_index_keeps_recent_lockfiles(tmp_path, monkeypatch):
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    lock_path = index_dir / ".tantivy-meta.lock"
    lock_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        engine_mod.tantivy.Index,
        "open",
        lambda _path: (_ for _ in ()).throw(ValueError("Failed to acquire Lockfile: PermissionDenied")),
    )
    with pytest.raises(IndexOpenError) as exc_info:
        SearchEngine(str(index_dir))

    assert "pas ete supprime automatiquement" in str(exc_info.value)
    assert lock_path.exists()


def test_open_index_retries_transient_missing_path(tmp_path, monkeypatch):
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    fake_index = FakeIndex()
    isdir_calls = {"count": 0}

    def fake_isdir(path):
        assert path == str(index_dir)
        isdir_calls["count"] += 1
        return isdir_calls["count"] >= 3

    monkeypatch.setattr(engine_mod.os.path, "isdir", fake_isdir)
    monkeypatch.setattr(engine_mod.tantivy.Index, "open", lambda path: fake_index)

    eng = SearchEngine(str(index_dir))
    assert eng.index is fake_index
    assert isdir_calls["count"] == 3


def _make_concurrent_engine(tmp_path, reload_duration=0.5):
    """Engine de test avec un reload simulable lent pour les tests de concurrence."""
    class ConcurrentSearcher:
        num_docs = 10
        num_segments = 2

        def search(self, _parsed, _limit):
            return type("Result", (), {"hits": [(1.0, "doc1")], "count": 1})()

        def doc(self, _doc_address):
            return type("Doc", (), {
                "get_first": lambda self, name: {
                    "content_raw": "john data",
                    "content": "john data",
                    "source": "C:/source",
                    "filename": "file.jsonl",
                    "type": "record",
                }.get(name)
            })()

    class ConcurrentIndex:
        def __init__(self, reload_duration):
            self.schema = {}
            self._active = 0
            self._guard = threading.Lock()
            self.overlap_detected = False
            self.reload_count = 0
            self._reload_duration = reload_duration

        def register_tokenizer(self, _name, _analyzer):
            pass

        def reload(self):
            with self._guard:
                self._active += 1
                self.reload_count += 1
                if self._active > 1:
                    self.overlap_detected = True
            time.sleep(self._reload_duration)
            with self._guard:
                self._active -= 1

        def searcher(self):
            return ConcurrentSearcher()

        def parse_query(self, query, _fields):
            return query

    with patch.object(SearchEngine, "__init__", lambda self, path: None):
        eng = SearchEngine.__new__(SearchEngine)

    index_dir = tmp_path / "idx"
    index_dir.mkdir(exist_ok=True)
    (index_dir / "meta.json").write_text('{"segments":[]}', encoding="utf-8")
    eng.index_path = str(index_dir)
    eng.index = ConcurrentIndex(reload_duration)
    eng.schema = {}
    eng._reload_lock = threading.Lock()
    eng._last_reload_at = 0.0
    eng._reload_in_progress = False
    eng._cached_stats = None
    eng._cached_stats_at = 0.0
    eng._cached_index_size = 0
    eng._cached_index_size_at = 0.0
    eng._index_size_refreshing = False
    return eng


def test_reload_never_runs_concurrently(tmp_path):
    """Deux threads qui appellent search/stats ensemble ne declenchent pas
    de reload concurrent : un seul thread execute reload(), l'autre l'ignore."""
    eng = _make_concurrent_engine(tmp_path, reload_duration=0.1)

    stats_thread = threading.Thread(target=eng.stats)
    search_thread = threading.Thread(target=lambda: eng.search("john"))
    stats_thread.start()
    search_thread.start()
    stats_thread.join()
    search_thread.join()

    assert eng.index.overlap_detected is False
    # Un seul reload : le second appelant a vu _reload_in_progress=True et a skip.
    assert eng.index.reload_count == 1


def test_search_does_not_block_on_slow_reload(tmp_path):
    """Un reload lent en cours ne doit PAS bloquer les autres search/stats.

    C'est le cas-cible du bug idle : avant, tout etait serialise sur
    _tantivy_lock, donc un reload bloque dans mmap fige toutes les recherches.
    """
    eng = _make_concurrent_engine(tmp_path, reload_duration=1.0)

    # Thread A : declenche un reload lent (1 s).
    a = threading.Thread(target=eng.stats)
    a.start()
    # Laisse A entrer dans reload().
    time.sleep(0.05)

    # Thread B : lance une search pendant que A est toujours dans reload.
    b_started = time.perf_counter()
    b_done = threading.Event()

    def run_search():
        eng.search("john")
        b_done.set()

    b = threading.Thread(target=run_search)
    b.start()
    # B doit terminer largement avant la fin de A (< 300 ms vs 1 s).
    assert b_done.wait(timeout=0.3), "search bloquee par reload en cours"
    b_elapsed = time.perf_counter() - b_started
    assert b_elapsed < 0.3, f"search trop lente : {b_elapsed:.2f}s"

    a.join()
    b.join()


def test_reload_is_throttled(tmp_path, monkeypatch):
    """Dans l'intervalle de throttle, seul le premier search declenche reload."""
    eng = _make_concurrent_engine(tmp_path, reload_duration=0.0)
    monkeypatch.setattr(engine_mod, "_RELOAD_INTERVAL_SECONDS", 60.0)

    eng.search("a")
    eng.search("b")
    eng.search("c")
    eng.stats()

    assert eng.index.reload_count == 1


def test_reload_happens_after_interval(tmp_path, monkeypatch):
    """Apres _RELOAD_INTERVAL_SECONDS, un nouvel appel declenche un reload."""
    eng = _make_concurrent_engine(tmp_path, reload_duration=0.0)
    # 100 ms + sleep 250 ms : marge large pour absorber la resolution timer Windows (15.6 ms).
    monkeypatch.setattr(engine_mod, "_RELOAD_INTERVAL_SECONDS", 0.1)

    eng.search("a")
    assert eng.index.reload_count == 1
    time.sleep(0.25)
    eng.search("b")
    assert eng.index.reload_count == 2


def test_search_survives_failing_reload(tmp_path):
    """Si reload() leve, search() continue avec le searcher courant."""
    eng = _make_concurrent_engine(tmp_path, reload_duration=0.0)

    def failing_reload():
        eng.index.reload_count += 1
        raise RuntimeError("E: disque inaccessible")

    eng.index.reload = failing_reload
    # search ne doit pas propager l'erreur de reload.
    result = eng.search("john")
    assert result["total"] == 1
    assert eng.index.reload_count == 1
    # L'etat reload_in_progress est bien remis a False meme apres l'exception.
    assert eng._reload_in_progress is False


# ---- Analyseur de requetes aligne sur l'indexeur Rust ----


@pytest.mark.parametrize("code,expected", [
    ("fr", "french"), ("en", "english"), ("es", "spanish"),
    ("de", "german"), ("it", "italian"), ("pt", "portuguese"),
    ("FR", "french"), ("xx", "english"),
])
def test_language_codes_match_rust_indexer(code, expected):
    assert SearchEngine._tantivy_py_language(code) == expected


@pytest.mark.parametrize("language", ["fr", "en", "es", "de", "it", "pt"])
def test_query_analyzer_builds_with_stemming_and_stop_words(tmp_path, language):
    """tantivy-py refuse les codes courts: l'analyseur doit tout de meme se construire."""
    (tmp_path / ".schema_config.toml").write_text(
        f'language = "{language}"\nuse_stemming = true\nuse_stop_words = true\n',
        encoding="utf-8",
    )
    engine = SearchEngine.__new__(SearchEngine)
    engine.index_path = str(tmp_path)
    assert engine._build_query_analyzer() is not None
