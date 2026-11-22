"""L'Oeil de Dieu — API FastAPI (point d'entree)."""

import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from deps import _ensure_index_loader_running, get_indexes_snapshot, limiter, load_indexes_config, refresh_ready_indexes
from models import HealthResponse
from settings import ALLOWED_HOSTS, ALLOWED_ORIGINS, ALLOW_LOCALHOST_NO_AUTH, API_KEY

logger = logging.getLogger("oeil-api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class StripQueryStringFilter(logging.Filter):
    """Retire la query string des logs d'acces uvicorn.

    Les recherches portent sur des emails, noms et telephones: elles ne doivent
    pas finir en clair dans uvicorn.log. Le chemin et le statut restent visibles.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = args[:2] + (args[2].split("?", 1)[0],) + args[3:]
        return True


logging.getLogger("uvicorn.access").addFilter(StripQueryStringFilter())


def startup():
    started_at = time.time()
    if not API_KEY and not ALLOW_LOCALHOST_NO_AUTH:
        logger.critical("API_KEY non configuree et bypass localhost desactive.")
        sys.exit(1)
    if not API_KEY:
        logger.warning("API_KEY absente, acces localhost uniquement.")
    load_indexes_config()
    logger.info("Startup backend termine en %.2fs", time.time() - started_at)


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup()
    yield


app = FastAPI(title="L'Oeil de Dieu API", version="2.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
# Ajoute en dernier, donc execute en premier: un Host inattendu (DNS rebinding)
# est rejete avant toute authentification ou tout traitement.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Trop de requetes. Reessayez plus tard."})


# ---- Enregistrement des routes ----
from routes.indexes import router as indexes_router  # noqa: E402
from routes.search import router as search_router  # noqa: E402
from routes.stats import router as stats_router  # noqa: E402
from routes.breaches import router as breaches_router  # noqa: E402
from routes.diagnostics import router as diagnostics_router  # noqa: E402

app.include_router(indexes_router)
app.include_router(search_router)
app.include_router(stats_router)
app.include_router(breaches_router)
app.include_router(diagnostics_router)


def _health_snapshot() -> HealthResponse:
    refresh_ready_indexes()
    _ensure_index_loader_running()
    snapshot = get_indexes_snapshot()
    ready = [k for k, v in snapshot.items() if v.get("status", "ready") == "ready"]
    loading = [k for k, v in snapshot.items() if v.get("status") == "loading"]
    errored = [k for k, v in snapshot.items() if v.get("status") == "error"]
    return HealthResponse(
        status="ok" if ready else ("error" if errored and not loading else "starting"),
        ready=bool(ready),
        indexes_loaded=len(ready),
        indexes=list(snapshot.keys()),
        indexes_ready=ready,
        indexes_loading=loading,
        indexes_error=errored,
    )


@app.get("/api/health", response_model=HealthResponse)
def health():
    """Etat detaille historique, conserve pour le frontend et le superviseur."""
    return _health_snapshot()


@app.get("/api/health/live")
def health_live():
    """Liveness: le processus HTTP est capable de repondre."""
    return {"status": "ok"}


@app.get("/api/health/ready", response_model=HealthResponse)
def health_ready():
    """Readiness: au moins un index est effectivement utilisable."""
    payload = _health_snapshot()
    if not payload.ready:
        return JSONResponse(status_code=503, content=payload.model_dump())
    return payload
