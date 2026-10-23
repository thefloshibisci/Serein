"""Bounded timeout diagnostics without provider messages, URLs or request input."""
from math import isfinite


_FIELDS=('read_timeout_seconds','write_timeout_seconds','connect_timeout_seconds',
         'pool_timeout_seconds','elapsed_seconds')


def _safe_numbers(values):
    if not isinstance(values,dict):return {}
    result={}
    for key in _FIELDS:
        value=values.get(key)
        if type(value) not in (int,float):continue
        try:
            if isfinite(value) and value>=0:result[key]=value
        except OverflowError:
            pass
    return result


def annotate_timeout(error,timeout,elapsed):
    # Cancellation may later become asyncio.wait_for's TimeoutError cause.
    error._serein_timeout_diagnostics=_safe_numbers({
        **{f'{name}_timeout_seconds':getattr(timeout,name) for name in ('read','write','connect','pool')},
        'elapsed_seconds':round(max(0,elapsed),6)})


def timeout_reason(error):
    import httpx
    categories=(
        (httpx.ReadTimeout,'read_timeout','读取模型响应超时'),
        (httpx.ConnectTimeout,'connect_timeout','连接上游模型超时'),
        (httpx.WriteTimeout,'write_timeout','发送模型请求超时'),
        (httpx.PoolTimeout,'pool_timeout','等待模型连接池超时'),
        (httpx.TimeoutException,'transport_timeout','模型传输超时'),
        (TimeoutError,'deadline_timeout','模型任务外层截止时间超时'),
    )
    category=next(((code,label) for kind,code,label in categories if isinstance(error,kind)),None)
    if category is None:return None
    details={};current=error;seen=set()
    for _ in range(4):
        if current is None or id(current) in seen:break
        seen.add(id(current))
        for key,value in _safe_numbers(getattr(current,'_serein_timeout_diagnostics',None)).items():
            details.setdefault(key,value)
        current=current.__cause__ if isinstance(error,TimeoutError) else None
    code,label=category
    suffix='；'+', '.join(f'{key}={details[key]:g}' for key in _FIELDS if key in details) if details else ''
    return f'{label} [{code}]{suffix}；请稍后继续。'
