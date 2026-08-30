async () => {
  // Chart repaint, evaluated by core/services/render.py before the settle
  // wait (the `force_resize` option). Charts mounted before their container's
  // size settled draw shrunk into the top-left corner; forcing a resize makes
  // them re-read the container and repaint at the final size.
  //
  // Recharts has no public getInstanceByDom. Its ResponsiveContainer watches the
  // container via ResizeObserver, so it needs a *genuine* size change: shrink to
  // 1px, wait two animation frames so the observer batches its callback, then
  // restore. ResizeObserver fires on both edges. A no-op on a page with no
  // responsive container.

  const rcs = document.querySelectorAll('.recharts-responsive-container');
  if (rcs.length === 0) return;
  const snap = [];
  for (const el of rcs) {
    snap.push({el, w: el.style.width, h: el.style.height});
    el.style.width = '1px';
    el.style.height = '1px';
  }
  // Two RAFs: first delivers the shrink, second is recharts' settle frame.
  // ResizeObserver coalesces multiple changes within one frame into one
  // delivery.
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  for (const s of snap) {
    s.el.style.width = s.w;
    s.el.style.height = s.h;
  }
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
}
