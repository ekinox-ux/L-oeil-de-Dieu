import re

from query_terms import has_search_terms, highlight_patterns
from settings import MAX_QUERY_LENGTH


MAX_CONTENT_SIZE = 512_000  # 500 Ko


def _preview(raw_content: str, context_chars: int) -> list[dict]:
    preview = raw_content[:context_chars * 2].strip()
    if preview:
        return [{"text": preview, "highlights": []}]
    return []


def extract_snippets(
    raw_content: str,
    query: str,
    context_chars: int = 2500,
    max_snippets: int = 5,
    fold_accents: bool = False,
) -> list[dict]:
    """Extrait des snippets du contenu brut avec contexte autour des termes recherches.

    Retourne une liste de dicts: {"text": str, "highlights": [(start, end), ...]}

    Le surlignage decoupe la requete comme l'index : `jose.martin@example.com`
    surligne `jose_martin@example.com`, que la recherche a trouve. Si aucune
    unite n'apparait telle quelle (phrase coupee par une cle JSON dans le
    contenu brut), les tokens sont surlignes un par un. Les positions sont en
    caracteres (points de code). fold_accents suit l'index : construit avec
    ascii_folding, `helene` y a trouve `Hélène`, qui est donc surligne.
    """
    if not raw_content or not query:
        return []

    # Limiter la taille des entrees pour eviter ReDoS
    if len(query) > MAX_QUERY_LENGTH:
        query = query[:MAX_QUERY_LENGTH]

    if len(raw_content) > MAX_CONTENT_SIZE:
        raw_content = raw_content[:MAX_CONTENT_SIZE]

    unit_patterns, token_patterns = highlight_patterns(query, fold=fold_accents)
    if not unit_patterns:
        # Recherche sur le nom du fichier, la source ou par exclusions : rien a
        # surligner, le debut du document reste affiche.
        return _preview(raw_content, context_chars) if has_search_terms(query) else []

    matches = []
    for patterns in (unit_patterns, token_patterns):
        if not patterns:
            continue
        try:
            # Le motif le plus long d'abord : "jean dupont" l'emporte sur jean.
            regex = re.compile("|".join(sorted(patterns, key=len, reverse=True)), re.IGNORECASE)
        except re.error:
            return []
        matches = list(regex.finditer(raw_content))
        if matches:
            break

    if not matches:
        # Fallback: retourner le debut du contenu
        return _preview(raw_content, context_chars)

    # Regrouper les matches proches et extraire les snippets
    snippets = []
    used_ranges = []

    for match in matches:
        if len(snippets) >= max_snippets:
            break

        start = max(0, match.start() - context_chars)
        end = min(len(raw_content), match.end() + context_chars)

        # Eviter les snippets qui se chevauchent
        overlaps = False
        for used_start, used_end in used_ranges:
            if start < used_end and end > used_start:
                overlaps = True
                break

        if overlaps:
            continue

        snippet_text = raw_content[start:end]

        # Correspondances trouvees sur le contenu entier : les bornes de mot
        # restent justes au bord de l'extrait.
        highlights = [
            (m.start() - start, m.end() - start)
            for m in matches
            if m.start() >= start and m.end() <= end
        ]

        # Ajouter des ellipses si tronque
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(raw_content) else ""

        snippets.append({
            "text": prefix + snippet_text + suffix,
            "highlights": [(h[0] + len(prefix), h[1] + len(prefix)) for h in highlights],
        })
        used_ranges.append((start, end))

    return snippets
