"""Route de diagnostic local pour l'exploitation."""

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request

from analyzer import CACHE_DIR, _get_lock_path, cleanup_stale_analyzer_locks
from deps import get_indexes_snapshot, verify_api_key, request_index_retry

router = APIRouter(prefix="/api")


def _lockfile_snapshot(index_path: str) -> list[dict]:
    path = Path(index_path)
    lockfiles = []
    for name in (".tantivy-meta.lock", ".tantivy-writer.lock"):
        lock_path = path / name
        if not lock_path.exists():
            continue
        try:
            stat = lock_path.stat()
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            size = 0
            mtime = 0.0
        lockfiles.append({
            "name": name,
            "path": str(lock_path),
            "size": size,
            "mtime": mtime,
        })
    return lockfiles


@router.get("/diagnostics")
def diagnostics(request: Request, _=Depends(verify_api_key)):
    payload = {
        "cache_dir": str(CACHE_DIR),
        "indexes": [],
    }

    for idx_id, cfg in get_indexes_snapshot().items():
        index_path = Path(cfg.get("path", ""))
        source_raw = cfg.get("source", "")
        source_path = Path(source_raw) if source_raw else None
        payload["indexes"].append({
            "id": idx_id,
            "name": cfg.get("name", idx_id),
            "status": cfg.get("status", "ready"),
            "error": cfg.get("error", ""),
            "retry_count": int(cfg.get("retry_count", 0) or 0),
            "retry_delay_seconds": int(cfg.get("retry_delay_seconds", 0) or 0),
            "next_retry_at": float(cfg.get("next_retry_at", 0.0) or 0.0),
            "last_error_at": float(cfg.get("last_error_at", 0.0) or 0.0),
            "last_ready_at": float(cfg.get("last_ready_at", 0.0) or 0.0),
            "path": str(index_path),
            "path_exists": index_path.is_dir(),
            "source": source_raw,
            "source_exists": source_path.is_dir() if source_path else False,
            "analyzer_lock": str(_get_lock_path(source_raw)) if source_raw else "",
            "analyzer_lock_exists": _get_lock_path(source_raw).exists() if source_raw else False,
            "lockfiles": _lockfile_snapshot(str(index_path)),
        })

    return payload


@router.post("/diagnostics/analyzer-locks/cleanup")
def cleanup_analyzer_locks(
    request: Request,
    source: str = Query(None, description="Source precise a nettoyer"),
    _=Depends(verify_api_key),
):
    result = cleanup_stale_analyzer_locks(source)
    return {
        "status": "ok",
        "removed_count": len(result["removed"]),
        "removed": result["removed"],
        "kept_count": len(result["kept"]),
        "kept": result["kept"],
    }


@router.post("/diagnostics/indexes/retry")
def retry_index(
    request: Request,
    id: str = Query(..., description="Index a relancer"),
    _=Depends(verify_api_key),
):
    cfg = request_index_retry(id)
    return {
        "status": "ok",
        "id": id,
        "index_status": cfg.get("status", "loading"),
    }
