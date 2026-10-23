# Public Curator material identity audit — 2026-10-01

## Tested checkout

- Branch: `codex/latest-shadow-portraits`.
- HEAD: `09767a6b2a7b6846274b43f94af29bfcd581d950`.
- Tests ran against the working tree, including pre-existing changes, rather than a clean HEAD checkout. At entry, 25 tracked files were modified and four untracked paths were present. This audit leaves those files alone and adds only this report and `tests/test_curator_material_identity.py`.
- All databases were temporary empty databases populated with synthetic fixtures. No deployment, production database, private configuration, or network service was accessed. No commit, push, deployment, or release was performed.

## Material identity result

The reported filtered-list indexing defect is absent from this public implementation, including the committed HEAD version of `pipeline_materials.py`.

`normalize_event_curator_output` keeps the expanded original proposals in `output` and obtains a separate filtered `normalized` plan. It calls `pipeline_materials.attach(review, output, normalized, component)`. The attachment function resolves each `event_index` against the original `output['events']`, validates the entire proposed source set, and then matches a retained Event through `event_ref`. It does not index the filtered plan using the original position. Deferred proposals still undergo source coverage and verbatim quotation validation.

The public protected continuation contract differs from the reported private merge trimming behavior. A blocked merge is deferred; an explicitly enabled, permitted protected extension becomes `append_only`. Its normalized source set still includes old and new sources. `first_event_writer_pass` selects only stable new messages for the append, and `request_for` filters Curator materials to those actual Writer message IDs. Old materials remain associated with the Event source union without leaking into the appended Writer paragraph. There is no reachable source-trimming branch requiring the private patch's extra attachment filtering today. If normalization later removes sources, attachment filtering must be revisited alongside `substantive_ids` and round admission.

The new regressions cover all 24 permutations of four proposals with transitive protected bridge deferrals, including removal before and between retained proposals; all proposals deferred; rejection of a fabricated quote or foreign source ID in a deferred proposal; protected append material selection across different session identities; and a complete two-window Event extension through Writer and persisted source references. No runtime fix was needed for material identity.

## Time, Track, Event, and session boundaries

- Scheduled settlement begins at 03:00 Asia/Shanghai. A scheduled batch's upper bound is that day's 03:00 watermark, with a 20-minute stability cutoff. Older pending messages can also enter this batch; it is not restricted to a single calendar day's messages. Explicit `include_recent=True` uses the current time and complete dialogue units.
- Router flush timing and dialogue completion determine when material is ready. They do not require a new semantic Track or Event. Daytime routing counts exchanges separately by session.
- Track visibility comes from proven routing activity within the configured lookback (default three days), with the same source/runtime/workspace boundary. `load_tracks` does not require the previous Track to have the current session identity. The cross-window regression extends the existing Event and retains both windows' exact source references.
- Sessions still identify original sources and transport batches. The host maps source/session pairs to internal integer session identities while preserving `original_session_id`; Event source references keep original session IDs. New windows do not automatically sever the activity. Tracks outside the lookback or in another source/runtime/workspace are intentionally absent from Router context.
- Raw ingestion deduplicates nonempty `source_event_id` within a source system. Clients must supply unique source event IDs across windows; session identity does not override that existing ingestion contract.

## Images and remaining risk

Public task media cleanup retains canonical raw attachments and saved image transcriptions. Completed task snapshots are compacted and disposable task media is removed; the seven-day cleanup expires task image references. The new image variant expires the first batch's task media, then continues the Event from a second window. Writer receives the saved image transcription without another image-model call, and raw attachment data remains intact.

An independent external-link issue was reproduced using a synthetic URL and a mocked image reader, with no network access: the first window successfully transcribes and settles an external image; the second window extends its Track after the mocked URL becomes unavailable. `transcribe_component` builds its image probe through `request_for` and `freeze_task_images` **before** attempting `reusable_transcriptions`. Consequently a generic original-image read error propagates despite the raw transcription being `complete`. HTTP 404/410 follows the separate missing-original path rather than this generic exception path; that path was not exercised in this one-off reproduction.

This was not caused by the public task-copy TTL. At the time of the initial audit, cached transcription reuse across batches still required original image bytes to establish the matching SHA-256. The user subsequently requested transcription-first reuse; the follow-up below implements that change.

## Follow-up: transcription-first image reuse

Canonical raw-row image transcriptions are now checked before fetching originals during the transcription probe and subsequent text-only Curator request. Valid successes provide source/position/hash receipts without an image URL. Only images lacking a usable transcript are frozen and sent to the image model. Writer continues receiving text-only transcriptions with its current owned/context-only scope, and mixed cached/live image sets retain exact coverage checks.

Newly persisted transcription items record a fingerprint of the canonical raw content and metadata. Changed content or attachment metadata invalidates the shortcut for that individual cached item. Fingerprints remain internal to the raw cache, preserving the existing agent transcription schema. Partial updates do not relabel older sibling images with a new fingerprint. Inline images additionally undergo a local byte-hash comparison without downloading. Legacy raw-row successes can be used under the existing append-only raw-ingestion contract and gain fingerprints on reuse; historical Event copies alone still require original bytes for validation. External URLs are treated as references to the saved image snapshot: changes behind an unchanged URL do not trigger a network refresh when the saved transcript is valid.

Runtime changes are limited to `pipeline.py`, `pipeline_images.py`, and `image_transcription.py`, preserving other pre-existing changes. `tests/test_transcription_first.py` adds nine synthetic regressions for expired external originals, legacy caches, mixed cached/new images, replaced attachments, malformed/foreign receipts, changed inline bytes, and per-image fingerprint preservation during partial updates. No external image server is contacted by those tests.

Follow-up validation: the new suite passed all nine cases. A combined run of transcription-first, image transcription, Event handoff, material identity, latest pipeline, and Writer concurrency suites passed 117 cases (before the ninth standalone regression was added); the final new suite then passed all nine. The original 136-case audit result below describes the pre-fix working tree.

Before pushing, only this task's changes were staged, including selected hunks from the shared `pipeline.py`. An isolated copy of that staged snapshot passed all **134 focused tests in 80.06 seconds**, without depending on unrelated uncommitted work.

## Verification

The new file alone: 28 passed. The existing material, Event handoff, Track continuation, and image transcription suites: 57 passed.

Combined focused command:

```powershell
C:/Python313/python.exe -m pytest tests/test_curator_material_identity.py tests/test_pipeline_materials.py tests/test_event_handoff.py tests/test_track_continuation_parity.py tests/test_image_transcription.py tests/test_latest_pipeline.py tests/test_pipeline_continuity.py tests/test_event_writer_concurrency.py -q
```

Combined result: **136 passed in 77.42 seconds**. Existing working-tree changes are included in that result; this audit does not claim clean-HEAD-only validation or full-repository validation.
