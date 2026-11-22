import base64
import json
import pytest
from fastapi.testclient import TestClient
from serein.api.http import create_app
from serein.api.appearance import MAX_REQUEST_BYTES, PREFIX
from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store

PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7L8AAAAASUVORK5CYII='
GIF = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'

@pytest.fixture
def instance(tmp_path):
    settings = Settings(tmp_path/'memory.db', index=tmp_path/'index.db', writable=True)
    initialize(settings)
    return settings, TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization':'Bearer synthetic'})

def test_images_survive_reopen_and_replace_only_selected_slot(instance):
    settings, client = instance
    assert client.get('/v1/appearance/images').json() == {'images':{}}
    for key in ('user', 'assistant', 'hero'):
        response = client.put('/v1/appearance/images/'+key, json={'data_url':PNG})
        assert response.status_code == 200, response.text
    assert client.put('/v1/appearance/images/user', json={'data_url':GIF}).status_code == 200
    other = TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization':'Bearer synthetic'})
    result = other.get('/v1/appearance/images')
    assert result.headers['cache-control'] == 'no-store'
    assert result.json()['images'] == {'user':GIF,'assistant':PNG,'hero':PNG}
    with Store(settings.database, read_only=True) as store:
        rows = store.conn.execute('SELECT name,value_json FROM background_state WHERE name LIKE ?', (PREFIX+'%',)).fetchall()
        assert len(rows) == 3
        assert json.loads(next(row['value_json'] for row in rows if row['name']==PREFIX+'user')) == GIF
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
    # Pictures are neither model settings nor identity/prompt fields.
    assert 'data:image' not in client.get('/v1/settings').text

@pytest.mark.parametrize('image', ['blob:temporary','data:image/svg+xml;base64,PHN2Zz4=',
    'data:image/png;base64,bm90IGEgcGljdHVyZQ==','data:image/png;base64,invalid!'])
def test_invalid_replacement_preserves_previous_image(instance, image):
    _, client = instance
    assert client.put('/v1/appearance/images/user', json={'data_url':PNG}).status_code == 200
    assert client.put('/v1/appearance/images/user', json={'data_url':image}).status_code == 400
    assert client.get('/v1/appearance/images').json()['images']['user'] == PNG

def test_images_require_auth_and_valid_slot_and_bounded_body(instance):
    settings, client = instance
    anonymous = TestClient(create_app(settings, token='synthetic', live=True))
    assert anonymous.get('/v1/appearance/images').status_code == 401
    assert anonymous.put('/v1/appearance/images/user', json={'data_url':PNG}).status_code == 401
    assert client.put('/v1/appearance/images/unknown', json={'data_url':PNG}).status_code == 422
    assert client.put('/v1/appearance/images/user', content=b'x'*(MAX_REQUEST_BYTES+1)).status_code == 413
    too_big = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'x'*(2*1024*1024)).decode()
    assert client.put('/v1/appearance/images/user', json={'data_url':too_big}).status_code == 400
    assert client.get('/v1/appearance/images').json() == {'images':{}}

def test_read_only_instance_can_read_but_not_replace(instance):
    settings, client = instance
    client.put('/v1/appearance/images/hero', json={'data_url':PNG})
    readonly = Settings(settings.database, index=settings.index, writable=False)
    client = TestClient(create_app(readonly, token='synthetic'), headers={'Authorization':'Bearer synthetic'})
    assert client.get('/v1/appearance/images').json()['images']['hero'] == PNG
    assert client.put('/v1/appearance/images/hero', json={'data_url':GIF}).status_code == 403


def test_light_manifest_and_versioned_independent_images(instance):
    import hashlib
    settings, client = instance
    for key, value in [('user', PNG), ('hero', GIF)]:
        assert client.put('/v1/appearance/images/'+key, json={'data_url':value}).status_code == 200
    manifest = client.get('/v1/appearance/images?metadata=true')
    assert manifest.headers['cache-control'] == 'no-store'
    assert manifest.json()['images'] == {key: hashlib.sha256(value.encode()).hexdigest()
                                         for key, value in [('user', PNG), ('hero', GIF)]}
    assert 'data:image' not in manifest.text
    version = manifest.json()['images']['user']
    assert client.get('/v1/appearance/images/user?version='+version).json() == {'data_url':PNG, 'version':version}
    assert client.get('/v1/appearance/images/assistant').status_code == 404
    anonymous = TestClient(create_app(settings, token='synthetic', live=True))
    assert anonymous.get('/v1/appearance/images/user?version='+version).status_code == 401
    client.put('/v1/appearance/images/user', json={'data_url':GIF})
    assert client.get('/v1/appearance/images/user?version='+version).status_code == 404
    assert client.get('/v1/appearance/images?metadata=true').json()['images']['user'] != version
    assert client.get('/v1/appearance/images/hero').json()['data_url'] == GIF
