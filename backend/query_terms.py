"""Decoupage des requetes utilisateur, aligne sur le tokenizer de l'index.

L'indexeur Rust decoupe le texte avec le SimpleTokenizer de Tantivy : un token
est une suite de caracteres alphanumeriques, tout le reste (ponctuation,
espaces, `_`, `@`, `'`) separe. Un mot de requete qui produit plusieurs tokens
(`jose.martin@example.com`, `O'Brien`) est donc cherche comme une phrase.

Ce module sert a trois choses :
- `query_candidates` produit les requetes a soumettre au parseur Tantivy : la
  requete telle quelle, puis des replis en phrases toujours valides quand le
  parseur la refuse (apostrophe, URL, IPv6, parenthese seule) ;
- un numero de telephone francais, quel que soit son format, est cherche sous
  toutes ses ecritures (`0612345678`, `06 12 34 56 78`, `+33 6 12...`) ;
- `highlight_patterns` produit les regex de surlignage qui reconnaissent ce
  que l'index a vraiment trouve, separateurs (et accents) compris.
"""

import itertools
import re
import unicodedata
from dataclasses import dataclass, field, replace

OPERATORS = frozenset({"AND", "OR", "NOT", "TO"})
# Champs texte du schema : le prefixe `champ:` est conserve en repli.
TEXT_FIELDS = frozenset({"content", "filename"})
# Champs non tokenises : absents du contenu affiche, jamais surlignes.
RAW_FIELDS = frozenset({"source", "type"})
# Champs absents du contenu affiche dans les extraits.
_NOT_HIGHLIGHTED_FIELDS = RAW_FIELDS | {"filename"}

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


# ---- numeros de telephone ----

# 06 12 34 56 78, 06.12.34.56.78, 06-12-34-56-78, 0612345678
_PHONE_NATIONAL = re.compile(r"0(?P<digits>[1-9](?:[ .\-]?\d{2}){4})")
# +33 6 12 34 56 78, +33 (0)6 12 34 56 78, 0033612345678
_PHONE_INTERNATIONAL = re.compile(
    r"(?:\+|00)33[ .\-]?(?:\(0\)[ .\-]?)?(?P<digits>[1-9](?:[ .\-]?\d{2}){4})"
)
# 33612345678 (format courant des fuites)
_PHONE_BARE_INTERNATIONAL = re.compile(r"33(?P<digits>[1-9]\d{8})")
_PHONE_PATTERNS = (_PHONE_NATIONAL, _PHONE_INTERNATIONAL, _PHONE_BARE_INTERNATIONAL)
# Un numero tape avec des espaces occupe au plus 7 unites (+33 (0)6 12 34 56 78).
_MAX_PHONE_UNITS = 7


def phone_digits(text: str) -> str | None:
    """Les 9 chiffres nationaux (sans le 0) si `text` est un numero francais.

    Un nombre nu de 9 chiffres n'est pas pris pour un numero : c'est aussi le
    format d'un SIREN.
    """
    for pattern in _PHONE_PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            return re.sub(r"\D", "", match.group("digits"))
    return None


def phone_variants(digits: str) -> list[list[str]]:
    """Ecritures d'un numero telles que l'index les decoupe en tokens.

    Une phrase de tokens (`06 12 34 56 78`) couvre aussi les points, tirets
    et barres, que le tokenizer traite comme des espaces.
    """
    national = "0" + digits
    pairs = [digits[i:i + 2] for i in range(1, 9, 2)]
    return [
        [national],                     # 0612345678
        [digits],                       # 612345678 (zero perdu a l'export)
        ["33" + digits],                # 33612345678, +33612345678
        ["0033" + digits],              # 0033612345678
        [national[:2], *pairs],         # 06 12 34 56 78
        ["33", digits[0], *pairs],      # +33 6 12 34 56 78
        ["33", digits],                 # +33 612345678
    ]


# ---- accents (filtre ascii_folding de l'index) ----

# Lettres que la decomposition Unicode ne ramene pas a l'ASCII.
_EXTRA_FOLDS = {
    "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "ß": "ss", "ẞ": "SS",
    "œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "þ": "th", "Þ": "TH", "ð": "d", "Ð": "D",
    "ı": "i", "ħ": "h", "Ħ": "H", "ŧ": "t", "Ŧ": "T",
}


def _fold_char(char: str) -> str:
    if char.isascii():
        return char
    if char in _EXTRA_FOLDS:
        return _EXTRA_FOLDS[char]
    decomposed = unicodedata.normalize("NFKD", char)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped if stripped and stripped.isascii() else char


def fold_accents(text: str) -> str:
    """Retire les accents comme le filtre AsciiFolding de Tantivy (approximation)."""
    return "".join(_fold_char(c) for c in text)


def _build_fold_tables() -> tuple[dict[str, str], dict[str, str]]:
    """Pour chaque lettre ASCII, les lettres latines accentuees qui s'y ramenent."""
    single: dict[str, set[str]] = {}
    multi: dict[str, set[str]] = {}
    for codepoint in itertools.chain(range(0xC0, 0x250), range(0x1E00, 0x1F00)):
        char = chr(codepoint)
        folded = _fold_char(char).lower()
        if folded == char.lower() or not folded.isascii() or not folded.isalnum():
            continue
        target = single if len(folded) == 1 else multi
        # Majuscules et minuscules sont toutes deux dans la plage parcourue.
        target.setdefault(folded, set()).add(char)
    to_class = {base: "".join(re.escape(c) for c in sorted(chars)) for base, chars in single.items()}
    to_alternatives = {
        base: "|".join(re.escape(c) for c in sorted(chars)) for base, chars in multi.items()
    }
    return to_class, to_alternatives


_FOLD_CLASSES, _FOLD_MULTI = _build_fold_tables()


def _char_regex(char: str) -> str:
    variants = _FOLD_CLASSES.get(char)
    if not variants:
        return re.escape(char)
    return f"[{re.escape(char)}{variants}]"


def _folded_token_regex(token: str) -> str:
    """Regex d'un token qui reconnait aussi ses formes accentuees (helene -> Hélène)."""
    folded = fold_accents(token).lower()
    parts = []
    position = 0
    while position < len(folded):
        pair = folded[position:position + 2]
        if len(pair) == 2 and pair in _FOLD_MULTI:
            # oeuvre -> Œuvre, strauss -> Strauß
            parts.append(f"(?:{_char_regex(pair[0])}{_char_regex(pair[1])}|{_FOLD_MULTI[pair]})")
            position += 2
            continue
        parts.append(_char_regex(folded[position]))
        position += 1
    return "".join(parts)


# ---- decoupage ----

@dataclass(frozen=True)
class QueryUnit:
    sign: str
    field: str | None
    text: str
    phrase: bool
    # Position de l'unite (signe compris) dans la requete d'origine.
    start: int = field(default=0, compare=False)
    end: int = field(default=0, compare=False)
    # Les 9 chiffres nationaux si l'unite est un numero de telephone.
    phone: str | None = None

    @property
    def is_operator(self) -> bool:
        return (
            self.phone is None and not self.phrase and not self.sign
            and self.field is None and self.text in OPERATORS
        )

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
        field_name = match.group("field")
        phrase = match.group("phrase")
        text = phrase if phrase is not None else match.group("word") or ""
        if field_name is not None and field_name not in TEXT_FIELDS and field_name not in RAW_FIELDS:
            # `http://...` ou `2001:db8::1` : pas un champ, le texte reste entier.
            if phrase is not None:
                text = f"{field_name}: {phrase}"
            else:
                text = f"{field_name}:{text}"
            field_name = None
        if not text and phrase is None:
            continue
        units.append(QueryUnit(
            match.group("sign"), field_name, text, phrase is not None,
            start=match.start(), end=match.end(),
        ))
    return units


def _can_join_phone(unit: QueryUnit, first: bool) -> bool:
    if unit.phrase or unit.field is not None or unit.is_operator:
        return False
    return first or not unit.sign


def _phone_unit(query: str, units: list[QueryUnit], start: int) -> tuple[QueryUnit, int] | None:
    """Le plus long numero de telephone qui commence a l'unite `start`."""
    first = units[start]
    if first.phrase:
        if first.field in (None, "content"):
            digits = phone_digits(first.text.strip())
            if digits:
                return replace(first, field=None, phone=digits), start + 1
        return None
    if not _can_join_phone(first, True):
        return None
    last = min(len(units), start + _MAX_PHONE_UNITS)
    for stop in range(last, start, -1):
        if not all(_can_join_phone(unit, False) for unit in units[start + 1:stop]):
            continue
        span = query[first.start:units[stop - 1].end]
        sign = ""
        digits = phone_digits(span)  # le + de +33 fait partie du numero
        if digits is None and first.sign:
            sign = first.sign
            digits = phone_digits(span[len(sign):])
        if digits:
            unit = QueryUnit(
                sign, None, span[len(sign):], False,
                start=first.start, end=units[stop - 1].end, phone=digits,
            )
            return unit, stop
    return None


def analyze_query(query: str) -> list[QueryUnit]:
    """Unites de la requete, numeros de telephone regroupes en une seule unite."""
    units = split_query(query)
    merged = []
    position = 0
    while position < len(units):
        found = _phone_unit(query, units, position)
        if found:
            merged.append(found[0])
            position = found[1]
        else:
            merged.append(units[position])
            position += 1
    return merged


# ---- reecriture pour le parseur ----

def _phone_clause(unit: QueryUnit) -> str:
    alternatives = [
        tokens[0] if len(tokens) == 1 else '"' + " ".join(tokens) + '"'
        for tokens in phone_variants(unit.phone)
    ]
    return f"{unit.sign}({' OR '.join(alternatives)})"


def _quoted_unit(unit: QueryUnit) -> str | None:
    tokens = tokenize(unit.text)
    if not tokens:
        return None
    field_prefix = f"{unit.field}:" if unit.field in TEXT_FIELDS else ""
    if unit.is_prefix and len(tokens) == 1:
        return f"{unit.sign}{field_prefix}{tokens[0]}*"
    suffix = "*" if unit.is_prefix else ""
    return f'{unit.sign}{field_prefix}"{" ".join(tokens)}"{suffix}'


def _render(query: str, units: list[QueryUnit], *, quote: bool, keep_operators: bool) -> str:
    parts = []
    for unit in units:
        if unit.is_operator:
            if keep_operators:
                parts.append(unit.text)
            continue
        if unit.phone:
            parts.append(_phone_clause(unit))
        elif quote:
            quoted = _quoted_unit(unit)
            if quoted:
                parts.append(quoted)
        else:
            parts.append(query[unit.start:unit.end])
    while parts and parts[-1] in OPERATORS:
        parts.pop()
    while parts and parts[0] in OPERATORS - {"NOT"}:
        parts.pop(0)
    return " ".join(parts)


def quote_query_terms(query: str, keep_operators: bool = True) -> str:
    """Reecrit une requete en phrases que le parseur Tantivy accepte toujours.

    Chaque mot devient la phrase de ses tokens : `o'brien` -> `"o brien"`,
    `http://example.net` -> `"http example net"`. Le resultat trouve les memes
    documents que le tokenizer de l'index. Les signes +/- , les champs texte,
    les prefixes `mot*` et, si keep_operators, les operateurs sont conserves.
    """
    return _render(query, analyze_query(query), quote=True, keep_operators=keep_operators)


def query_candidates(query: str) -> list[str]:
    """Requetes a essayer dans l'ordre, la premiere que le parseur accepte gagne.

    1. la requete telle quelle (numeros de telephone etendus a leurs variantes) ;
    2. chaque mot en phrase, operateurs conserves ;
    3. chaque mot en phrase, sans operateurs.
    """
    units = analyze_query(query)
    if any(unit.phone for unit in units):
        strict = _render(query, units, quote=False, keep_operators=True)
    else:
        strict = query
    candidates = []
    for candidate in (
        strict,
        _render(query, units, quote=True, keep_operators=True),
        _render(query, units, quote=True, keep_operators=False),
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


# ---- surlignage ----

def has_search_terms(query: str) -> bool:
    """Vrai si la requete contient autre chose que des operateurs."""
    return any(
        not unit.is_operator and (unit.phone or tokenize(unit.text))
        for unit in analyze_query(query)
    )


def _tokens_pattern(tokens: list[str], prefix: bool, fold: bool = False) -> str:
    token_regex = _folded_token_regex if fold else re.escape
    body = _SEPARATOR.join(token_regex(token) for token in tokens)
    if prefix:
        return _BEFORE + body + r"[^\W_]*"
    return _BEFORE + body + _AFTER


def highlight_patterns(query: str, fold: bool = False) -> tuple[list[str], list[str]]:
    """Regex de surlignage d'une requete.

    Retourne (unites, tokens) : une regex par unite cherchee (les tokens d'un
    mot ou d'une phrase, separes par n'importe quelle ponctuation), puis une
    regex par token isole, pour le repli quand une phrase de l'index est
    coupee dans le JSON brut par une cle. Les unites exclues (-mot, NOT mot),
    les operateurs et les champs absents des extraits sont ignores. Un numero
    de telephone donne une regex par ecriture, jamais de tokens isoles (`12`
    surlignerait tous les nombres). Avec fold, les accents sont ignores, comme
    dans un index construit avec ascii_folding.
    """
    unit_patterns: list[str] = []
    token_patterns: list[str] = []
    seen_units: set[str] = set()
    seen_tokens: set[str] = set()

    def add(patterns: list[str], seen: set[str], pattern: str) -> None:
        if pattern.lower() not in seen:
            seen.add(pattern.lower())
            patterns.append(pattern)

    negate_next = False
    for unit in analyze_query(query):
        if unit.is_operator:
            negate_next = unit.text == "NOT"
            continue
        negated = negate_next or unit.sign == "-"
        negate_next = False
        if negated or unit.field in _NOT_HIGHLIGHTED_FIELDS:
            continue
        if unit.phone:
            for tokens in phone_variants(unit.phone):
                add(unit_patterns, seen_units, _tokens_pattern(tokens, False))
            continue
        tokens = tokenize(unit.text)
        if not tokens:
            continue
        if len(tokens) == 1 and len(tokens[0]) < 2 and not unit.is_prefix:
            continue  # une lettre seule surlignerait tout le document
        add(unit_patterns, seen_units, _tokens_pattern(tokens, unit.is_prefix, fold))
        for position, token in enumerate(tokens):
            last = position == len(tokens) - 1
            if len(token) < 2 and not (last and unit.is_prefix):
                continue
            add(token_patterns, seen_tokens, _tokens_pattern([token], last and unit.is_prefix, fold))
    return unit_patterns, token_patterns
