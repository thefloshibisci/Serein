from serein.model_runtime import request_for


def _model(base_url, protocol='openai'):
    return {'base_url': base_url, 'model': 'kimi-k2.7-code', 'api_key': 'k', 'protocol': protocol}


def test_user_agent_always_sent_without_opencode_session_elsewhere():
    _, headers, _ = request_for(_model('https://api.deepseek.com/v1'), {'messages': []})
    assert headers['User-Agent'].startswith('serein/')
    assert headers['Authorization'] == 'Bearer k'
    assert 'x-opencode-session' not in headers


def test_opencode_uses_window_id_as_session():
    _, headers, _ = request_for(_model('https://opencode.ai/inference/go/openai/v1'), {'messages': []}, window_id='w-1')
    assert headers['x-opencode-session'] == 'w-1'


def test_opencode_background_session_is_stable():
    m = _model('https://opencode.ai/inference/go/openai/v1')
    first = request_for(m, {'messages': []})[1]['x-opencode-session']
    second = request_for(m, {'messages': []})[1]['x-opencode-session']
    assert first == second and first.startswith('serein-task-')


def test_opencode_anthropic_branch_keeps_api_key():
    _, headers, _ = request_for(_model('https://opencode.ai/inference/go/anthropic/v1', 'anthropic'),
                                {'messages': [{'role': 'user', 'content': 'hi'}]}, window_id='w-2')
    assert headers['x-api-key'] == 'k' and headers['x-opencode-session'] == 'w-2'
