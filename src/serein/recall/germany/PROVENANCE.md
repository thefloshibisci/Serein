# Compatibility implementation provenance
These recall algorithms derive from the author's earlier Ombre-Brain implementation,
upstream revision 9c99c591d0ff810b4d9f8691c829dbb50d45c0be, adapted by Serein.
Public derivation starts at Serein ea49ac918bd717206004e2c92c044cd3fa327b7e.
The package name records the compatibility boundary; no deployment is discovered.
Public changes replace private defaults, examples and identity values while preserving
evidence, lifecycle, mixed selection and post-selection cooldown behavior.
Automatic mixed recall has no universal cosine floor. Explicit lookup keeps its caller cutoff.

Public adapter update: Serein d60e272ec9bdc745dcc572f28cbb309057f5be48 adds at most one reviewed Scene neighbor to the six direct candidates, scored together without an edge bonus or a reserved final card. Domain, Arc and post-selection cooldown boundaries still apply.

Public adapter update, 2026-10-12: `CandidateGateway.collect_signals` returns
scope, lane signals and policy diagnostics. Removed retired per-type freshness
sorting, quota selection, duplicate entity lookup and unused Arc projection.
Mixed ranking remains the sole selection path, preserving public candidate
budgets, batched entity lookup, diagnostics and opt-in association.
