import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from serein.api.mcp import create_server
from serein.application import Application
from serein.compat.diaries import Diaries
from serein.config import Settings
from serein.core.store import Store


PRIVATE = False


@pytest.fixture(params=['legacy'] if PRIVATE else ['legacy', 'core'])
def notebook(tmp_path, request):
    settings = Settings(tmp_path / 'synthetic.db', writable=True)
    with Store(settings.database):
        pass
    if request.param == 'legacy':
        Diaries(settings.database, initialize=True)
    server = create_server(Application(settings), private=PRIVATE)

    def call(name='read_diary', **args):
        result = asyncio.run(server.call_tool(name, args))
        if isinstance(result, tuple):
            return result[1]
        assert len(result) == 1
        if name == 'read_diary':
            return result[0].text
        return json.loads(result[0].text)

    def create(body='Synthetic body', title='Synthetic title', date='2026-09-18', **extra):
        return call('write_diary', content=body, title=title, date=date, **extra)['id']

    return settings, server, call, create


def ids(text):
    return [int(value) for value in re.findall(r'^diary_id: (\d+)$', text, re.M)]


def test_mcp_author_is_fixed(notebook):
    _, server, call, _ = notebook
    schema = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == 'write_diary')
    assert 'author' not in schema.inputSchema['properties']
    assert call('write_diary', content='AI diary')['author'] == 'ai'
    with pytest.raises(Exception):
        call('write_diary', content='Invalid author', author='Haven')


def test_default_directory_is_bounded_and_paginated(notebook):
    _, server, call, create = notebook
    keys = [create(body=f'Opening {i}. ' + 'long prose ' * 600 + ' END_SENTINEL') for i in range(7)]
    call('comment_diary', diary_id=keys[-1], content='COMMENT_SENTINEL')
    first = call()
    assert ids(first) == list(reversed(keys))[:5]
    assert first.startswith('Diary directory\ncount: 5\n')
    assert 'next_offset: 5' in first and 'has_more: true' in first
    assert 'END_SENTINEL' not in first and 'COMMENT_SENTINEL' not in first
    assert 'body:' not in first and '[diary_list]' not in first
    assert all(len(line.removeprefix('excerpt: ')) <= 150 for line in first.splitlines() if line.startswith('excerpt: '))
    second = call(offset=5)
    assert ids(second) == list(reversed(keys))[5:]
    assert 'has_more: false' in second and 'next_offset:' not in second
    assert 'count: 0' in call(offset=50)
    schema = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == 'read_diary')
    assert schema.inputSchema['properties']['limit']['default'] == 5
    assert {'query', 'offset'} <= schema.inputSchema['properties'].keys()
    assert schema.outputSchema is None


def test_search_title_before_body_excerpt_and_date_filter(notebook):
    _, _, call, create = notebook
    title = create(title='旧日的雨伞', body='旧日的正文', date='2026-09-17')
    older = create(body='前文 ' * 250 + '雨伞在门边。' + ' 后文' * 250)
    newer = create(body='还有一把雨伞', date='2026-09-19')
    create(body='完全不相关')
    first = call(query='雨伞', limit=2)
    assert ids(first) == [title, newer]
    second = call(query='雨伞', limit=2, offset=2)
    assert ids(second) == [older] and 'excerpt: …' in second
    assert '雨伞在门边。' in second
    assert ids(call(query='雨伞', date='2026-09-18')) == [older]
    assert ids(call(date='2026-09-17')) == [title]
    assert 'body:' not in call(date='2026-09-17')
    assert 'count: 0' in call(query='不存在的词')
    assert ids(call(query='   ')) == ids(call())
    literal = create(body='literal 100%_ done')
    assert ids(call(query='%_')) == [literal]
    case = create(body='Mixed CASE Query')
    assert ids(call(query='case query')) == [case]
    assert 'count: 0' in call(query="' OR 1=1 --")


def test_only_id_returns_complete_body_and_comments(notebook):
    _, _, call, create = notebook
    body = 'Long original body.\n' * 800 + 'END_SENTINEL'
    key = create(body=body)
    call('comment_diary', diary_id=key, content='Complete comment')
    text = call(diary_id=key)
    assert text.startswith('Diary\n') and body in text and 'Complete comment' in text
    assert '[diary_list]' not in text and 'excerpt:' not in text
    assert 'count: 0' in call(diary_id=key, date='2026-09-19')
    assert 'count: 0' in call(diary_id=999999)


def test_search_cannot_disclose_locked_or_deleted_matches(notebook):
    _, _, call, create = notebook
    lock = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    sealed = create(title='Secret title', body='Secret body', unlock_at=lock)
    deleted = create(title='Secret deleted', body='Secret deleted body')
    call('delete_diary', diary_id=deleted)
    visible = create(title='Readable', body='Secret visible body')
    assert ids(call(query='Secret', limit=1)) == [visible]
    assert 'has_more: false' in call(query='Secret', limit=1)
    assert ids(call()) == [visible]
    assert 'Secret body' not in call(diary_id=sealed)
    assert 'Secret deleted body' not in call(diary_id=deleted)


def test_unlock_boundary_and_literal_keyword_excerpts(notebook):
    settings, _, call, create = notebook
    key = create(body='Short start ' + 'padding ' * 500 + 'needle[42] tail')
    text = call(query='needle[42]')
    assert ids(text) == [key] and 'needle[42]' in text
    with Store(settings.database) as store:
        legacy = store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='diaries'").fetchone()
        table = 'diaries' if legacy else 'diary_entries'
        store.conn.execute(f'UPDATE {table} SET unlock_at=? WHERE id=?', ('broken-timestamp', key))
        store.conn.commit()
    assert 'count: 0' in call(query='needle[42]')
    with Store(settings.database) as store:
        store.conn.execute(f'UPDATE {table} SET unlock_at=? WHERE id=?',
                           ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), key))
        store.conn.commit()
    assert ids(call(query='needle[42]')) == [key]


@pytest.mark.parametrize('args', [
    {'limit': 0}, {'limit': 21}, {'offset': -1}, {'diary_id': 0}, {'diary_id': -1},
    {'diary_id': 1, 'query': 'word'}, {'diary_id': 1, 'offset': 1}, {'date': 'not-a-date'},
])
def test_invalid_selectors_do_not_fall_back_to_bulk_read(notebook, args):
    _, _, call, _ = notebook
    with pytest.raises(Exception):
        call(**args)
