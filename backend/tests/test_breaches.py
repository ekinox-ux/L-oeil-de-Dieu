"""Tests pour la route /api/breaches et le service breach_lookup."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Environnement de test et sys.path: voir conftest.py.
import deps
import main
from services.breach_lookup import (
    BdEntry,
    BreachResult,
    HibpDetails,
    ProviderResponse,
    _lookup_breachdirectory,
    _lookup_hibp,
)


class DummyEngine:
    def search(self, query, limit=20, offset=0):
        return {"query": query, "total": 0, "limit": limit, "offset": offset, "hits": []}

    def stats(self):
        return {"total_docs": 0, "num_segments": 0, "index_size_bytes": 0, "index_size": "0", "segments": []}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    deps.indexes.clear()
    deps.indexes["test"] = {
        "engine": DummyEngine(),
        "name": "Test",
        "path": str(tmp_path),
        "source": str(tmp_path),
    }
    monkeypatch.setattr(deps, "API_KEY", "test-bot-key")
    monkeypatch.setattr(deps, "ALLOW_LOCALHOST_NO_AUTH", False)
    monkeypatch.setattr(deps, "ALLOWED_ROOTS", [tmp_path])
    return TestClient(main.app, raise_server_exceptions=False)


def _headers():
    return {"X-API-Key": "test-bot-key"}


# ---- Tests de normalisation des providers ----

def test_hibp_normalizes_response():
    """HIBP provider normalise tous les champs disponibles."""
    fake_data = [
        {
            "Name": "Adobe",
            "Title": "Adobe",
            "Domain": "adobe.com",
            "BreachDate": "2013-10-04",
            "AddedDate": "2013-12-04T00:00:00Z",
            "ModifiedDate": "2022-05-15T23:52:49Z",
            "PwnCount": 152445165,
            "Description": "Leaked data <em>with</em> HTML",
            "LogoPath": "https://logos.haveibeenpwned.com/Adobe.png",
            "DataClasses": ["Email addresses", "Passwords"],
            "IsVerified": True,
            "IsFabricated": False,
            "IsSensitive": False,
            "IsRetired": False,
            "IsSpamList": False,
            "IsMalware": False,
            "IsSubscriptionFree": False,
            "IsStealerLog": False,
            "Attribution": None,
            "DisclosureUrl": None,
        }
    ]
    with patch("services.breach_lookup._http_get_json", return_value=fake_data):
        with patch("services.breach_lookup.HIBP_API_KEY", "fake-key"):
            result = _lookup_hibp("test@example.com")

    assert result.ok is True
    assert len(result.results) == 1
    b = result.results[0]
    # Common fields
    assert b.name == "Adobe"
    assert b.domain == "adobe.com"
    assert b.breach_date == "2013-10-04"
    assert b.description == "Leaked data <em>with</em> HTML"
    assert "Email addresses" in b.data_classes
    assert b.source_provider == "hibp"
    assert b.logo_url == "https://logos.haveibeenpwned.com/Adobe.png"
    assert b.password_exposed is True
    # HIBP details
    h = b.hibp_details
    assert h is not None
    assert h.title == "Adobe"
    assert h.added_date == "2013-12-04T00:00:00Z"
    assert h.modified_date == "2022-05-15T23:52:49Z"
    assert h.pwn_count == 152445165
    assert h.is_verified is True
    assert h.is_fabricated is False
    assert h.is_sensitive is False
    assert h.is_retired is False
    assert h.is_spam_list is False
    assert h.is_malware is False
    assert h.is_subscription_free is False
    assert h.is_stealer_log is False
    assert h.attribution == ""
    assert h.disclosure_url == ""


def test_hibp_no_password_sets_false():
    """HIBP breach sans 'Passwords' dans DataClasses -> password_exposed=False."""
    fake_data = [
        {
            "Name": "ShareThis",
            "Domain": "sharethis.com",
            "BreachDate": "2018-07-01",
            "DataClasses": ["Email addresses"],
        }
    ]
    with patch("services.breach_lookup._http_get_json", return_value=fake_data):
        with patch("services.breach_lookup.HIBP_API_KEY", "fake-key"):
            result = _lookup_hibp("test@example.com")

    assert result.results[0].password_exposed is False
    assert result.results[0].logo_url == ""


def test_hibp_no_key_returns_error():
    """HIBP sans cle API retourne ok=false."""
    with patch("services.breach_lookup.HIBP_API_KEY", ""):
        result = _lookup_hibp("test@example.com")
    assert result.ok is False
    assert "non configuree" in result.error


def test_hibp_404_returns_empty():
    """HIBP 404 = aucune fuite, pas une erreur."""
    import urllib.error
    with patch("services.breach_lookup.HIBP_API_KEY", "fake-key"):
        with patch("services.breach_lookup._http_get_json", side_effect=urllib.error.HTTPError(None, 404, "Not Found", {}, None)):
            result = _lookup_hibp("clean@example.com")
    assert result.ok is True
    assert result.results == []


def test_breachdirectory_normalizes_response():
    """BreachDirectory normalise avec bd_entries individuels."""
    fake_data = {
        "result": [
            {"email": "t@x.com", "hash_password": True, "password": "Test1***",
             "sha1": "8308651804facb7b9af8ffc53a33a22d6a1c8ac2",
             "hash": "59A/Ao+PVv7KMee7zkWj2c2RM5gxfHg=", "sources": "LinkedIn.com"},
            {"email": "t@x.com", "hash_password": True, "password": "Wolve***",
             "sha1": "362c151071ae52b2ebcb0358fef08f17b706fca9",
             "hash": "GCW5GDO8TYzejZutpSQ5Wc6bLJplPCL8uYJzWg==", "sources": "Chegg.com"},
            {"email": "t@x.com", "has_password": False, "sources": "ShareThis.com"},
            {"email": "t@x.com", "hash_password": True, "password": "abc***",
             "sha1": "aaa", "hash": "bbb", "sources": "LinkedIn.com"},  # 2nd credential same source
        ]
    }
    with patch("services.breach_lookup._http_get_json", return_value=fake_data):
        with patch("services.breach_lookup.BREACHDIRECTORY_RAPIDAPI_KEY", "fake-key"):
            result = _lookup_breachdirectory("test@example.com")

    assert result.ok is True
    assert len(result.results) == 3
    names = {r.name for r in result.results}
    assert names == {"LinkedIn.com", "Chegg.com", "ShareThis.com"}

    # LinkedIn: 2 credentials grouped
    linkedin = next(r for r in result.results if r.name == "LinkedIn.com")
    assert linkedin.password_exposed is True
    assert "Passwords" in linkedin.data_classes
    assert len(linkedin.bd_entries) == 2
    assert linkedin.bd_entries[0].password == "Test1***"
    assert linkedin.bd_entries[0].sha1 == "8308651804facb7b9af8ffc53a33a22d6a1c8ac2"
    assert linkedin.bd_entries[0].hash == "59A/Ao+PVv7KMee7zkWj2c2RM5gxfHg="
    assert linkedin.bd_entries[0].hash_password is True
    assert linkedin.bd_entries[1].password == "abc***"

    # Chegg: 1 credential
    chegg = next(r for r in result.results if r.name == "Chegg.com")
    assert len(chegg.bd_entries) == 1
    assert chegg.bd_entries[0].password == "Wolve***"

    # ShareThis: no password
    sharethis = next(r for r in result.results if r.name == "ShareThis.com")
    assert sharethis.password_exposed is False
    assert sharethis.data_classes == []
    assert len(sharethis.bd_entries) == 1
    assert sharethis.bd_entries[0].has_password is False


def test_breachdirectory_sources_as_list():
    """BreachDirectory gere aussi sources comme liste (ancien format)."""
    fake_data = {
        "result": [
            {"sources": ["LinkedIn", "Dropbox"]},
        ]
    }
    with patch("services.breach_lookup._http_get_json", return_value=fake_data):
        with patch("services.breach_lookup.BREACHDIRECTORY_RAPIDAPI_KEY", "fake-key"):
            result = _lookup_breachdirectory("test@example.com")

    assert result.ok is True
    assert len(result.results) == 2
    names = {r.name for r in result.results}
    assert names == {"LinkedIn", "Dropbox"}


def test_breachdirectory_no_key_returns_error():
    with patch("services.breach_lookup.BREACHDIRECTORY_RAPIDAPI_KEY", ""):
        result = _lookup_breachdirectory("test@example.com")
    assert result.ok is False
    assert "non configuree" in result.error


# ---- Tests de la route agrégée ----

def test_breaches_route_all_providers_ok(client, monkeypatch):
    """Route /api/breaches avec tous les providers OK et sous-objets enrichis."""
    async def fake_lookup_all(email):
        return [
            ProviderResponse(name="hibp", ok=True, results=[
                BreachResult(name="Adobe", domain="adobe.com", breach_date="2013-10-04",
                             source_provider="hibp", logo_url="https://logos.haveibeenpwned.com/Adobe.png",
                             password_exposed=True,
                             hibp_details=HibpDetails(
                                 title="Adobe", pwn_count=152445165,
                                 added_date="2013-12-04T00:00:00Z",
                                 is_verified=True, is_sensitive=False)),
            ]),
            ProviderResponse(name="breachdirectory", ok=True, results=[
                BreachResult(name="LinkedIn.com", source_provider="breachdirectory",
                             password_exposed=True, data_classes=["Passwords"],
                             bd_entries=[BdEntry(source="LinkedIn.com", password="Test1***",
                                                 hash_password=True, sha1="abc123")]),
            ]),
        ]

    monkeypatch.setattr("routes.breaches.lookup_all", fake_lookup_all)

    res = client.get("/api/breaches?email=test@example.com", headers=_headers())
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["providers_ok"] == 2
    assert data["summary"]["total_hits"] == 2
    # HIBP enriched
    hibp_r = data["providers"][0]["results"][0]
    assert hibp_r["logo_url"] == "https://logos.haveibeenpwned.com/Adobe.png"
    assert hibp_r["password_exposed"] is True
    hd = hibp_r["hibp_details"]
    assert hd is not None
    assert hd["title"] == "Adobe"
    assert hd["pwn_count"] == 152445165
    assert hd["added_date"] == "2013-12-04T00:00:00Z"
    assert hd["is_verified"] is True
    assert hibp_r["bd_entries"] == []
    # BD enriched
    bd_r = data["providers"][1]["results"][0]
    assert bd_r["password_exposed"] is True
    assert bd_r["hibp_details"] is None
    assert len(bd_r["bd_entries"]) == 1
    assert bd_r["bd_entries"][0]["password"] == "Test1***"
    assert bd_r["bd_entries"][0]["sha1"] == "abc123"
    assert bd_r["bd_entries"][0]["hash_password"] is True


def test_breaches_route_partial_failure(client, monkeypatch):
    """Un provider en erreur ne fait pas echouer les autres."""
    async def fake_lookup_all(email):
        return [
            ProviderResponse(name="hibp", ok=True, results=[
                BreachResult(name="Adobe", source_provider="hibp"),
            ]),
            ProviderResponse(name="breachdirectory", ok=False, error="timeout"),
        ]

    monkeypatch.setattr("routes.breaches.lookup_all", fake_lookup_all)

    res = client.get("/api/breaches?email=test@example.com", headers=_headers())
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["providers_ok"] == 1
    assert data["summary"]["providers_failed"] == 1
    assert data["summary"]["total_hits"] == 1
    bd = next(p for p in data["providers"] if p["name"] == "breachdirectory")
    assert bd["ok"] is False
    assert bd["error"] == "timeout"


def test_breaches_route_zero_results(client, monkeypatch):
    """Aucun resultat de tous les providers."""
    async def fake_lookup_all(email):
        return [
            ProviderResponse(name="hibp", ok=True, results=[]),
            ProviderResponse(name="breachdirectory", ok=True, results=[]),
        ]

    monkeypatch.setattr("routes.breaches.lookup_all", fake_lookup_all)

    res = client.get("/api/breaches?email=clean@example.com", headers=_headers())
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["total_hits"] == 0
    assert data["summary"]["providers_ok"] == 2


def test_breaches_route_invalid_email(client):
    """Email invalide retourne 400."""
    res = client.get("/api/breaches?email=not-an-email", headers=_headers())
    assert res.status_code == 400


def test_breaches_route_requires_auth(client):
    """Route /api/breaches necessite authentification."""
    res = client.get("/api/breaches?email=test@example.com")
    assert res.status_code == 403


def test_breaches_route_all_providers_failed(client, monkeypatch):
    """Tous les providers en erreur — la route reste 200 avec summary coherent."""
    async def fake_lookup_all(email):
        return [
            ProviderResponse(name="hibp", ok=False, error="Cle absente"),
            ProviderResponse(name="breachdirectory", ok=False, error="timeout"),
        ]

    monkeypatch.setattr("routes.breaches.lookup_all", fake_lookup_all)

    res = client.get("/api/breaches?email=test@example.com", headers=_headers())
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["providers_ok"] == 0
    assert data["summary"]["providers_failed"] == 2
    assert data["summary"]["total_hits"] == 0
