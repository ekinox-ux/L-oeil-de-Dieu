"""Pertinence : ET par defaut, telephones, accents, nom de fichier, cache.

Toutes les donnees sont fictives (domaines example.*, numeros de test).
"""

import re
import threading
from unittest.mock import patch

import pytest
import tantivy
from tantivy import Filter, TextAnalyzerBuilder, Tokenizer

from engine import SearchEngine
from query_terms import (
    analyze_query,
    fold_accents,
    highlight_patterns,
    phone_digits,
    query_candidates,
)
from snippets import extract_snippets

PHONE = "612345678"


def _highlighted(snippets):
    return [s["text"][a:b] for s in snippets for a, b in s["highlights"]]


# ---- numeros de telephone ----

@pytest.mark.parametrize("text", [
    "0612345678",
    "06 12 34 56 78",
    "06.12.34.56.78",
    "06-12-34-56-78",
    "+33 6 12 34 56 78",
    "+33612345678",
    "+33 (0)6 12 34 56 78",
    "0033612345678",
    "33612345678",
])
def test_phone_formats_are_recognized(text):
    assert phone_digits(text) == PHONE


@pytest.mark.parametrize("text", [
    "612345678",       # 9 chiffres nus : aussi le format d'un SIREN
    "01/02/2019",      # une date
    "06 12 34 56",     # trop court
    "0012345678",      # 00 n'est pas un indicatif francais ici
    "061234567890",    # trop long
])
def test_non_phone_numbers_are_left_alone(text):
    assert phone_digits(text) is None


def test_phone_typed_with_spaces_becomes_one_unit():
    units = analyze_query("jean 06 12 34 56 78 paris")

    assert [u.phone for u in units] == [None, PHONE, None]
    assert [u.text for u in units] == ["jean", "06 12 34 56 78", "paris"]


def test_phone_query_is_expanded_to_all_its_writings():
    [strict, *_] = query_candidates("+33 6 12 34 56 78")

    for variant in ("0612345678", "612345678", "33612345678", "0033612345678",
                    '"06 12 34 56 78"', '"33 6 12 34 56 78"', '"33 612345678"'):
        assert variant in strict
    assert strict.startswith("(") and strict.endswith(")")


def test_excluded_phone_excludes_every_writing():
    [strict, *_] = query_candidates("jean -06.12.34.56.78")

    assert strict.startswith("jean -(0612345678 OR ")


def test_query_without_phone_is_untouched():
    assert query_candidates("jean dupont")[0] == "jean dupont"


def test_phone_highlights_every_format_but_not_lone_pairs():
    raw = "a 06.12.34.56.78 b +33 6 12 34 56 78 c 33612345678 d 0612345678 e 12 34 f 10612345678"

    highlighted = _highlighted(extract_snippets(raw, "06 12 34 56 78", context_chars=200, max_snippets=1))

    assert highlighted == ["06.12.34.56.78", "33 6 12 34 56 78", "33612345678", "0612345678"]


# ---- accents ----

def test_fold_accents_matches_ascii_folding():
    assert fold_accents("Hélène Strauß Œuvre Łódź") == "Helene Strauss OEuvre Lodz"


def test_folded_highlight_ignores_accents():
    raw = "Hélène STRAUSS et Œuvre, helene Strauß"

    highlighted = _highlighted(extract_snippets(
        raw, "helene strauss oeuvre", context_chars=100, max_snippets=1, fold_accents=True,
    ))

    assert highlighted == ["Hélène", "STRAUSS", "Œuvre", "helene", "Strauß"]


def test_highlight_keeps_accents_for_an_index_without_folding():
    raw = "Hélène et helene"

    assert _highlighted(extract_snippets(raw, "helene", context_chars=50)) == ["helene"]


def test_folded_highlight_respects_word_boundaries():
    unit_patterns, _ = highlight_patterns("helene", fold=True)
    regex = re.compile(unit_patterns[0], re.IGNORECASE)

    assert regex.search("Hélènes") is None
    assert regex.search("(Hélène)").group() == "Hélène"


# ---- nom de fichier ----

def test_filename_terms_are_not_highlighted_in_content():
    unit_patterns, _ = highlight_patterns("filename:fuite alice")

    assert len(unit_patterns) == 1


# ---- moteur reel sur des index jetables ----

def _analyzer(fold):
    builder = TextAnalyzerBuilder(Tokenizer.simple())
    builder = builder.filter(Filter.remove_long(250)).filter(Filter.lowercase())
    if fold:
        builder = builder.filter(Filter.ascii_fold())
    return builder.build()


def _build_index(path, docs, *, fold=False, write_config=True):
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("source", stored=True, tokenizer_name="raw")
    builder.add_text_field("filename", stored=True, tokenizer_name="custom_tech")
    builder.add_text_field("type", stored=True, tokenizer_name="raw")
    builder.add_text_field("content", stored=False, tokenizer_name="custom_tech")
    builder.add_text_field("content_raw", stored=True, index_option="basic", tokenizer_name="raw")
    index = tantivy.Index(builder.build(), path=str(path))
    index.register_tokenizer("custom_tech", _analyzer(fold))
    writer = index.writer(heap_size=15_000_000, num_threads=1)
    for i, (filename, text) in enumerate(docs):
        writer.add_document(tantivy.Document(
            source=f"C:/fictif/{i}.jsonl", filename=filename, type="record",
            content=text, content_raw=text,
        ))
    writer.commit()
    writer.wait_merging_threads()
    del writer, index
    if write_config:
        (path / ".schema_config.toml").write_text(
            f'language = "en"\nascii_folding = {"true" if fold else "false"}\n', encoding="utf-8",
        )
    return SearchEngine(str(path))


DOCS = [
    ("fuite_exemple_2019.jsonl", "jean dupont paris"),
    ("fuite_exemple_2019.jsonl", "jean martin lyon"),
    ("autre.jsonl", "marie dupont"),
    ("autre.jsonl", "tel 06.12.34.56.78"),
    ("autre.jsonl", "tel +33 6 12 34 56 78"),
    ("autre.jsonl", "phone 33612345678"),
    ("autre.jsonl", "num 612345678"),
    ("autre.jsonl", "Hélène Strauß"),
]


@pytest.fixture()
def engine(tmp_path):
    return _build_index(tmp_path, DOCS)


def _sources(result):
    return sorted(int(hit["source"].rsplit("/", 1)[1].split(".")[0]) for hit in result["hits"])


def test_every_word_is_required_by_default(engine):
    assert _sources(engine.search("jean dupont")) == [0]
    assert _sources(engine.search("jean dupont", match_all=False)) == [0, 1, 2]


def test_explicit_or_still_means_or(engine):
    assert _sources(engine.search("jean OR marie")) == [0, 1, 2]
    # (jean OU marie) ET dupont
    assert _sources(engine.search("jean OR marie dupont")) == [0, 2]


def test_relaxable_only_when_or_would_change_something(engine):
    assert engine.search("jean lyon paris")["relaxable"] is True
    assert engine.search("introuvable")["relaxable"] is False
    assert engine.search("jean OR marie")["relaxable"] is False


def test_filename_is_no_longer_searched_by_default(engine):
    assert engine.search("fuite")["total"] == 0
    assert _sources(engine.search("filename:fuite")) == [0, 1]


def test_phone_finds_every_writing(engine):
    assert _sources(engine.search("06 12 34 56 78")) == [3, 4, 5, 6]
    assert _sources(engine.search("tel +33612345678")) == [3, 4]


def test_index_without_folding_keeps_accents(engine):
    assert engine.ascii_folding is False
    assert engine.search("helene")["total"] == 0
    assert engine.search("hélène")["total"] == 1


def test_legacy_index_without_config_keeps_accents(tmp_path):
    eng = _build_index(tmp_path, DOCS, write_config=False)

    assert eng.ascii_folding is False
    assert eng.search("helene")["total"] == 0


def test_folded_index_ignores_accents(tmp_path):
    eng = _build_index(tmp_path, DOCS, fold=True)

    assert eng.ascii_folding is True
    for query in ("helene", "Hélène", "HELENE", "strauss", "strauß"):
        assert _sources(eng.search(query)) == [7], query


def test_count_false_skips_the_total(engine):
    result = engine.search("jean", limit=1, count=False)

    assert result["total"] is None
    assert result["has_more"] is True
    assert len(result["hits"]) == 1


# ---- cache des classements ----

class _Doc:
    def __init__(self, n):
        self.n = n

    def get_first(self, name):
        return {"content_raw": f"doc {self.n}", "source": "C:/fictif", "filename": "f", "type": "t"}.get(name)


def _counting_engine(tmp_path, total_hits=3):
    calls = []

    class Searcher:
        def search(self, _parsed, limit, count=True):
            calls.append({"limit": limit, "count": count})
            hits = [(1.0 - i / 100, i) for i in range(min(limit, total_hits))]
            return type("R", (), {"hits": hits, "count": total_hits if count else None})()

        def doc(self, address):
            return _Doc(address)

    class Index:
        def searcher(self):
            return Searcher()

        def parse_query(self, query, _fields, **_kwargs):
            return query

        def reload(self):
            pass

    with patch.object(SearchEngine, "__init__", lambda self, path: None):
        eng = SearchEngine.__new__(SearchEngine)
    eng.index_path = str(tmp_path)
    eng.index = Index()
    eng._reload_lock = threading.Lock()
    eng._last_reload_at = float("inf")
    eng._reload_in_progress = False
    eng._index_signature = ("commit", 1)
    return eng, calls


def test_next_pages_reuse_the_cached_ranking(tmp_path):
    eng, calls = _counting_engine(tmp_path, total_hits=50)

    first = eng.search("jean", limit=20, offset=0)
    again = eng.search("jean", limit=20, offset=0, count=False)
    second = eng.search("jean", limit=20, offset=20, count=False)
    back = eng.search("jean", limit=20, offset=0)

    assert first["total"] == 50 and back["total"] == 50
    assert again["total"] == 50  # deja compte, rendu sans recompter
    assert [hit["content_raw"] for hit in second["hits"]][:1] == ["doc 20"]
    # Page 1 comptee, page 2 classee plus loin sans recompter, retour en cache.
    assert calls == [{"limit": 21, "count": True}, {"limit": 41, "count": False}]


def test_cache_is_dropped_when_the_index_changes(tmp_path):
    eng, calls = _counting_engine(tmp_path)

    eng.search("jean")
    eng._index_signature = ("commit", 2)
    eng.search("jean")

    assert len(calls) == 2


def test_match_modes_are_cached_separately(tmp_path):
    eng, calls = _counting_engine(tmp_path)

    eng.search("jean dupont", match_all=True)
    eng.search("jean dupont", match_all=False)

    assert len(calls) == 2


def test_reload_with_new_commit_clears_the_cache(tmp_path):
    eng, calls = _counting_engine(tmp_path)
    (tmp_path / "meta.json").write_text("{}", encoding="utf-8")
    eng._index_signature = eng._read_index_signature()
    eng.search("jean")

    (tmp_path / "meta.json").write_text('{"segments": []}', encoding="utf-8")
    eng._last_reload_at = 0.0
    eng.search("jean")

    assert len(calls) == 2
    assert len(eng._get_result_cache()) == 1
