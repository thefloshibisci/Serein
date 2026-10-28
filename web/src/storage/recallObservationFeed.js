import { mergeObservationRows, normalizeObservationPage, reviewedObservationIds } from './recallObservationPagination.js';

export const observationSources = ['hook', 'gateway'];
export const terminalObservationStates = new Set(['completed', 'failed', 'interrupted']);
export const emptyObservationFeed = () => ({
  loaded: false, rows: [], buffered: [], reviewed: [], afterId: 0,
  hasMore: false, nextBeforeId: null, loading: false, loadingEarlier: false, error: '',
});
export const emptyObservationFeeds = () => Object.fromEntries(observationSources.map(key => [key, emptyObservationFeed()]));

export function pendingObservationIds(feed) {
  return mergeObservationRows(feed.rows, feed.buffered)
    .filter(row => row.payload?.observation_version && !terminalObservationStates.has(row.payload.request_status))
    .map(row => Number(row.id));
}

// Serialize reads per source. Head synchronization has its own cursor; loading
// history must never overwrite it or be discarded by a later head response.
export function createObservationFeed({ request, onChange, canReveal = () => false, reviews = () => ({}) }) {
  let feeds = emptyObservationFeeds();
  let disposed = false;
  const flights = new Map();
  const pendingOffsets = { hook: 0, gateway: 0 };
  const checkedReviews = { hook: new Set(), gateway: new Set() };
  const publish = (key, value) => {
    if (disposed) return;
    feeds = { ...feeds, [key]: value };
    onChange(feeds);
  };
  function reveal(key) {
    const feed = feeds[key];
    publish(key, { ...feed, rows: mergeObservationRows(feed.rows, feed.buffered), buffered: [] });
  }
  async function read(key, older = false) {
    if (disposed || !observationSources.includes(key)) return;
    if (flights.has(key)) {
      const flight = flights.get(key);
      // Reaching the footer during a refresh must queue the older page rather
      // than consume the IntersectionObserver event without loading anything.
      if (older && !flight.older) return flight.promise.then(() => read(key, true));
      return flight.promise;
    }
    const start = feeds[key];
    if (older && (!start.loaded || !start.hasMore || !start.nextBeforeId)) return;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20_000);
    const initial = !start.loaded;
    const pending = pendingObservationIds(start);
    // Bound each recheck without starving requests outside the first 500 IDs.
    const offset = pending.length ? pendingOffsets[key] % pending.length : 0;
    const pendingIds = [...pending.slice(offset), ...pending.slice(0, offset)].slice(0, 500);
    const personalIds = reviewedObservationIds(reviews(), key);
    const unreadReviews = personalIds.filter(id => !checkedReviews[key].has(id));
    const reviewIds = older ? [] : [...new Set([...unreadReviews, ...pendingIds])].slice(0, 500);
    const options = { signal: controller.signal,
      reviewIds,
      ...(older ? { beforeId: start.nextBeforeId } : initial ? {} : { afterId: start.afterId }),
    };
    publish(key, { ...start, loading: !older, loadingEarlier: older });
    const flight = { controller, promise: null, older };
    flights.set(key, flight);
    flight.promise = (async () => {
      try {
        const page = normalizeObservationPage(await request(key, options));
        if (disposed) return;
        for (const id of reviewIds) if (personalIds.includes(id)) checkedReviews[key].add(id);
        const current = feeds[key];
        let next;
        if (initial) {
          next = { ...current, loaded: true, rows: page.items, reviewed: page.reviewedItems,
            hasMore: page.hasMore, nextBeforeId: page.nextBeforeId,
            afterId: Math.max(0, ...page.items.map(row => Number(row.id) || 0)),
          };
        } else if (older) {
          next = { ...current, rows: mergeObservationRows(current.rows, page.items),
            hasMore: page.hasMore, nextBeforeId: page.nextBeforeId,
          };
        } else {
          const visible = new Set(current.rows.map(row => String(row.id)));
          const known = new Set([...current.rows, ...current.buffered, ...page.items].map(row => Number(row.id)));
          const recovered = page.reviewedItems.filter(row => personalIds.includes(Number(row.id)) && !known.has(Number(row.id)));
          const recoveredIds = new Set(recovered.map(row => String(row.id)));
          const touched = [...page.items, ...page.reviewedItems.filter(row => !recoveredIds.has(String(row.id)))];
          const buffered = mergeObservationRows(current.buffered, touched.filter(row => !visible.has(String(row.id))));
          const rows = mergeObservationRows(current.rows, touched.filter(row => visible.has(String(row.id))));
          next = { ...current, rows, buffered,
            reviewed: mergeObservationRows(current.reviewed, [...recovered, ...touched.filter(row => current.reviewed.some(old => String(old.id) === String(row.id)))]),
            afterId: Math.max(current.afterId, page.nextAfterId || 0),
          };
          pendingOffsets[key] = offset + pendingIds.filter(id => reviewIds.includes(id)).length;
          if (canReveal(key)) next = { ...next, rows: mergeObservationRows(rows, buffered), buffered: [] };
        }
        publish(key, { ...next, error: '' });
      } catch (error) {
        if (!disposed) publish(key, { ...feeds[key], error: error?.name === 'AbortError'
          ? '读取记录超时，已保留当前列表。' : error?.message || '暂时无法读取记录，已保留当前列表。' });
      } finally {
        clearTimeout(timer);
        flights.delete(key);
        if (!disposed) publish(key, { ...feeds[key], loading: false, loadingEarlier: false });
      }
    })();
    return flight.promise;
  }
  return { snapshot: () => feeds, refresh: key => read(key), loadEarlier: key => read(key, true), reveal,
    refreshReviewed: key => flights.has(key) ? flights.get(key).promise.then(() => read(key)) : read(key),
    dispose() { disposed = true; for (const { controller } of flights.values()) controller.abort(); },
  };
}
