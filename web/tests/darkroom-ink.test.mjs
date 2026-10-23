import test from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { playDarkroomInk } from "../src/components/darkroomInk.js";

function withPlayback(blockLengths, check) {
  const dom = new JSDOM('<div id="letter"></div>', { pretendToBeVisual: true });
  const previous = new Map(["window", "document"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const key of previous.keys()) Object.defineProperty(globalThis, key, { value: dom.window[key], configurable: true });
  const container = document.getElementById("letter");
  const revealed = [];
  let glyphIndex = 0;
  blockLengths.forEach((length, block) => {
    for (let index = 0; index < length; index++) {
      const glyph = document.createElement("span");
      glyph.className = "darkroom-ink-glyph";
      glyph.dataset.inkBlock = block;
      glyph.dataset.index = glyphIndex++;
      glyph.textContent = "雨";
      container.append(glyph);
    }
  });
  window.matchMedia = () => ({ matches: false });
  const frames = new Map();
  let nextFrame = 0;
  window.requestAnimationFrame = callback => { frames.set(++nextFrame, callback); return nextFrame; };
  window.cancelAnimationFrame = id => frames.delete(id);
  window.Element.prototype.animate = function () {
    if (this !== container) revealed.push(Number(this.dataset.index));
    return { cancel() {} };
  };
  let finish;
  try {
    finish = playDarkroomInk(container);
    const start = performance.now();
    const advance = elapsed => {
      const callbacks = [...frames.values()];
      frames.clear();
      callbacks.forEach(callback => callback(start + elapsed));
    };
    check({ container, revealed, advance });
  } finally {
    finish?.();
    dom.window.close();
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
}

test("long paragraphs reveal in document order without later paragraphs starting early", () => {
  withPlayback([320, 320, 320], ({ container, revealed, advance }) => {
    advance(550);
    assert.equal(container.querySelectorAll('[data-ink-block="1"]:not(.is-pending), [data-ink-block="2"]:not(.is-pending)').length, 0);
    advance(320 * 22 + 100);
    assert.equal(container.querySelectorAll('[data-ink-block="0"].is-pending').length, 0);
    assert.equal(container.querySelectorAll('[data-ink-block="2"]:not(.is-pending)').length, 0);
    advance(960 * 22);
    assert.deepEqual(revealed, Array.from({ length: 960 }, (_, index) => index));
  });
});

test("a long letter keeps its reading pace beyond 4.8 seconds", () => {
  withPlayback([900], ({ container, advance }) => {
    advance(4800);
    assert(container.querySelectorAll(".is-pending").length > 600, "Long text must not be squeezed into a fixed total duration");
    advance(12000);
    assert(container.querySelectorAll(".is-pending").length > 300);
    advance(900 * 22);
    assert.equal(container.querySelectorAll(".is-pending").length, 0);
  });
});
