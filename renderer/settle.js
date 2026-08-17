(budgetMs) => new Promise(resolve => {
  // Settle probe, evaluated by core/services/render.py before the audit and
  // the screenshot. Resolves once the widget stops changing: fonts applied,
  // every <img> settled, and no DOM mutation for two consecutive animation
  // frames. Replaces a fixed sleep so the audit and the screenshot describe
  // the same finished state.
  const root = document.getElementById('widget-root');
  if (!root) { resolve({quiet: false, reason: 'no-root'}); return; }
  const deadline = performance.now() + budgetMs;
  let done = false;
  const finish = (reason) => { if (!done) { done = true; resolve({quiet: reason === 'quiet', reason}); } };

  const images = [...root.querySelectorAll('img')].filter(i => !i.complete);
  const waits = [document.fonts ? document.fonts.ready : Promise.resolve()];
  for (const img of images) {
    waits.push(new Promise(r => {
      img.addEventListener('load', r, {once: true});
      img.addEventListener('error', r, {once: true});
    }));
  }

  Promise.all(waits).then(() => {
    let quiet = 0;
    const observer = new MutationObserver(() => { quiet = 0; });
    observer.observe(root, {subtree: true, childList: true, attributes: true, characterData: true});
    const tick = () => {
      if (done) return;
      if (performance.now() > deadline) { observer.disconnect(); finish('budget'); return; }
      if (++quiet >= 2) { observer.disconnect(); finish('quiet'); return; }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });

  setTimeout(() => finish('budget'), budgetMs);
})
