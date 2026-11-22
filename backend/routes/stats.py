"""Routes de statistiques et analyse."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from analyzer import get_status, load_cache, run_analysis
from deps import _ensure_index_loader_running, get_engine, get_index_id_for_config, get_indexes_snapshot, limiter, mark_index_error, refresh_ready_indexes, validate_path_safe, verify_api_key
from engine import IndexAccessError, SearchEngine
from models import AnalyzeResponse, AnalyzeStatus, StatsResponse
from settings import MAX_TOP_DOMAINS, MAX_TOP_EMAILS, MAX_TOP_PHONES

logger = logging.getLogger("oeil-api")
router = APIRouter(prefix="/api")


def _truncate_analysis(analysis: dict, top_emails_limit: int, top_domains_limit: int,
                       top_phones_limit: int, top_sources_limit: int) -> None:
    """Tronque les tops d'une analyse aux limites demandees (in-place)."""
    if "emails" in analysis:
        analysis["emails"]["top_emails"] = analysis["emails"].get("top_emails", [])[:top_emails_limit]
        analysis["emails"]["top_domains"] = analysis["emails"].get("top_domains", [])[:top_domains_limit]
    if "phones" in analysis:
        analysis["phones"]["top_phones"] = analysis["phones"].get("top_phones", [])[:top_phones_limit]
    if "sources" in analysis:
        analysis["sources"] = analysis["sources"][:top_sources_limit]


def _merge_top_by_key(lists: list[list[dict]], key: str, value_key: str = "count") -> list[dict]:
    """Fusionne plusieurs listes de tops par cle, additionne les valeurs, retrie desc."""
    merged: dict[str, int] = {}
    for items in lists:
        for item in items:
            k = item.get(key, "")
            if k:
                merged[k] = merged.get(k, 0) + item.get(value_key, 0)
    result = [{key: k, value_key: v} for k, v in merged.items()]
    result.sort(key=lambda x: x[value_key], reverse=True)
    return result


def _aggregate_all(top_emails_limit: int, top_domains_limit: int,
                   top_phones_limit: int, top_sources_limit: int) -> dict:
    """Agregation logique de tous les index charges."""
    total_docs = 0
    num_segments = 0
    total_size_bytes = 0
    index_size_pending = False
    all_segments = []
    skipped = []

    # Analyses collectees
    all_analyses = []

    for idx_id, cfg in get_indexes_snapshot().items():
        status = cfg.get("status", "ready")
        if status != "ready":
            skipped.append({"id": idx_id, "name": cfg["name"], "status": status})
            continue

        engine = cfg["engine"]
        try:
            engine_stats = engine.stats()
        except Exception:
            logger.exception("Erreur stats index %s", idx_id)
            skipped.append({"id": idx_id, "name": cfg["name"], "status": "stats_error"})
            continue

        total_docs += engine_stats.get("total_docs", 0)
        num_segments += engine_stats.get("num_segments", 0)
        total_size_bytes += engine_stats.get("index_size_bytes", 0)
        index_size_pending = index_size_pending or engine_stats.get("index_size_pending", False)

        # Segments avec index_id et prefixe
        for seg in engine_stats.get("segments", []):
            all_segments.append({
                "id": f"{idx_id}:{seg['id']}",
                "docs": seg["docs"],
                "index_id": idx_id,
            })

        # Analyse cache
        source_dir = cfg.get("source", "")
        if source_dir:
            analysis = load_cache(source_dir)
            if analysis:
                all_analyses.append(analysis)

    data = {
        "total_docs": total_docs,
        "num_segments": num_segments,
        "index_size_bytes": total_size_bytes,
        "index_size": "Calcul..." if index_size_pending else SearchEngine._format_size(total_size_bytes),
        "index_size_pending": index_size_pending,
        "segments": all_segments[:50],
        "index_id": "all",
        "index_name": "Tous les index",
        "skipped_indexes": skipped,
    }

    if all_analyses:
        # scan_date: la plus recente parmi les analyses fusionnees
        scan_date = max(a.get("scan_date", "") for a in all_analyses)

        merged_analysis = {
            "scan_date": scan_date,
            "scan_duration_seconds": round(sum(a.get("scan_duration_seconds", 0) for a in all_analyses), 1),
            "source_dir": "",
            "total_files": sum(a.get("total_files", 0) for a in all_analyses),
            "files_processed": sum(a.get("files_processed", 0) for a in all_analyses),
            "total_lines": sum(a.get("total_lines", 0) for a in all_analyses),
            "complete": all(a.get("complete", False) for a in all_analyses),
            "limits": {
                "email_counter_limit": max((a.get("limits", {}).get("email_counter_limit", 0) for a in all_analyses), default=0),
                "phone_counter_limit": max((a.get("limits", {}).get("phone_counter_limit", 0) for a in all_analyses), default=0),
            },
            "emails": {
                # unique_count: somme — APPROXIMATIF (doublons inter-index non dedupliques)
                "unique_count": sum(a.get("emails", {}).get("unique_count", 0) for a in all_analyses),
                "total_occurrences": sum(a.get("emails", {}).get("total_occurrences", 0) for a in all_analyses),
                "perso_count": sum(a.get("emails", {}).get("perso_count", 0) for a in all_analyses),
                "pro_count": sum(a.get("emails", {}).get("pro_count", 0) for a in all_analyses),
                "top_emails": _merge_top_by_key(
                    [a.get("emails", {}).get("top_emails", []) for a in all_analyses], "email"),
                "top_domains": _merge_top_by_key(
                    [a.get("emails", {}).get("top_domains", []) for a in all_analyses], "domain"),
                "tld_distribution": _merge_top_by_key(
                    [a.get("emails", {}).get("tld_distribution", []) for a in all_analyses], "tld"),
            },
            "phones": {
                # unique_count: somme — APPROXIMATIF (doublons inter-index non dedupliques)
                "unique_count": sum(a.get("phones", {}).get("unique_count", 0) for a in all_analyses),
                "total_occurrences": sum(a.get("phones", {}).get("total_occurrences", 0) for a in all_analyses),
                "top_phones": _merge_top_by_key(
                    [a.get("phones", {}).get("top_phones", []) for a in all_analyses], "phone"),
            },
            "sources": _merge_top_by_key(
                [a.get("sources", []) for a in all_analyses], "name", "files"),
        }

        _truncate_analysis(merged_analysis, top_emails_limit, top_domains_limit,
                           top_phones_limit, top_sources_limit)
        data["analysis"] = merged_analysis

    return data


@router.get("/stats", response_model=StatsResponse)
@limiter.limit("30/minute")
def stats(
    request: Request,
    index: str = Query(None, description="ID de l'index ('all' pour vue globale)"),
    top_emails_limit: int = Query(20, ge=1, le=MAX_TOP_EMAILS, description="Nombre max d'emails dans top_emails"),
    top_domains_limit: int = Query(10, ge=1, le=MAX_TOP_DOMAINS, description="Nombre max de domaines"),
    top_phones_limit: int = Query(20, ge=1, le=MAX_TOP_PHONES, description="Nombre max de telephones dans top_phones"),
    top_sources_limit: int = Query(10, ge=1, le=50, description="Nombre max de sources dans l'analyse"),
    _=Depends(verify_api_key),
):
    t0 = time.time()

    if index == "all":
        refresh_ready_indexes()
        snapshot = get_indexes_snapshot()
        ready_count = sum(1 for cfg in snapshot.values() if cfg.get("status", "ready") == "ready")
        if not snapshot:
            raise HTTPException(503, "Aucun index charge.")
        if ready_count == 0:
            _ensure_index_loader_running()
            raise HTTPException(503, "Aucun index pret. Chargement en cours...")
        result = _aggregate_all(top_emails_limit, top_domains_limit, top_phones_limit, top_sources_limit)
        logger.info("GET /api/stats?index=all servi en %.0fms", (time.time() - t0) * 1000)
        return result

    engine, cfg = get_engine(index)
    resolved_id = index or get_index_id_for_config(cfg)

    try:
        data = engine.stats()
    except IndexAccessError as exc:
        if resolved_id:
            mark_index_error(resolved_id, exc)
        logger.exception("Erreur stats index indisponible")
        raise HTTPException(503, "Index temporairement indisponible.")
    except Exception:
        logger.exception("Erreur stats")
        raise HTTPException(500, "Erreur lors de la recuperation des statistiques.")

    source_dir = cfg.get("source", "")
    if source_dir:
        analysis = load_cache(source_dir)
        if analysis:
            _truncate_analysis(analysis, top_emails_limit, top_domains_limit,
                               top_phones_limit, top_sources_limit)
            data["analysis"] = analysis

    # Utiliser l'id reel retourne par get_engine (pas forcement le premier du dict)
    data["index_id"] = resolved_id or ""
    data["index_name"] = cfg.get("name", "")
    logger.info("GET /api/stats?index=%s servi en %.0fms", data["index_id"], (time.time() - t0) * 1000)
    return data


@router.post("/analyze", response_model=AnalyzeResponse)
@limiter.limit("2/hour")
def analyze(
    request: Request,
    index: str = Query(None, description="ID de l'index"),
    source: str = Query(None, description="Chemin des fichiers sources (override)"),
    _=Depends(verify_api_key),
):
    _, cfg = get_engine(index)
    source_dir = source or cfg.get("source", "")

    if not source_dir:
        raise HTTPException(400, "Aucun dossier source configure.")

    # Le chemin resolu sert uniquement au controle de securite. La cle des
    # artefacts reste le chemin tel que configure: c'est celle que lisent
    # /api/stats, les diagnostics et doctor.py. Un lecteur reseau resolu en
    # UNC produirait sinon un cache que personne ne relit.
    validate_path_safe(source_dir)
    started = run_analysis(source_dir)
    if not started:
        logger.info("Analyse deja en cours pour %s", source_dir)
        return AnalyzeResponse(status="already_running")
    logger.info("Analyse lancee pour %s", source_dir)
    return AnalyzeResponse(status="started")


@router.get("/analyze/status", response_model=AnalyzeStatus)
def analyze_status(request: Request, _=Depends(verify_api_key)):
    return get_status()
