import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createObservationFeed, emptyObservationFeeds, observationSources } from '../storage/recallObservationFeed.js';
import { recallObservationPageLimits } from '../storage/recallObservationPagination.js';

function scrollContainer(element) {
  for (let parent = element?.parentElement; parent; parent = parent.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(parent).overflowY) && parent.scrollHeight > parent.clientHeight) return parent;
  }
  return document.scrollingElement;
}
function viewportTop(scroller) {
  return !scroller || scroller === document.scrollingElement ? 0 : scroller.getBoundingClientRect().top;
}
function captureAnchor(root) {
  if (!root) return null;
  const scroller = scrollContainer(root);
  const top = viewportTop(scroller);
  const row = [...root.querySelectorAll('[data-observation-id]')].find(element => element.getBoundingClientRect().bottom > top);
  return row ? { element: row, top: row.getBoundingClientRect().top, scroller } : null;
}
function atHead(root) {
  if (!root || root.contains(document.activeElement) && document.activeElement.matches('input,textarea,select')) return false;
  if (root?.querySelector('.observation-draft, details[open]')) return false;
  return root.getBoundingClientRect().top >= viewportTop(scrollContainer(root)) - 24;
}

export function useRecallObservationFeed({ source, reviews, ready, rootRef, bottomRef }) {
  const [feeds, setFeeds] = useState(emptyObservationFeeds);
  const controllerRef = useRef(null);
  const currentRef = useRef({ source, reviews });
  const anchorRef = useRef(null);
  const previousReviews = useRef(reviews);
  currentRef.current = { source, reviews };

  useLayoutEffect(() => {
    const anchor = anchorRef.current;
    anchorRef.current = null;
    if (anchor?.source === source && anchor.element.isConnected && anchor.scroller) {
      anchor.scroller.scrollTop += anchor.element.getBoundingClientRect().top - anchor.top;
    }
  }, [feeds, source]);

  useEffect(() => {
    if (!ready) return;
    const controller = createObservationFeed({
      reviews: () => currentRef.current.reviews,
      canReveal: key => key === currentRef.current.source && atHead(rootRef.current),
      onChange(next) {
        const root = rootRef.current;
        if (!anchorRef.current && !atHead(root)) {
          const anchor = captureAnchor(root);
          if (anchor) anchorRef.current = { ...anchor, source: currentRef.current.source };
        }
        setFeeds(next);
      },
      async request(key, { signal, ...options }) {
        const response = await fetch(key === 'hook' ? '/__serein/assistant-bridge/hook-injections' : '/__serein/gateway/injections', {
          method: 'POST', cache: 'no-store', signal, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ limit: recallObservationPageLimits[key], ...options }),
        });
        const payload = await response.json();
        if (!response.ok || payload?.status !== 'ok') throw new Error(payload?.message || payload?.error || '没有读到记录');
        return payload;
      },
    });
    controllerRef.current = controller;
    for (const key of observationSources) controller.refresh(key);
    return () => { controller.dispose(); controllerRef.current = null; anchorRef.current = null; };
  }, [ready, rootRef]);

  useEffect(() => {
    if (previousReviews.current === reviews) return;
    previousReviews.current = reviews;
    for (const key of observationSources) controllerRef.current?.refreshReviewed(key);
  }, [reviews]);

  // Only poll while this component and browser tab are visible. A recursive
  // timer and the feed's per-source lock prevent overlapping network reads.
  useEffect(() => {
    if (!ready) return;
    let cancelled = false;
    let timer;
    let running = false;
    const tick = async () => {
      clearTimeout(timer);
      if (cancelled || document.hidden || running) return;
      running = true;
      try { await controllerRef.current?.refresh(source); } finally { running = false; }
      if (!cancelled && !document.hidden) timer = setTimeout(tick, 5000);
    };
    const resume = () => { clearTimeout(timer); if (!document.hidden) tick(); };
    tick();
    document.addEventListener('visibilitychange', resume);
    window.addEventListener('focus', resume);
    return () => { cancelled = true; clearTimeout(timer); document.removeEventListener('visibilitychange', resume); window.removeEventListener('focus', resume); };
  }, [ready, source]);

  const loaded = feeds[source].loaded;
  useEffect(() => {
    const target = bottomRef.current;
    if (!loaded || !target || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(entries => {
      if (!document.hidden && entries.some(entry => entry.isIntersecting)) controllerRef.current?.loadEarlier(source);
    }, { rootMargin: '0px 0px 80px 0px' });
    observer.observe(target);
    // Do not recreate after every response: a visible footer must not drain
    // the whole history when a filter leaves no matching cards on a page.
    return () => observer.disconnect();
  }, [loaded, source, bottomRef]);

  const refresh = useCallback(() => controllerRef.current?.refresh(currentRef.current.source), []);
  const loadEarlier = useCallback(key => controllerRef.current?.loadEarlier(key), []);
  const reveal = useCallback(() => {
    const root = rootRef.current;
    controllerRef.current?.reveal(currentRef.current.source);
    anchorRef.current = null;
    root?.scrollIntoView({ block: 'start', behavior: 'instant' });
  }, [rootRef]);
  return { feeds, refresh, loadEarlier, reveal };
}
