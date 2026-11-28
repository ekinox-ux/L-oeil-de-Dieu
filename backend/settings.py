import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
load_dotenv(BASE_DIR / ".env")


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_positive_int(name: str, default: int) -> int:
    """Entier strictement positif; 0, negatif ou invalide => valeur par defaut.

    Les anciennes configurations utilisaient 0 pour "illimite". Le ramener a 1
    tronquerait silencieusement les classements: on retombe sur le defaut.
    """
    value = _get_int(name, default)
    return value if value > 0 else default


def _get_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = (PROJECT_DIR / path).resolve()
    return path


API_KEY = os.getenv("API_KEY", "")
HIBP_API_KEY = os.getenv("HIBP_API_KEY", "")
BREACHDIRECTORY_RAPIDAPI_KEY = os.getenv("BREACHDIRECTORY_RAPIDAPI_KEY", "")
BREACHDIRECTORY_RAPIDAPI_HOST = os.getenv("BREACHDIRECTORY_RAPIDAPI_HOST", "breachdirectory.p.rapidapi.com")
BREACH_LOOKUP_TIMEOUT = _get_int("OEIL_BREACH_LOOKUP_TIMEOUT", 10)
ALLOWED_ORIGINS = [item.strip() for item in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",") if item.strip()]
# En-tetes Host acceptes: protege l'acces local contre le DNS rebinding.
ALLOWED_HOSTS = [item.strip() for item in os.getenv("OEIL_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if item.strip()]
INDEXES_CONFIG = _get_path("OEIL_INDEXES_CONFIG", BASE_DIR / "indexes.local.json")
ALLOW_LOCALHOST_NO_AUTH = _get_bool("OEIL_ALLOW_LOCALHOST_NO_AUTH", True)
CACHE_DIR = _get_path("OEIL_CACHE_DIR", BASE_DIR)
ALLOWED_ROOTS = [_get_path("__roots__", Path(item)) for item in os.getenv("OEIL_ALLOWED_ROOTS", "").split(",") if item.strip()]
# -- Limites API --
# Ces plafonds controlent le volume max retourne par l'API.
# Les clients (frontend, bot) appliquent leurs propres limites d'affichage
# (frontend: 20 resultats/page, bot: 5 resultats/page) qui sont toujours
# inferieures ou egales a ces valeurs backend.
MAX_QUERY_LENGTH = _get_int("OEIL_MAX_QUERY_LENGTH", 500)
MAX_SEARCH_OFFSET = max(0, _get_int("OEIL_MAX_SEARCH_OFFSET", 10_000))
MAX_TOP_EMAILS = _get_int("OEIL_MAX_TOP_EMAILS", 100)
MAX_TOP_PHONES = _get_int("OEIL_MAX_TOP_PHONES", 50)
MAX_TOP_DOMAINS = _get_int("OEIL_MAX_TOP_DOMAINS", 30)

# -- Cache des recherches --
# Classements (scores et adresses des documents, jamais le contenu) gardes en
# memoire par index : la page suivante ou precedente ne relance pas Tantivy.
# Vide des que l'index change ; 0 entree desactive le cache.
SEARCH_CACHE_ENTRIES = max(0, _get_int("OEIL_SEARCH_CACHE_ENTRIES", 32))
SEARCH_CACHE_TTL_SECONDS = max(0, _get_int("OEIL_SEARCH_CACHE_TTL_SECONDS", 300))
# Recherche sur tous les index : un index qui ne repond pas dans ce delai est
# ignore (resultats partiels signales), sous le timeout de 180 s des clients.
ALL_INDEXES_SEARCH_TIMEOUT_SECONDS = max(1, _get_int("OEIL_ALL_INDEXES_SEARCH_TIMEOUT_SECONDS", 150))
ALL_INDEXES_SEARCH_WORKERS = max(1, _get_int("OEIL_ALL_INDEXES_SEARCH_WORKERS", 8))

# -- Ouverture des index --
# Un lock Tantivy ne peut pas etre declare orphelin de facon fiable a partir
# de son age. Le supprimer pendant qu'un writer est vivant peut endommager
# l'index : le nettoyage automatique reste donc volontairement desactive.
INDEX_RETRY_DELAY_SECONDS = max(1, _get_int("OEIL_INDEX_RETRY_DELAY_SECONDS", 10))
INDEX_MAX_RETRY_DELAY_SECONDS = max(INDEX_RETRY_DELAY_SECONDS, _get_int("OEIL_INDEX_MAX_RETRY_DELAY_SECONDS", 60))
BACKGROUND_INDEX_AUTOLOAD = _get_bool("OEIL_BACKGROUND_INDEX_AUTOLOAD", True)
INDEX_PATH_CHECK_ATTEMPTS = max(1, _get_int("OEIL_INDEX_PATH_CHECK_ATTEMPTS", 3))
INDEX_PATH_CHECK_DELAY_MS = max(0, _get_int("OEIL_INDEX_PATH_CHECK_DELAY_MS", 500))
INDEX_PATH_RECHECK_SECONDS = max(1, _get_int("OEIL_INDEX_PATH_RECHECK_SECONDS", 15))
INDEX_PATH_FAILURE_THRESHOLD = max(1, _get_int("OEIL_INDEX_PATH_FAILURE_THRESHOLD", 6))

# -- Limites analyseur --
# Les ensembles d'identifiants uniques sont conserves sur disque dans SQLite.
# Ces plafonds ne concernent que les compteurs necessaires aux classements.
# Une valeur absente, nulle ou negative applique le defaut (jamais "illimite":
# charger des centaines de millions de compteurs en RAM n'est pas viable).
ANALYZER_MAX_EMAIL_ENTRIES = _get_positive_int("OEIL_ANALYZER_MAX_EMAIL_ENTRIES", 100_000)
ANALYZER_MAX_PHONE_ENTRIES = _get_positive_int("OEIL_ANALYZER_MAX_PHONE_ENTRIES", 50_000)
ANALYZER_MAX_DOMAIN_ENTRIES = _get_positive_int("OEIL_ANALYZER_MAX_DOMAIN_ENTRIES", 100_000)
ANALYZER_SAVE_EVERY = max(1, _get_int("OEIL_ANALYZER_SAVE_EVERY", 100))
ANALYZER_DB_BATCH_SIZE = max(100, _get_int("OEIL_ANALYZER_DB_BATCH_SIZE", 10_000))
ANALYZER_MAX_LINE_BYTES = max(1_024, _get_int("OEIL_ANALYZER_MAX_LINE_BYTES", 8 * 1024 * 1024))


def resolve_runtime_path(path_str: str) -> Path:
    expanded = os.path.expandvars(path_str)
    path = Path(expanded).expanduser()
    try:
        return path.resolve()
    except OSError:
        # Certains volumes montes Windows (reseau, virtuels) peuvent echouer
        # sur resolve()/realpath tout en restant exploitables via leur chemin brut.
        return path if path.is_absolute() else (PROJECT_DIR / path)
