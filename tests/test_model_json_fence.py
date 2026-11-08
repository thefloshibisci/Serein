import asyncio
import json

import httpx
import pytest

from serein import model_runtime as runtime


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_json_completion_unwraps_fence_after_protocol_conversion(monkeypatch, protocol):
    content = '```json\n{"event_draft":"line one\\nline two", "evidence_sufficient":true}\n```'
    upstream = ({'content': [{'type': 'text', 'text': content}], 'stop_reason': 'end_turn'}
                if protocol == 'anthropic' else {'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return httpx.Response(200, json=upstream)
    monkeypatch.setattr(runtime.httpx, 'AsyncClient', Client)
    model = {'protocol': protocol, 'model': 'synthetic', 'base_url': 'https://example.invalid/v1'}
    payload = {'messages': [{'role': 'user', 'content': 'synthetic'}], 'response_format': {'type': 'json_object'}}
    response = asyncio.run(runtime.complete(model, payload))
    assert json.loads(response['choices'][0]['message']['content']) == {
        'event_draft': 'line one\nline two', 'evidence_sufficient': True}


@pytest.mark.parametrize('content', [
    'prose\n```json\n{}\n```', '```json\n{}\n```\ntrailer',
    '```json\n{"broken":}\n```', '```json\n[]\n```',
    '```json\n{}\n```\n```json\n{}\n```', '```json\n{}',
])
def test_ambiguous_or_invalid_responses_are_not_repaired(content):
    response = {'choices': [{'message': {'content': content}}]}
    assert runtime.normalize_json_response({'response_format': {'type': 'json_object'}}, response) is response


def test_chat_and_truncated_outputs_stay_unchanged_and_source_is_preserved():
    response = {'choices': [{'message': {'content': '```json\n{}\n```'}}]}
    assert runtime.normalize_json_response({}, response) is response
    normalized = runtime.normalize_json_response({'response_format': {'type': 'json_object'}}, response)
    assert normalized['choices'][0]['message']['content'] == '{}'
    assert response['choices'][0]['message']['content'].startswith('```')
    response['choices'][0]['finish_reason'] = 'length'
    assert runtime.normalize_json_response({'response_format': {'type': 'json_object'}}, response) is response
