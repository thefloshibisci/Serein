import json
import sqlite3

import pytest

pytest.importorskip("httpx")
import httpx

from serein.adapters.embedding import EmbeddingClient
from serein.core import Store
from serein.core.search import build_index


@pytest.fixture
def client(tmp_path, monkeypatch):
    database, index = tmp_path / "canonical.db", tmp_path / "index.db"
    with Store(database):
        pass
    build_index(database, index)
    profile = {"model": "test-model", "provider_host": "embedding.example", "document_instruction": "",
               "query_instruction": "Retrieve memories", "max_chars": 6000}
    with sqlite3.connect(index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
    monkeypatch.setenv("SEREIN_TEST_EMBEDDING_KEY", "test-secret")
    return EmbeddingClient(database, index, "https://embedding.example/v1/embeddings", "SEREIN_TEST_EMBEDDING_KEY")


def test_query_preserves_cached_input_contract_and_dimension(client):
    def handle(request):
        body = json.loads(request.content)
        assert body == {"model": "test-model", "input": "Instruct: Retrieve memories\nQuery: 归航", "encoding_format": "float"}
        assert request.headers["authorization"] == "Bearer test-secret"
        return httpx.Response(200, json={"model": "test-model", "data": [{"embedding": [3, 4]}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        result = client.query("归航", client=http)
    assert result["query"] == "归航" and result["embedding"] == [0.6, 0.8]


def test_provider_failure_and_dimension_mismatch_are_not_silent_fallbacks(client):
    for response in [httpx.Response(401, text="private upstream error test-secret"),
                     httpx.Response(200, json={"model": "test-model", "data": [{"embedding": [1]}]}),
                     httpx.Response(200, json={"model": "different", "data": [{"embedding": [1, 2]}]})]:
        with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as http:
            with pytest.raises(ValueError) as error:
                client.query("归航", client=http)
            assert "test-secret" not in str(error.value)


def test_document_batch_maps_positions_and_rejects_duplicate_indexes(client):
    def handle(request):
        assert json.loads(request.content)["input"] == ["第一篇", "第二篇"]
        return httpx.Response(200, json={"model": "test-model", "data": [
            {"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        assert client.documents(["第一篇", "第二篇"], client=http) == [[1, 0], [0, 1]]
    response = httpx.Response(200, json={"model": "test-model", "data": [{"index": 0, "embedding": [1, 0]}] * 2})
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(ValueError, match="positions"):
            client.documents(["第一篇", "第二篇"], client=http)


def test_instruction_prefix_is_part_of_preserved_truncation_contract(client):
    client.profile['max_chars'] = 40
    assert client.prepare('x' * 60, 'Retrieve memories') == ('Instruct: Retrieve memories\nQuery: ' + 'x' * 60)[:40]
    assert client.prepare('x' * 60, 'Store memories', kind='Document') == ('Instruct: Store memories\nDocument: ' + 'x' * 60)[:40]


def test_siliconflow_vl_large_batch_preserves_global_ownership(client):
    client.endpoint = 'https://api.siliconflow.cn/v1/embeddings'
    client.profile['model'] = 'Qwen/Qwen3-VL-Embedding-8B'
    sizes = []
    def handle(request):
        body = json.loads(request.content)
        texts = body['input']
        sizes.append(len(texts))
        assert len(texts) <= 8
        return httpx.Response(200, json={'model': client.profile['model'], 'data': [
            {'index': i, 'embedding': [1, 0] if int(text) % 2 == 0 else [0, 1]}
            for i, text in reversed(list(enumerate(texts)))]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        result = client.documents([str(i) for i in range(19)], client=http)
    assert sizes == [8, 8, 3]
    assert result == [[1, 0] if i % 2 == 0 else [0, 1] for i in range(19)]


def test_siliconflow_vl_subbatch_still_rejects_duplicate_positions(client):
    client.endpoint = 'https://api.siliconflow.cn/v1/embeddings'
    client.profile['model'] = 'Qwen/Qwen3-VL-Embedding-8B'
    def handle(request):
        count = len(json.loads(request.content)['input'])
        return httpx.Response(200, json={'model': client.profile['model'], 'data': [
            {'index': 0, 'embedding': [1, 0]} for _ in range(count)]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ValueError, match='positions'):
            client.documents([str(i) for i in range(9)], client=http)
