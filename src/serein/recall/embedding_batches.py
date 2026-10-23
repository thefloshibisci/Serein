"""Bound provider calls inside one guarded index operation; persist on its thread."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import Event

from .index import unit_vector


def _failure_priority(error):
    if not isinstance(error,Exception):return 2
    from ..adapters.embedding import EmbeddingTransientError
    transient=isinstance(error,EmbeddingTransientError)
    try:
        import httpx
        transient=transient or isinstance(error,(httpx.TimeoutException,httpx.NetworkError))
    except ImportError:pass
    return 0 if transient else 1


def request_batches(client,items,*,batch_size,concurrency):
    if type(batch_size) is not int or not 1<=batch_size<=64:
        raise ValueError('batch_size must be an integer between 1 and 64')
    if type(concurrency) is not int or not 1<=concurrency<=4:
        raise ValueError('concurrency must be an integer between 1 and 4')
    batches=iter(items[start:start+batch_size] for start in range(0,len(items),batch_size))
    failed=Event()
    def request(batch):
        try:
            vectors=client.documents([item['input'] for item in batch])
            if len(vectors)!=len(batch):raise ValueError('Provider returned an unexpected number of document vectors')
            return [unit_vector(vector,client.dimension) for vector in vectors]
        except BaseException:
            failed.set()
            raise
    if concurrency==1:
        for batch in batches:yield batch,request(batch)
        return
    with ThreadPoolExecutor(max_workers=concurrency,thread_name_prefix='serein-embedding') as pool:
        pending={};error=None
        def submit():
            batch=next(batches,None)
            if batch is not None:pending[pool.submit(request,batch)]=batch
        for _ in range(concurrency):submit()
        try:
            while pending:
                done,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in done:
                    batch=pending.pop(future)
                    try:vectors=future.result()
                    except BaseException as exc:
                        if error is None or _failure_priority(exc)>_failure_priority(error):error=exc
                    else:yield batch,vectors
                # A failure stops new calls. Already issued successful batches
                # are still yielded for validation/persistence before raising.
                if not failed.is_set():
                    while len(pending)<concurrency:
                        before=len(pending);submit()
                        if len(pending)==before:break
            if error is not None:raise error
        finally:
            for future in pending:future.cancel()
