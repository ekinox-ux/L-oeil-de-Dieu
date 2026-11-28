import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from snippets import extract_snippets


def test_extract_snippets_highlights_query_term():
    snippets = extract_snippets('Alice lives in Paris and likes Python.', 'Paris', context_chars=6)
    assert len(snippets) == 1
    snippet = snippets[0]
    assert 'Paris' in snippet['text']
    start, end = snippet['highlights'][0]
    assert snippet['text'][start:end].lower() == 'paris'


def test_extract_snippets_supports_prefix_queries():
    snippets = extract_snippets('developer devops design', 'dev*', context_chars=20)
    assert len(snippets) == 1
    highlighted = [snippets[0]['text'][start:end].lower() for start, end in snippets[0]['highlights']]
    assert 'developer' in highlighted
    assert 'devops' in highlighted


def test_extract_snippets_returns_preview_when_no_match():
    snippets = extract_snippets('abcdef ghijkl mnopqr', 'zzzz', context_chars=4)
    assert snippets == [{'text': 'abcdef g', 'highlights': []}]


def test_extract_snippets_ignores_only_operators():
    assert extract_snippets('alpha beta gamma', 'AND OR NOT') == []


def test_extract_snippets_limits_overlapping_ranges():
    text = 'alpha beta gamma alpha beta gamma'
    snippets = extract_snippets(text, 'alpha', context_chars=8, max_snippets=5)
    assert len(snippets) >= 1
    assert len(snippets) <= 2


def test_extract_snippets_previews_filter_only_queries():
    # filename:, source: ou exclusions seules : rien a surligner, mais le
    # document trouve reste lisible.
    for query in ("filename:fuite", "source:x", "-bob"):
        assert extract_snippets("alpha beta gamma", query) == [{"text": "alpha beta gamma", "highlights": []}]
