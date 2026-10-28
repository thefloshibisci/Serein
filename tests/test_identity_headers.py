import httpx
import pytest
from serein.model_runtime import request_for


def _model(base_url, protocol='openai'):
    return {'base_url': base_url, 'model': 'kimi-k2.7-code', 'api_key': 'k', 'protocol': protocol}


def test_user_agent_always_sent_without_opencode_session_elsewhere():
    _, headers, _ = request_for(_model('https://api.deepseek.com/v1'), {'messages': []})
    assert headers['User-Agent'].startswith('serein/')
    assert headers['Authorization'] == 'Bearer k'
    assert 'x-opencode-session' not in headers


def test_opencode_uses_window_id_as_session():
    model = _model('https://opencode.ai/inference/go/openai/v1')
    _, headers, _ = request_for(model, {'messages': []}, window_id='w-1')
    session = headers['x-opencode-session']
    assert session.startswith('serein-window-')
    assert session == request_for(model, {'messages': [{'role':'user','content':'next'}]}, window_id='w-1')[1]['x-opencode-session']
    assert session != request_for(model, {'messages': []}, window_id='w-2')[1]['x-opencode-session']


def test_opencode_background_session_is_stable():
    m = _model('https://opencode.ai/inference/go/openai/v1')
    first = request_for(m, {'messages': []})[1]['x-opencode-session']
    second = request_for(m, {'messages': []})[1]['x-opencode-session']
    assert first == second and first.startswith('serein-task-')


def test_opencode_anthropic_branch_keeps_api_key():
    _, headers, _ = request_for(_model('https://opencode.ai/inference/go/anthropic/v1', 'anthropic'),
                                {'messages': [{'role': 'user', 'content': 'hi'}]}, window_id='w-2')
    assert headers['x-api-key'] == 'k'
    assert headers['x-opencode-session'] == request_for(_model('https://opencode.ai/inference/go/openai/v1'),
                                                      {'messages': []}, window_id='w-2')[1]['x-opencode-session']


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('window', ['中文会话', 'a' * 200, 'line\r\nbreak\x00'])
def test_window_identity_is_bounded_header_safe_and_not_sent_verbatim(protocol, window):
    model = _model('https://opencode.ai/inference/go/v1', protocol)
    url, headers, body = request_for(model, {'messages': []}, window_id=window)
    request = httpx.Request('POST', url, headers=headers, json=body)
    session = request.headers['x-opencode-session']
    assert session.isascii() and len(session) <= 128
    assert all(33 <= ord(char) <= 126 for char in session)
    assert window not in session


def test_distinct_long_windows_do_not_share_upstream_session():
    model = _model('https://opencode.ai/inference/go/openai/v1')
    first = request_for(model, {'messages': []}, window_id='a' * 128 + 'one')[1]['x-opencode-session']
    second = request_for(model, {'messages': []}, window_id='a' * 128 + 'two')[1]['x-opencode-session']
    assert first != second


@pytest.mark.parametrize('url', ['https://opencode.ai.example/v1', 'https://example.test/opencode.ai',
                               'https://opencode.ai@example.test/v1'])
def test_other_hosts_never_receive_opencode_session(url):
    _, headers, _ = request_for(_model(url), {'messages': []}, window_id='private-window')
    assert 'x-opencode-session' not in headers
