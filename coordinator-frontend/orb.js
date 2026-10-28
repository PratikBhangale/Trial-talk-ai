// "Thinking" indicator for the participant chat: the thinking-orbs engine (Libraries.dev, MIT, vendored in
// ui/vendor/thinking-orbs) painted on a small canvas. The engine is pure geometry; this file only draws it.
// "breathing" is the calmest of its nine states: a ring slowly morphing, right for a health context.

import { resolvePreset, MODE_FRAMES, paintFrame } from '/ui/vendor/thinking-orbs/engine.es.js';

const SIZE = 20;  // the engine's inline-text preset

function isDark() {
  const t = document.documentElement.dataset.theme;
  return t ? t === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
}

// Draws into the canvas until the returned stop() is called. Reduced motion: one still frame.
window.mountOrb = canvas => {
  const dpr = Math.min(2, devicePixelRatio || 1), ctx = canvas.getContext('2d');
  canvas.width = canvas.height = SIZE * dpr;
  const { mode, speed, opts } = resolvePreset('breathing', SIZE);
  const draw = t => {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, SIZE, SIZE);
    paintFrame(ctx, MODE_FRAMES[mode](SIZE, t * speed, opts), isDark());
  };
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) { draw(0.8); return () => {}; }
  let raf, start = performance.now();
  const loop = now => { draw((now - start) / 1000); raf = requestAnimationFrame(loop); };
  raf = requestAnimationFrame(loop);
  return () => cancelAnimationFrame(raf);
};
