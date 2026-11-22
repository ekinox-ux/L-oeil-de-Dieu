"""Routes de gestion des index."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from deps import (
    _ensure_index_loader_running,
    _indexes_lock,
    get_indexes_snapshot,
    indexes,
    refresh_ready_indexes,
    save_indexes_config,
    validate_id,
    validate_path_safe,
    verify_api_key,
)
from engine import SearchEngine
from models import AddIndexResponse, IndexInfo

logger = logging.getLogger("oeil-api")
router = APIRouter(prefix="/api")


@router.get("/indexes", response_model=list[IndexInfo])
def list_indexes(request: Request, _=Depends(verify_api_key)):
    refresh_ready_indexes()
    _ensure_index_loader_running()
    snapshot = get_indexes_snapshot()
    return [
        IndexInfo(
            id=idx_id,
            name=cfg["name"],
            path=cfg["path"],
            source=cfg.get("source", ""),
            status=cfg.get("status", "ready"),
            error=cfg.get("error", ""),
            last_error_at=float(cfg.get("last_error_at", 0.0) or 0.0),
            retry_count=int(cfg.get("retry_count", 0) or 0),
            next_retry_at=float(cfg.get("next_retry_at", 0.0) or 0.0),
            retry_delay_seconds=int(cfg.get("retry_delay_seconds", 0) or 0),
            last_ready_at=float(cfg.get("last_ready_at", 0.0) or 0.0),
        )
        for idx_id, cfg in snapshot.items()
    ]


@router.post("/indexes/add", response_model=AddIndexResponse)
def add_index(
    request: Request,
    id: str = Query(...),
    name: str = Query(...),
    path: str = Query(...),
    source: str = Query(""),
    _=Depends(verify_api_key),
):
    validate_id(id)

    with _indexes_lock:
        if id in indexes:
            raise HTTPException(409, "L'index existe deja.")

    resolved_path = validate_path_safe(path)
    resolved_source = str(validate_path_safe(source)) if source else ""

    try:
        engine = SearchEngine(str(resolved_path))
    except FileNotFoundError:
        raise HTTPException(404, "Index introuvable au chemin specifie.")
    except Exception:
        logger.exception("Erreur ajout index [%s]", id)
        raise HTTPException(500, "Erreur interne lors de l'ajout de l'index.")

    with _indexes_lock:
        # Un autre appel a pu enregistrer le meme identifiant pendant
        # l'ouverture de l'index, qui peut etre lente sur un disque externe.
        if id in indexes:
            raise HTTPException(409, "L'index existe deja.")
        indexes[id] = {
            "engine": engine,
            "name": name,
            "path": str(resolved_path),
            "source": resolved_source,
            "status": "ready",
            "error": "",
            "last_error_at": 0.0,
            "retry_count": 0,
            "next_retry_at": 0.0,
            "retry_delay_seconds": 0,
            "last_ready_at": time.time(),
            "autoload": True,
        }
        save_indexes_config()

    return AddIndexResponse(id=id, name=name)
