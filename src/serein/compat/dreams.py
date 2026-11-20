"""Existing Dream model rules over retained SQLite works and atomic surfacing."""

import json
import logging
from pathlib import Path
from datetime import datetime, timezone

from ..core.store import Store, digest, encode, now
from .background import germany_config
from .germany.dream_engine import DreamEngine, DreamRecord, EmptyDreamResponse


class Dreams(DreamEngine):
    def _dream_prompt(self):
        from ..deployment import read_settings
        prompt = read_settings(self.database)['dream']['main_prompt'].strip()
        return (prompt+'\n\n以下是本次梦境写作任务：\n\n' if prompt else '') + super()._dream_prompt()

    def _payload_for(self, *args, **kwargs):
        from ..deployment import identity
        self.identity = identity(self.database)
        return super()._payload_for(*args, **kwargs)

    async def run_due(self, bucket_mgr, embedding_engine=None, now=None, raw_event_store=None):
        from ..deployment import read_settings
        self.daily_probability = float(read_settings(self.database)['dream']['daily_probability'])
        if self.enabled and self.auto_enabled:
            with Store(self.database, read_only=True) as store:
                row = store.conn.execute("SELECT name FROM background_state WHERE name GLOB 'dream-draft:*' ORDER BY name LIMIT 1").fetchone()
            if row is not None:
                record = self._finish_draft(row['name'])
                if record is not None:
                    if embedding_engine is not None and getattr(embedding_engine, 'enabled', False):
                        try:
                            await embedding_engine.generate_and_store(record.dream_id, '；'.join(record.metadata['recall_cues']))
                        except Exception:
                            logging.getLogger(__name__).exception('Recovered dream cue indexing failed')
                    return {'status': 'created', 'id': record.dream_id, 'date': record.metadata['local_date']}
        return await super().run_due(bucket_mgr, embedding_engine, now, raw_event_store)

    def __init__(self, settings):
        self.database = settings.database
        super().__init__(germany_config(settings))
        from ..deployment import task_model
        if task_model(settings.database,'dreams'):
            from ..model_runtime import TaskClient
            self.client=TaskClient(settings.database,'dreams')

    @staticmethod
    def _attempt_state(store, date_key):
        events = [row[0] for row in store.conn.execute(
            "SELECT event FROM historical_work_events WHERE event IN "
            "('generation_started','generated','probability_skipped','generation_retryable') "
            "AND json_extract(metadata_json,'$.local_date')=?", (date_key,))]
        if 'generated' in events or store.conn.execute(
                "SELECT 1 FROM historical_works WHERE kind='dream' AND json_extract(metadata_json,'$.local_date')=? LIMIT 1",
                (date_key,)).fetchone():
            return 'generated'
        started = events.count('generation_started')
        if started == 1 and 'generation_retryable' in events:
            return 'retry'
        if started:
            return 'attempted'
        return 'probability_miss' if 'probability_skipped' in events else ''

    def _daily_decision_for_date(self, date_key):
        with Store(self.database, read_only=True) as store:
            return self._attempt_state(store, date_key)

    def _claim_generation_attempt(self, date_key, *, force=False):
        with Store(self.database) as store, store.transaction(immediate=True):
            if not force and self._attempt_state(store, date_key) not in ('', 'retry'):
                return False
            self.log_event(store, 'generation_started', {
                'local_date': date_key, 'started_at': now(), 'manual': force})
        return True

    def _request_failed(self, date_key, error, *, force=False):
        from ..model_runtime import UpstreamError

        status = error.response.status_code if isinstance(error, UpstreamError) else getattr(error, 'status_code', None)
        if not force and (isinstance(error, EmptyDreamResponse) or status == 429):
            self._log_event('generation_retryable', {'local_date': date_key})

    def _publish_dream(self, metadata, body):
        key = 'dream-draft:' + metadata['dream_id']
        with Store(self.database) as store, store.transaction(immediate=True):
            store.conn.execute('INSERT INTO background_state VALUES (?,?)',
                               (key, encode({'metadata': metadata, 'body': body})))
        return self._finish_draft(key) or DreamRecord(metadata, body, Path(metadata['dream_id']))

    def _finish_draft(self, key):
        with Store(self.database) as store, store.transaction(immediate=True):
            row = store.conn.execute('SELECT value_json FROM background_state WHERE name=?', (key,)).fetchone()
            if row is None:
                return None  # Another worker already published it.
            draft = json.loads(row[0])
            metadata = draft['metadata']
            record = self.save_record(store, metadata, draft['body'])
            self.log_event(store, 'generated', {k: metadata[k] for k in ('dream_id', 'generated_at', 'local_date', 'ai_name')})
            store.conn.execute('DELETE FROM background_state WHERE name=?', (key,))
            return record

    @staticmethod
    def record(row):
        return DreamRecord(json.loads(row['metadata_json']), row['body_md'], Path(row['id']))

    def list_records(self):
        with Store(self.database, read_only=True) as store:
            return [self.record(row) for row in store.conn.execute(
                "SELECT * FROM historical_works WHERE kind='dream' AND id NOT IN (SELECT document_id FROM deletions) ORDER BY id")]

    def _read_events(self):
        with Store(self.database, read_only=True) as store:
            return [json.loads(row[0]) for row in store.conn.execute(
                'SELECT metadata_json FROM historical_work_events ORDER BY origin,line_number')]

    @staticmethod
    def save_record(store, metadata, body):
        body = body.strip()
        key = metadata['dream_id']
        store.conn.execute("INSERT INTO historical_works VALUES (?,'dream',1,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET body_md=excluded.body_md,body_sha256=excluded.body_sha256,metadata_json=excluded.metadata_json",
            (key,key,body,digest(body),encode(metadata),'serein-live',''))
        return DreamRecord(metadata, body, Path(key))

    def _write_record(self, metadata, body):
        with Store(self.database) as store, store.transaction():
            return self.save_record(store, metadata, body)

    @staticmethod
    def log_event(store, event, payload):
        sequence = store.conn.execute("SELECT COALESCE(MAX(line_number),0)+1 FROM historical_work_events WHERE origin='serein-live'").fetchone()[0]
        store.conn.execute('INSERT INTO historical_work_events VALUES (?,?,?,?,?)',
            ('serein-live',sequence,payload.get('dream_id',''),event,encode({'event':event,**payload})))

    def _log_event(self, event, payload):
        with Store(self.database) as store, store.transaction():
            self.log_event(store, event, payload)

    def _delete_record(self, record, reason, embedding_engine=None):
        with Store(self.database) as store, store.transaction():
            store.conn.execute('INSERT OR IGNORE INTO deletions VALUES (?,?,?)', (record.dream_id,now(),encode({'reason':reason})))
            self.log_event(store,'deleted',{'dream_id':record.dream_id,'generated_at':record.metadata.get('generated_at'),'deleted_at':now(),'reason':reason})
        if embedding_engine:
            embedding_engine.delete_embedding(record.dream_id)

    def claim_surface(self, record, retain, embedding_engine=None):
        with Store(self.database) as store:
            store.conn.execute('BEGIN IMMEDIATE')
            row=store.conn.execute("SELECT * FROM historical_works WHERE id=? AND kind='dream' AND id NOT IN (SELECT document_id FROM deletions)",(record.dream_id,)).fetchone()
            if row is None:
                return {'status':'skipped','reason':'record_missing'}
            current=self.record(row)
            if current.surfaced:
                return {'status':'skipped','reason':'already_claimed'}
            stamp=datetime.now(timezone.utc).isoformat(timespec='seconds')
            surfaced=self.save_record(store,{**current.metadata,'surfaced':True,'surfaced_at':stamp},current.body)
            self.log_event(store,'surfaced',{'dream_id':record.dream_id,'generated_at':current.metadata.get('generated_at'),'surfaced_at':stamp})
            if not retain:
                store.conn.execute('INSERT INTO deletions VALUES (?,?,?)',(record.dream_id,stamp,encode({'reason':'surfaced_one_shot'})))
                self.log_event(store,'deleted',{'dream_id':record.dream_id,'generated_at':current.metadata.get('generated_at'),'deleted_at':stamp,'reason':'surfaced_one_shot'})
            store.conn.commit()
        if not retain and embedding_engine:
            embedding_engine.delete_embedding(record.dream_id)
        return {'status':'injected','reason':'resonant','retained':bool(retain),'text':self._format_surface(surfaced),
            'dream_id':record.dream_id,'generated_at':surfaced.metadata.get('generated_at'),'surfaced_at':stamp,
            'source_bucket_ids':[str(key) for key in surfaced.metadata.get('source_bucket_ids',[]) if str(key or '').strip()][:5]}
