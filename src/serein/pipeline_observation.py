"""Report returned pipeline holds, not only raised exceptions, without polling spam."""

HOLD_STATES = frozenset({'blocked', 'needs_repair', 'paused', 'retry_wait', 'retained', 'awaiting_agent'})


def report(result, previous, logger):
    state = result.get('status', '')
    if state not in HOLD_STATES:
        if previous:
            logger.info('Event pipeline advanced after a hold: status=%s', state)
        return None
    holds = result.get('blocked_scopes') or [result]
    signature = (state, tuple(sorted((str(item.get('batch_id', '')),
        str(item.get('reason', ''))[:500]) for item in holds)))
    if signature != previous:
        logger.warning('Event pipeline requires attention: status=%s holds=%s; originals retained',
                       state, signature[1])
    return signature
