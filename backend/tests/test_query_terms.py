"""Requetes refusees par le parseur, surlignage aligne sur le tokenizer.

Toutes les donnees sont fictives (domaines example.*, numeros de test).
"""

import threading
from unittest.mock import patch

import pytest
import tantivy
from tantivy import Filter, TextAnalyzerBuilder, Tokenizer

from engine import IndexAccessError, SearchEngine, SearchQueryError
from query_terms import highlight_patterns, quote_query_terms, split_query
from snippets import extract_snippets


def _highlighted(snippets):
    return [s["text"][a:b] for s in snippets for a, b in s["highlights"]]


# ---- quote_query_terms ----

@pytest.mark.parametrize("query, expected", [
    ("o'brien", '"o brien"'),
    ("http://example.net/profil", '"http example net profil"'),
    ("2001:db8::1", '"2001 db8 1"'),
    ("(test", '"test"'),
    ("sean o'brien", '"sean" "o brien"'),
    ("-o'brien dupont", '-"o brien" "dupont"'),
    ("filename:d'artagnan", 'filename:"d artagnan"'),
    ('"jean dupont', '"jean dupont"'),
    ("dev* o'brien", 'dev* "o brien"'),
    ("o'brien AND dupont", '"o brien" AND "dupont"'),
    ("o'brien AND", '"o brien"'),
])
def test_quote_query_terms(query, expected):
    assert quote_query_terms(query) == expected


def test_quote_query_terms_can_drop_operators():
    assert quote_query_terms("o'brien AND dupont", keep_operators=False) == '"o brien" "dupont"'


def test_quote_query_terms_drops_units_without_tokens():
    assert quote_query_terms('( ) "" ::') == ""


def test_split_query_keeps_unknown_prefix_in_text():
    [unit] = split_query("http://example.net")
    assert unit.field is None
    assert unit.text == "http://example.net"


# ---- surlignage ----

def test_dotted_email_query_highlights_underscore_email():
    raw = '{"email":"jose_martin@example.com","ville":"Lyon"}'

    snippets = extract_snippets(raw, "jose.martin@example.com", context_chars=50)

    assert _highlighted(snippets) == ["jose_martin@example.com"]


def test_apostrophe_and_hyphen_are_highlighted_as_phrases():
    raw = "Sean O'Brien et Jean-Pierre Petit"

    assert _highlighted(extract_snippets(raw, "o'brien", context_chars=50)) == ["O'Brien"]
    assert _highlighted(extract_snippets(raw, "jean-pierre", context_chars=50)) == ["Jean-Pierre"]


def test_highlight_respects_token_boundaries():
    raw = "martinez martin"

    assert _highlighted(extract_snippets(raw, "martin", context_chars=50)) == ["martin"]


def test_excluded_terms_are_not_highlighted():
    raw = "alice bob carole"

    assert _highlighted(extract_snippets(raw, "alice -bob NOT carole", context_chars=50)) == ["alice"]


def test_raw_fields_are_not_highlighted():
    unit_patterns, _ = highlight_patterns("source:record alice")
    assert len(unit_patterns) == 1


def test_phrase_split_by_json_key_falls_back_to_tokens():
    raw = '{"prenom":"Jean","nom":"Dupont"}'

    highlighted = _highlighted(extract_snippets(raw, '"jean dupont"', context_chars=50))

    assert highlighted == ["Jean", "Dupont"]


def test_match_far_in_long_document_is_shown():
    raw = '{"bio":"' + "x " * 4000 + '","email":"jose_martin@example.com"}'

    snippets = extract_snippets(raw, "jose.martin@example.com", context_chars=100)

    assert "jose_martin@example.com" in snippets[0]["text"]
    assert snippets[0]["highlights"]


def test_highlight_at_snippet_edge_keeps_word_boundary():
    raw = "zzzjose " + "y" * 20 + " jose"

    snippets = extract_snippets(raw, "jose", context_chars=22)

    assert _highlighted(snippets) == ["jose"]


# ---- moteur reel sur un index jetable ----

def _analyzer():
    return (
        TextAnalyzerBuilder(Tokenizer.simple())
        .filter(Filter.remove_long(250))
        .filter(Filter.lowercase())
        .build()
    )


@pytest.fixture()
def real_engine(tmp_path):
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("source", stored=True, tokenizer_name="raw")
    builder.add_text_field("filename", stored=True, tokenizer_name="custom_tech")
    builder.add_text_field("type", stored=True, tokenizer_name="raw")
    builder.add_text_field("content", stored=False, tokenizer_name="custom_tech")
    builder.add_text_field("content_raw", stored=True, index_option="basic", tokenizer_name="raw")
    index = tantivy.Index(builder.build(), path=str(tmp_path))
    index.register_tokenizer("custom_tech", _analyzer())
    writer = index.writer(heap_size=15_000_000, num_threads=1)
    for i, text in enumerate([
        "Sean O'Brien http://example.net/profil",
        "ip 2001:db8::1 fin",
        "Marie Curie marie@example.com",
    ]):
        writer.add_document(tantivy.Document(
            source=f"C:/fictif/{i}.jsonl", filename="f.jsonl", type="record",
            content=text, content_raw=text,
        ))
    writer.commit()
    writer.wait_merging_threads()
    del writer, index
    return SearchEngine(str(tmp_path))


@pytest.mark.parametrize("query, expected_total", [
    ("o'brien", 1),
    ("http://example.net/profil", 1),
    ("2001:db8::1", 1),
    ("(curie", 1),
    ("sean o'brien", 1),
])
def test_queries_rejected_by_the_parser_now_search(real_engine, query, expected_total):
    result = real_engine.search(query)
    assert result["total"] == expected_total


def test_valid_syntax_is_unchanged(real_engine):
    assert real_engine.search("marie OR sean")["total"] == 2


# ---- lecture des documents ----

class _Doc:
    def __init__(self, n):
        self.n = n

    def get_first(self, name):
        return {"content_raw": f"doc {self.n}", "source": "C:/fictif", "filename": "f", "type": "t"}.get(name)


def _engine_with_docs(tmp_path, doc_impl):
    class Searcher:
        def search(self, _parsed, _limit):
            return type("R", (), {"hits": [(1.0, 0), (0.9, 1), (0.8, 2)], "count": 3})()

        def doc(self, address):
            return doc_impl(address)

    class Index:
        def searcher(self):
            return Searcher()

        def parse_query(self, query, _fields):
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
    return eng


def test_one_unreadable_document_is_skipped(tmp_path):
    def doc(address):
        if address == 1:
            raise OSError("bloc corrompu")
        return _Doc(address)

    result = _engine_with_docs(tmp_path, doc).search("doc")

    assert [hit["content_raw"] for hit in result["hits"]] == ["doc 0", "doc 2"]
    assert result["total"] == 3


def test_all_documents_unreadable_raises_index_error(tmp_path):
    def doc(_address):
        raise OSError("disque absent")

    with pytest.raises(IndexAccessError):
        _engine_with_docs(tmp_path, doc).search("doc")


def test_unrecoverable_query_still_raises(tmp_path):
    eng = _engine_with_docs(tmp_path, _Doc)
    tried = []

    def always_fails(query, _fields):
        tried.append(query)
        raise ValueError("syntaxe")

    eng.index.parse_query = always_fails

    with pytest.raises(SearchQueryError):
        eng.search("o'brien AND dupont")
    assert tried == ["o'brien AND dupont", '"o brien" AND "dupont"', '"o brien" "dupont"']


def test_longest_unit_wins_when_patterns_overlap():
    raw = "contact: jean dupont"

    assert _highlighted(extract_snippets(raw, 'jean "jean dupont"', context_chars=50)) == ["jean dupont"]
