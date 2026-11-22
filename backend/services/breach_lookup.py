"""Service de consultation de bases de fuites — providers et agregation."""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from settings import (
    BREACH_LOOKUP_TIMEOUT,
    BREACHDIRECTORY_RAPIDAPI_HOST,
    BREACHDIRECTORY_RAPIDAPI_KEY,
    HIBP_API_KEY,
)

logger = logging.getLogger("oeil-breach")


# ---- Sous-structures enrichies ----

@dataclass
class HibpDetails:
    """Champs specifiques HIBP — tous optionnels."""

    title: str = ""
    added_date: str = ""
    modified_date: str = ""
    pwn_count: int = 0
    is_verified: bool = False
    is_fabricated: bool = False
    is_sensitive: bool = False
    is_retired: bool = False
    is_spam_list: bool = False
    is_malware: bool = False
    is_subscription_free: bool = False
    is_stealer_log: bool = False
    attribution: str = ""
    disclosure_url: str = ""


@dataclass
class BdEntry:
    """Une entree individuelle BreachDirectory (credential expose)."""

    source: str = ""
    password: str = ""
    hash_password: bool = False
    sha1: str = ""
    hash: str = ""
    has_password: bool = False


# ---- Structure commune ----

@dataclass
class BreachResult:
    """Resultat normalise d'une fuite."""

    name: str
    domain: str = ""
    breach_date: str = ""
    description: str = ""
    data_classes: list[str] = field(default_factory=list)
    source_provider: str = ""
    logo_url: str = ""
    password_exposed: bool = False
    # Sous-objets enrichis (un seul rempli selon le provider)
    hibp_details: HibpDetails | None = None
    bd_entries: list[BdEntry] = field(default_factory=list)


@dataclass
class ProviderResponse:
    """Reponse d'un provider unique."""

    name: str
    ok: bool
    results: list[BreachResult] = field(default_factory=list)
    error: str = ""


def _http_get_json(url: str, headers: dict, timeout: int = BREACH_LOOKUP_TIMEOUT) -> dict | list:
    """GET synchrone avec urllib — retourne le JSON parse."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---- HIBP ----

def _lookup_hibp(email: str) -> ProviderResponse:
    """Interroge Have I Been Pwned — extrait tous les champs disponibles."""
    if not HIBP_API_KEY:
        return ProviderResponse(name="hibp", ok=False, error="Cle API HIBP non configuree")

    url = f"https://haveibeenpwned.com/api/v3/breachedaccount/{urllib.request.quote(email, safe='@')}?truncateResponse=false"
    headers = {"hibp-api-key": HIBP_API_KEY, "user-agent": "L-Oeil-de-Dieu"}

    try:
        data = _http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        e.close()  # libere la socket portee par la reponse d'erreur
        if e.code == 404:
            return ProviderResponse(name="hibp", ok=True, results=[])
        if e.code == 429:
            return ProviderResponse(name="hibp", ok=False, error="Rate limit HIBP atteint")
        return ProviderResponse(name="hibp", ok=False, error=f"HTTP {e.code}")
    except Exception as e:
        logger.exception("Erreur HIBP (email non journalise)")
        return ProviderResponse(name="hibp", ok=False, error=str(e))

    results = []
    for b in data if isinstance(data, list) else []:
        data_classes = b.get("DataClasses", [])
        results.append(BreachResult(
            name=b.get("Name", ""),
            domain=b.get("Domain", ""),
            breach_date=b.get("BreachDate", ""),
            description=b.get("Description", ""),
            data_classes=data_classes,
            source_provider="hibp",
            logo_url=b.get("LogoPath", ""),
            password_exposed="Passwords" in data_classes,
            hibp_details=HibpDetails(
                title=b.get("Title", ""),
                added_date=b.get("AddedDate", ""),
                modified_date=b.get("ModifiedDate", ""),
                pwn_count=b.get("PwnCount", 0) or 0,
                is_verified=b.get("IsVerified", False),
                is_fabricated=b.get("IsFabricated", False),
                is_sensitive=b.get("IsSensitive", False),
                is_retired=b.get("IsRetired", False),
                is_spam_list=b.get("IsSpamList", False),
                is_malware=b.get("IsMalware", False),
                is_subscription_free=b.get("IsSubscriptionFree", False),
                is_stealer_log=b.get("IsStealerLog", False),
                attribution=b.get("Attribution") or "",
                disclosure_url=b.get("DisclosureUrl") or "",
            ),
        ))
    return ProviderResponse(name="hibp", ok=True, results=results)


# ---- BreachDirectory (RapidAPI) ----

def _lookup_breachdirectory(email: str) -> ProviderResponse:
    """Interroge BreachDirectory — expose les credentials individuels."""
    if not BREACHDIRECTORY_RAPIDAPI_KEY:
        return ProviderResponse(name="breachdirectory", ok=False, error="Cle RapidAPI non configuree")

    url = f"https://{BREACHDIRECTORY_RAPIDAPI_HOST}/?func=auto&term={urllib.request.quote(email, safe='@')}"
    headers = {
        "x-rapidapi-host": BREACHDIRECTORY_RAPIDAPI_HOST,
        "x-rapidapi-key": BREACHDIRECTORY_RAPIDAPI_KEY,
        "Content-Type": "application/json",
    }

    try:
        data = _http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        e.close()  # libere la socket portee par la reponse d'erreur
        if e.code == 429:
            return ProviderResponse(name="breachdirectory", ok=False, error="Rate limit RapidAPI atteint")
        return ProviderResponse(name="breachdirectory", ok=False, error=f"HTTP {e.code}")
    except Exception as e:
        logger.exception("Erreur BreachDirectory (email non journalise)")
        return ProviderResponse(name="breachdirectory", ok=False, error=str(e))

    # BreachDirectory renvoie {result: [...]} — chaque entree est un credential
    # expose avec sources (string ou list), password, sha1, hash, etc.
    # On groupe par source: un BreachResult par source, avec toutes les
    # entrees individuelles dans bd_entries.
    raw_results = data.get("result", []) if isinstance(data, dict) else []
    if not isinstance(raw_results, list):
        raw_results = []

    # Collecter les entrees par source
    source_entries: dict[str, list[BdEntry]] = {}
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        sources_raw = entry.get("sources", "")
        if isinstance(sources_raw, list):
            source_names = [s for s in sources_raw if isinstance(s, str) and s]
        elif isinstance(sources_raw, str) and sources_raw:
            source_names = [sources_raw]
        else:
            continue

        bd_entry = BdEntry(
            source=source_names[0] if source_names else "",
            password=entry.get("password", ""),
            hash_password=bool(entry.get("hash_password", False)),
            sha1=entry.get("sha1", ""),
            hash=entry.get("hash", ""),
            has_password=bool(entry.get("has_password", entry.get("hash_password", False))),
        )

        for src_name in source_names:
            bd_entry_for_src = BdEntry(
                source=src_name,
                password=bd_entry.password,
                hash_password=bd_entry.hash_password,
                sha1=bd_entry.sha1,
                hash=bd_entry.hash,
                has_password=bd_entry.has_password,
            )
            source_entries.setdefault(src_name, []).append(bd_entry_for_src)

    results = []
    for src_name, entries in source_entries.items():
        pwd_exposed = any(e.hash_password or bool(e.password) for e in entries)
        data_classes = ["Passwords"] if pwd_exposed else []
        results.append(BreachResult(
            name=src_name,
            source_provider="breachdirectory",
            password_exposed=pwd_exposed,
            data_classes=data_classes,
            bd_entries=entries,
        ))

    return ProviderResponse(name="breachdirectory", ok=True, results=results)


# ---- Agregation ----

_PROVIDERS = [
    ("hibp", _lookup_hibp),
    ("breachdirectory", _lookup_breachdirectory),
]


async def lookup_all(email: str) -> list[ProviderResponse]:
    """Interroge tous les providers en parallele. Ne leve jamais d'exception."""
    tasks = [asyncio.to_thread(fn, email) for _name, fn in _PROVIDERS]
    return list(await asyncio.gather(*tasks))
