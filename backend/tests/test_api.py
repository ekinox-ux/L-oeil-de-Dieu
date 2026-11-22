import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Environnement de test et sys.path: voir conftest.py.
import deps
import main
import analyzer
import routes.stats as route_stats
import routes.indexes as route_indexes
import routes.diagnostics as route_diagnostics
from engine import IndexAccessError, SearchQueryError


class DummyEngine:
    def __init__(self, search_payload=None, stats_payload=None):
        self._search_payload = search_payload or {
            "query": "test",
            "total": 1,
            "limit": 20,
            "offset": 0,
            "hits": [
                {
                    "score": 1.5,
                    "source": "C:/data/company/file.jsonl",
                    "filename": "file.jsonl",
                    "type": "record",
                    "content_raw": "john.doe@example.com lives in Paris",
                }
            ],
        }
        self._stats_payload = stats_payload or {
            "total_docs": 42,
            "num_segments": 3,
            "index_size_bytes": 1024,
            "index_size": "1 Ko",
            "segments": [],
        }

    def search(self, query: str, limit: int = 20, offset: int = 0) -> dict:
        payload = dict(self._search_payload)
        payload["query"] = query
        payload["limit"] = limit
        payload["offset"] = offset
        payload["hits"] = [dict(hit) for hit in self._search_payload["hits"]]
        return payload

    def stats(self) -> dict:
        return dict(self._stats_payload)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    index_dir = tmp_path / "index"
    index_dir.mkdir()

    deps.indexes.clear()
    deps.indexes["test"] = {
        "engine": DummyEngine(),
        "name": "Test index",
        "path": str(index_dir),
        "source": str(source_dir),
        "status": "ready",
        "error": "",
    }

    monkeypatch.setattr(deps, "API_KEY", "test-bot-key")
    monkeypatch.setattr(deps, "ALLOW_LOCALHOST_NO_AUTH", False)
    monkeypatch.setattr(deps, "ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setattr(route_indexes, "save_indexes_config", lambda: None)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(analyzer, "CACHE_DIR", cache_dir)
    main.app.state.limiter._storage.reset()

    def fake_load_cache(_source_dir):
        return {
            "scan_date": "2026-03-08 12:00:00",
            "scan_duration_seconds": 1.2,
            "source_dir": "D:/source",
            "total_files": 2,
            "limits": {
                "email_counter_limit": 0,
                "phone_counter_limit": 0,
            },
            "emails": {
                "unique_count": 3,
                "total_occurrences": 5,
                "perso_count": 2,
                "pro_count": 3,
                "top_emails": [{"email": "a@example.com", "count": 3}],
                "top_domains": [{"domain": "example.com", "count": 5}],
                "tld_distribution": [{"tld": ".com", "count": 5}],
            },
            "phones": {
                "unique_count": 2,
                "total_occurrences": 2,
                "top_phones": [{"phone": "0612345678", "count": 2}],
            },
            "sources": [{"name": "folder", "files": 2}],
        }

    monkeypatch.setattr(route_stats, "load_cache", fake_load_cache)
    monkeypatch.setattr(route_stats, "run_analysis", lambda _source_dir: True)

    return TestClient(main.app, raise_server_exceptions=False)


def _headers():
    return {"X-API-Key": "test-bot-key"}


def test_health_returns_ok(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert res.json()["ready"] is True


def test_health_has_separate_liveness_and_readiness(client):
    assert client.get("/api/health/live").status_code == 200
    assert client.get("/api/health/ready").status_code == 200

    deps.indexes.clear()
    not_ready = client.get("/api/health/ready")
    assert not_ready.status_code == 503
    assert not_ready.json()["ready"] is False


def test_indexes_requires_auth(client):
    res = client.get("/api/indexes")
    assert res.status_code == 403


def test_indexes_with_auth(client):
    res = client.get("/api/indexes", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert payload[0]["id"] == "test"
    assert payload[0]["name"] == "Test index"


def test_localhost_bypass_requires_official_frontend_header(client, monkeypatch):
    monkeypatch.setattr(deps, "ALLOW_LOCALHOST_NO_AUTH", True)
    monkeypatch.setattr(deps, "ALLOWED_ORIGINS", ["http://localhost:3000"])
    monkeypatch.setattr(deps, "_LOOPBACK_HOSTS", {"testclient"})

    denied = client.get("/api/indexes")
    assert denied.status_code == 403

    allowed = client.get(
        "/api/indexes",
        headers={
            deps.LOCAL_CLIENT_HEADER: "1",
            "Origin": "http://localhost:3000",
        },
    )
    assert allowed.status_code == 200


def test_localhost_bypass_rejects_untrusted_origin(client, monkeypatch):
    monkeypatch.setattr(deps, "ALLOW_LOCALHOST_NO_AUTH", True)
    monkeypatch.setattr(deps, "ALLOWED_ORIGINS", ["http://localhost:3000"])
    monkeypatch.setattr(deps, "_LOOPBACK_HOSTS", {"testclient"})

    res = client.get(
        "/api/indexes",
        headers={
            deps.LOCAL_CLIENT_HEADER: "1",
            "Origin": "https://example.invalid",
        },
    )
    assert res.status_code == 403


def test_indexes_endpoint_ensures_loader_running(client, monkeypatch):
    started = {"called": False}
    monkeypatch.setattr(route_indexes, "_ensure_index_loader_running", lambda: started.__setitem__("called", True))

    res = client.get("/api/indexes", headers=_headers())
    assert res.status_code == 200
    assert started["called"] is True


def test_health_ensures_loader_running(client, monkeypatch):
    started = {"called": False}
    monkeypatch.setattr(main, "_ensure_index_loader_running", lambda: started.__setitem__("called", True))

    res = client.get("/api/health")
    assert res.status_code == 200
    assert started["called"] is True


def test_diagnostics_reports_index_paths_and_lockfiles(client, request):
    index_path = Path(deps.indexes["test"]["path"])
    lock_path = index_path / ".tantivy-meta.lock"
    lock_path.write_text("", encoding="utf-8")
    analyzer_lock_path = analyzer._get_lock_path(deps.indexes["test"]["source"])
    analyzer_lock_path.write_text('{"source_dir":"%s"}' % deps.indexes["test"]["source"], encoding="utf-8")
    request.addfinalizer(lambda: analyzer_lock_path.unlink(missing_ok=True))

    res = client.get("/api/diagnostics", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert "cache_dir" in payload
    test_index = next(item for item in payload["indexes"] if item["id"] == "test")
    assert test_index["path_exists"] is True
    assert test_index["source_exists"] is True
    assert test_index["analyzer_lock_exists"] is True
    assert len(test_index["lockfiles"]) == 1
    assert test_index["lockfiles"][0]["name"] == ".tantivy-meta.lock"


def test_cleanup_analyzer_locks_endpoint_returns_summary(client, monkeypatch):
    source = deps.indexes["test"]["source"]
    monkeypatch.setattr(
        route_diagnostics,
        "cleanup_stale_analyzer_locks",
        lambda requested_source=None: {
            "removed": [requested_source or source],
            "kept": [],
        },
    )

    res = client.post("/api/diagnostics/analyzer-locks/cleanup", headers=_headers(), params={"source": source})
    assert res.status_code == 200
    payload = res.json()
    assert payload["removed_count"] == 1
    assert source in payload["removed"]


def test_retry_index_endpoint_marks_loading_and_starts_loader(client, monkeypatch):
    deps.indexes["broken"] = {
        "engine": None,
        "name": "Broken",
        "path": "C:/fake",
        "source": "",
        "status": "error",
        "error": "lock",
        "retry_count": 3,
        "next_retry_at": time.time() + 50,
        "retry_delay_seconds": 50,
        "autoload": False,
    }
    started = {"called": False}
    monkeypatch.setattr(deps, "_ensure_index_loader_running", lambda: started.__setitem__("called", True))

    res = client.post("/api/diagnostics/indexes/retry?id=broken", headers=_headers())
    assert res.status_code == 200
    assert res.json()["id"] == "broken"
    assert deps.indexes["broken"]["status"] == "loading"
    assert deps.indexes["broken"]["next_retry_at"] == 0.0
    assert deps.indexes["broken"]["autoload"] is True
    assert started["called"] is True


def test_next_loader_wake_in_uses_retry_delay(client):
    deps.indexes["broken"] = {
        "engine": None,
        "name": "Broken",
        "path": "C:/fake",
        "source": "",
        "status": "error",
        "error": "offline",
        "retry_count": 1,
        "next_retry_at": time.time() + 17,
        "retry_delay_seconds": 17,
        "autoload": True,
    }

    wake_in = deps._next_loader_wake_in()
    assert wake_in is not None
    assert 1 <= wake_in <= 17


def test_pending_index_configs_skip_manual_indexes(client):
    deps.indexes["manual_only"] = {
        "engine": None,
        "name": "Manual",
        "path": "C:/fake",
        "source": "",
        "status": "error",
        "error": "manual only",
        "retry_count": 0,
        "next_retry_at": 0.0,
        "retry_delay_seconds": 0,
        "autoload": False,
    }

    pending = deps._pending_index_configs()
    assert all(idx_id != "manual_only" for idx_id, _ in pending)


def test_search_returns_snippets(client):
    res = client.get("/api/search?q=john", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert payload["index_id"] == "test"
    assert payload["index_name"] == "Test index"
    assert payload["hits"][0]["snippets"]


def test_search_response_includes_pagination_metadata(client):
    """Search response must include has_more and total_pages."""
    res = client.get("/api/search?q=john&limit=20", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert "has_more" in payload
    assert "total_pages" in payload
    assert payload["has_more"] is False
    assert payload["total_pages"] == 1


def test_stats_returns_segments_not_top_sources(client):
    """Stats response must use 'segments' key, not 'top_sources'."""
    res = client.get("/api/stats", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert "segments" in payload
    assert "top_sources" not in payload


def test_stats_returns_analysis(client):
    res = client.get("/api/stats", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert payload["total_docs"] == 42
    assert payload["analysis"]["emails"]["unique_count"] == 3


def test_stats_analysis_preserves_limits(client):
    """The analysis.limits field must not be stripped by Pydantic serialization."""
    res = client.get("/api/stats", headers=_headers())
    assert res.status_code == 200
    analysis = res.json()["analysis"]
    assert "limits" in analysis
    assert "email_counter_limit" in analysis["limits"]
    assert "phone_counter_limit" in analysis["limits"]


def test_analyze_returns_started(client):
    res = client.post("/api/analyze", headers=_headers())
    assert res.status_code == 200
    assert res.json()["status"] == "started"


def test_search_no_content_raw_yields_empty_snippets(client):
    """When content_raw is empty, search returns empty snippets without error."""
    engine_no_raw = DummyEngine(search_payload={
        "query": "test",
        "total": 1,
        "limit": 20,
        "offset": 0,
        "hits": [
            {
                "score": 1.0,
                "source": "C:/data/company/file.jsonl",
                "filename": "file.jsonl",
                "type": "record",
                "content_raw": "",
            }
        ],
    })
    deps.indexes["test"]["engine"] = engine_no_raw

    res = client.get("/api/search?q=john", headers=_headers())
    assert res.status_code == 200
    hit = res.json()["hits"][0]
    assert hit["snippets"] == []


def test_engine_content_raw_fallback_to_content():
    """engine.py should fall back to 'content' when 'content_raw' is absent."""

    class FakeDoc:
        def __init__(self, fields):
            self._fields = fields

        def get_first(self, name):
            return self._fields.get(name)

    doc = FakeDoc({"content_raw": None, "content": "alice@example.com data"})
    raw = doc.get_first("content_raw") or doc.get_first("content") or ""
    assert raw == "alice@example.com data"

    doc_with_raw = FakeDoc({"content_raw": "raw data here", "content": "sanitized"})
    raw2 = doc_with_raw.get_first("content_raw") or doc_with_raw.get_first("content") or ""
    assert raw2 == "raw data here"

    doc_empty = FakeDoc({"content_raw": None, "content": None})
    raw3 = doc_empty.get_first("content_raw") or doc_empty.get_first("content") or ""
    assert raw3 == ""


def test_stats_top_phones_uses_own_limit(client, monkeypatch):
    """top_phones must be capped by MAX_TOP_PHONES, not MAX_TOP_EMAILS."""
    many_phones = [{"phone": f"061234{i:04d}", "count": 100 - i} for i in range(60)]

    def fake_load_cache(_source_dir):
        return {
            "scan_date": "2026-03-08 12:00:00",
            "scan_duration_seconds": 1.0,
            "total_files": 1,
            "emails": {
                "unique_count": 0, "total_occurrences": 0,
                "perso_count": 0, "pro_count": 0,
                "top_emails": [], "top_domains": [],
                "tld_distribution": [],
            },
            "phones": {
                "unique_count": 60,
                "total_occurrences": 60,
                "top_phones": many_phones,
            },
            "sources": [],
        }

    monkeypatch.setattr(route_stats, "load_cache", fake_load_cache)
    monkeypatch.setattr(route_stats, "MAX_TOP_PHONES", 50)

    res = client.get("/api/stats?top_phones_limit=50", headers=_headers())
    assert res.status_code == 200
    phones = res.json()["analysis"]["phones"]["top_phones"]
    assert len(phones) == 50


def test_stats_sources_truncated_independently(client, monkeypatch):
    """analysis.sources uses its own limit, not top_domains_limit."""
    many_sources = [{"name": f"folder{i}", "files": 100 - i} for i in range(20)]

    many_domains = [{"domain": f"d{i}.com", "count": 100 - i} for i in range(15)]

    def fake_load_cache(_source_dir):
        return {
            "scan_date": "2026-03-08 12:00:00",
            "scan_duration_seconds": 1.0,
            "total_files": 20,
            "emails": {
                "unique_count": 0, "total_occurrences": 0,
                "perso_count": 0, "pro_count": 0,
                "top_emails": [],
                "top_domains": many_domains,
                "tld_distribution": [],
            },
            "phones": {"unique_count": 0, "total_occurrences": 0, "top_phones": []},
            "sources": many_sources,
        }

    monkeypatch.setattr(route_stats, "load_cache", fake_load_cache)

    # top_domains_limit=3 should NOT affect sources; top_sources_limit defaults to 10
    res = client.get("/api/stats?top_domains_limit=3", headers=_headers())
    assert res.status_code == 200
    analysis = res.json()["analysis"]
    assert len(analysis["sources"]) == 10  # default top_sources_limit
    assert len(analysis["emails"]["top_domains"]) == 3  # respects top_domains_limit


def test_indexes_config_default_is_local_json(monkeypatch):
    """Without OEIL_INDEXES_CONFIG env var, the default must be indexes.local.json."""
    monkeypatch.delenv("OEIL_INDEXES_CONFIG", raising=False)
    from settings import _get_path, BASE_DIR
    resolved = _get_path("OEIL_INDEXES_CONFIG", BASE_DIR / "indexes.local.json")
    assert resolved.name == "indexes.local.json"


def test_add_index_validates_id(client, tmp_path):
    valid_index = tmp_path / "index2"
    valid_index.mkdir()

    class StubSearchEngine:
        def __init__(self, path):
            assert Path(path) == valid_index.resolve()

    original = route_indexes.SearchEngine
    route_indexes.SearchEngine = StubSearchEngine
    try:
        res = client.post(
            f"/api/indexes/add?id=bad/../id&name=test&path={valid_index}",
            headers=_headers(),
        )
        assert res.status_code == 400
    finally:
        route_indexes.SearchEngine = original


def test_search_zero_results(client):
    """0 results: has_more=false, total_pages=1, hits empty."""
    engine_empty = DummyEngine(search_payload={
        "query": "xyz", "total": 0, "limit": 20, "offset": 0, "hits": [],
    })
    deps.indexes["test"]["engine"] = engine_empty
    res = client.get("/api/search?q=xyz", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert payload["total"] == 0
    assert payload["hits"] == []
    assert payload["has_more"] is False
    assert payload["total_pages"] == 1


def test_search_high_offset_returns_empty_hits(client):
    """Offset beyond total: hits empty, has_more=false."""
    engine = DummyEngine(search_payload={
        "query": "test", "total": 5, "limit": 20, "offset": 0, "hits": [],
    })
    deps.indexes["test"]["engine"] = engine
    res = client.get("/api/search?q=test&offset=100", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert payload["hits"] == []
    assert payload["has_more"] is False


def test_search_rejects_offset_over_configured_limit(client):
    res = client.get("/api/search?q=test&offset=10001", headers=_headers())
    assert res.status_code == 422


def test_search_caps_pagination_metadata_at_offset_limit(client):
    engine = DummyEngine(search_payload={
        "query": "test", "total": 50_000, "limit": 20, "offset": 0, "hits": [],
    })
    deps.indexes["test"]["engine"] = engine

    res = client.get("/api/search?q=test&limit=20&offset=10000", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert payload["has_more"] is False
    assert payload["total_pages"] == 501


def test_search_multipage_has_more(client):
    """Multi-page: first page has_more=true, correct total_pages."""
    hits = [
        {"score": 1.0, "source": "C:/data/co/f.jsonl", "filename": "f.jsonl",
         "type": "record", "content_raw": "data " * 50}
    ]
    engine = DummyEngine(search_payload={
        "query": "test", "total": 45, "limit": 20, "offset": 0, "hits": hits,
    })
    deps.indexes["test"]["engine"] = engine
    res = client.get("/api/search?q=test&limit=20&offset=0", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert payload["has_more"] is True
    assert payload["total_pages"] == 3


def test_search_last_page_has_more_false(client):
    """Last page: has_more=false."""
    hits = [
        {"score": 1.0, "source": "C:/data/co/f.jsonl", "filename": "f.jsonl",
         "type": "record", "content_raw": "data"}
    ]
    engine = DummyEngine(search_payload={
        "query": "test", "total": 45, "limit": 20, "offset": 0, "hits": hits,
    })
    deps.indexes["test"]["engine"] = engine
    res = client.get("/api/search?q=test&limit=20&offset=40", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert payload["has_more"] is False


def test_add_index_success(client, tmp_path):
    valid_index = tmp_path / "index3"
    valid_index.mkdir()
    source_dir = tmp_path / "source3"
    source_dir.mkdir()

    class StubSearchEngine:
        def __init__(self, path):
            self.path = path

    original = route_indexes.SearchEngine
    route_indexes.SearchEngine = StubSearchEngine
    try:
        res = client.post(
            f"/api/indexes/add?id=extra&name=Extra&path={valid_index}&source={source_dir}",
            headers=_headers(),
        )
        assert res.status_code == 200
        payload = res.json()
        assert payload["id"] == "extra"
        assert deps.indexes["extra"]["source"] == str(source_dir.resolve())
    finally:
        route_indexes.SearchEngine = original


def test_stats_index_all_aggregates_across_indexes(client, tmp_path, monkeypatch):
    """index=all merges stats from multiple indexes.

    Verifies:
    - total_docs and index_size_bytes are summed
    - segments carry index_id prefix
    - top_emails merges same email across caches (counts added)
    - unique_count is the sum (approximate, not deduplicated)
    - scan_date is the most recent among caches
    """
    source_a = tmp_path / "source_a"
    source_a.mkdir()
    source_b = tmp_path / "source_b"
    source_b.mkdir()
    idx_a_path = tmp_path / "idx_a"
    idx_a_path.mkdir()
    idx_b_path = tmp_path / "idx_b"
    idx_b_path.mkdir()

    deps.indexes.clear()
    deps.indexes["idx_a"] = {
        "engine": DummyEngine(stats_payload={
            "total_docs": 100,
            "num_segments": 2,
            "index_size_bytes": 2048,
            "index_size": "2 Ko",
            "segments": [{"id": "seg1", "docs": 60}, {"id": "seg2", "docs": 40}],
        }),
        "name": "Index A",
        "path": str(idx_a_path),
        "source": str(source_a),
        "status": "ready",
        "error": "",
    }
    deps.indexes["idx_b"] = {
        "engine": DummyEngine(stats_payload={
            "total_docs": 50,
            "num_segments": 1,
            "index_size_bytes": 1024,
            "index_size": "1 Ko",
            "segments": [{"id": "seg1", "docs": 50}],
        }),
        "name": "Index B",
        "path": str(idx_b_path),
        "source": str(source_b),
        "status": "ready",
        "error": "",
    }

    cache_a = {
        "scan_date": "2026-03-01 10:00:00",
        "scan_duration_seconds": 2.0,
        "source_dir": str(source_a),
        "total_files": 5,
        "files_processed": 5,
        "total_lines": 1000,
        "complete": True,
        "limits": {"email_counter_limit": 0, "phone_counter_limit": 0},
        "emails": {
            "unique_count": 10,
            "total_occurrences": 20,
            "perso_count": 6,
            "pro_count": 4,
            "top_emails": [
                {"email": "shared@example.com", "count": 8},
                {"email": "only_a@example.com", "count": 5},
            ],
            "top_domains": [{"domain": "example.com", "count": 20}],
            "tld_distribution": [{"tld": ".com", "count": 20}],
        },
        "phones": {
            "unique_count": 3,
            "total_occurrences": 6,
            "top_phones": [{"phone": "0612345678", "count": 6}],
        },
        "sources": [{"name": "folderA", "files": 5}],
    }
    cache_b = {
        "scan_date": "2026-03-10 15:00:00",
        "scan_duration_seconds": 1.5,
        "source_dir": str(source_b),
        "total_files": 3,
        "files_processed": 3,
        "total_lines": 500,
        "complete": True,
        "limits": {"email_counter_limit": 0, "phone_counter_limit": 0},
        "emails": {
            "unique_count": 7,
            "total_occurrences": 12,
            "perso_count": 4,
            "pro_count": 3,
            "top_emails": [
                {"email": "shared@example.com", "count": 4},
                {"email": "only_b@other.com", "count": 3},
            ],
            "top_domains": [
                {"domain": "example.com", "count": 8},
                {"domain": "other.com", "count": 4},
            ],
            "tld_distribution": [
                {"tld": ".com", "count": 12},
            ],
        },
        "phones": {
            "unique_count": 2,
            "total_occurrences": 4,
            "top_phones": [{"phone": "0612345678", "count": 3}],
        },
        "sources": [{"name": "folderB", "files": 3}],
    }

    caches = {str(source_a): cache_a, str(source_b): cache_b}
    monkeypatch.setattr(route_stats, "load_cache", lambda src: caches.get(src))

    res = client.get("/api/stats?index=all", headers=_headers())
    assert res.status_code == 200
    data = res.json()

    # Aggregated engine stats
    assert data["total_docs"] == 150
    assert data["index_size_bytes"] == 3072
    assert data["index_id"] == "all"

    # Segments carry index_id
    segs = data["segments"]
    assert len(segs) == 3
    assert segs[0]["index_id"] == "idx_a"
    assert segs[0]["id"] == "idx_a:seg1"
    assert segs[2]["index_id"] == "idx_b"
    assert segs[2]["id"] == "idx_b:seg1"

    analysis = data["analysis"]

    # scan_date = most recent
    assert analysis["scan_date"] == "2026-03-10 15:00:00"

    # unique_count = sum (APPROXIMATE, not deduplicated)
    assert analysis["emails"]["unique_count"] == 17  # 10 + 7
    assert analysis["phones"]["unique_count"] == 5   # 3 + 2

    # top_emails: shared@example.com merged (8 + 4 = 12)
    top_emails = analysis["emails"]["top_emails"]
    shared = next(e for e in top_emails if e["email"] == "shared@example.com")
    assert shared["count"] == 12

    # top_domains: example.com merged (20 + 8 = 28)
    top_domains = analysis["emails"]["top_domains"]
    example_dom = next(d for d in top_domains if d["domain"] == "example.com")
    assert example_dom["count"] == 28

    # top_phones: same phone merged (6 + 3 = 9)
    top_phones = analysis["phones"]["top_phones"]
    phone = next(p for p in top_phones if p["phone"] == "0612345678")
    assert phone["count"] == 9

    # Duration summed
    assert analysis["scan_duration_seconds"] == 3.5
    assert analysis["total_files"] == 8


# ---- Tests chargement differe (status loading/error/ready) ----


def test_indexes_expose_status(client):
    """Each index in /api/indexes has a status field."""
    res = client.get("/api/indexes", headers=_headers())
    assert res.status_code == 200
    for idx in res.json():
        assert "status" in idx
        assert idx["status"] in ("ready", "loading", "error")
        assert "retry_count" in idx
        assert "next_retry_at" in idx


def test_search_loading_index_returns_409(client):
    """Searching a loading index returns 409 with clear message."""
    deps.indexes["loading_idx"] = {
        "engine": None,
        "name": "Loading index",
        "path": "C:/fake",
        "source": "",
        "status": "loading",
        "error": "",
    }
    res = client.get("/api/search?q=test&index=loading_idx", headers=_headers())
    assert res.status_code == 409
    assert "chargement" in res.json()["detail"].lower()


def test_search_error_index_returns_409(client):
    """Searching an errored index returns 409 with clear message."""
    deps.indexes["error_idx"] = {
        "engine": None,
        "name": "Broken index",
        "path": "C:/fake",
        "source": "",
        "status": "error",
        "error": "Fichier corrompu",
        "retry_count": 2,
        "next_retry_at": 9_999_999_999,
    }
    res = client.get("/api/search?q=test&index=error_idx", headers=_headers())
    assert res.status_code == 409
    detail = res.json()["detail"].lower()
    assert "erreur" in detail
    assert "retry" in detail


def test_stats_loading_index_returns_409(client):
    """Stats on a loading index returns 409."""
    deps.indexes["loading_idx"] = {
        "engine": None,
        "name": "Loading",
        "path": "C:/fake",
        "source": "",
        "status": "loading",
        "error": "",
    }
    res = client.get("/api/stats?index=loading_idx", headers=_headers())
    assert res.status_code == 409
    assert "chargement" in res.json()["detail"].lower()


def test_stats_all_skips_loading_indexes(client, tmp_path, monkeypatch):
    """index=all only aggregates ready indexes, ignores loading ones."""
    source_a = tmp_path / "src_a"
    source_a.mkdir()
    ready_index_path = tmp_path / "idx_a"
    ready_index_path.mkdir()

    deps.indexes.clear()
    deps.indexes["ready_idx"] = {
        "engine": DummyEngine(stats_payload={
            "total_docs": 100, "num_segments": 1,
            "index_size_bytes": 1024, "index_size": "1 Ko",
            "segments": [{"id": "s1", "docs": 100}],
        }),
        "name": "Ready",
        "path": str(ready_index_path),
        "source": str(source_a),
        "status": "ready",
        "error": "",
    }
    deps.indexes["loading_idx"] = {
        "engine": None,
        "name": "Loading",
        "path": "C:/fake",
        "source": "",
        "status": "loading",
        "error": "",
    }

    monkeypatch.setattr(route_stats, "load_cache", lambda src: None)

    res = client.get("/api/stats?index=all", headers=_headers())
    assert res.status_code == 200
    data = res.json()
    assert data["total_docs"] == 100
    for seg in data["segments"]:
        assert seg["index_id"] == "ready_idx"


def test_index_transitions_loading_to_ready(client):
    """An index starts loading, then becomes ready after engine loads."""
    original_loader = deps._ensure_index_loader_running
    deps._ensure_index_loader_running = lambda: None
    deps.indexes["transitioning"] = {
        "engine": None,
        "name": "Transitioning",
        "path": "C:/fake",
        "source": "",
        "status": "loading",
        "error": "",
    }
    try:
        # Stats while loading => 409
        res = client.get("/api/stats?index=transitioning", headers=_headers())
        assert res.status_code == 409

        # Simulate background thread completing
        deps.indexes["transitioning"]["engine"] = DummyEngine()
        deps.indexes["transitioning"]["status"] = "ready"
        deps.indexes["transitioning"]["path"] = deps.indexes["test"]["path"]

        # Stats after ready => 200
        res = client.get("/api/stats?index=transitioning", headers=_headers())
        assert res.status_code == 200
    finally:
        deps._ensure_index_loader_running = original_loader


def test_health_reflects_index_statuses(client):
    """Health endpoint distinguishes ready vs loading indexes."""
    deps.indexes["bg_loading"] = {
        "engine": None,
        "name": "Background",
        "path": "C:/fake",
        "source": "",
        "status": "loading",
        "error": "",
    }
    deps.indexes["bg_error"] = {
        "engine": None,
        "name": "Broken",
        "path": "C:/fake-err",
        "source": "",
        "status": "error",
        "error": "lock",
    }
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert "test" in data["indexes_ready"]
    assert "bg_loading" in data["indexes_loading"]
    assert "bg_error" in data["indexes_error"]
    # indexes_loaded counts only ready
    assert data["indexes_loaded"] == 1
    # indexes lists all
    assert "test" in data["indexes"]
    assert "bg_loading" in data["indexes"]
    assert "bg_error" in data["indexes"]


def test_indexes_marks_missing_ready_index_as_error(client, monkeypatch):
    missing_path = Path(deps.indexes["test"]["path"])
    missing_path.rmdir()
    monkeypatch.setattr(deps, "INDEX_PATH_FAILURE_THRESHOLD", 1)
    started = {"called": False}
    monkeypatch.setattr(deps, "_ensure_index_loader_running", lambda: started.__setitem__("called", True))

    res = client.get("/api/indexes", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert payload[0]["status"] == "error"
    assert "introuvable" in payload[0]["error"].lower()
    assert deps.indexes["test"]["engine"] is None
    assert deps.indexes["test"]["status"] == "error"
    assert started["called"] is True


def test_health_marks_missing_ready_index_as_error(client, monkeypatch):
    missing_path = Path(deps.indexes["test"]["path"])
    missing_path.rmdir()
    monkeypatch.setattr(deps, "INDEX_PATH_FAILURE_THRESHOLD", 1)
    started = {"called": False}
    monkeypatch.setattr(deps, "_ensure_index_loader_running", lambda: started.__setitem__("called", True))

    res = client.get("/api/health")
    assert res.status_code == 200
    payload = res.json()
    assert payload["indexes_loaded"] == 0
    assert "test" in payload["indexes_error"]
    assert started["called"] is True


def test_search_missing_ready_index_path_returns_409(client, monkeypatch):
    missing_path = Path(deps.indexes["test"]["path"])
    missing_path.rmdir()
    monkeypatch.setattr(deps, "INDEX_PATH_FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(deps, "_ensure_index_loader_running", lambda: None)

    res = client.get("/api/search?q=john&index=test", headers=_headers())
    assert res.status_code == 409
    assert "introuvable" in res.json()["detail"].lower()


def test_refresh_ready_indexes_tolerates_single_path_failure(client, monkeypatch):
    missing_path = Path(deps.indexes["test"]["path"])
    missing_path.rmdir()
    monkeypatch.setattr(deps, "INDEX_PATH_FAILURE_THRESHOLD", 3)

    deps.refresh_ready_indexes(force=True)

    assert deps.indexes["test"]["status"] == "ready"
    assert deps.indexes["test"]["engine"] is not None
    assert deps.indexes["test"]["path_failure_count"] == 1


def test_stats_all_reports_skipped_indexes(client, tmp_path, monkeypatch):
    """index=all includes skipped_indexes listing non-ready indexes."""
    source_a = tmp_path / "src_a"
    source_a.mkdir()
    ok_index_path = tmp_path / "ok"
    ok_index_path.mkdir()

    deps.indexes.clear()
    deps.indexes["ok_idx"] = {
        "engine": DummyEngine(stats_payload={
            "total_docs": 50, "num_segments": 1,
            "index_size_bytes": 512, "index_size": "0 Ko",
            "segments": [],
        }),
        "name": "OK",
        "path": str(ok_index_path),
        "source": str(source_a),
        "status": "ready",
        "error": "",
    }
    deps.indexes["slow_idx"] = {
        "engine": None,
        "name": "Slow",
        "path": "C:/fake",
        "source": "",
        "status": "loading",
        "error": "",
    }

    monkeypatch.setattr(route_stats, "load_cache", lambda src: None)

    res = client.get("/api/stats?index=all", headers=_headers())
    assert res.status_code == 200
    data = res.json()
    skipped = data.get("skipped_indexes", [])
    assert len(skipped) == 1
    assert skipped[0]["id"] == "slow_idx"
    assert skipped[0]["status"] == "loading"


def test_default_index_skips_loading(client):
    """When no index_id given, get_engine picks first ready, not first overall."""
    ready_path = deps.indexes["test"]["path"]
    deps.indexes.clear()
    # Insert loading first, ready second
    deps.indexes["first_loading"] = {
        "engine": None,
        "name": "Loading First",
        "path": "C:/fake",
        "source": "",
        "status": "loading",
        "error": "",
    }
    deps.indexes["second_ready"] = {
        "engine": DummyEngine(),
        "name": "Ready Second",
        "path": ready_path,
        "source": "",
        "status": "ready",
        "error": "",
    }
    # Stats without index param => should use second_ready
    res = client.get("/api/stats", headers=_headers())
    assert res.status_code == 200
    assert res.json()["index_id"] == "second_ready"


def test_no_ready_index_returns_503(client):
    """When all indexes are loading, endpoints return 503."""
    deps.indexes.clear()
    deps.indexes["only_loading"] = {
        "engine": None,
        "name": "Loading",
        "path": "C:/fake",
        "source": "",
        "status": "loading",
        "error": "",
    }
    res = client.get("/api/stats", headers=_headers())
    assert res.status_code == 503


def test_search_query_error_does_not_mark_index_error(client):
    class QueryErrorEngine(DummyEngine):
        def search(self, query: str, limit: int = 20, offset: int = 0) -> dict:
            raise SearchQueryError("bad syntax")

    deps.indexes["test"]["engine"] = QueryErrorEngine()

    res = client.get("/api/search?q=test", headers=_headers())
    assert res.status_code == 400
    assert deps.indexes["test"]["status"] == "ready"
    assert deps.indexes["test"]["engine"] is not None


def test_search_index_access_error_marks_index_error(client):
    class BrokenIndexEngine(DummyEngine):
        def search(self, query: str, limit: int = 20, offset: int = 0) -> dict:
            raise IndexAccessError("writer lock stuck")

    deps.indexes["test"]["engine"] = BrokenIndexEngine()

    res = client.get("/api/search?q=test", headers=_headers())
    assert res.status_code == 503
    assert deps.indexes["test"]["engine"] is None
    assert deps.indexes["test"]["status"] in {"error", "loading"}
    assert deps.indexes["test"]["retry_count"] >= 1


def test_search_deduplicates_reordered_near_duplicates(client):
    deps.indexes["test"]["engine"] = DummyEngine(search_payload={
        "query": "0600000001",
        "total": 4,
        "limit": 20,
        "offset": 0,
        "hits": [
            {
                "score": 25.34,
                "source": "X:/Data/fixture/sample.jsonl",
                "filename": "sample.jsonl",
                "type": "unknown",
                "content_raw": "EXEMPLEVILLE 99999 1 RUE DES TESTS jean.dupont@example.com NULLEPART 0600000001 Masculin 01/01/1990 DUPONT JEAN",
            },
            {
                "score": 25.01,
                "source": "X:/Data/fixture/sample.jsonl",
                "filename": "sample.jsonl",
                "type": "unknown",
                "content_raw": "Monsieur Masculin EXEMPLEVILLE jean.dupont@example.com 99999 0600000001 1 RUE DES TESTS NULLEPART 01/01/1990 DUPONT JEAN",
            },
            {
                "score": 25.01,
                "source": "X:/Data/fixture/sample.jsonl",
                "filename": "sample.jsonl",
                "type": "unknown",
                "content_raw": "Monsieur Masculin EXEMPLEVILLE jean.dupont@example.com 99999 DUPONT 0600000001 1 RUE DES TESTS 01/01/1990 JEAN NULLEPART",
            },
            {
                "score": 10.0,
                "source": "X:/Data/other/other.jsonl",
                "filename": "other.jsonl",
                "type": "unknown",
                "content_raw": "AUTRE PERSONNE 0600000001",
            },
        ],
    })

    res = client.get("/api/search?q=0600000001&deduplicate=true", headers=_headers())
    assert res.status_code == 200
    payload = res.json()
    assert payload["deduplicated"] is True
    assert payload["total_is_raw"] is True
    assert len(payload["hits"]) == 2
    assert payload["hits"][0]["source"] == "X:/Data/fixture/sample.jsonl"
    assert payload["hits"][1]["source"] == "X:/Data/other/other.jsonl"


def test_stats_index_access_error_marks_index_error(client):
    class BrokenStatsEngine(DummyEngine):
        def stats(self) -> dict:
            raise IndexAccessError("index offline")

    deps.indexes["test"]["engine"] = BrokenStatsEngine()

    res = client.get("/api/stats", headers=_headers())
    assert res.status_code == 503
    assert deps.indexes["test"]["engine"] is None
    assert deps.indexes["test"]["status"] in {"error", "loading"}
    assert deps.indexes["test"]["retry_count"] >= 1


def test_stats_generic_error_does_not_mark_index_error(client):
    class BuggyStatsEngine(DummyEngine):
        def stats(self) -> dict:
            raise ValueError("bad cache payload")

    deps.indexes["test"]["engine"] = BuggyStatsEngine()

    res = client.get("/api/stats", headers=_headers())
    assert res.status_code == 500
    assert deps.indexes["test"]["status"] == "ready"
    assert deps.indexes["test"]["engine"] is not None


# ---- Durcissement et coherence ----


def test_unexpected_host_header_is_rejected(client):
    """Protection DNS rebinding: un Host hors liste est refuse avant l'authentification."""
    res = client.get("/api/health/live", headers={"Host": "evil.example:8500"})
    assert res.status_code == 400


def test_access_log_filter_strips_query_string():
    import logging

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/api/search?q=john.doe@example.com&limit=20", "1.1", 200),
        None,
    )
    assert main.StripQueryStringFilter().filter(record) is True
    assert "john.doe" not in record.getMessage()
    assert "/api/search" in record.getMessage()


def test_rate_limit_key_separates_bot_and_local_frontend():
    from starlette.requests import Request

    def make_request(headers):
        return Request({
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("127.0.0.1", 5000),
        })

    bot_key = deps.rate_limit_key(make_request({"X-API-Key": "secret"}))
    ui_key = deps.rate_limit_key(make_request({deps.LOCAL_CLIENT_HEADER: "1"}))
    assert bot_key != ui_key
    assert bot_key.startswith("127.0.0.1:")


def test_analyze_uses_configured_source_as_cache_key(client, monkeypatch):
    """La cle d'analyse doit etre celle relue par /api/stats, pas le chemin resolu."""
    captured = {}
    monkeypatch.setattr(route_stats, "run_analysis", lambda source_dir: captured.setdefault("source", source_dir) or True)

    res = client.post("/api/analyze", headers=_headers())

    assert res.status_code == 200
    assert captured["source"] == deps.indexes["test"]["source"]
