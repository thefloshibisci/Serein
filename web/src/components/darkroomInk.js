// Reveal live Markdown glyphs while preserving its structure.
const graphemes = typeof Intl.Segmenter === "function" ? new Intl.Segmenter("zh-CN", { granularity: "grapheme" }) : null;
const words = typeof Intl.Segmenter === "function" ? new Intl.Segmenter("zh-CN", { granularity: "word" }) : null;
const split = value => graphemes ? [...graphemes.segment(value)].map(item => item.segment) : Array.from(value);
const element = (className, children, properties = {}) => ({ type: "element", tagName: "span", properties: { className: [className], ...properties }, children });

export function darkroomInkMarkup() {
  return tree => {
    let initial = false;
    let paragraph = 0;
    function visit(node, blockIndex = 0) {
      if (!node.children || ["code", "pre"].includes(node.tagName)) return;
      const isParagraph = node.tagName === "p";
      if (isParagraph) blockIndex = paragraph++;
      node.children = node.children.flatMap(child => {
        if (child.type !== "text") { visit(child, blockIndex); return [child]; }
        const groups = [];
        let previous;
        for (const unit of words ? [...words.segment(child.value)].map(item => item.segment) : split(child.value)) {
          if (/^\s+$/u.test(unit)) { groups.push({ type: "text", value: unit }); continue; }
          const closing = /^[，。！？、；：）》」』】…,.!?;:)+]+$/u.test(unit);
          const group = closing && previous ? previous : element(split(unit).length <= 24 ? "darkroom-ink-word" : "darkroom-ink-unit", []);
          if (group !== previous) groups.push(group);
          for (const glyph of split(unit)) {
            const raised = !initial && blockIndex === 0 && paragraph > 0;
            if (raised) initial = true;
            group.children.push(element(`darkroom-ink-glyph${raised ? " darkroom-ink-initial" : ""}`, [{ type: "text", value: glyph }], { "data-ink-block": blockIndex }));
          }
          previous = group;
        }
        return [element("darkroom-ink-run", [
          element("sr-only", [{ type: "text", value: child.value }]),
          element("darkroom-ink-visual", groups, { ariaHidden: "true" }),
        ])];
      });
    }
    visit(tree);
  };
}

export function playDarkroomInk(container) {
  const glyphs = [...container.querySelectorAll(".darkroom-ink-glyph")];
  if (!glyphs.length || !container.animate || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches || document.hidden) return () => {};
  // One clock for the entire letter: later paragraphs wait for earlier text.
  const queue = glyphs.map((glyph, index) => ({ glyph, at: index * 22 }));
  glyphs.forEach(glyph => glyph.classList.add("is-pending"));
  let frame = 0;
  let next = 0;
  const animations = new Set();
  const start = performance.now();
  function finish() {
    window.cancelAnimationFrame(frame);
    glyphs.forEach(glyph => glyph.classList.remove("is-pending"));
    animations.forEach(animation => animation.cancel());
    animations.clear();
    document.removeEventListener("visibilitychange", onVisibility);
  }
  function onVisibility() { if (document.hidden) finish(); }
  function tick(now) {
    while (next < queue.length && queue[next].at <= now - start) {
      const { glyph } = queue[next++];
      glyph.classList.remove("is-pending");
      const animation = glyph.animate([
        { opacity: 0, filter: "blur(3px)" },
        { opacity: 1, filter: "blur(0px)" },
      ], { duration: 180, easing: "cubic-bezier(.215,.61,.355,1)", fill: "backwards" });
      animations.add(animation);
      animation.onfinish = () => {
        animations.delete(animation);
        animation.cancel();
        if (next === queue.length && !animations.size) document.removeEventListener("visibilitychange", onVisibility);
      };
    }
    if (next < queue.length) frame = window.requestAnimationFrame(tick);
  }
  document.addEventListener("visibilitychange", onVisibility);
  frame = window.requestAnimationFrame(tick);
  return finish;
}
