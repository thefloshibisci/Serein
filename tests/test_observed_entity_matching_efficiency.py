"""Observed entity matching stays exact without compiling every Han name."""

import random
import re
import sqlite3
from contextlib import closing

import pytest

from serein.recall.germany.memory_recall import observed_entities as observed


def previous_spans(text, term, *, limit=8, _folded_text=None):
    if not text or not term:
        return []
    pattern = (f'(?<![A-Za-z0-9_.-]){re.escape(term)}(?![A-Za-z0-9_.-])'
               if re.fullmatch('[A-Za-z0-9_.-]+', term) else re.escape(term))
    return [match.span() for match in list(re.finditer(pattern, text, re.IGNORECASE))[:limit]]


@pytest.mark.parametrize('text,term', [
    ('还记得星海城和星海吗？星海城', '星海城'),
    ('哈哈哈哈哈', '哈哈'),
    ('中文123数字，123，a123，123b，_123，123-，123.，123', '123'),
    ('ı123 123İ ſ123 123K 中文123中文', '123'),
    ('a... ...b ... 中文...中文', '...'),
    ('中文-中文 - ----', '-'),
    ('JSON json Json json2 xjson _json json- json. JSON', 'JSON'),
    ('i I İ ı，İ南和i南，ı南', 'İ'),
    ('i I İ ı，İ南和i南，ı南', 'ı南'),
    ('ſ S s ss，K K k', 'S'),
    ('ſ S s ss，K K k', 'K'),
    ('β ϐ Β，μ µ Μ，θ ϑ Θ，φ ϕ Φ', 'ϐ'),
    ('β ϐ Β，μ µ Μ，θ ϑ Θ，φ ϕ Φ', 'µ'),
    ('β ϐ Β，μ µ Μ，θ ϑ Θ，φ ϕ Φ', 'ϑ'),
    ('ß ẞ SS ss', 'ß'),
    ('ss ﬅ ﬆ', 'ß'),
    ('ß ﬅ ﬆ', 'SS'),
    ('ﬅ ﬆ ST st', 'ﬅ'),
    ('A+B A?B A.B A[B]', 'A+B'),
    ('全角ＡＢＣ及abc和Ａｂｃ', 'ＡＢＣ'),
    ('emoji🌟🌟，🌟，e\u0301 É é', '🌟'),
    ('不记得星海城，没提到北风港', '星海城'),
    ('', '星海'),
    ('星海', ''),
])
@pytest.mark.parametrize('limit', [-2, -1, 0, 1, 4, 8])
def test_spans_keep_unicode_boundary_offsets_and_limits(text, term, limit):
    assert observed._term_spans(text, term, limit=limit) == previous_spans(text, term, limit=limit)


def test_random_literal_spans_match_previous_regex():
    rng = random.Random(81109)
    alphabet = list('北风星海城 0123.-_aAbBsSiIkKİıſKβϐμµθϑφϕßẞéÉ🌟[]+?')
    for _ in range(1200):
        text = ''.join(rng.choices(alphabet, k=80))
        start = rng.randrange(70)
        term = text[start:start + rng.randrange(1, 8)]
        if rng.randrange(2):
            term = term.swapcase()
        limit = rng.choice([1, 4, 8])
        assert observed._term_spans(text, term, limit=limit) == previous_spans(text, term, limit=limit)


def test_absence_fold_covers_every_python_regex_case_equivalence():
    # Proof against the interpreter's complete Unicode simple-case mapping,
    # including regex-specific equivalences. Production uses no private APIs.
    import _sre
    from re._casefix import _EXTRA_CASES

    for code in range(0x110000):
        lower = _sre.unicode_tolower(code)
        if lower != code:
            assert observed._regex_casefold(chr(code)) == observed._regex_casefold(chr(lower))
    for code, others in _EXTRA_CASES.items():
        for other in others:
            assert observed._regex_casefold(chr(code)) == observed._regex_casefold(chr(other))


def make_index(tmp_path):
    index = observed.ObservedEntityShadowIndex({'state_dir': str(tmp_path)})
    with closing(sqlite3.connect(index.db_path)) as conn, conn:
        conn.execute('CREATE TABLE observed_entities (owner_kind TEXT, owner_id TEXT, '
                     'entity_key TEXT, entity_text TEXT, occurrence_count INTEGER, '
                     'source_count INTEGER, confidence_basis TEXT, scope_eligible INTEGER)')
        rows = [('scene', f'scene{i}', f'entity{i}', f'北风港{i:05}', 2, 1, 'source', 1)
                for i in range(2400)]
        rows += [('event', 'long', 'long', '北风港12345', 5, 2, 'source', 1),
                 ('scene', 'short', 'short', '北风港', 3, 1, 'source', 1),
                 ('scene', 'same', 'long', '北风港12345', 1, 1, 'source', 0),
                 ('event', 'latin', 'latin', 'Serein', 1, 1, 'body', 0)]
        conn.executemany('INSERT INTO observed_entities VALUES (?,?,?,?,?,?,?,?)', rows)
    return index


def test_complete_owner_matches_keep_priority_overlap_and_allow_list(tmp_path, monkeypatch):
    index = make_index(tmp_path)
    query = '没忘记北风港12345；SeReIn与北风港00678。'
    allowed = [('event', 'long'), ('scene', 'short'), ('scene', 'same'),
               ('event', 'latin'), ('scene', 'scene678')]
    current = index.owner_query_matches(query, owner_keys=allowed)
    with monkeypatch.context() as comparison:
        comparison.setattr(observed, '_term_spans', previous_spans)
        previous = index.owner_query_matches(query, owner_keys=allowed)
    assert current == previous
    assert {row['owner_id'] for row in current} == {'long', 'same', 'latin', 'scene678'}
    assert next(row for row in current if row['owner_id'] == 'latin')['entity'] == 'SeReIn'


def test_large_cold_han_catalog_avoids_dynamic_regex_compilation(tmp_path, monkeypatch):
    index = make_index(tmp_path)
    query = '想起北风港00678。'
    observed._term_pattern.cache_clear()
    re.purge()
    compiled = []
    original = observed.re.compile

    def track(pattern, *args, **kwargs):
        compiled.append(pattern)
        return original(pattern, *args, **kwargs)

    monkeypatch.setattr(observed.re, 'compile', track)
    result = index.owner_query_matches(query)
    assert {row['owner_id'] for row in result} == {'scene678'}
    assert compiled == []  # Absent Latin entity also needs no regex compilation.


def test_large_cold_cased_catalog_only_compiles_potential_matches(tmp_path, monkeypatch):
    index = make_index(tmp_path)
    with closing(sqlite3.connect(index.db_path)) as conn, conn:
        conn.execute('DELETE FROM observed_entities')
        rows = [('event', f'event{i}', f'entity{i}', f'SereinProject{i:05}星港',
                 2, 1, 'source', 1) for i in range(2400)]
        rows.append(('scene', 'special', 'special', 'Istanbul星港', 2, 1, 'source', 1))
        conn.executemany('INSERT INTO observed_entities VALUES (?,?,?,?,?,?,?,?)', rows)
    observed._term_pattern.cache_clear()
    re.purge()
    original = observed.re.compile
    compiled = []

    def track(pattern, *args, **kwargs):
        compiled.append(pattern)
        return original(pattern, *args, **kwargs)

    monkeypatch.setattr(observed.re, 'compile', track)
    query = '没忘记sErEiNpRoJeCt00678星港及ıstanbul星港。'
    current = index.owner_query_matches(query)
    assert {row['owner_id'] for row in current} == {'event678', 'special'}
    assert len(compiled) == 2
    with monkeypatch.context() as comparison:
        comparison.setattr(observed, '_term_spans', previous_spans)
        reference = index.owner_query_matches(query)
    assert current == reference


def test_cased_pattern_cache_survives_standard_regex_cache_eviction(monkeypatch):
    observed._term_pattern.cache_clear()
    assert observed._term_spans('Serein', 'serein') == [(0, 6)]
    for i in range(700):
        re.compile(f'unrelated-{i}')
    monkeypatch.setattr(observed.re, 'compile', lambda *a, **kw: pytest.fail('recompiled cached entity'))
    assert observed._term_spans('SeReIn', 'serein') == [(0, 6)]
