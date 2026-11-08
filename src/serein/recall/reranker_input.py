"""Bounded canonical evidence for the existing single reranker request."""

from .passages import regions
from .scene import evidence_text


def memory_document(document, passages, *, max_chars=4000):
    """Keep short bodies whole; add nearby context to at most two long-body hits.

    Passage offsets refer to canonical prose, not the Scene evidence projection.
    Validate them and stay inside evidence regions so comments cannot leak in.
    The caller supplies the already scored passages in descending score order.
    """
    prefix = f"title: {document['title'].strip()}\nbody: "
    if document['metadata'].get('memory_role') == 'feel':
        prefix = 'type: feel (当时的主观感受，不是事实证据)\n' + prefix
    body = document['body_md']
    evidence = evidence_text(document) if document['kind'] == 'scene' else body
    complete = prefix + evidence
    if len(complete) <= max_chars:
        return complete
    budget = max_chars - len(prefix)
    if budget <= 0:
        return prefix[:max_chars]

    allowed = regions(document)
    seeds = []
    for passage in passages:
        start, end = passage.get('start_offset'), passage.get('end_offset')
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(body)
                or body[start:end] != passage.get('text')):
            continue
        region = next(((a, b) for a, b in allowed if a <= start < end <= b), None)
        if region is not None:
            seeds.append((start, end, *region))
        if len(seeds) == 2:
            break
    if not seeds:
        return complete[:max_chars]

    separator = '\n[...]\n'
    window_budget = (budget - len(separator) * (len(seeds) - 1)) // len(seeds)
    if window_budget <= 0:
        return complete[:max_chars]
    windows = []
    for start, end, region_start, region_end in seeds:
        # Share the budget before restoring source order; a late best hit must
        # not disappear because an earlier window consumed the entire limit.
        end = min(end, start + window_budget)
        margin = min(200, (window_budget - (end - start)) // 2)
        windows.append((max(region_start, start - margin), min(region_end, end + margin)))
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return prefix + separator.join(body[start:end].strip() for start, end in merged)
