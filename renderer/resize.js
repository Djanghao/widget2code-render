async () => {
  // Chart repaint, evaluated by core/services/render.py before the settle
  // wait (the `force_resize` option). Charts mounted before their container's
  // size settled draw shrunk into the top-left corner; forcing a resize makes
  // them re-read the container and repaint at the final size.
  //
  // Two libraries, two mechanisms:
  // - ECharts: direct API. Walk `[_echarts_instance_]` elements and call
  //   `instance.resize()`; echarts is bound on window by main.jsx for this.
  // - Recharts: no public getInstanceByDom. Its ResponsiveContainer watches
  //   the container via ResizeObserver, so it needs a *genuine* size change:
  //   shrink to 1px, wait two animation frames so the observer batches its
  //   callback, then restore. ResizeObserver fires on both edges.
  //
  // Both branches are no-ops when their library isn't on the page.
  if (window.echarts) {
    for (const el of document.querySelectorAll('[_echarts_instance_]')) {
      const inst = window.echarts.getInstanceByDom(el);
      if (inst) inst.resize();
    }
  }

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
