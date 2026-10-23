# Shared bridge boundary evidence

This public fix adapts the approach reviewed in reference commit `cd9b241` without copying deployment data or changing existing feature defaults.

When material review is enabled, two Events can retain a complete declared bridge source while citing different parts of it as boundary evidence. Each quote must occur verbatim in the original and be retained as `main`/`mixed` on exactly one Event side. Full source ownership is unchanged. Both sides still need evidence; shared quotes retained on both sides, background/omitted text, foreign context, ordinary-source overlap and incomplete material coverage remain invalid. Without material review, boundary evidence still uses exclusively owned sources.

Compact expansion counts owners from the final selected-base plus new-unit union. A retry inheriting a current stable bridge from a selected predecessor therefore preserves the shared role even when its compact proposal does not repeat that root. Protected/manual/blocked predecessor deferrals, atomic units and source coverage remain enforced. This does not permit borrowing an active unselected predecessor's sources.

The Curator's generated prompt and packaged role rules describe the same exception. No optional setting is enabled automatically.

## Reference implementation review

The reference patch's overall ownership approach is sound. One issue was reproduced in its boundary and material validators, evaluated in isolation without importing service initialization or opening any database:

- The boundary validator searches `quote in _remaining(content, omissions)` after deleting omission text.
- With original text `abXcd abcd RIGHT`, the left side omits `abcd`, then `X`, then `RIGHT`. The original `abcd` citation is omitted, but deleting `X` joins `ab` and `cd` into a new `abcd`.
- The right side omits `abXcd` and `abcd`, retaining `RIGHT`. The reference validators accept `abcd` as left evidence and `RIGHT` as right evidence although the left citation was reconstructed across omitted text. Complete material coverage and ordinary ownership gates do not catch this case.

The public adaptation checks quote intervals in the original against omission intervals instead of searching a concatenated remainder. A quote crossing an omission cannot count as retained evidence. Overlapping omission intervals are respected; repeated omission strings are treated conservatively, so their occurrences cannot be used to establish one-sided boundary evidence. Broader unambiguous quotes or exclusive sources can still establish the boundary. This changes only the new shared-quote exception; existing Writer material and source binding contracts are preserved.

A smaller prompt consistency issue remains in the reference's `build_event_track_curator_prompt` at line 3789: its boundary JSON example still asks for quotes from exclusively owned originals, while the patched role rules permit shared material fragments. This is an outdated example rather than another ownership-gate failure. The public generated prompt and packaged rules both describe the opt-in exception.

The reference implementation was not edited in this task.

## Verification

`tests/test_shared_bridge_boundary.py` contains 20 synthetic cases covering distinct shared quotes, inherited retry ownership, protected/manual/blocked deferrals, active unselected predecessor rejection, missing bilateral evidence, foreign context, invalid omissions, full coverage on deferred proposals, disabled material review, ordinary overlaps, reconstructed quotes, overlapping/repeated omissions, atomic unit preservation and prompt consistency.

The focused boundary, continuity, materials and new regression suites passed 33 cases. A broader working-tree run passed 173 cases with one failure in an existing retry-count test. The same failure was reproduced independently on unmodified `main` at `a82e14b110a48386f8b4045924ab3e93f4feafec`: `test_pause_budget_persists_api_attempts_and_retry_route_is_authenticated` expects three `pipeline_attempts` rows but observes four. This is outside the shared bridge fix and was not changed.

An isolated snapshot of that `main` plus only this fix was also tested, preserving the newer main-branch Curator introduction and excluding unrelated working-tree changes: 172 passed and the same pre-existing retry-count test failed. The final atomic-unit regression was then added and checked with the focused suites. No private repository changes, commit, push or deployment were performed.
