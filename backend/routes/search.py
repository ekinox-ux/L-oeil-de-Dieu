"""Route de recherche."""

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from deps import (
    _ensure_index_loader_running,
    get_engine,
    get_index_id_for_config,
    get_indexes_snapshot,
    limiter,
    mark_index_error,
    refresh_ready_indexes,
    verify_api_key,
)
from engine import IndexAccessError, SearchQueryError
from models import SearchHit, SearchResponse, Snippet
from settings import (
    ALL_INDEXES_SEARCH_TIMEOUT_SECONDS,
    ALL_INDEXES_SEARCH_WORKERS,
    MAX_QUERY_LENGTH,
    MAX_SEARCH_OFFSET,
)
from snippets import extract_snippets

logger = logging.getLogger("oeil-api")
router = APIRouter(prefix="/api")
_SIGNATURE_TOKEN_RE = re.compile(r"[\w@.+-]+", re.UNICODE)
ALL_INDEXES = "all"
ALL_INDEXES_NAME = "Tous les index"
# Partage entre les requetes : un index bloque n'immobilise qu'un thread.
_all_indexes_executor = ThreadPoolExecutor(
    max_workers=ALL_INDEXES_SEARCH_WORKERS, thread_name_prefix="search-all",
)


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


@dataclass
class _Target:
    index_id: str
    name: str
    engine: object


@dataclass
class _Outcome:
    total: int | None
    has_more: bool
    hits: list[dict]
    relaxable: bool
    skipped: list[dict]


def _search_one(engine, q: str, limit: int, offset: int, match_all: bool, count: bool) -> _Outcome:
    results = engine.search(q, limit=limit, offset=offset, match_all=match_all, count=count)
    return _Outcome(
        total=results["total"],
        has_more=results.get("has_more", False),
        hits=results["hits"],
        relaxable=bool(results.get("relaxable")),
        skipped=[],
    )


def _ready_targets() -> list[_Target]:
    refresh_ready_indexes()
    snapshot = get_indexes_snapshot()
    if not snapshot:
        raise HTTPException(503, "Aucun index charge.")
    targets = [
        _Target(idx_id, cfg.get("name", idx_id), cfg["engine"])
        for idx_id, cfg in snapshot.items()
        if cfg.get("status", "ready") == "ready" and cfg.get("engine") is not None
    ]
    if not targets:
        _ensure_index_loader_running()
        raise HTTPException(503, "Aucun index pret.")
    return targets


def _search_all(
    targets: list[_Target], q: str, limit: int, offset: int, match_all: bool, count: bool,
) -> _Outcome:
    """Interroge chaque index en parallele puis fusionne par score.

    Chaque index classe ses offset + limit meilleurs documents (en cache pour
    les pages suivantes) ; seuls ceux de la page sont lus. Les scores BM25 ne
    sont pas strictement comparables d'un index a l'autre : l'ordre entre
    fuites est approximatif. Un index en erreur ou trop lent est ignore et
    signale (partial).
    """
    window = offset + limit
    futures = [
        (target, _all_indexes_executor.submit(
            target.engine.find, q, window, 0, match_all=match_all, count=count,
        ))
        for target in targets
    ]
    _done, pending = wait([future for _, future in futures], timeout=ALL_INDEXES_SEARCH_TIMEOUT_SECONDS)

    pages = []
    skipped = []
    for target, future in futures:
        if future in pending:
            future.cancel()
            logger.warning("Index [%s] ignore : pas de reponse en %ss", target.index_id,
                           ALL_INDEXES_SEARCH_TIMEOUT_SECONDS)
            skipped.append({"id": target.index_id, "name": target.name, "reason": "timeout"})
            continue
        try:
            pages.append((target, future.result()))
        except SearchQueryError:
            raise
        except IndexAccessError as exc:
            mark_index_error(target.index_id, exc)
            logger.exception("Index [%s] ignore : erreur d'acces", target.index_id)
            skipped.append({"id": target.index_id, "name": target.name, "reason": "error"})
        except Exception:
            logger.exception("Index [%s] ignore : erreur inattendue", target.index_id)
            skipped.append({"id": target.index_id, "name": target.name, "reason": "error"})
    if not pages:
        raise IndexAccessError("Aucun index n'a repondu.")

    ranked = []
    for order, (target, page) in enumerate(pages):
        for rank, (score, address) in enumerate(page.hits):
            ranked.append((score, order, rank, address))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    selected = ranked[offset:window]

    hits_by_slot: dict[tuple[int, int], dict] = {}
    for order, (target, page) in enumerate(pages):
        mine = [(score, address, rank) for score, o, rank, address in selected if o == order]
        if not mine:
            continue
        try:
            read = target.engine.read_hits(page.searcher, [(score, address) for score, address, _ in mine])
        except IndexAccessError as exc:
            mark_index_error(target.index_id, exc)
            logger.exception("Index [%s] ignore : documents illisibles", target.index_id)
            skipped.append({"id": target.index_id, "name": target.name, "reason": "error"})
            continue
        for (_score, _address, rank), hit in zip(mine, read):
            if hit is not None:
                hit["index_id"] = target.index_id
                hit["index_name"] = target.name
                hits_by_slot[(order, rank)] = hit

    hits = [hits_by_slot[(o, rank)] for _score, o, rank, _ in selected if (o, rank) in hits_by_slot]
    totals = [page.total for _, page in pages]
    return _Outcome(
        total=None if any(total is None for total in totals) else sum(totals),
        has_more=len(ranked) > window or any(page.has_more for _, page in pages),
        hits=hits,
        relaxable=any(page.relaxable for _, page in pages),
        skipped=skipped,
    )


@router.get("/search", response_model=SearchResponse)
@limiter.limit("10/minute")
def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH, description="Requete de recherche"),
    index: str = Query(None, description="ID de l'index a interroger ('all' pour tous)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=MAX_SEARCH_OFFSET),
    context: int = Query(2500, ge=50, le=2500, description="Caracteres de contexte"),
    deduplicate: bool = Query(
        False,
        description="Supprime les quasi-doublons de la page; total reste le total brut",
    ),
    match: str = Query(
        "auto",
        pattern="^(auto|all|any)$",
        description=(
            "all: chaque mot exige ; any: au moins un mot ; auto: all, puis any "
            "si rien n'est trouve (relaxed=true)"
        ),
    ),
    count: bool = Query(
        True,
        description="false: ne recompte pas le total (pages suivantes, total deja connu)",
    ),
    _=Depends(verify_api_key),
):
    if index == ALL_INDEXES:
        targets = _ready_targets()
        resolved_id = ALL_INDEXES
        index_name = ALL_INDEXES_NAME

        def run(match_all: bool) -> _Outcome:
            return _search_all(targets, q, limit, offset, match_all, count)
    else:
        engine, cfg = get_engine(index)
        resolved_id = index or get_index_id_for_config(cfg)
        index_name = cfg.get("name", "")
        targets = [_Target(resolved_id or "", index_name, engine)]

        def run(match_all: bool) -> _Outcome:
            # La deduplication reste limitee a la page courante. Cela preserve une
            # pagination stable : une page ne consomme plus silencieusement des hits
            # appartenant aux pages suivantes.
            return _search_one(engine, q, limit, offset, match_all, count)

    match_all = match != "any"
    try:
        outcome = run(match_all)
        relaxed = False
        if match == "auto" and offset == 0 and not outcome.hits and outcome.relaxable:
            # Aucun document ne contient tous les mots : ceux qui en contiennent un.
            outcome = run(False)
            match_all = False
            relaxed = True
    # Le contenu de la requete (email, nom, telephone) n'est jamais journalise:
    # seule sa longueur l'est, pour le diagnostic.
    except SearchQueryError:
        # Le message du parseur cite des fragments de la requete: non journalise.
        logger.warning("Requete invalide (%s caracteres)", len(q))
        raise HTTPException(400, "Requete de recherche invalide.")
    except IndexAccessError as exc:
        if resolved_id and resolved_id != ALL_INDEXES:
            mark_index_error(resolved_id, exc)
        logger.exception("Erreur d'acces index (requete de %s caracteres)", len(q))
        raise HTTPException(503, "Index temporairement indisponible.")
    except Exception:
        logger.exception("Erreur de recherche inattendue (requete de %s caracteres)", len(q))
        raise HTTPException(500, "Erreur interne lors de la recherche.")

    folding = {target.index_id: bool(getattr(target.engine, "ascii_folding", False)) for target in targets}
    single_folding = folding.get(targets[0].index_id, False) if len(targets) == 1 else False
    total = outcome.total
    page_hits = _deduplicate_hits(outcome.hits) if deduplicate else outcome.hits
    hits = []
    for hit in page_hits[:limit]:
        raw = hit.get("content_raw") or ""
        fold = folding.get(hit["index_id"], False) if hit.get("index_id") else single_folding
        snippets_raw = extract_snippets(raw, q, context_chars=context, fold_accents=fold)
        hits.append(SearchHit(
            score=hit["score"],
            source=hit.get("source"),
            filename=hit.get("filename"),
            type=hit.get("type"),
            index_id=hit.get("index_id"),
            index_name=hit.get("index_name"),
            snippets=[Snippet(**s) for s in snippets_raw],
        ))

    if total is None:
        total_pages = None
    else:
        total_pages = max(1, min(math.ceil(total / limit), (MAX_SEARCH_OFFSET // limit) + 1))
    return SearchResponse(
        query=q,
        total=total,
        limit=limit,
        offset=offset,
        has_more=outcome.has_more and offset + limit <= MAX_SEARCH_OFFSET,
        total_pages=total_pages,
        deduplicated=deduplicate,
        total_is_raw=deduplicate,
        match_mode="all" if match_all else "any",
        relaxed=relaxed,
        partial=bool(outcome.skipped),
        skipped_indexes=outcome.skipped,
        index_id=resolved_id or "",
        index_name=index_name,
        hits=hits,
    )
