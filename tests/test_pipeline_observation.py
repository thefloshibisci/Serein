import logging
from serein.pipeline_observation import report


def test_returned_hold_is_logged_once_until_it_changes(caplog):
    logger=logging.getLogger('synthetic.pipeline')
    caplog.set_level(logging.INFO,logger=logger.name)
    hold={'status':'blocked','blocked_scopes':[{'batch_id':'synthetic','reason':'missing proof'}]}
    previous=report(hold,None,logger)
    assert len(caplog.records)==1 and 'missing proof' in caplog.text
    assert report(hold,previous,logger)==previous
    assert len(caplog.records)==1
    changed={'status':'needs_repair','batch_id':'synthetic','reason':'changed evidence'}
    previous=report(changed,previous,logger)
    assert len(caplog.records)==2
    assert report({'status':'processed'},previous,logger) is None
    assert 'advanced after a hold' in caplog.text


def test_quiet_statuses_do_not_produce_warning_spam(caplog):
    logger=logging.getLogger('synthetic.pipeline')
    previous=None
    for state in ('current','settled_today','waiting_settlement_window','auto_paused'):
        previous=report({'status':state},previous,logger)
    assert not caplog.records
