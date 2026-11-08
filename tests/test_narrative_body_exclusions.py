"""A synthetic reproduction of withdrawn material disappearing on body save."""
from test_live_clients import live
from test_live_narratives import seed
from serein.compat.narratives import narrative_transaction


def test_body_only_save_preserves_every_withdrawal_kind(live):
    settings, client = live
    event, scene = seed(client, settings)
    excluded = {'excluded_scene_ids': ['withdrawn-scene'], 'excluded_event_ids': ['withdrawn-event'],
                'excluded_diary_ids': [710], 'excluded_darkroom_ids': [711],
                'excluded_upload_ids': ['withdrawn-upload']}
    with narrative_transaction(settings.database, write=True) as rolls:
        before = rolls.read('narrative_test')
        result = rolls.publish(narrative_id='narrative_test', document=before['full_document'],
            expected_revision=before['revision'], title=before['title'], arc_key=before['arc_key'],
            source_scene_ids=[scene], source_event_ids=[event], **excluded)
        assert result['status'] == 'updated', result
        current = rolls.read('narrative_test')
        result = rolls.save_body('narrative_test', 'A revised synthetic body.',
            expected_revision=current['revision'], expected_document_sha256=current['document_sha256'])
        assert result['status'] == 'updated', result
        after = rolls.read('narrative_test')
    for key, values in excluded.items():
        assert after[key] == values
    assert after['linked_scene_ids'] == [scene] and after['linked_event_ids'] == [event]
