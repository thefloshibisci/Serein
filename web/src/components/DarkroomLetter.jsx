import { identityName } from "../storage/instanceStore.js";
import { useId, useLayoutEffect, useRef, useState } from "react";
import { LockSimple } from "@phosphor-icons/react";
import { MarkdownProjection } from "./MarkdownProjection.jsx";
import { WhisperTitle } from "./WhisperTitle.jsx";

export function DarkroomLetter({ entry, open, sealed, clock, unlockLabel, countdown, onToggle }) {
  const id = useId();
  const foldRef = useRef(null);
  const itemRef = useRef(null);
  const frameRef = useRef(null);
  const [retained, setRetained] = useState(open);
  const remaining = Math.max(0, Math.ceil((new Date(entry.unlockAt).getTime() - clock) / 1000));

  useLayoutEffect(() => {
    if (sealed) return;
    const item = itemRef.current;
    const title = item.querySelector("strong");
    const draw = () => {
      const bounds = item.getBoundingClientRect();
      const label = title.getBoundingClientRect();
      const width = Math.max(2, bounds.width);
      const y = label.top - bounds.top + label.height / 2;
      const bottom = Math.max(y, bounds.height - 20);
      const startLeft = Math.max(1, label.left - bounds.left - 12);
      const startRight = Math.min(width - 1, label.right - bounds.left + 12);
      const [left, right] = frameRef.current.children;
      left.setAttribute("d", `M${startLeft} ${y}H1V${bottom}H${width / 2}`);
      right.setAttribute("d", `M${startRight} ${y}H${width - 1}V${bottom}H${width / 2}`);
    };
    draw();
    if (!window.ResizeObserver) return;
    const observer = new window.ResizeObserver(draw);
    observer.observe(item);
    observer.observe(title);
    return () => observer.disconnect();
  }, [open, retained, sealed]);

  useLayoutEffect(() => {
    const fold = foldRef.current;
    const from = fold.getBoundingClientRect().height;
    if (open) setRetained(true);
    fold.style.height = open ? "auto" : "0px";
    const to = open ? fold.scrollHeight : 0;
    if (from === to || !fold.animate || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      setRetained(open);
      return;
    }
    const timing = { duration: sealed ? (open ? 520 : 400) : (open ? 760 : 520), easing: "cubic-bezier(.22,1,.36,1)" };
    const animation = fold.animate([{ height: `${from}px` }, { height: `${to}px` }], timing);
    const notice = sealed ? fold.querySelector(".darkroom-letter-wait") : null;
    const slide = notice?.animate([
      { transform: open ? "translateY(-18px)" : "translateY(0px)" },
      { transform: open ? "translateY(0px)" : "translateY(-18px)" },
    ], { ...timing, fill: "both" });
    animation.finished.then(() => { if (!open) setRetained(false); }, () => {});
    return () => {
      const height = fold.getBoundingClientRect().height;
      animation.cancel();
      slide?.cancel();
      fold.style.height = `${height}px`;
    };
  }, [open, sealed]);

  const waiting = <span className="darkroom-letter-wait" role="status">
    <LockSimple size={15} weight="light" aria-hidden="true" />
    <span>
      <time dateTime={entry.unlockAt || undefined}>{unlockLabel} 开启</time>
      <span className="darkroom-letter-countdown">{remaining > 0 ? `还有 ${countdown(remaining)}` : remaining === 0 ? "正在等待信件开启。" : "等待约定的开启时间。"}</span>
    </span>
  </span>;

  return <div ref={itemRef} className={`darkroom-letter-item${open && !sealed ? " is-open" : ""}`}>
    {!sealed && <svg ref={frameRef} className="darkroom-letter-frame" aria-hidden="true" focusable="false">
      <path pathLength="1" /><path pathLength="1" />
    </svg>}
    <button className={`darkroom-letter${sealed ? " darkroom-letter--sealed" : ""}`}
      id={`${id}-title`} type="button" aria-expanded={open} aria-controls={`${id}-body`}
      aria-label={sealed ? `未开启的信，${unlockLabel} 开启` : `读信：${entry.title}`} onClick={onToggle}>
      {sealed ? <>
        <span className="sr-only">未开启的信</span>
        <strong className="darkroom-letter-title--sealed" aria-hidden="true">未到约定的那一天</strong>
      </> : <WhisperTitle text={entry.title} />}
    </button>
    {sealed && <div ref={foldRef} className="darkroom-letter-sealed-notice" id={`${id}-body`} aria-hidden={!open} inert={!open}>
      {open || retained ? <div className="darkroom-letter-sealed-notice__inner">{waiting}</div> : null}
    </div>}
    {!sealed && <div ref={foldRef} className="darkroom-letter-fold" id={`${id}-body`} role="region"
      aria-labelledby={`${id}-title`} aria-hidden={!open} inert={!open}>
      {open || retained ? <div className="darkroom-letter-paper">
        <MarkdownProjection key={`${entry.id}-open`} className="darkroom-open-room__content darkroom-letter-ink" content={entry.body} />
        <footer className="darkroom-letter-signature">
          <time dateTime={`${entry.date}T${entry.time}`}>{entry.date.replaceAll("-", ".")} · {entry.time}</time>
          <span>{identityName(entry.role)}</span>
        </footer>
      </div> : null}
    </div>}
  </div>;
}
