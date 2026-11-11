"""Continuation state matching the verified Bridge message-level Track router."""
import json
import re
from datetime import datetime, timedelta, timezone
from ..core.store import digest, encode
from ..source_time import source_datetime, message_datetime, source_julianday
from . import pipeline_latest as latest


def scope_for(source, session):
    return digest(encode([source, session]))[:20]


def next_ordinal(scope, cards):
    numbers = [int(match[1]) for card in cards if
               (match := re.fullmatch(r'session_' + re.escape(str(scope)) + r'_track_([0-9]+)', card['track_id']))]
    return max(numbers, default=0) + 1


def assert_before(store, cards, before_id):
    """Pre-routing cards must precede both the raw ID and the source clock."""
    from . import pipeline as p
    current=store.conn.execute('SELECT created_at,source FROM raw_events WHERE id=?',(before_id,)).fetchone()
    if current is None:raise p.RoutingRecoveryError('Pre-routing source anchor is missing')
    reference=source_datetime(current[0],current[1])
    for card in cards:
        anchors=card.get('recent_source_message_ids')
        if not isinstance(anchors,list) or not anchors:
            raise p.RoutingRecoveryError('Pre-routing Track has no proved source anchors: '+card['track_id'])
        for key in anchors:
            row=store.conn.execute('SELECT created_at,source FROM raw_events WHERE id=?',(key,)).fetchone() if type(key) is int else None
            if row is None or key>=before_id or source_datetime(row[0],row[1])>reference:
                raise p.RoutingRecoveryError('Pre-routing Track contains future source material: '+card['track_id'])


def _historical_card(store, track_id, original, before_id, reference, message, cache, receipts):
    """Choose only proved prose from before this new routing boundary.

    A late reply may start inside an accepted frame that also read its earlier
    question. That frame's resulting card is future context for this start. Its
    verified input card is usable; a Track first created by that frame is simply
    not an earlier candidate. Missing or damaged history is not this case.
    """
    from . import pipeline as p
    from .pipeline_recovery import SOURCE_STATES, _frames, _message_key, assert_published_frame_ledger
    provenance=store.conn.execute('SELECT * FROM pipeline_route_provenance WHERE raw_id=?',
                                   (original['id'],)).fetchone()
    if provenance is None:
        raise p.RoutingRecoveryError('No bounded historical pre-routing Track frame: '+track_id)
    if json.loads(provenance['route_json']) != json.loads(original['route_json']):
        raise p.RoutingRecoveryError('pre-routing route disagrees with its recorded producer: '+track_id)
    producer=store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?',
                               (provenance['batch_id'],)).fetchone()
    if producer is None:
        raise p.RoutingRecoveryError('pre-routing Track producer is missing: '+track_id)
    if producer['id'] not in cache:
        frames=_frames(store.conn.execute('PRAGMA database_list').fetchone()[2],dict(producer))
        raw_ids=list({m['id'] for f in frames for m in f['messages']});raw_rows={}
        for offset in range(0,len(raw_ids),400):
            chunk=raw_ids[offset:offset+400];marks=','.join('?' for _ in chunk)
            raw_rows.update((row['id'],row) for row in store.conn.execute('SELECT * FROM raw_events WHERE id IN ('+marks+')',chunk))
        for frame in frames:
            for frozen in frame['messages']:
                raw=raw_rows.get(frozen['id'])
                if raw is None or _message_key(message(raw)) != _message_key(frozen):
                    raise p.RoutingRecoveryError('pre-routing producer source changed: '+str(frozen['id']))
        cache[producer['id']]=frames
    frames=cache[producer['id']]
    linked=[frame for frame in frames if original['id'] in {m['id'] for m in frame['messages']}]
    if len(linked)!=1:
        raise p.RoutingRecoveryError('pre-routing Track has no unique accepted producer frame: '+track_id)
    linked=linked[0]
    assignment=next((a for a in linked['assignments'] if a['source_message_id']==original['id']),None)
    if assignment != json.loads(provenance['route_json']):
        raise p.RoutingRecoveryError('pre-routing route disagrees with accepted frame: '+track_id)
    crosses_start=(max(m['id'] for m in linked['messages'])>=before_id
                   or any(message_datetime(m)>reference for m in linked['messages']))
    if crosses_start:
        # A valid overlapping frame is different from a hole in the durable
        # routing ledger. Never use omission/replay to silently recreate deleted
        # assignments for still-unprocessed members of an accepted frame.
        assert_published_frame_ledger(store,linked)
    historical=None;selected=None;action='use_prior_frame';prefix_before=True
    for frame in frames:
        bounded=(max(m['id'] for m in frame['messages'])<before_id
                 and all(message_datetime(m)<=reference for m in frame['messages']))
        if prefix_before and bounded:
            prior=next((card for card in frame['tracks'] if card['track_id']==track_id),None)
            if prior is not None:historical=prior;selected=frame
        elif prefix_before and frame is linked and frame.get('source_proof_complete'):
            prior=next((card for card in frame.get('pre_tracks',[]) if card['track_id']==track_id),None)
            if prior is not None:
                assert_before(store,[prior],before_id)
                historical=prior;selected=frame;action='use_pre_frame_track'
        prefix_before=prefix_before and bounded
        if frame is linked:break
    if historical is not None:
        assert_before(store,[historical],before_id)
        # Ordinary exact history needs no special receipt. Boundary fallbacks
        # keep IDs and proof bounds in the new frozen input, never model prose.
        if action=='use_pre_frame_track' or not prefix_before:
            receipts.append({'action':action,'track_id':track_id,'producer_batch_id':producer['id'],
                'producer_job_id':selected['job_id'],'before_source_message_id':before_id,
                'frame_source_message_ids':[m['id'] for m in linked['messages']],
                'reason':'accepted_frame_crosses_routing_start'})
        return historical
    if (producer['status'] in SOURCE_STATES and linked.get('source_proof_complete')
            and track_id in linked.get('created_track_ids',[]) and not prefix_before):
        receipts.append({'action':'omit_future_track','track_id':track_id,
            'producer_batch_id':producer['id'],'producer_job_id':linked['job_id'],
            'before_source_message_id':before_id,
            'frame_source_message_ids':[m['id'] for m in linked['messages']],
            'reason':'track_created_in_accepted_frame_crossing_routing_start'})
        return None
    raise p.RoutingRecoveryError('No bounded historical pre-routing Track frame: '+track_id)


def load_tracks(store, source, session, before_id, message, *, lookback_days=3, history_receipts=None):
    """Show tracks with proven routed activity in the preceding N days.

    Public API clients need not supply a window identity. The current raw
    message supplies the clock; persisted route receipts supply activity.
    """
    if type(lookback_days) is not int or not 1<=lookback_days<=365:
        raise ValueError('Track lookback must be an integer between 1 and 365 days')
    current=store.conn.execute('SELECT created_at,metadata_json FROM raw_events WHERE id=? AND source=?',
                               (before_id,source)).fetchone()
    if current is None:raise ValueError('Track routing anchor is missing')
    reference=source_datetime(current['created_at'],source)
    lower=reference-timedelta(days=lookback_days)
    metadata=json.loads(current['metadata_json'] or '{}')
    boundary=(metadata.get('runtime',''),metadata.get('workspace_root',''))
    scope=scope_for(source,session)
    from .pipeline_recovery import history_reset_cutoff
    cutoff=history_reset_cutoff(store,scope,before_id)
    anchors={}
    clock=source_julianday('r')
    rows=store.conn.execute('SELECT r.*,p.route_json FROM pipeline_routes p JOIN raw_events r ON r.id=p.raw_id '
        f'WHERE r.source=? AND r.id<? AND ({clock})>=julianday(?) '
        f'AND ({clock})<=julianday(?) ORDER BY r.id DESC',
        (source,before_id,lower.isoformat(),reference.isoformat()))
    for row in rows:
        if row['id'] < cutoff:continue
        stamp=source_datetime(row['created_at'],source)
        if not lower<=stamp.astimezone(timezone.utc)<=reference:continue
        meta=json.loads(row['metadata_json'] or '{}')
        if (meta.get('runtime',''),meta.get('workspace_root',''))!=boundary:continue
        route=json.loads(row['route_json'])
        for track_id in [route['primary_track_id'],*route['context_track_ids']]:
            previous=anchors.get(track_id)
            if previous is None or (stamp,row['id'])>(previous[0],previous[1]['id']):
                anchors[track_id]=(stamp,row)
    cards=[];history_cache={};receipts=[]
    for row in store.conn.execute('SELECT id,card_json,scope FROM pipeline_tracks ORDER BY id'):
        anchor=anchors.get(row['id'])
        if anchor is None:continue
        card=json.loads(row['card_json'])
        card.setdefault('last_session_id',row['scope'])
        match=re.fullmatch(r'session_(.+)_track_[0-9]+',card['track_id'])
        card.setdefault('origin_session_id',match[1] if match else row['scope'])
        original=anchor[1]
        try:assert_before(store,[card],before_id);bounded=True
        except ValueError:bounded=False
        # Never re-anchor prose that has read material beyond this routing start.
        # Recover the historical card from its accepted frame; a partial frame
        # cannot provide an earlier throughline, even when its raw route survives.
        has_provenance=store.conn.execute('SELECT 1 FROM pipeline_route_provenance WHERE raw_id=?',
                                          (original['id'],)).fetchone()
        if not bounded or has_provenance:
            card=_historical_card(store,card['track_id'],original,before_id,reference,message,
                                  history_cache,receipts)
            if card is None:continue
        cards.append(card)
    if history_receipts is not None:history_receipts.extend(receipts)
    all_ids=[{'track_id':row[0]} for row in store.conn.execute('SELECT id FROM pipeline_tracks')]
    return cards,next_ordinal(scope,all_ids)


def parked(cards):
    return [{**card, 'status': 'parked'} if card.get('status') == 'active' else dict(card) for card in cards]


def update_cards(cards, assignments, updates, messages, scope):
    current = {card['track_id']: card for card in parked(cards)}
    by_id = {m['id']: m for m in messages}
    for update in updates:
        key = update['track_id']
        primary = [a['source_message_id'] for a in assignments if a['primary_track_id'] == key]
        context = [a['source_message_id'] for a in assignments if key in a['context_track_ids']]
        anchor = (primary or context)[-1]
        previous = current.get(key, {})
        current[key] = {**previous, **update, 'origin_session_id': previous.get('origin_session_id', scope),
                        'last_session_id': scope, 'recent_source_message_ids': [anchor],
                        'recent_turns': latest.transcript_payload([by_id[anchor]])}
    return list(current.values())


def persist(conn, cards, scope, *, preserve_newer=False):
    for card in cards:
        if preserve_newer:
            row = conn.execute('SELECT card_json FROM pipeline_tracks WHERE id=?', (card['track_id'],)).fetchone()
            if row:
                previous = [key for key in json.loads(row[0]).get('recent_source_message_ids', []) if type(key) is int]
                incoming = [key for key in card.get('recent_source_message_ids', []) if type(key) is int]
                if previous and (not incoming or max(incoming) < max(previous)):
                    continue
        # Unused parked cards still belong to the last window that referenced
        # them. Saving a different batch must not move their continuation scope.
        conn.execute('INSERT INTO pipeline_tracks VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET '
                     'scope=excluded.scope,card_json=excluded.card_json',
                     (card['track_id'], card.get('last_session_id', scope), encode(card)))
