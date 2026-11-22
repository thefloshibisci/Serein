export const recallObservationPageLimits = Object.freeze({ hook: 20, gateway: 20 });

function cleanId(value) { return String(value ?? '').trim(); }
function numericId(value) {
  const parsed = Number.parseInt(cleanId(value), 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 0;
}
function newerRow(previous, incoming) {
  if (!previous) return incoming;
  const old = previous.payload;
  const next = incoming.payload;
  if (old?.observation_version && next?.observation_version) {
    if (Number(old.observation_revision || 0) > Number(next.observation_revision || 0)) return previous;
    const final = new Set(['completed', 'failed', 'interrupted', 'incomplete']);
    if (final.has(old.request_status) && !final.has(next.request_status)) return previous;
  }
  return incoming;
}

export function mergeObservationRows(currentRows, incomingRows) {
  const byId = new Map();
  for (const row of [...(Array.isArray(currentRows) ? currentRows : []), ...(Array.isArray(incomingRows) ? incomingRows : [])]) {
    const id = cleanId(row?.id);
    if (id) byId.set(id, newerRow(byId.get(id), row));
  }
  return [...byId.values()].sort((left, right) => {
    const idDifference = numericId(right?.id) - numericId(left?.id);
    if (idDifference) return idDifference;
    const createdDifference = cleanId(right?.created_at).localeCompare(cleanId(left?.created_at));
    return createdDifference || cleanId(right?.id).localeCompare(cleanId(left?.id));
  });
}

export function normalizeObservationPage(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const reviewedItems = Array.isArray(payload?.reviewed_items) ? payload.reviewed_items : [];
  const nextBeforeId = numericId(payload?.next_before_id ?? payload?.next_cursor) || null;
  return {
    items, reviewedItems, rows: mergeObservationRows(items, reviewedItems),
    hasMore: Boolean(payload?.has_more && nextBeforeId), nextBeforeId,
    nextAfterId: numericId(payload?.next_after_id) || null,
  };
}

export function reviewedObservationIds(reviews, source) {
  const keys = reviews && typeof reviews === 'object' && !Array.isArray(reviews) ? Object.keys(reviews) : [];
  const ids = keys.flatMap(key => {
    if (source === 'hook') {
      const match = key.match(/^hook-(\d+)$/);
      return match ? [Number.parseInt(match[1], 10)] : [];
    }
    return /^\d+$/.test(key) ? [Number.parseInt(key, 10)] : [];
  });
  return [...new Set(ids.filter(id => Number.isInteger(id) && id > 0))].slice(0, 500);
}
