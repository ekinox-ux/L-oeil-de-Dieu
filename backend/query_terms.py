"""Decoupage des requetes utilisateur, aligne sur le tokenizer de l'index.

L'indexeur Rust decoupe le texte avec le SimpleTokenizer de Tantivy : un token
est une suite de caracteres alphanumeriques, tout le reste (ponctuation,
espaces, `_`, `@`, `'`) separe. Un mot de requete qui produit plusieurs tokens
(`jose.martin@example.com`, `O'Brien`) est donc cherche comme une phrase.

Ce module sert a deux choses :
- `quote_query_terms` reecrit une requete refusee par le parseur Tantivy
  (apostrophe, URL, IPv6, parenthese seule) en phrases toujours valides ;
- `highlight_patterns` produit les regex de surlignage qui reconnaissent ce
  que l'index a vraiment trouve, separateurs compris.
"""

import re
from dataclasses import dataclass

OPERATORS = frozenset({"AND", "OR", "NOT", "TO"})
# Champs texte du schema : le prefixe `champ:` est conserve en repli.
TEXT_FIELDS = frozenset({"content", "filename"})
# Champs non tokenises : absents du contenu affiche, jamais surlignes.
RAW_FIELDS = frozenset({"source", "type"})

_TOKEN_RE = re.compile(r"[^\W_]+")
_UNIT_RE = re.compile(
    r'(?P<sign>[+-]?)'
    r'(?:(?P<field>[A-Za-z_]\w*):(?=\S))?'
    r'(?:"(?P<phrase>[^"]*)"?|(?P<word>[^\s"]+))'
)
# Bornes d'un token : ni precede ni suivi d'un caractere alphanumerique.
_BEFORE = r"(?<![^\W_])"
_AFTER = r"(?![^\W_])"
_SEPARATOR = r"[\W_]+"


@dataclass(frozen=True)
class QueryUnit:
    sign: str
    field: str | None
    text: str
    phrase: bool

    @property
    def is_operator(self) -> bool:
        return not self.phrase and not self.sign and self.field is None and self.text in OPERATORS

    @property
    def is_prefix(self) -> bool:
        return not self.phrase and self.text.endswith("*")


def tokenize(text: str) -> list[str]:
    """Tokens tels que l'index les voit (casse conservee)."""
    return _TOKEN_RE.findall(text)


def split_query(query: str) -> list[QueryUnit]:
    """Decoupe une requete en unites : mot, phrase entre guillemets ou operateur."""
    units = []
    for match in _UNIT_RE.finditer(query):
        field = match.group("field")
        phrase = match.group("phrase")
        text = phrase if phrase is not None else match.group("word") or ""
        if field is not None and field not in TEXT_FIELDS and field not in RAW_FIELDS:
            # `http://...` ou `2001:db8::1` : pas un champ, le texte reste entier.
            if phrase is not None:
                text = f"{field}: {phrase}"
            else:
                text = f"{field}:{text}"
            field = None
        if not text and phrase is None:
            continue
        units.append(QueryUnit(match.group("sign"), field, text, phrase is not None))
    return units


def _quoted_unit(unit: QueryUnit) -> str | None:
    tokens = tokenize(unit.text)
    if not tokens:
        return None
    field = f"{unit.field}:" if unit.field in TEXT_FIELDS else ""
    if unit.is_prefix and len(tokens) == 1:
        return f"{unit.sign}{field}{tokens[0]}*"
    suffix = "*" if unit.is_prefix else ""
    return f'{unit.sign}{field}"{" ".join(tokens)}"{suffix}'


def quote_query_terms(query: str, keep_operators: bool = True) -> str:
    """Reecrit une requete en phrases que le parseur Tantivy accepte toujours.

    Chaque mot devient la phrase de ses tokens : `o'brien` -> `"o brien"`,
    `http://example.net` -> `"http example net"`. Le resultat trouve les memes
    documents que le tokenizer de l'index. Les signes +/- , les champs texte,
    les prefixes `mot*` et, si keep_operators, les operateurs sont conserves.
    """
    parts = []
    for unit in split_query(query):
        if unit.is_operator:
            if keep_operators:
                parts.append(unit.text)
            continue
        quoted = _quoted_unit(unit)
        if quoted:
            parts.append(quoted)
    while parts and parts[-1] in OPERATORS:
        parts.pop()
    while parts and parts[0] in OPERATORS - {"NOT"}:
        parts.pop(0)
    return " ".join(parts)


def _tokens_pattern(tokens: list[str], prefix: bool) -> str:
    body = _SEPARATOR.join(re.escape(token) for token in tokens)
    if prefix:
        return _BEFORE + body + r"[^\W_]*"
    return _BEFORE + body + _AFTER


def highlight_patterns(query: str) -> tuple[list[str], list[str]]:
    """Regex de surlignage d'une requete.

    Retourne (unites, tokens) : une regex par unite cherchee (les tokens d'un
    mot ou d'une phrase, separes par n'importe quelle ponctuation), puis une
    regex par token isole, pour le repli quand une phrase de l'index est
    coupee dans le JSON brut par une cle. Les unites exclues (-mot, NOT mot),
    les operateurs et les champs non tokenises sont ignores.
    """
    unit_patterns: list[str] = []
    token_patterns: list[str] = []
    seen_units: set[str] = set()
    seen_tokens: set[str] = set()
    negate_next = False
    for unit in split_query(query):
        if unit.is_operator:
            negate_next = unit.text == "NOT"
            continue
        negated = negate_next or unit.sign == "-"
        negate_next = False
        if negated or unit.field in RAW_FIELDS:
            continue
        tokens = tokenize(unit.text)
        if not tokens:
            continue
        if len(tokens) == 1 and len(tokens[0]) < 2 and not unit.is_prefix:
            continue  # une lettre seule surlignerait tout le document
        pattern = _tokens_pattern(tokens, unit.is_prefix)
        if pattern.lower() not in seen_units:
            seen_units.add(pattern.lower())
            unit_patterns.append(pattern)
        for position, token in enumerate(tokens):
            last = position == len(tokens) - 1
            if len(token) < 2 and not (last and unit.is_prefix):
                continue
            token_pattern = _tokens_pattern([token], last and unit.is_prefix)
            if token_pattern.lower() not in seen_tokens:
                seen_tokens.add(token_pattern.lower())
                token_patterns.append(token_pattern)
    return unit_patterns, token_patterns
