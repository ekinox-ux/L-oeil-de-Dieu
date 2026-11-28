import asyncio
import importlib.util
from pathlib import Path

import pytest

BOT_PATH = Path(__file__).resolve().parents[1] / 'bot.py'


@pytest.fixture()
def bot_module(monkeypatch):
    monkeypatch.setenv('DISCORD_TOKEN', 'test-token')
    monkeypatch.setenv('API_BASE', 'http://127.0.0.1:8500')
    monkeypatch.setenv('ROLE_NAME', 'OSINT')
    monkeypatch.setenv('API_KEY', 'secret')

    spec = importlib.util.spec_from_file_location('oeil_bot_test', BOT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_truncate_keeps_short_text(bot_module):
    assert bot_module.truncate('abc', 10) == 'abc'


def test_truncate_shortens_long_text(bot_module):
    assert bot_module.truncate('abcdefghij', 8) == 'abcde...'


def test_format_hit_uses_company_name_and_snippet(bot_module):
    hit = {
        'source': r'C:\data\ACME\row.jsonl',
        'type': 'record',
        'score': 1.234,
        'snippets': [{'text': 'alice@example.com appears in the row'}],
    }

    rendered = bot_module.format_hit(hit, 2)

    assert '**2. ACME**' in rendered
    assert '(record)' in rendered
    assert 'score: 1.23' in rendered
    assert 'alice@example.com appears in the row' in rendered


def test_api_get_adds_api_key_header(bot_module, monkeypatch):
    seen = {}

    class FakeResponse:
        status = 200

        async def json(self):
            return {'status': 'ok'}

    class FakeRequestContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSession:
        closed = False

        def get(self, url, params=None, headers=None, timeout=None):
            seen['url'] = url
            seen['params'] = params
            seen['headers'] = headers
            seen['timeout'] = timeout
            return FakeRequestContext()

    monkeypatch.setattr(bot_module, '_http_session', FakeSession())

    async def fake_sleep(_delay):
        return None
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)

    payload = asyncio.run(bot_module.api_get('/api/health', {'index': 'france'}, retries=0))

    assert payload == {'status': 'ok'}
    assert seen['url'] == 'http://127.0.0.1:8500/api/health'
    assert seen['params'] == {'index': 'france'}
    assert seen['headers']['X-API-Key'] == 'secret'


def test_paginator_footer_partial_results(bot_module):
    """When loaded hits < total, footer should show 'X affiches sur Y'."""
    hits = [{'source': f'C:\\data\\co{i}\\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}
            for i in range(10)]
    paginator = bot_module.SearchPaginator(query='test', hits=hits, total=500, user_id=1)
    embed = paginator.build_embed()
    assert '10 resultats affiches sur 500' in embed.footer.text
    assert 'page 1/2' in embed.footer.text


def test_paginator_footer_all_results(bot_module):
    """When all results are loaded, footer should show simple count."""
    hits = [{'source': f'C:\\data\\co{i}\\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}
            for i in range(3)]
    paginator = bot_module.SearchPaginator(query='test', hits=hits, total=3, user_id=1)
    embed = paginator.build_embed()
    assert '3 resultats' in embed.footer.text
    assert 'affiches sur' not in embed.footer.text


def test_paginator_single_page(bot_module):
    """Single page: both buttons disabled, no 'affiches sur'."""
    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='test', hits=hits, total=1, user_id=1)
    assert paginator.max_page == 0
    embed = paginator.build_embed()
    assert 'page 1/1' in embed.footer.text
    assert 'affiches sur' not in embed.footer.text


def test_paginator_empty_snippets(bot_module):
    """Hit with empty snippets list renders without code block."""
    hit = {'source': r'C:\data\ACME\row.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}
    rendered = bot_module.format_hit(hit, 1)
    assert '```' not in rendered
    assert '**1. ACME**' in rendered


def test_paginator_navigation(bot_module):
    """Navigate to page 2 and back."""
    hits = [{'source': f'C:\\data\\co{i}\\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}
            for i in range(12)]
    paginator = bot_module.SearchPaginator(query='test', hits=hits, total=12, user_id=1)
    assert paginator.max_page == 2  # 12 hits / 5 per page = 3 pages (0,1,2)
    # Move to page 2
    paginator.page = 1
    paginator._update_buttons()
    embed = paginator.build_embed()
    assert 'page 2/3' in embed.footer.text
    assert not paginator.btn_prev.disabled
    assert not paginator.btn_next.disabled
    # Move to last page
    paginator.page = 2
    paginator._update_buttons()
    assert paginator.btn_next.disabled


def test_paginator_breach_button_disabled_without_email(bot_module):
    """Breach button disabled when query has no email."""
    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='john doe', hits=hits, total=1, user_id=1)
    assert paginator.btn_hibp.disabled is True
    assert paginator.btn_hibp.style == bot_module.discord.ButtonStyle.secondary


def test_paginator_breach_button_enabled_with_email(bot_module):
    """Breach button enabled when query contains an email."""
    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='alice@example.com', hits=hits, total=1, user_id=1)
    assert paginator.btn_hibp.disabled is False
    assert paginator._email is not None
    assert paginator._email.group(0) == 'alice@example.com'


def test_paginator_breach_button_extracts_email_from_mixed_query(bot_module):
    """Breach button extracts email from a mixed query string."""
    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='recherche bob@test.org 2024', hits=hits, total=1, user_id=1)
    assert paginator._email is not None
    assert paginator._email.group(0) == 'bob@test.org'
    assert paginator.btn_hibp.disabled is False


class FakeInteractionResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, content, **kwargs):
        self.sent_message = content


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, embeds=None, **kwargs):
        self.sent.append({'content': content, 'embed': embed, 'embeds': embeds, 'kwargs': kwargs})
        return None


class FakeUser:
    def __init__(self, user_id=1):
        self.id = user_id


class FakeInteraction:
    def __init__(self):
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()
        self.user = FakeUser()


def test_breach_flow_hibp_with_bd_error(bot_module, monkeypatch):
    """HIBP results + BD error: paginator shows both on page 1."""
    fake_data = {
        "summary": {"providers_ok": 1, "providers_failed": 1, "total_hits": 2},
        "providers": [
            {"name": "hibp", "ok": True, "results": [
                {"name": "Adobe", "breach_date": "2013-10-04", "domain": "adobe.com",
                 "data_classes": ["Emails", "Passwords"], "password_exposed": True,
                 "hibp_details": {"title": "Adobe", "pwn_count": 152445165,
                                  "is_verified": True, "is_sensitive": False,
                                  "is_fabricated": False, "is_malware": False, "is_stealer_log": False},
                 "bd_entries": []},
                {"name": "LinkedIn", "breach_date": "2012-05-05", "data_classes": [],
                 "password_exposed": False, "hibp_details": {"pwn_count": 164611595, "is_verified": True},
                 "bd_entries": []},
            ]},
            {"name": "breachdirectory", "ok": False, "error": "timeout"},
        ],
    }

    async def fake_api_get(endpoint, params=None, **kw):
        assert endpoint == "/api/breaches"
        assert params == {"email": "alice@example.com"}
        return fake_data

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)

    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='alice@example.com', hits=hits, total=1, user_id=1)
    interaction = FakeInteraction()

    asyncio.run(paginator.btn_hibp.callback(interaction))

    assert interaction.response.deferred
    assert len(interaction.followup.sent) == 1
    embed = interaction.followup.sent[0]['embed']
    view = interaction.followup.sent[0]['kwargs'].get('view')
    assert isinstance(view, bot_module.BreachPaginator)

    # 3 entries (2 hibp + 1 bd error) fit on 1 page
    assert view.max_page == 0
    assert embed.title == "Fuites — alice@example.com"
    # HIBP results
    assert "── hibp ──" in embed.description
    assert "**Adobe**" in embed.description
    assert "2013-10-04" in embed.description
    assert "**[MDP]**" in embed.description
    assert "[verifie]" in embed.description
    assert "152 445 165 comptes" in embed.description
    assert "Domaine: adobe.com" in embed.description
    assert "Emails, Passwords" in embed.description
    # BD error visible on same page
    assert "── breachdirectory ──" in embed.description
    assert "timeout" in embed.description
    # Footer
    assert "2 fuite(s)" in embed.footer.text
    assert "1 provider(s) OK" in embed.footer.text
    assert "1 en erreur" in embed.footer.text
    assert "page 1/1" in embed.footer.text


def test_breach_flow_bd_credentials(bot_module, monkeypatch):
    """BD only: credentials visible in paginator."""
    fake_data = {
        "summary": {"providers_ok": 1, "providers_failed": 0, "total_hits": 1},
        "providers": [
            {"name": "breachdirectory", "ok": True, "results": [
                {"name": "LinkedIn.com", "password_exposed": True, "data_classes": ["Passwords"],
                 "hibp_details": None,
                 "bd_entries": [
                     {"source": "LinkedIn.com", "password": "Test1***", "hash_password": True,
                      "sha1": "8308651804fa", "hash": "", "has_password": True},
                 ]},
            ]},
        ],
    }

    async def fake_api_get(endpoint, params=None, **kw):
        return fake_data

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)

    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='bd@example.com', hits=hits, total=1, user_id=1)
    interaction = FakeInteraction()

    asyncio.run(paginator.btn_hibp.callback(interaction))

    embed = interaction.followup.sent[0]['embed']
    assert "── breachdirectory ──" in embed.description
    assert "LinkedIn.com" in embed.description
    assert "**[MDP]**" in embed.description
    assert "`mdp: Test1***" in embed.description
    assert "hash_pwd: oui" in embed.description
    assert "sha1: 8308651804fa" in embed.description


def test_breach_flow_hibp_only(bot_module, monkeypatch):
    """HIBP only with results, BD empty: 1 entry, 1 page."""
    fake_data = {
        "summary": {"providers_ok": 2, "providers_failed": 0, "total_hits": 1},
        "providers": [
            {"name": "hibp", "ok": True, "results": [
                {"name": "Adobe", "breach_date": "2013-10-04", "password_exposed": True,
                 "data_classes": ["Passwords"], "hibp_details": {"is_verified": True}, "bd_entries": []},
            ]},
            {"name": "breachdirectory", "ok": True, "results": []},
        ],
    }

    async def fake_api_get(endpoint, params=None, **kw):
        return fake_data

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)

    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='hibp@example.com', hits=hits, total=1, user_id=1)
    interaction = FakeInteraction()

    asyncio.run(paginator.btn_hibp.callback(interaction))

    embed = interaction.followup.sent[0]['embed']
    assert "── hibp ──" in embed.description
    assert "**Adobe**" in embed.description
    assert "page 1/1" in embed.footer.text
    assert "1 fuite(s)" in embed.footer.text


def test_breach_flow_mixed_hibp_and_bd(bot_module, monkeypatch):
    """HIBP + BD results: both visible on same page with provider headers."""
    fake_data = {
        "summary": {"providers_ok": 2, "providers_failed": 0, "total_hits": 3},
        "providers": [
            {"name": "hibp", "ok": True, "results": [
                {"name": "Adobe", "breach_date": "2013-10-04", "domain": "adobe.com",
                 "data_classes": ["Emails", "Passwords"], "password_exposed": True,
                 "hibp_details": {"title": "Adobe", "pwn_count": 152445165, "is_verified": True},
                 "bd_entries": []},
            ]},
            {"name": "breachdirectory", "ok": True, "results": [
                {"name": "LinkedIn.com", "password_exposed": True, "data_classes": ["Passwords"],
                 "hibp_details": None,
                 "bd_entries": [
                     {"source": "LinkedIn.com", "password": "Pass***", "hash_password": True,
                      "sha1": "abc123", "hash": "def456", "has_password": True},
                 ]},
                {"name": "Chegg.com", "password_exposed": False, "data_classes": [],
                 "hibp_details": None, "bd_entries": [
                     {"source": "Chegg.com", "password": "", "hash_password": False,
                      "sha1": "", "hash": "", "has_password": False},
                 ]},
            ]},
        ],
    }

    async def fake_api_get(endpoint, params=None, **kw):
        return fake_data

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)

    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='mixed@example.com', hits=hits, total=1, user_id=1)
    interaction = FakeInteraction()

    asyncio.run(paginator.btn_hibp.callback(interaction))

    embed = interaction.followup.sent[0]['embed']
    view = interaction.followup.sent[0]['kwargs']['view']
    # 3 entries fit on 1 page
    assert view.max_page == 0

    # Both providers with headers
    assert "── hibp ──" in embed.description
    assert "**Adobe**" in embed.description
    assert "── breachdirectory ──" in embed.description
    assert "LinkedIn.com" in embed.description
    assert "mdp: Pass***" in embed.description
    assert "sha1: abc123" in embed.description
    assert "hash: def456" in embed.description
    assert "Chegg.com" in embed.description
    assert "pas de mot de passe" in embed.description
    # Footer
    assert "3 fuite(s)" in embed.footer.text
    assert "2 provider(s) OK" in embed.footer.text
    assert "0 en erreur" in embed.footer.text


def test_breach_flow_hibp_verbose_bd_paginated(bot_module, monkeypatch):
    """HIBP has many results: BD lands on a later page, accessible via navigation."""
    hibp_results = []
    for i in range(8):
        hibp_results.append({
            "name": f"Breach{i}", "breach_date": f"2020-0{i % 9 + 1}-01",
            "domain": f"site{i}.com", "password_exposed": True,
            "data_classes": ["Emails", "Passwords", "Usernames", "IP addresses", "Phone numbers"],
            "description": "Long breach description with details. " * 6,
            "hibp_details": {"title": f"Breach Title {i}", "pwn_count": 10000000 + i,
                             "is_verified": True, "is_sensitive": True,
                             "added_date": "2020-01-01T00:00:00Z",
                             "modified_date": "2021-06-01T00:00:00Z",
                             "attribution": "Unknown attacker group",
                             "disclosure_url": "https://example.com/disclosure"},
            "bd_entries": [],
        })
    fake_data = {
        "summary": {"providers_ok": 2, "providers_failed": 0, "total_hits": 10},
        "providers": [
            {"name": "hibp", "ok": True, "results": hibp_results},
            {"name": "breachdirectory", "ok": True, "results": [
                {"name": "LinkedIn.com", "password_exposed": True, "data_classes": ["Passwords"],
                 "hibp_details": None,
                 "bd_entries": [
                     {"source": "LinkedIn.com", "password": "Visible***", "hash_password": True,
                      "sha1": "deadbeef", "hash": "", "has_password": True},
                 ]},
                {"name": "Dropbox.com", "password_exposed": False, "data_classes": [],
                 "hibp_details": None, "bd_entries": [
                     {"source": "Dropbox.com", "password": "", "hash_password": False,
                      "sha1": "", "hash": "", "has_password": False},
                 ]},
            ]},
        ],
    }

    async def fake_api_get(endpoint, params=None, **kw):
        return fake_data

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)

    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='verbose@example.com', hits=hits, total=1, user_id=1)
    interaction = FakeInteraction()

    asyncio.run(paginator.btn_hibp.callback(interaction))

    view = interaction.followup.sent[0]['kwargs']['view']
    assert view.max_page >= 1  # multiple pages

    # Page 1: starts with HIBP
    page1 = view.build_embed()
    assert "── hibp ──" in page1.description
    assert "Breach0" in page1.description
    assert "page 1/" in page1.footer.text

    # All pages respect Discord's 4096 char limit
    for p in range(view.max_page + 1):
        view.page = p
        embed = view.build_embed()
        assert len(embed.description) <= 4096, f"page {p+1} exceeds 4096 chars: {len(embed.description)}"

    # Last page contains BD results
    view.page = view.max_page
    view._update_buttons()
    last_page = view.build_embed()
    assert "── breachdirectory ──" in last_page.description
    assert "LinkedIn.com" in last_page.description
    assert "mdp: Visible***" in last_page.description
    assert "sha1: deadbeef" in last_page.description
    assert "Dropbox.com" in last_page.description

    # Navigation buttons state on last page
    assert not view.bp_prev.disabled
    assert view.bp_next.disabled


def test_breach_pages_respect_size_limit(bot_module):
    """Every page description stays under 4096 chars even with large entries."""
    # Simulate 15 entries each ~500 chars — would be ~7500 chars total,
    # must split across multiple pages.
    providers = [
        {"name": "hibp", "ok": True, "results": [
            {"name": f"Breach{i}", "breach_date": "2020-01-01",
             "domain": f"site{i}.com", "password_exposed": True,
             "data_classes": ["Emails", "Passwords"],
             "description": "X" * 300,
             "hibp_details": {"title": f"Breach{i}", "pwn_count": 5000000,
                              "is_verified": True, "added_date": "2020-01-01T00:00:00Z",
                              "modified_date": "2021-01-01T00:00:00Z"},
             "bd_entries": []}
            for i in range(15)
        ]},
    ]
    summary = {"providers_ok": 1, "providers_failed": 0, "total_hits": 15}

    view = bot_module.BreachPaginator("test@x.com", providers, summary, user_id=1)
    assert view.max_page >= 1, "should have multiple pages"

    all_entries_seen = set()
    for p in range(view.max_page + 1):
        view.page = p
        embed = view.build_embed()
        assert len(embed.description) <= 4096, f"page {p+1}: {len(embed.description)} chars > 4096"
        # Collect which entries appear
        for i in range(15):
            if f"Breach{i}" in embed.description:
                all_entries_seen.add(i)

    # All 15 entries must appear across all pages (no data lost)
    assert all_entries_seen == set(range(15)), f"missing entries: {set(range(15)) - all_entries_seen}"


def test_breach_flow_clean(bot_module, monkeypatch):
    """Full breach flow: 0 hits, 0 failures → 'Aucune fuite connue'."""
    fake_data = {
        "summary": {"providers_ok": 2, "providers_failed": 0, "total_hits": 0},
        "providers": [
            {"name": "hibp", "ok": True, "results": []},
            {"name": "breachdirectory", "ok": True, "results": []},
        ],
    }

    async def fake_api_get(endpoint, params=None, **kw):
        return fake_data

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)

    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='clean@example.com', hits=hits, total=1, user_id=1)
    interaction = FakeInteraction()

    asyncio.run(paginator.btn_hibp.callback(interaction))

    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Fuites — clean@example.com"
    assert "Aucune fuite connue" in embed.description
    assert embed.color.value == 0x22c55e
    assert "hibp: OK" in embed.footer.text


def test_breach_flow_api_error(bot_module, monkeypatch):
    """Full breach flow: api_get returns None → error embed."""
    async def fake_api_get(endpoint, params=None, **kw):
        return None

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)

    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='fail@example.com', hits=hits, total=1, user_id=1)
    interaction = FakeInteraction()

    asyncio.run(paginator.btn_hibp.callback(interaction))

    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Backend injoignable"
    assert "fuites" in embed.description.lower()


def test_wait_for_backend_retries_until_success(bot_module, monkeypatch):
    calls = []

    async def fake_api_get(endpoint, params=None, **kw):
        calls.append((endpoint, params))
        if len(calls) < 3:
            return None
        return {'status': 'ok', 'indexes_loaded': 1}

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)

    payload = asyncio.run(bot_module.wait_for_backend(attempts=3, delay_seconds=0.01))

    assert payload == {'status': 'ok', 'indexes_loaded': 1}
    assert len(calls) == 3


def test_wait_for_backend_returns_none_after_last_attempt(bot_module, monkeypatch):
    async def fake_api_get(_endpoint, params=None, **kw):
        return None

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)

    payload = asyncio.run(bot_module.wait_for_backend(attempts=2, delay_seconds=0.01))

    assert payload is None


# ---- Tests chargement differe (409 index loading/error) ----


def _make_fake_session(response_class):
    """Helper: cree une FakeSession avec un type de reponse donne."""
    class FakeCtx:
        async def __aenter__(self):
            return response_class()
        async def __aexit__(self, *a):
            return False

    class FakeSess:
        closed = False
        def get(self, url, **kw):
            return FakeCtx()

    return FakeSess()


def test_api_get_returns_detail_on_409(bot_module, monkeypatch):
    """api_get returns structured error with _error flag on 409."""

    class Resp409:
        status = 409
        async def json(self):
            return {"detail": "L'index 'Foreign' est en cours de chargement."}

    monkeypatch.setattr(bot_module, '_http_session', _make_fake_session(Resp409))
    async def fake_sleep(_): pass
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)
    result = asyncio.run(bot_module.api_get('/api/search', {'q': 'test', 'index': 'foreign'}, retries=0))
    assert result is not None
    assert result.get("_error") is True
    assert result.get("_error_type") == "index_unavailable"
    assert "chargement" in result.get("detail", "")


def test_cmd_search_loading_index_shows_message(bot_module, monkeypatch):
    """Bot /search on loading index shows 'Index indisponible' embed."""
    async def fake_api_get(endpoint, params=None, **kw):
        if endpoint == "/api/search":
            return {"_error": True, "_error_type": "index_unavailable", "detail": "L'index 'Foreign' est en cours de chargement."}
        return None

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_search.callback(interaction, query="test", limit=20, index="foreign"))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Index indisponible"
    assert "chargement" in embed.description
    assert embed.color.value == 0xf59e0b


def test_cmd_stats_loading_index_shows_message(bot_module, monkeypatch):
    """Bot /stats on loading index shows 'Index indisponible' embed."""
    async def fake_api_get(endpoint, params=None, **kw):
        if endpoint == "/api/stats":
            return {"_error": True, "_error_type": "index_unavailable", "detail": "L'index 'Foreign' est en cours de chargement."}
        return None

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_stats.callback(interaction, index="foreign"))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Index indisponible"
    assert "chargement" in embed.description
    assert embed.color.value == 0xf59e0b


def test_cmd_search_error_index_shows_message(bot_module, monkeypatch):
    """Bot /search on errored index shows error detail."""
    async def fake_api_get(endpoint, params=None, **kw):
        return {"_error": True, "_error_type": "index_unavailable", "detail": "L'index 'Foreign' est en erreur: fichier corrompu"}

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_search.callback(interaction, query="test", limit=20, index="foreign"))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Index indisponible"
    assert "erreur" in embed.description.lower()


# ---- Tests robustesse reseau ----


def test_api_get_returns_rate_limit_on_429(bot_module, monkeypatch):
    """api_get returns typed error on 429."""
    class Resp429:
        status = 429
        async def json(self):
            return {"detail": "rate limited"}

    monkeypatch.setattr(bot_module, '_http_session', _make_fake_session(Resp429))
    async def fake_sleep(_): pass
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)
    result = asyncio.run(bot_module.api_get('/api/search', retries=0))
    assert result["_error"] is True
    assert result["_error_type"] == "rate_limit"
    assert result["_status"] == 429


def test_api_get_returns_server_error_on_500(bot_module, monkeypatch):
    """api_get returns typed error on 500+."""
    class Resp500:
        status = 500
        async def json(self):
            return {"detail": "internal"}

    monkeypatch.setattr(bot_module, '_http_session', _make_fake_session(Resp500))
    async def fake_sleep(_): pass
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)
    result = asyncio.run(bot_module.api_get('/api/search', retries=0))
    assert result["_error"] is True
    assert result["_error_type"] == "server_error"
    assert result["_status"] == 500


def test_api_get_returns_timeout_error(bot_module, monkeypatch):
    """api_get returns typed timeout error."""
    class FakeCtx:
        async def __aenter__(self):
            raise asyncio.TimeoutError()
        async def __aexit__(self, *a):
            return False

    class FakeSess:
        closed = False
        def get(self, url, **kw):
            return FakeCtx()

    monkeypatch.setattr(bot_module, '_http_session', FakeSess())
    async def fake_sleep(_): pass
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)
    result = asyncio.run(bot_module.api_get('/api/search', retries=0))
    assert result["_error"] is True
    assert result["_error_type"] == "timeout"


def test_api_get_returns_none_on_connection_error(bot_module, monkeypatch):
    """api_get returns None on connection error (backend injoignable)."""
    class FakeCtx:
        async def __aenter__(self):
            raise ConnectionRefusedError("Connection refused")
        async def __aexit__(self, *a):
            return False

    class FakeSess:
        closed = False
        def get(self, url, **kw):
            return FakeCtx()

    monkeypatch.setattr(bot_module, '_http_session', FakeSess())
    async def fake_sleep(_): pass
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)
    result = asyncio.run(bot_module.api_get('/api/search', retries=0))
    assert result is None


def test_api_get_retry_succeeds_on_second_attempt(bot_module, monkeypatch):
    """api_get retries on timeout and succeeds on second attempt."""
    calls = []

    class FakeCtxTimeout:
        async def __aenter__(self):
            raise asyncio.TimeoutError()
        async def __aexit__(self, *a):
            return False

    class FakeResp200:
        status = 200
        async def json(self):
            return {"ok": True}

    class FakeCtxOk:
        async def __aenter__(self):
            return FakeResp200()
        async def __aexit__(self, *a):
            return False

    class FakeSess:
        closed = False
        def get(self, url, **kw):
            calls.append(url)
            if len(calls) == 1:
                return FakeCtxTimeout()
            return FakeCtxOk()

    monkeypatch.setattr(bot_module, '_http_session', FakeSess())
    async def fake_sleep(_): pass
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)
    result = asyncio.run(bot_module.api_get('/api/search', retries=1))
    assert result == {"ok": True}
    assert len(calls) == 2


def test_api_get_retry_connection_error_succeeds_on_second_attempt(bot_module, monkeypatch):
    """api_get retries on connection error (None) and succeeds on second attempt."""
    calls = []

    class FakeCtxConnErr:
        async def __aenter__(self):
            raise ConnectionRefusedError("Connection refused")
        async def __aexit__(self, *a):
            return False

    class FakeResp200:
        status = 200
        async def json(self):
            return {"ok": True}

    class FakeCtxOk:
        async def __aenter__(self):
            return FakeResp200()
        async def __aexit__(self, *a):
            return False

    class FakeSess:
        closed = False
        def get(self, url, **kw):
            calls.append(url)
            if len(calls) == 1:
                return FakeCtxConnErr()
            return FakeCtxOk()

    monkeypatch.setattr(bot_module, '_http_session', FakeSess())
    async def fake_sleep(_): pass
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)
    result = asyncio.run(bot_module.api_get('/api/search', retries=1))
    assert result == {"ok": True}
    assert len(calls) == 2


def test_cmd_search_timeout_shows_correct_message(bot_module, monkeypatch):
    """Bot /search on timeout shows 'Timeout backend' embed."""
    async def fake_api_get(endpoint, params=None, **kw):
        return {"_error": True, "_error_type": "timeout", "detail": "Le backend met trop de temps a repondre."}

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_search.callback(interaction, query="test", limit=20, index=None))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Timeout backend"
    assert embed.color.value == 0xf59e0b


def test_cmd_search_rate_limit_shows_correct_message(bot_module, monkeypatch):
    """Bot /search on 429 shows 'Limite atteinte' embed."""
    async def fake_api_get(endpoint, params=None, **kw):
        return {"_error": True, "_error_type": "rate_limit", "detail": "Trop de requetes."}

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_search.callback(interaction, query="test", limit=20, index=None))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Limite atteinte"
    assert embed.color.value == 0xf59e0b


def test_cmd_search_server_error_shows_correct_message(bot_module, monkeypatch):
    """Bot /search on 500 shows 'Erreur backend' embed."""
    async def fake_api_get(endpoint, params=None, **kw):
        return {"_error": True, "_error_type": "server_error", "detail": "Erreur interne."}

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_search.callback(interaction, query="test", limit=20, index=None))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Erreur backend"
    assert embed.color.value == 0xf43f5e


def test_cmd_search_connection_error_shows_injoignable(bot_module, monkeypatch):
    """Bot /search when backend unreachable shows 'Backend injoignable'."""
    async def fake_api_get(endpoint, params=None, **kw):
        return None

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_search.callback(interaction, query="test", limit=20, index=None))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Backend injoignable"
    assert embed.color.value == 0xf43f5e


def test_cmd_stats_timeout_shows_correct_message(bot_module, monkeypatch):
    """Bot /stats on timeout shows 'Timeout backend'."""
    async def fake_api_get(endpoint, params=None, **kw):
        return {"_error": True, "_error_type": "timeout", "detail": "Timeout."}

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_stats.callback(interaction, index=None))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Timeout backend"


def test_cmd_stats_rate_limit_shows_correct_message(bot_module, monkeypatch):
    """Bot /stats on 429 shows 'Limite atteinte'."""
    async def fake_api_get(endpoint, params=None, **kw):
        return {"_error": True, "_error_type": "rate_limit", "detail": "Rate limited."}

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_stats.callback(interaction, index=None))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Limite atteinte"


def test_breach_timeout_shows_embed(bot_module, monkeypatch):
    """Breach button on timeout shows typed error embed instead of text."""
    async def fake_api_get(endpoint, params=None, **kw):
        return {"_error": True, "_error_type": "timeout", "detail": "Timeout."}

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='fail@example.com', hits=hits, total=1, user_id=1)
    interaction = FakeInteraction()
    asyncio.run(paginator.btn_hibp.callback(interaction))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Timeout backend"


def test_breach_rate_limit_shows_embed(bot_module, monkeypatch):
    """Breach button on 429 shows typed error embed."""
    async def fake_api_get(endpoint, params=None, **kw):
        return {"_error": True, "_error_type": "rate_limit", "detail": "Rate limited."}

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='fail@example.com', hits=hits, total=1, user_id=1)
    interaction = FakeInteraction()
    asyncio.run(paginator.btn_hibp.callback(interaction))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Limite atteinte"


def test_breach_connection_error_shows_embed(bot_module, monkeypatch):
    """Breach button when backend unreachable shows embed instead of text."""
    async def fake_api_get(endpoint, params=None, **kw):
        return None

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    paginator = bot_module.SearchPaginator(query='fail@example.com', hits=hits, total=1, user_id=1)
    interaction = FakeInteraction()
    asyncio.run(paginator.btn_hibp.callback(interaction))
    embed = interaction.followup.sent[0]['embed']
    assert embed.title == "Backend injoignable"
    assert "fuites" in embed.description.lower()


# ---- Tests expiration des vues ----


class FakeMessage:
    """Simule un discord.Message pour tester on_timeout."""
    def __init__(self):
        self.edited_embed = None
        self.edited_view = None

    async def edit(self, embed=None, view=None):
        self.edited_embed = embed
        self.edited_view = view


def test_search_paginator_loads_next_page_from_backend(bot_module, monkeypatch):
    calls = []

    async def fake_api_get(endpoint, params=None, **kwargs):
        calls.append((endpoint, params, kwargs))
        hits = [
            {
                'source': f'C:\\data\\co{i}\\f.jsonl',
                'type': 'record',
                'score': 1.0,
                'snippets': [],
            }
            for i in range(5, 10)
        ]
        return {'total': 12, 'hits': hits}

    monkeypatch.setattr(bot_module, 'api_get', fake_api_get)
    first_hits = [
        {
            'source': f'C:\\data\\co{i}\\f.jsonl',
            'type': 'record',
            'score': 1.0,
            'snippets': [],
        }
        for i in range(5)
    ]
    view = bot_module.SearchPaginator(
        query='test',
        hits=first_hits,
        total=12,
        user_id=1,
        index='france',
        result_limit=12,
    )
    fake_msg = FakeMessage()
    view.message = fake_msg
    interaction = FakeInteraction()

    asyncio.run(view.btn_next.callback(interaction))

    assert interaction.response.deferred is True
    assert calls == [(
        '/api/search',
        {'q': 'test', 'limit': 5, 'offset': 5, 'context': bot_module.SEARCH_CONTEXT_CHARS, 'index': 'france'},
        {'timeout': 180.0, 'retry_on_timeout': False},
    )]
    assert view.page == 1
    assert fake_msg.edited_embed is not None
    assert 'page 2/3' in fake_msg.edited_embed.footer.text
    assert '**6.' in fake_msg.edited_embed.description


def test_search_paginator_timeout_is_600(bot_module):
    """SearchPaginator timeout is 10 minutes."""
    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    view = bot_module.SearchPaginator(query='test', hits=hits, total=1, user_id=1)
    assert view.timeout == 600


def test_breach_paginator_timeout_is_600(bot_module):
    """BreachPaginator timeout is 10 minutes."""
    providers = [{"name": "hibp", "ok": True, "results": [
        {"name": "Test", "breach_date": "", "data_classes": [], "password_exposed": False,
         "hibp_details": {}, "bd_entries": []}
    ]}]
    view = bot_module.BreachPaginator("a@b.com", providers, {"total_hits": 1, "providers_ok": 1, "providers_failed": 0}, user_id=1)
    assert view.timeout == 600


def test_search_paginator_on_timeout_disables_buttons(bot_module):
    """on_timeout disables all buttons and updates footer."""
    hits = [{'source': f'C:\\data\\co{i}\\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}
            for i in range(3)]
    view = bot_module.SearchPaginator(query='test', hits=hits, total=3, user_id=1)
    fake_msg = FakeMessage()
    view.message = fake_msg

    asyncio.run(view.on_timeout())

    for item in view.children:
        assert item.disabled is True
    assert fake_msg.edited_embed is not None
    assert "expiree" in fake_msg.edited_embed.footer.text.lower()


def test_breach_paginator_on_timeout_disables_buttons(bot_module):
    """on_timeout disables all buttons and updates footer."""
    providers = [{"name": "hibp", "ok": True, "results": [
        {"name": "Test", "breach_date": "", "data_classes": [], "password_exposed": False,
         "hibp_details": {}, "bd_entries": []}
    ]}]
    view = bot_module.BreachPaginator("a@b.com", providers, {"total_hits": 1, "providers_ok": 1, "providers_failed": 0}, user_id=1)
    fake_msg = FakeMessage()
    view.message = fake_msg

    asyncio.run(view.on_timeout())

    for item in view.children:
        assert item.disabled is True
    assert fake_msg.edited_embed is not None
    assert "expiree" in fake_msg.edited_embed.footer.text.lower()


def test_search_paginator_on_timeout_no_message_no_crash(bot_module):
    """on_timeout without message reference doesn't crash."""
    hits = [{'source': r'C:\data\co0\f.jsonl', 'type': 'record', 'score': 1.0, 'snippets': []}]
    view = bot_module.SearchPaginator(query='test', hits=hits, total=1, user_id=1)
    asyncio.run(view.on_timeout())
    for item in view.children:
        assert item.disabled is True


# ---- Tests timeouts par route ----


def test_cmd_search_uses_180s_timeout(bot_module, monkeypatch):
    """cmd_search passes timeout=180.0 to api_get."""
    seen_kw = {}

    async def spy_api_get(endpoint, params=None, **kw):
        seen_kw.update(kw)
        return {"total": 0, "hits": [], "query": "test"}

    monkeypatch.setattr(bot_module, 'api_get', spy_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_search.callback(interaction, query="test", limit=20, index=None))
    assert seen_kw.get("timeout") == 180.0


def test_cmd_stats_uses_default_timeout(bot_module, monkeypatch):
    """/stats uses default 30s timeout (no explicit timeout kwarg)."""
    seen_kw = {}

    async def spy_api_get(endpoint, params=None, **kw):
        seen_kw.update(kw)
        return {"total_docs": 0, "index_size": "0", "num_segments": 0, "index_name": "test"}

    monkeypatch.setattr(bot_module, 'api_get', spy_api_get)
    interaction = FakeInteraction()
    asyncio.run(bot_module.cmd_stats.callback(interaction, index=None))
    assert "timeout" not in seen_kw


# ---- Revue : cooldown, budget d'embed, timeouts de recherche ----


class _RespondingInteractionResponse:
    def __init__(self, done=False):
        self.messages = []
        self._done = done

    def is_done(self):
        return self._done

    async def send_message(self, content, **kwargs):
        self.messages.append((content, kwargs))
        self._done = True


class _ErrorInteraction:
    def __init__(self, done=False):
        self.response = _RespondingInteractionResponse(done)


def test_cooldown_error_answers_the_user(bot_module):
    """CommandOnCooldown herite de CheckFailure : il doit quand meme repondre."""
    from discord import app_commands

    error = app_commands.CommandOnCooldown(app_commands.Cooldown(5, 60.0), 42.4)
    assert isinstance(error, app_commands.CheckFailure)
    interaction = _ErrorInteraction()

    asyncio.run(bot_module.on_app_command_error(interaction, error))

    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert "cooldown" in content and "42s" in content
    assert kwargs.get("ephemeral") is True


def test_cooldown_error_skips_when_already_answered(bot_module):
    from discord import app_commands

    error = app_commands.CommandOnCooldown(app_commands.Cooldown(5, 60.0), 3.0)
    interaction = _ErrorInteraction(done=True)

    asyncio.run(bot_module.on_app_command_error(interaction, error))

    assert interaction.response.messages == []


def test_role_check_failure_sends_nothing_more(bot_module):
    """has_required_role() a deja repondu : le handler ne repond pas une 2e fois."""
    from discord import app_commands

    interaction = _ErrorInteraction()

    asyncio.run(bot_module.on_app_command_error(interaction, app_commands.CheckFailure()))

    assert interaction.response.messages == []


def _long_hit(i, highlight_at=2000):
    text = "x" * highlight_at + "TROUVE" + "y" * 3000
    return {
        'source': f'C:\\data\\Societe{i}\\f.jsonl',
        'type': 'record',
        'score': 1.0,
        'snippets': [{'text': text, 'highlights': [[highlight_at, highlight_at + 6]]}],
    }


def test_page_of_long_hits_fits_in_embed_and_shows_all_results(bot_module):
    hits = [_long_hit(i) for i in range(5)]

    text = bot_module.format_hits_page(hits, 1)

    assert len(text) <= bot_module.EMBED_DESCRIPTION_LIMIT
    for i in range(5):
        assert f"**{i + 1}. Societe{i}**" in text
    assert text.count("TROUVE") == 5, "chaque extrait doit garder sa correspondance"
    assert text.count("```") == 10, "chaque bloc de code doit etre ferme"


def test_paginator_embed_respects_discord_limit(bot_module):
    hits = [_long_hit(i) for i in range(5)]
    paginator = bot_module.SearchPaginator(query='test', hits=hits, total=5, user_id=1)

    embed = paginator.build_embed()

    assert len(embed.description) <= bot_module.EMBED_DESCRIPTION_LIMIT
    assert "**5. Societe4**" in embed.description


def test_excerpt_centers_on_first_highlight(bot_module):
    text = "a" * 500 + "CIBLE" + "b" * 500

    excerpt = bot_module._excerpt(text, [[500, 505]], 101)

    assert len(excerpt) <= 101
    assert "CIBLE" in excerpt
    assert excerpt.startswith("…") and excerpt.endswith("…")


def test_excerpt_without_highlight_keeps_the_start(bot_module):
    excerpt = bot_module._excerpt("debut " + "z" * 200, [], 50)

    assert excerpt.startswith("debut")
    assert len(excerpt) <= 50


def test_format_hit_neutralizes_code_fences(bot_module):
    hit = {
        'source': r'C:\data\ACME\row.jsonl',
        'type': 'record',
        'score': 1.0,
        'snippets': [{'text': 'avant ``` apres', 'highlights': []}],
    }

    rendered = bot_module.format_hit(hit, 1)

    assert rendered.count("```") == 2


def _timeout_session(calls):
    class FakeCtxTimeout:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *a):
            return False

    class FakeSess:
        closed = False

        def get(self, url, **kw):
            calls.append(url)
            return FakeCtxTimeout()

    return FakeSess()


def test_search_timeout_is_not_retried(bot_module, monkeypatch):
    """Apres un timeout, la recherche tourne encore cote backend : pas de relance."""
    calls = []
    monkeypatch.setattr(bot_module, '_http_session', _timeout_session(calls))

    async def fake_sleep(_):
        pass
    monkeypatch.setattr(bot_module.asyncio, 'sleep', fake_sleep)

    result = asyncio.run(bot_module.api_get('/api/search', retries=1, retry_on_timeout=False))

    assert result["_error_type"] == "timeout"
    assert len(calls) == 1


def test_cmd_search_disables_timeout_retry_and_asks_short_context(bot_module, monkeypatch):
    seen = {}

    async def spy_api_get(endpoint, params=None, **kw):
        seen['params'] = params
        seen['kw'] = kw
        return {"total": 0, "hits": [], "query": "test"}

    monkeypatch.setattr(bot_module, 'api_get', spy_api_get)
    asyncio.run(bot_module.cmd_search.callback(FakeInteraction(), query="test", limit=20, index=None))

    assert seen['kw'].get('retry_on_timeout') is False
    assert seen['params']['context'] == bot_module.SEARCH_CONTEXT_CHARS
    assert seen['params']['context'] < 2500
