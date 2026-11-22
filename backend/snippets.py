import re

from settings import MAX_QUERY_LENGTH


MAX_CONTENT_SIZE = 512_000  # 500 Ko


def extract_snippets(raw_content: str, query: str, context_chars: int = 2500, max_snippets: int = 5) -> list[dict]:
    """Extrait des snippets du contenu brut avec contexte autour des termes recherches.

    Retourne une liste de dicts: {"text": str, "highlights": [(start, end), ...]}
    """
    if not raw_content or not query:
        return []

    # Limiter la taille des entrees pour eviter ReDoS
    if len(query) > MAX_QUERY_LENGTH:
        query = query[:MAX_QUERY_LENGTH]

    if len(raw_content) > MAX_CONTENT_SIZE:
        raw_content = raw_content[:MAX_CONTENT_SIZE]

    # Extraire les termes de la query (ignorer les operateurs)
    operators = {"AND", "OR", "NOT", "TO"}
    terms = []
    for part in re.findall(r'"[^"]+"|[\w*.@]+', query):
        clean = part.strip('"')
        if clean.upper() not in operators and len(clean) > 1:
            terms.append(clean)

    if not terms:
        return []

    # Construire le pattern de recherche
    patterns = []
    for term in terms:
        escaped = re.escape(term)
        if escaped.endswith(r"\*"):
            # Prefix search: dev* -> dev\w*
            escaped = escaped[:-2] + r"\w*"
        patterns.append(escaped)

    combined = "|".join(patterns)
    try:
        regex = re.compile(combined, re.IGNORECASE)
    except re.error:
        return []

    # Trouver toutes les occurrences
    matches = list(regex.finditer(raw_content))
    if not matches:
        # Fallback: retourner le debut du contenu
        preview = raw_content[:context_chars * 2].strip()
        if preview:
            return [{"text": preview, "highlights": []}]
        return []

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

        # Recalculer les positions des highlights dans le snippet
        highlights = []
        for m in regex.finditer(snippet_text):
            highlights.append((m.start(), m.end()))

        # Ajouter des ellipses si tronque
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(raw_content) else ""

        snippets.append({
            "text": prefix + snippet_text + suffix,
            "highlights": [(h[0] + len(prefix), h[1] + len(prefix)) for h in highlights],
        })
        used_ranges.append((start, end))

    return snippets
