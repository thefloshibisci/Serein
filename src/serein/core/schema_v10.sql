-- Feel uses the existing Scene identity/revision/lifecycle machinery.
-- Plans deliberately do not live in documents or reminders.
CREATE TABLE authored_plans (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK(revision > 0),
    payload_json TEXT NOT NULL
);
CREATE TABLE authored_plan_revisions (
    plan_id TEXT NOT NULL REFERENCES authored_plans(id),
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(plan_id, revision)
);
CREATE TABLE ob_type_imports (
    batch_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);
PRAGMA user_version=10;
