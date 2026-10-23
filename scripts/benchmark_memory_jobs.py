"""Offline semantic-index and Event Writer measurements in disposable instances."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
import cProfile
import gc
import json
from pathlib import Path
import pstats
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.recall.index import build_index
from serein.recall.vectors import fill_vectors
from serein.recall.worker import update_pending


def measure_index(owners,batch_size,concurrency,delay,target=None):
    with tempfile.TemporaryDirectory(prefix='serein-index-benchmark-') as directory:
        root=Path(directory);settings=Settings(root/'memory.db',root/'index.db',writable=True)
        with Store(settings.database) as store,store.transaction(immediate=True):
            for i in range(owners):store.create(str(i),'event','Synthetic event',f'Synthetic original {i}')
            if target:
                store.conn.executemany('INSERT INTO index_outbox(document_id) VALUES (?)',((str(i),) for i in range(target)))
        build_index(settings.database,settings.index)
        profile=dict(model='synthetic',provider_host='synthetic.invalid',document_instruction='',query_instruction='',max_chars=6000)
        with closing(sqlite3.connect(settings.index)) as conn,conn:
            conn.executemany('INSERT INTO settings VALUES (?,?)',[('embedding_profile',json.dumps(profile)),('embedding_dimension','2')])
        save_settings(settings.database,{'recall':{'embedding_batch_size':batch_size,'embedding_concurrency':concurrency}})
        class Client:
            dimension=2
            def __init__(self):self.profile=profile;self.calls=0;self.active=0;self.maximum=0;self.lock=threading.Lock()
            def documents(self,texts):
                with self.lock:self.calls+=1;self.active+=1;self.maximum=max(self.maximum,self.active)
                try:
                    time.sleep(delay)
                    return [[1,0] for _ in texts]
                finally:
                    with self.lock:self.active-=1
        client=Client();profiler=cProfile.Profile();started=time.perf_counter();profiler.enable()
        result=update_pending(settings,client=client) if target else fill_vectors(settings,client=client)
        profiler.disable();duration=time.perf_counter()-started
        selected={key[2]:round(row[3],4) for key,row in pstats.Stats(profiler).stats.items()
                  if key[2] in ('coverage','refresh_index','rebuild_entities','fill_vectors','update_scope')}
        report={'kind':'incremental_index' if target else 'whole_vectors','owners':owners,'target':target or owners,
                'batch_size':batch_size,'concurrency':concurrency,'simulated_request_delay_seconds':delay,
                'seconds':round(duration,4),'requests':client.calls,'max_in_flight':client.maximum,'profile_seconds':selected,
                'updated':result.get('updated',result.get('embedded')),'real_model_calls':0}
        assert report['updated']==(target or owners)
        gc.collect()
        return report


def synthetic_output(role,request):
    if role=='track_router':
        return {'message_assignments':[{'source_message_id':row['id'],'primary_track_ref':'new:1',
            'context_track_refs':[],'routing_role':'primary_activity'} for row in request['messages']],
            'track_updates':[{'track_ref':'new:1','subject':'Synthetic plans','throughline':'Independent activities',
                              'event_policy':'default','status':'active'}]}
    if role=='event_curator':
        component=request['component'];messages={row['id']:row for row in component['messages']}
        roots=[row['unit_root_message_id'] for row in component['memberships'] if row['unit_root_message_id'] in messages]
        groups=[roots[start:start+2] for start in range(0,len(roots),2)]
        return {'events':[{'action':'create','base_event_ids':[],'primary_track_id':component['track_ids'][0],
                           'owned_unit_roots':group} for group in groups],
                'skip_unit_roots':[],'defer_unit_roots':[],
                'decision_review':{'events':[{'event_index':i,'reason':'A separate synthetic activity'} for i in range(len(groups))],
                    'boundaries':[{'left_event_index':i,'right_event_index':i+1,'reason':'Independent plans',
                        'evidence':[{'source_message_id':root,'quote':messages[root]['content']} for root in (groups[i][0],groups[i+1][0])]}
                        for i in range(len(groups)-1)],'dispositions':[]}}
    if role=='event_writer':
        from serein.extensions.pipeline_latest import _SELF_REVIEW_KEYS
        source=request['messages'][0];span={'source_message_id':source['id'],'quote':source['content']}
        sentence=source['content']
        return {'title':'Synthetic activity','event_draft':sentence,'recallable':True,'evidence_sufficient':True,
            'kept_details':[sentence],'discarded_details':[],
            'claim_groups':[{'claim_group_id':'g1','claim_type':'fact','owner':'双方','render_mode':'direct','focus_role':'core',
                'summary':sentence,'source_spans':[span]}],
            'sentence_evidence':[{'sentence_index':0,'sentence':sentence,'claim_group_ids':['g1'],'source_spans':[span]}],
            'self_review':{key:True for key in _SELF_REVIEW_KEYS}}
    raise ValueError('Unexpected synthetic role')


def measure_writer(concurrency,delay):
    from serein.bootstrap import initialize
    from serein.compat.raw_archive import raw_archive
    from serein.extensions.pipeline import advance
    with tempfile.TemporaryDirectory(prefix='serein-writer-benchmark-') as directory:
        root=Path(directory);settings=Settings(root/'memory.db',root/'index.db',writable=True);initialize(settings)
        save_settings(settings.database,{'pipeline':{'event_writer_concurrency':concurrency}})
        raw_archive(settings).ingest([{'source_event_id':str(i),'session_id':'synthetic','role':'user' if i%2==0 else 'assistant',
            'text':f'Synthetic plan {i//2}' if i%2==0 else f'Synthetic reply {i//2}',
            'created_at':f'2025-01-01T00:{i:02}:00Z'} for i in range(8)],source='synthetic')
        calls=Counter();active=0;maximum=0
        async def runner(role,request):
            nonlocal active,maximum
            calls[role]+=1
            if role=='event_writer':active+=1;maximum=max(maximum,active)
            try:
                await asyncio.sleep(delay)
                return synthetic_output(role,request)
            finally:
                if role=='event_writer':active-=1
        started=time.perf_counter();result=asyncio.run(advance(settings.database,include_recent=True,runner=runner))
        duration=time.perf_counter()-started
        assert result.get('events')==4,result
        with Store(settings.database,read_only=True) as store:
            assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='event'").fetchone()[0]==4
            assert store.conn.execute('SELECT count(*) FROM evidence_bindings').fetchone()[0]==8
        gc.collect()
        return {'kind':'event_pipeline_batch','originals':8,'events':4,'concurrency':concurrency,
            'simulated_request_delay_seconds':delay,'seconds':round(duration,4),'calls':dict(calls),
            'max_writers_in_flight':maximum,'real_model_calls':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind',choices=('index','writer'),default='index')
    parser.add_argument('--owners',type=int,default=400)
    parser.add_argument('--target',type=int,help='Measure one incremental window (1..100), not a full fill')
    parser.add_argument('--batch-size',type=int,choices=range(1,65),default=16)
    parser.add_argument('--concurrency',type=int,choices=(1,2,4),default=1)
    parser.add_argument('--delay',type=float,default=.1)
    args=parser.parse_args()
    if args.owners<1 or args.delay<0 or (args.target is not None and not 1<=args.target<=min(100,args.owners)):
        parser.error('Invalid owners, target or simulated delay')
    print(json.dumps(measure_writer(args.concurrency,args.delay) if args.kind=='writer' else
        measure_index(args.owners,args.batch_size,args.concurrency,args.delay,args.target)))
