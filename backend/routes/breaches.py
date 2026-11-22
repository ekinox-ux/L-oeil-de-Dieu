"""Route agrégée de consultation des bases de fuites."""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from deps import limiter, verify_api_key
from models import (
    BdEntryModel,
    BreachesResponse,
    BreachResultModel,
    BreachSummary,
    HibpDetailsModel,
    ProviderResponseModel,
)
from services.breach_lookup import lookup_all

logger = logging.getLogger("oeil-api")
router = APIRouter(prefix="/api")

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


@router.get("/breaches", response_model=BreachesResponse)
@limiter.limit("10/minute")
async def breaches(
    request: Request,
    email: str = Query(..., min_length=3, max_length=254, description="Email a verifier"),
    _=Depends(verify_api_key),
):
    if not EMAIL_REGEX.match(email):
        raise HTTPException(400, "Format email invalide.")

    provider_responses = await lookup_all(email)

    providers = []
    providers_ok = 0
    providers_failed = 0
    total_hits = 0

    for pr in provider_responses:
        results = []
        for r in pr.results:
            hibp_details = None
            if r.hibp_details:
                h = r.hibp_details
                hibp_details = HibpDetailsModel(
                    title=h.title, added_date=h.added_date,
                    modified_date=h.modified_date, pwn_count=h.pwn_count,
                    is_verified=h.is_verified, is_fabricated=h.is_fabricated,
                    is_sensitive=h.is_sensitive, is_retired=h.is_retired,
                    is_spam_list=h.is_spam_list, is_malware=h.is_malware,
                    is_subscription_free=h.is_subscription_free,
                    is_stealer_log=h.is_stealer_log,
                    attribution=h.attribution, disclosure_url=h.disclosure_url,
                )
            bd_entries = [
                BdEntryModel(
                    source=e.source, password=e.password,
                    hash_password=e.hash_password, sha1=e.sha1,
                    hash=e.hash, has_password=e.has_password,
                )
                for e in r.bd_entries
            ]
            results.append(BreachResultModel(
                name=r.name, domain=r.domain, breach_date=r.breach_date,
                description=r.description, data_classes=r.data_classes,
                source_provider=r.source_provider, logo_url=r.logo_url,
                password_exposed=r.password_exposed,
                hibp_details=hibp_details, bd_entries=bd_entries,
            ))
        providers.append(ProviderResponseModel(
            name=pr.name,
            ok=pr.ok,
            results=results,
            error=pr.error,
        ))
        if pr.ok:
            providers_ok += 1
            total_hits += len(results)
        else:
            providers_failed += 1

    return BreachesResponse(
        query=email,
        providers=providers,
        summary=BreachSummary(
            providers_ok=providers_ok,
            providers_failed=providers_failed,
            total_hits=total_hits,
        ),
    )
