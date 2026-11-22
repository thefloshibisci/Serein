import test from "node:test";
import assert from "node:assert/strict";
import { compareScenesByTime, sceneTimeLabel } from "../src/utils/sceneTime.js";
import { compareFactEventsByEnd } from "../src/utils/factEventTime.js";

test("same-day Scenes use original time rather than ID or recent edits", () => {
  const rows = [
    { id: "z", date: "2026-11-09", memoryTime: "2026-11-09T01:00:01+08:00", sourceUpdatedAt: "2099-01-01" },
    { id: "a", date: "2026-11-09", memoryTime: "2026-11-09T01:00:59+08:00" },
  ];
  assert.deepEqual([...rows].sort(compareScenesByTime).map(row => row.id), ["a", "z"]);
  assert.equal(rows[0].id, "z");
  assert.equal(sceneTimeLabel(rows[1]), "2026-11-09 01:00");
});

test("declared dates stay primary and use original record time only within the date", () => {
  const rows = [
    { id: "old", date: "2020-01-01", memoryTime: "2020-01-01", recordedAt: "2099-01-01T00:00:00Z" },
    { id: "z", date: "2026-11-08", memoryTime: "2026-11-08", recordedAt: "2026-11-08T01:00:00Z" },
    { id: "a", date: "2026-11-08", memoryTime: "2026-11-08", recordedAt: "2026-11-08T02:00:00Z" },
  ];
  assert.deepEqual(rows.sort(compareScenesByTime).map(row => row.id), ["a", "z", "old"]);
  assert.equal(sceneTimeLabel(rows[2]), "2020-01-01");
});

test("offset timestamps compare actual instants and display the Shanghai day", () => {
  const rows = [
    { id: "z", memoryTime: "2026-11-08T18:10:00Z" },
    { id: "a", memoryTime: "2026-11-09T02:20:00+08:00" },
  ];
  assert.equal(sceneTimeLabel(rows[0]), "2026-11-09 02:10");
  assert.deepEqual(rows.sort(compareScenesByTime).map(row => row.id), ["a", "z"]);
});

test("old caches sort by date and unknown dates stay at the bottom", () => {
  const rows = [{ id: "unknown" }, { id: "old", date: "2025-01-01" }, { id: "new", date: "2026-11-09" }];
  assert.deepEqual(rows.sort(compareScenesByTime).map(row => row.id), ["new", "old", "unknown"]);
  assert.equal(sceneTimeLabel(rows[2]), "时间未记录");
});

test("Event ordering keeps source seconds and offsets instead of minute and ID", () => {
  const rows = [
    { item_id: "z", source_ended_at: "2026-11-09T01:00:01+08:00", local_end_date: "2026-11-09", local_end_time: "01:00" },
    { item_id: "a", source_ended_at: "2026-11-08T17:00:59Z", local_end_date: "2026-11-09", local_end_time: "01:00" },
  ];
  assert.deepEqual(rows.sort(compareFactEventsByEnd).map(row => row.item_id), ["a", "z"]);
});

test("Event ordering uses end time for spanning events and local fallback for older cache", () => {
  const rows = [
    { item_id: "new_start", local_date: "2026-11-09", local_start_time: "09:00", local_end_time: "10:00" },
    { item_id: "late_end", local_date: "2026-11-08", local_start_time: "20:00", local_end_date: "2026-11-09", local_end_time: "11:00" },
    { item_id: "unknown" },
  ];
  assert.deepEqual(rows.sort(compareFactEventsByEnd).map(row => row.item_id), ["late_end", "new_start", "unknown"]);
});
