"""Read-only Germany policy slice; provenance: PROVENANCE.md."""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import closing
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Any
from typing import Iterable
from ..identity import identity_names
from ..query_terms import GENERIC_LEXICAL_STOPWORDS
_INTENT_PATTERNS = (('entity_detail', 'none', re.compile('是谁|是什么人|什么来头|指的是谁|是哪位|怎么评价|如何评价|怎么看待|说过什么|说了什么|提到过什么|聊过什么|指什么|是什么意思')), ('progress', 'latest_relevant_member', re.compile('看到哪|读到哪|做到哪|进行到哪|进展(?:到哪|如何|怎么样)|追到哪')), ('timeline', 'timeline', re.compile('后来|后续|之后|怎么发展|如何发展|发展成|演变|时间线')), ('recent', 'latest_relevant_member', re.compile('最近(?:怎么样|如何|发生了什么|有什么)')), ('member_search', 'member_search', re.compile('第(?:一|1)次|初次|最初|刚开始')), ('member_search', 'member_search', re.compile('哪一段|那一段|这段|其中一段|某一段|提到.+(?:那段|一段)')), ('recall_reference', 'arc_index', re.compile('还记得|记得|想起|回忆|上次(?:聊|说|看|读|做)')))
_ASCII_TERM = re.compile('[A-Za-z0-9_.-]+\\Z')
_ASCII_BOUNDARY = re.compile('[A-Za-z0-9_.-]', re.IGNORECASE)
_CASEFOLD_SPECIAL_I = str.maketrans({'İ': 'i', 'ı': 'i'})

def _key(value: Any) -> str:
    normalized = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return re.sub('[\\W_]+', '', normalized, flags=re.UNICODE)

@lru_cache(maxsize=4096)
def _term_pattern(term: str):
    pattern = f'(?<![A-Za-z0-9_.-]){re.escape(term)}(?![A-Za-z0-9_.-])' if _ASCII_TERM.fullmatch(term) else re.escape(term)
    return re.compile(pattern, re.IGNORECASE)


def _regex_casefold(text: str) -> str:
    # Python IGNORECASE also treats dotted/dotless I as plain I. This fold is
    # only an absence prefilter; original regexes still decide every match.
    return text.translate(_CASEFOLD_SPECIAL_I).casefold()


def _term_spans(text: str, term: str, *, limit: int=8,
                _folded_text: str | None=None) -> list[tuple[int, int]]:
    if not text or not term or limit == 0:
        return []
    # Han names, digits and punctuation have no case variants. Avoid compiling
    # one regex per distinct entity while scanning a large observed catalog.
    if term.lower() == term.upper() and term.casefold() == term:
        bounded = bool(_ASCII_TERM.fullmatch(term))
        spans = []
        offset = 0
        while (start := text.find(term, offset)) != -1:
            end = start + len(term)
            if bounded and ((start and _ASCII_BOUNDARY.fullmatch(text[start - 1]))
                            or (end < len(text) and _ASCII_BOUNDARY.fullmatch(text[end]))):
                offset = start + 1
                continue
            spans.append((start, end))
            if limit > 0 and len(spans) >= limit:
                return spans
            offset = end
        return spans[:limit] if limit < 0 else spans
    folded_text = _regex_casefold(text) if _folded_text is None else _folded_text
    if _regex_casefold(term) not in folded_text:
        return []
    matches = _term_pattern(term).finditer(text)
    # Keep Python's Unicode IGNORECASE semantics for cased names (including
    # dotted/dotless I and long s), and stop once the requested spans are found.
    if limit < 0:
        return [match.span() for match in list(matches)[:limit]]
    return [match.span() for match in islice(matches, limit)]

def _query_intent(query: str) -> tuple[str, str]:
    for intent, operator, pattern in _INTENT_PATTERNS:
        if pattern.search(query):
            return (intent, operator)
    return ('none', 'none')

class ObservedEntityShadowIndex:

    def __init__(self, config: dict[str, Any]):
        state_dir = str(config.get('state_dir') or os.path.join(os.path.dirname(os.path.abspath(config.get('buckets_dir', 'buckets'))), 'state'))
        self.db_path = os.path.join(state_dir, 'observed_entity_shadow.sqlite')
        names = identity_names(config or {})
        stop_values = {*GENERIC_LEXICAL_STOPWORDS, '我', '你', '她', '他', '我们', '你们', '他们', '哥哥', '老婆', '老公', '用户', 'Assistant', '事情', '东西', '内容', '记忆', '片段', '问题', '今天', '昨天', '明天'}
        for field in ('relationship_terms', 'user_aliases', 'assistant_aliases'):
            stop_values.update(names.get(field) or [])
        self.stop_keys = frozenset((_key(value) for value in stop_values if _key(value)))

    def _connect(self, *, readonly: bool=False) -> sqlite3.Connection:
        if readonly:
            conn = sqlite3.connect(f'{Path(self.db_path).resolve().as_uri()}?mode=ro', uri=True, timeout=10.0)
        else:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def owner_query_matches(self, query: str, *, owner_keys: Iterable[tuple[str, str]] | None=None, limit: int=24) -> list[dict[str, Any]]:
        text = str(query or '').strip()
        if not text or not os.path.exists(self.db_path):
            return []
        allowed = {(str(kind or '').strip().lower(), str(owner_id or '').strip()) for kind, owner_id in owner_keys if str(kind or '').strip().lower() in {'scene', 'event'} and str(owner_id or '').strip()} if owner_keys is not None else None
        if allowed is not None and (not allowed):
            return []
        where_sql = ''
        params: list[str] = []
        if allowed is not None:
            where_sql = ' WHERE ' + ' OR '.join(('(owner_kind=? AND owner_id=?)' for _ in allowed))
            for kind, owner_id in sorted(allowed):
                params.extend([kind, owner_id])
        with closing(self._connect(readonly=True)) as conn:
            rows = conn.execute(f'\n                SELECT owner_kind, owner_id, entity_key, entity_text,\n                       occurrence_count, source_count, confidence_basis,\n                       scope_eligible\n                FROM observed_entities\n                {where_sql}\n                ORDER BY LENGTH(entity_text) DESC, source_count DESC,\n                         occurrence_count DESC, entity_key, owner_kind, owner_id\n                ', params).fetchall()
        matched_spans: dict[str, tuple[int, int]] = {}
        rejected_entity_keys: set[str] = set()
        occupied_spans: list[tuple[int, int]] = []
        output: list[dict[str, Any]] = []
        bounded_limit = max(1, min(100, int(limit or 24)))
        folded_text = _regex_casefold(text)
        for row in rows:
            owner_key = (str(row['owner_kind']), str(row['owner_id']))
            if allowed is not None and owner_key not in allowed:
                continue
            entity_key = str(row['entity_key'])
            if entity_key in rejected_entity_keys:
                continue
            span = matched_spans.get(entity_key)
            if span is None:
                spans = _term_spans(text, str(row['entity_text']), limit=1, _folded_text=folded_text)
                if not spans:
                    rejected_entity_keys.add(entity_key)
                    continue
                span = spans[0]
                if any((not (span[1] <= old_start or span[0] >= old_end) for old_start, old_end in occupied_spans)):
                    rejected_entity_keys.add(entity_key)
                    continue
                matched_spans[entity_key] = span
                occupied_spans.append(span)
            output.append({'owner_kind': owner_key[0], 'owner_id': owner_key[1], 'entity': text[span[0]:span[1]], 'start_offset': span[0], 'end_offset': span[1], 'occurrence_count': int(row['occurrence_count']), 'source_count': int(row['source_count']), 'confidence_basis': str(row['confidence_basis']), 'scope_eligible': bool(row['scope_eligible']), 'source_kind': 'observed_entity'})
            if len(output) >= bounded_limit:
                break
        return output

    def resolve_query(self, query: str) -> dict[str, Any]:
        text = str(query or '').strip()
        intent, operator = _query_intent(text)
        if not text:
            return {'status': 'no_scope', 'intent': intent, 'operator': operator, 'scope_anchor': None, 'retrieval_allowed': False, 'decision_applied': False}
        if not os.path.exists(self.db_path):
            return {'status': 'scope_index_unavailable', 'intent': intent, 'operator': operator, 'scope_anchor': None, 'retrieval_allowed': False, 'decision_applied': False}
        with closing(self._connect(readonly=True)) as conn:
            rows = conn.execute('SELECT * FROM scope_anchors ORDER BY LENGTH(entity_text) DESC, entity_key, arc_key').fetchall()
        matches: list[dict[str, Any]] = []
        occupied: list[tuple[int, int]] = []
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        entity_order: list[str] = []
        for row in rows:
            entity_key = str(row['entity_key'])
            if entity_key not in grouped:
                entity_order.append(entity_key)
            grouped[entity_key].append(row)
        entity_order.sort(key=lambda key: (-len(str(grouped[key][0]['entity_text'])), key))
        folded_text = _regex_casefold(text)
        for entity_key in entity_order:
            mappings = grouped[entity_key]
            entity = str(mappings[0]['entity_text'])
            for start, end in _term_spans(text, entity, limit=4, _folded_text=folded_text):
                if any((not (end <= old_start or start >= old_end) for old_start, old_end in occupied)):
                    continue
                occupied.append((start, end))
                for row in mappings:
                    matches.append({'entity': text[start:end], 'start_offset': start, 'end_offset': end, 'arc_key': str(row['arc_key']), 'source_kind': str(row['source_kind']), 'trusted': bool(row['trusted']), 'arc_count': int(row['arc_count'])})
                break
        trusted_arc_keys = sorted({row['arc_key'] for row in matches if row['trusted']})
        residue = text
        for start, end in sorted({(row['start_offset'], row['end_offset']) for row in matches}, reverse=True):
            residue = residue[:start] + ' ' * (end - start) + residue[end:]
        residue = ' '.join(residue.split())
        if len(trusted_arc_keys) == 1:
            chosen = next((row for row in matches if row['trusted'] and row['arc_key'] == trusted_arc_keys[0]))
            scope_anchor = {'entity': chosen['entity'], 'arc_key': chosen['arc_key'], 'source_kind': chosen['source_kind']}
            return {'status': 'scoped_recall' if intent != 'none' else 'scope_only', 'intent': intent, 'operator': operator, 'intent_view': residue, 'scope_anchor': scope_anchor, 'matches': matches, 'retrieval_allowed': True, 'decision_applied': False}
        if matches:
            return {'status': 'ambiguous_scope', 'intent': intent, 'operator': operator, 'intent_view': residue, 'scope_anchor': None, 'candidate_arc_keys': sorted({row['arc_key'] for row in matches}), 'matches': matches, 'retrieval_allowed': True, 'decision_applied': False}
        return {'status': 'insufficient_scope' if intent != 'none' else 'no_scope', 'intent': intent, 'operator': operator, 'intent_view': text, 'scope_anchor': None, 'matches': [], 'retrieval_allowed': False, 'decision_applied': False}
