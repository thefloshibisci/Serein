"""Interpret source clocks without changing original evidence or deduplication keys.

Older OrangeChat clients sent device-local wall time without an offset. For
those records use the service's historical Shanghai clock; other legacy Bridge
sources retain their UTC convention. New clients must send an explicit offset.
"""
from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))


def source_datetime(value, source=''):
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=SHANGHAI if source == 'orangechat' else timezone.utc)
    return stamp.astimezone(timezone.utc)


def message_datetime(message):
    return source_datetime(message['created_at'], message.get('source') or message.get('source_system', ''))


def source_julianday(alias=''):
    """SQL equivalent for internal raw_events queries, including old mobile rows."""
    if alias not in ('', 'r'):
        raise ValueError('Unsupported raw-events alias')
    prefix = alias + '.' if alias else ''
    stamp, source = prefix+'created_at', prefix+'source'
    return (f"CASE WHEN {source}='orangechat' AND substr({stamp},-1)!='Z' "
            f"AND instr(substr({stamp},11),'+')=0 AND instr(substr({stamp},11),'-')=0 "
            f"THEN julianday({stamp},'-8 hours') ELSE julianday({stamp}) END")
