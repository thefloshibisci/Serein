"""Measure synthetic file archival in a disposable instance, without model calls."""
import argparse
import json
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store
from serein.imports import advance_import, stage


def peak_memory_bytes():
    """Process-lifetime peak, including fixture construction and preview parsing."""
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[
                (key,ctypes.c_size_t) for key in ('PeakWorkingSetSize','WorkingSetSize',
                'QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
        counters=Counters();counters.cb=ctypes.sizeof(counters)
        current=ctypes.windll.kernel32.GetCurrentProcess;current.restype=wintypes.HANDLE
        memory=ctypes.windll.psapi.GetProcessMemoryInfo
        memory.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        if memory(current(),ctypes.byref(counters),counters.cb):return counters.PeakWorkingSetSize
    return None


def measure(entries,batch_size,process_limit=None):
    with tempfile.TemporaryDirectory(prefix='serein-import-benchmark-') as directory:
        root=Path(directory)
        settings=Settings(root/'memory.db',root/'index.db',writable=True)
        initialize(settings)
        # Stable IDs/timestamps and short prose make the workload reproducible.
        content=json.dumps({'id':'synthetic-benchmark','messages':[
            {'id':str(index),'role':'user' if index%2==0 else 'assistant',
             'content':f'Synthetic archive message {index}.','timestamp':1700000000+index}
            for index in range(entries)]},separators=(',',':'))
        started=time.perf_counter()
        preview=stage(settings.database,content,'synthetic.json','conversation',False)
        preview_seconds=time.perf_counter()-started
        processed=0;batches=0;first_batch=None;started=time.perf_counter()
        while processed<entries and (process_limit is None or processed<process_limit):
            before=time.perf_counter()
            result=advance_import(settings,preview['id'],batch_size=batch_size)
            if first_batch is None:first_batch=time.perf_counter()-before
            processed=result['processed'];batches+=1
        import_seconds=time.perf_counter()-started
        with Store(settings.database,read_only=True) as store:
            archived=store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]
            queued=store.conn.execute('SELECT count(*) FROM index_outbox').fetchone()[0]
        assert archived==processed and queued==0
        return {'python':platform.python_version(),'platform':platform.system(),'sqlite':sqlite3.sqlite_version,
            'entries':entries,'input_bytes':len(content.encode()),'batch_size':batch_size,
            'processed':processed,'batches':batches,'preview_seconds':round(preview_seconds,3),
            'import_seconds':round(import_seconds,3),'first_batch_seconds':round(first_batch or 0,3),
            'entries_per_second':round(processed/import_seconds,1),
            'database_bytes':settings.database.stat().st_size,'peak_process_memory_bytes':peak_memory_bytes(),
            'index_outbox':queued,'model_calls':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entries',type=int,default=2500)
    parser.add_argument('--batch-size',type=int,choices=(25,250),default=25)
    parser.add_argument('--process-limit',type=int,help='Measure a prefix only, rounded up to a complete batch')
    args=parser.parse_args()
    if args.entries<1 or (args.process_limit is not None and args.process_limit<1):parser.error('counts must be positive')
    print(json.dumps(measure(args.entries,args.batch_size,args.process_limit)))
