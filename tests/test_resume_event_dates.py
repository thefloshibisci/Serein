"""Synthetic chronology checks; reading never rewrites imported memories."""
import json

import pytest
from fastapi.testclient import TestClient

from test_public_features import settings
from serein.api.http import create_app
from serein.application import Application
from serein.core.store import Store, Conflict
from serein.deployment import save_settings
from serein.extensions.handoff import resume_text
from serein.extensions.handoff_dates import event_time


def configure(settings, **extra):
    save_settings(settings.database, {'features': {'resume': True}, 'resume': {
        'latest_shadow': False, 'favorite_scenes': False, 'selected_memories': False,
        'recent_events': True, 'recent_originals': False, 'pending_originals': False, **extra}})


def test_backfilled_old_event_does_not_displace_ten_newer_sources(settings):
    configure(settings)
    with Store(settings.database) as store:
        for i in range(1, 13):
            store.create(f'recent-{i:02}', 'event', 'Synthetic recent record', 'Recent source',
                         metadata={'source_ended_at': f'2026-11-{i:02}T10:00:00+08:00'},
                         created_at='2026-11-12T12:00:00Z')
        store.create('backfilled', 'event', '2026-08-30 old authored record', 'Old source',
                     metadata={'source_started_at': '2026-08-30T10:29:20', 'source_ended_at': '2026-08-30T10:29:20'},
                     created_at='2099-11-08T03:59:38Z')
        # Undated imports and title strings cannot outrank real source clocks.
        store.create('unknown', 'event', '2099-01-01 is just prose', 'No source clock', created_at='2099-01-01T00:00:00Z')
        before = list(store.conn.iterdump())
    page = Application(settings).contributions.tools['resume']()
    assert page['event_ids'] == [f'recent-{i:02}' for i in range(3, 13)]
    assert page['total_recent_events'] == 10
    assert all(i['date_basis']=='source_records' for i in page['items'])
    with Store(settings.database, read_only=True) as store:
        assert list(store.conn.iterdump()) == before


@pytest.mark.parametrize('meta,created,expected,basis', [
    ({'source_ended_at':'2026-11-01T23:30:00-05:00'}, '2099-01-01', '2026-11-02', 'source_records'),
    ({'source_ended_at':'2026-11-01T20:00:00'}, '2099-01-01', '2026-11-02', 'source_records'),
    ({'local_date':'2026-10-01','local_end_date':'2026-11-05'}, '2099-01-01', '2026-11-05', 'source_records'),
    ({'source_ended_at':'invalid','source_started_at':'2026-11-03T12:00:00+08:00'}, '2099-01-01', '2026-11-03', 'source_records'),
    ({'source_ended_at':'invalid','local_date':'2026-02-31'}, '2026-11-08T23:00:00Z', '2026-11-09', 'stored_at'),
    ({}, 'invalid', '', 'unknown'),
])
def test_timezones_source_end_date_precision_and_invalid_fallback(meta, created, expected, basis):
    _, result = event_time(meta, created)
    assert result['date'] == expected and result['date_basis'] == basis


def test_selection_sorts_actual_instants_and_skips_unreadable_records(settings):
    configure(settings)
    with Store(settings.database) as store:
        for key, source in [('earlier','2026-11-08T10:30:00+08:00'), ('later','2026-11-08T03:00:00Z')]:
            store.create(key, 'event', key, key, metadata={'source_ended_at':source})
        store.create('locked','event','Secret','Secret',metadata={'source_ended_at':'2099-01-01T00:00:00Z','locked':True})
        store.create('archived','event','Archived','Archived',lifecycle='archived',metadata={'source_ended_at':'2099-01-01T00:00:00Z'})
        store.create('superseded','event','Superseded','Old version',lifecycle='superseded',metadata={'source_ended_at':'2099-01-01T00:00:00Z'})
        store.create('unknown','event','Unknown','Fallback',created_at='2099-01-01T00:00:00Z')
    page = Application(settings).contributions.tools['resume']()
    assert page['event_ids'] == ['unknown','earlier','later']
    assert page['items'][0]['date_basis'] == 'stored_at'


def test_http_mcp_text_and_selected_event_preserve_storage_and_source_dates(settings):
    configure(settings, selected_memories=True, selected_ids=['old'])
    with Store(settings.database) as store:
        store.create('old','event','Old synthetic record','Original body',created_at='2026-11-08T03:59:38Z',
                     metadata={'source_ended_at':'2026-08-30T10:29:20+08:00'})
    client = TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    response = client.post('/v1/extensions/resume',json={})
    assert response.status_code == 200, response.text
    page = response.json()
    assert len(page['items']) == 1  # Explicit selection and recent list remain deduplicated.
    item = page['items'][0]
    assert item['date']=='2026-08-30' and item['created_at']=='2026-11-08T03:59:38Z'
    assert 'date_basis: "source_records"' in resume_text(page)
    assert 'date: "2026-08-30"' in resume_text(page)


def test_date_change_invalidates_pagination(settings):
    configure(settings)
    with Store(settings.database) as store:
        store.create('long','event','Long','Synthetic. '*2000,metadata={'source_ended_at':'2026-11-07T12:00:00Z'})
    resume = Application(settings).contributions.tools['resume']
    page = resume()
    assert page['has_more']
    with Store(settings.database) as store:
        old = store.read('long')
        store.revise('long',expected_revision=1,title=old['title'],body_md=old['body_md'],
                     metadata={'source_ended_at':'2026-11-08T12:00:00Z'})
    with pytest.raises(Conflict,match='changed; restart'):
        resume(cursor=page['next_cursor'])
