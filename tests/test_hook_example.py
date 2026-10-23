"""The public Hook example must distinguish prepared cards from actual delivery."""
import io
import json
import pytest

from examples.hook_host import LocalDeliveries, PreparedTurn, SereinHook
from test_public_settings import deployment


@pytest.fixture
def hook_api(deployment, monkeypatch):
    settings, client = deployment
    current = {'client': client}
    calls = []

    def recall(self, query, **options):
        calls.append(options)
        selected = [] if 'scene:synthetic' in options['delivered_ids'] else ['scene:synthetic']
        return {'selected_refs': selected, 'context': 'Synthetic memory' if selected else '', 'cards': []}

    def urlopen(request, timeout):
        path = request.full_url.removeprefix('https://serein.example')
        body = json.loads(request.data) if request.data else None
        response = current['client'].request(request.get_method(), path, json=body)
        response.raise_for_status()
        return io.BytesIO(response.content)

    monkeypatch.setattr('serein.application.Services.recall', recall)
    monkeypatch.setattr('examples.hook_host.urlopen', urlopen)
    return settings, current, calls


def test_example_prepares_once_and_acknowledges_only_after_host_success(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        assert timeout == 15
        assert request.get_header("Authorization") == "Bearer synthetic-key"
        path = request.full_url.split("https://serein.example", 1)[1]
        body = json.loads(request.data) if request.data else None
        calls.append((request.get_method(), path, body))
        if path.startswith("/v1/host/deliveries?"):
            result = {"status": "ok", "has_more": False, "items": [
                {"window_id": "chat-001", "reported_by": "serein_chat_proxy", "delivered_ids": ["scene:other"]},
                {"window_id": "chat-001", "reported_by": "host", "delivered_ids": ["scene:old"]},
                {"window_id": "another-window", "reported_by": "host", "delivered_ids": ["scene:unrelated"]},
            ]}
        elif path == "/api/hook/recall":
            result = {"ok": True, "injected": False, "recalled_ids": ["event:new"],
                      "additional_context": "Synthetic memory context"}
        else:
            assert path == "/v1/host/deliveries"
            result = {"status": "recorded", "delivered_ids": body["delivered_ids"]}
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr("examples.hook_host.urlopen", fake_urlopen)
    client = SereinHook("https://serein.example", "synthetic-key")
    source = [{"role": "user", "content": "New question"}]
    turn = client.prepare("chat-001", source)
    assert source == [{"role": "user", "content": "New question"}]
    assert "Synthetic memory context" in turn.messages[-1]["content"]
    assert turn.delivered_ids == ["event:new"]
    assert calls[1][2]["delivered_ids"] == ["scene:old"]
    assert len(calls) == 2  # Looking up memory alone does not record delivery.
    assert client.record_success(turn)["status"] == "recorded"
    assert calls[2][2] == {"receipt_id": turn.receipt_id, "window_id": "chat-001",
                           "delivered_ids": ["event:new"]}


@pytest.mark.parametrize('success', [False, True])
def test_real_history_recovers_only_successful_host_delivery_after_restart(hook_api, success):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.chat_state import record_delivery
    from serein.core.store import Store

    settings, current, calls = hook_api
    record_delivery(settings.database, 'chat-001', 'gateway:synthetic', ['scene:gateway'])
    hook = SereinHook('https://serein.example', 'synthetic')
    messages = [{'role': 'user', 'content': 'Synthetic question'}]
    turn = hook.prepare('chat-001', messages)
    assert calls[-1]['delivered_ids'] == []
    assert 'Synthetic memory' in turn.messages[-1]['content']
    if success:
        # The host received the prepared input and completed; retry its same acknowledgement.
        hook.record_success(turn)
        hook.record_success(turn)
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM host_deliveries').fetchone()[0] == 1 + success
        payloads = [json.loads(row[0]) for row in store.conn.execute('SELECT payload FROM host_deliveries')]
        assert all('reported_by' in row for row in payloads if row['receipt_id'] == 'gateway:synthetic')
        assert all('reported_by' not in row for row in payloads if row['receipt_id'] == turn.receipt_id)

    current['client'] = TestClient(create_app(settings, token='synthetic', live=True),
                                 headers={'Authorization': 'Bearer synthetic'})
    reopened = SereinHook('https://serein.example', 'synthetic')
    history = current['client'].get('/v1/host/deliveries').json()['items']
    assert next(row for row in history if row['receipt_id'] == 'gateway:synthetic')['reported_by'] == 'serein_chat_proxy'
    assert reopened.recent_delivered_ids('chat-001') == (['scene:synthetic'] if success else [])
    retry = reopened.prepare('chat-001', messages)
    assert retry.delivered_ids == ([] if success else ['scene:synthetic'])
    assert reopened.prepare('another-window', messages).delivered_ids == ['scene:synthetic']
    if success:
        assert current['client'].post('/v1/host/deliveries', json={
            'receipt_id': turn.receipt_id, 'window_id': 'chat-001', 'delivered_ids': []}).status_code == 400


def test_host_can_supply_its_own_cooldown_without_reading_server_history(monkeypatch):
    captured = []
    hook = SereinHook('https://serein.example', 'synthetic')

    def request(method, path, body=None):
        assert method == 'POST' and path == '/api/hook/recall'
        captured.append(body['delivered_ids'])
        return {'ok': True, 'recalled_ids': [], 'additional_context': ''}

    monkeypatch.setattr(hook, '_json', request)
    messages = [{'role': 'user', 'content': 'Synthetic question'}]
    hook.prepare('chat-001', messages, delivered_ids=['scene:old', 'scene:old'])
    hook.prepare('chat-001', messages, delivered_ids=[])
    assert captured == [['scene:old'], []]
    with pytest.raises(ValueError):
        hook.prepare('chat-001', messages, delivered_ids=[None])


def test_history_counts_empty_successful_turns_in_the_last_five(hook_api):
    _, current, _ = hook_api
    for number in range(6):
        current['client'].post('/v1/host/deliveries', json={
            'receipt_id': f'host:{number}', 'window_id': 'chat-001',
            'delivered_ids': [f'scene:{number}'] if number % 2 == 0 else []}).raise_for_status()
    hook = SereinHook('https://serein.example', 'synthetic')
    assert hook.recent_delivered_ids('chat-001') == ['scene:4', 'scene:2']


def test_local_receipts_survive_restart_without_serein_history(hook_api, tmp_path):
    _, current, calls = hook_api
    path = str(tmp_path / 'host-deliveries.db')
    deliveries = LocalDeliveries(path)
    hook = SereinHook('https://serein.example', 'synthetic')
    messages = [{'role': 'user', 'content': 'Synthetic question'}]
    turn = hook.prepare('chat-001', messages, delivered_ids=deliveries.recent_delivered_ids('chat-001'))
    # Failure leaves no receipt, so a fresh host can retry the same memory.
    assert LocalDeliveries(path).recent_delivered_ids('chat-001') == []
    assert 'Synthetic memory' in turn.messages[-1]['content']
    deliveries.record_success(turn)
    deliveries.record_success(turn)
    with pytest.raises(ValueError):
        deliveries.record_success(PreparedTurn(turn.window_id, turn.receipt_id, turn.messages, []))
    with pytest.raises(ValueError):
        deliveries.record_success(PreparedTurn('another-window', turn.receipt_id, turn.messages, turn.delivered_ids))
    reopened = LocalDeliveries(path)
    retry = SereinHook('https://serein.example', 'synthetic').prepare(
        'chat-001', messages, delivered_ids=reopened.recent_delivered_ids('chat-001'))
    assert calls[-1]['delivered_ids'] == ['scene:synthetic'] and retry.delivered_ids == []
    assert reopened.recent_delivered_ids('another-window') == []
    assert current['client'].get('/v1/host/deliveries').json()['items'] == []
    # Empty successful turns age out an older memory; idempotent retries do not.
    for number in range(5):
        empty = PreparedTurn('chat-001', f'empty:{number}', messages, [])
        reopened.record_success(empty)
        reopened.record_success(empty)
    assert LocalDeliveries(path).recent_delivered_ids('chat-001') == []
