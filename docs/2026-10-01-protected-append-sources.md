# Protected append evidence

The public edition continues to defer merges involving protected or manual
predecessors. It does not adopt the reference's merge-to-append shortcut.

With `append_protected` enabled, a single protected predecessor may now continue
when a retry contains both previously bound messages and genuinely new messages.
All-old retries, forked predecessors, and collisions with other canonical cards
remain blocked. The complete old-plus-new source union remains bound to the Event.
Only stable sources outside the selected predecessor's source set go to Writer as
owned evidence. The old title and body remain available as context; settlement
preserves the old title, body, and recall setting and appends the new paragraph.

## Reference synchronization rule

Commit `ee37959` in Haven Bridge allows a protected merge to become an append to
one predecessor. Removing another selected predecessor must not remove its
ownership protection. If any stable binding still belongs to that removed
predecessor, reject that conversion and defer the proposal. Do not simply drop
the binding: it would change the reviewed activity and atomic unit accounting.

For the reference's `append_candidates` branch, keep its existing checks and add:

```python
and not any(
    item["source_message_id"] in stable_ids
    and item["source_message_id"] in candidate["source_message_ids"]
    for candidate in selected_candidates
    if candidate["event_id"] != append_candidates[0]["event_id"]
    for item in bindings
)
```

This guard only concerns selected predecessors removed by that conversion. It
does not alter the reference's explicitly declared cross-Track bridge exception.

Regression fixtures must cover target sources `[1,2]`, a manual predecessor's
sources `[3,4]`, and stable sources `[1,2,3,4]`: no new evidence exists, and merging
must defer. Also test `[1,2,3,4,5,6]`: genuinely new `[5,6]` does not authorize
consuming the manual predecessor's `[3,4]`; that merge must also defer. A normal
single-target retry with sources `[1,2,3,4]` sends only `[3,4]` to Writer.

## Verification

Nine new public regressions cover retry ownership, no-new-source rejection,
opt-in, fork protection, unselected manual collisions, both protected merge
fixtures, and serial/parallel Writer requests with old-body context. The focused
append, Track continuation, shared boundary, materials, audit, and material
identity suites passed 85 tests using synthetic data only.
