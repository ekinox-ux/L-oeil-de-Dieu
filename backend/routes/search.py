"""Route de recherche."""

import logging
import math
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from deps import get_engine, get_index_id_for_config, limiter, mark_index_error, verify_api_key
from engine import IndexAccessError, SearchQueryError
from models import SearchHit, SearchResponse, Snippet
from settings import MAX_QUERY_LENGTH, MAX_SEARCH_OFFSET
from snippets import extract_snippets

logger = logging.getLogger("oeil-api")
router = APIRouter(prefix="/api")
_SIGNATURE_TOKEN_RE = re.compile(r"[\w@.+-]+", re.UNICODE)


def _token_signature(raw_content: str) -> tuple[str, ...]:
    return tuple(sorted(_SIGNATURE_TOKEN_RE.findall(raw_content.lower())))


def _is_near_duplicate(existing_tokens: tuple[str, ...], candidate_tokens: tuple[str, ...]) -> bool:
    if existing_tokens == candidate_tokens:
        return True
    existing_set = set(existing_tokens)
    candidate_set = set(candidate_tokens)
    if not existing_set or not candidate_set:
        return False
    overlap = len(existing_set & candidate_set)
    union = len(existing_set | candidate_set)
    if union == 0:
        return False
    return (overlap / union) >= 0.85


def _deduplicate_hits(hits: list[dict]) -> list[dict]:
    signatures_by_source: dict[str, list[tuple[str, ...]]] = {}
    deduped = []
    for hit in hits:
        raw = hit.get("content_raw", "") or ""
        source = hit.get("source") or ""
        signature = _token_signature(raw)
        existing_signatures = signatures_by_source.setdefault(source, [])
        if any(_is_near_duplicate(existing, signature) for existing in existing_signatures):
            continue
        existing_signatures.append(signature)
        deduped.append(hit)
    return deduped


@router.get("/search", response_model=SearchResponse)
@limiter.limit("10/minute")
def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH, description="Requete de recherche"),
    index: str = Query(None, description="ID de l'index a interroger"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=MAX_SEARCH_OFFSET),
    context: int = Query(2500, ge=50, le=2500, description="Caracteres de contexte"),
    deduplicate: bool = Query(
        False,
        description="Supprime les quasi-doublons de la page; total reste le total brut",
    ),
    _=Depends(verify_api_key),
):
    engine, cfg = get_engine(index)
    resolved_id = index or get_index_id_for_config(cfg)

    try:
        # La deduplication reste limitee a la page courante. Cela preserve une
        # pagination stable : une page ne consomme plus silencieusement des hits
        # appartenant aux pages suivantes.
        results = engine.search(q, limit=limit, offset=offset)
    # Le contenu de la requete (email, nom, telephone) n'est jamais journalise:
    # seule sa longueur l'est, pour le diagnostic.
    except SearchQueryError:
        # Le message du parseur cite des fragments de la requete: non journalise.
        logger.warning("Requete invalide (%s caracteres)", len(q))
        raise HTTPException(400, "Requete de recherche invalide.")
    except IndexAccessError as exc:
        if resolved_id:
            mark_index_error(resolved_id, exc)
        logger.exception("Erreur d'acces index (requete de %s caracteres)", len(q))
        raise HTTPException(503, "Index temporairement indisponible.")
    except Exception:
        logger.exception("Erreur de recherche inattendue (requete de %s caracteres)", len(q))
        raise HTTPException(500, "Erreur interne lors de la recherche.")

    total = results["total"]
    page_hits = _deduplicate_hits(results["hits"]) if deduplicate else results["hits"]
    hits = []
    for hit in page_hits[:limit]:
        raw = hit.get("content_raw") or ""
        snippets_raw = extract_snippets(raw, q, context_chars=context)
        hits.append(SearchHit(
            score=hit["score"],
            source=hit.get("source"),
            filename=hit.get("filename"),
            type=hit.get("type"),
            snippets=[Snippet(**s) for s in snippets_raw],
        ))

    return SearchResponse(
        query=results["query"],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + limit < total and offset + limit <= MAX_SEARCH_OFFSET,
        total_pages=max(1, min(math.ceil(total / limit), (MAX_SEARCH_OFFSET // limit) + 1)),
        deduplicated=deduplicate,
        total_is_raw=deduplicate,
        index_id=resolved_id or "",
        index_name=cfg.get("name", ""),
        hits=hits,
    )
