() => {
  // Render audit, evaluated by core/services/render.py after the page settles.
  // One pass over #widget-root collecting every fact the PNG cannot show.
  // Each note is a measurement of the rendered result, never a console
  // message: console text drifts with the browser version, repeats itself,
  // and says nothing about elements the browser silently accepted.
  //
  //   overflow   in-flow content extends past the root's border-box. The
  //              screenshot is clipped at that box, so the excess is
  //              invisible. Skips are deliberate and were validated against
  //              the reference implementations that produced the targets:
  //              absolute/fixed subtrees, anything inside an <svg> (its own
  //              viewBox clips it), and negative-margin subtrees are
  //              conventional intentional overhang — adding them back drops
  //              the signal ratio from 72:1 to about 2:1. Content already cut
  //              off by a nearer overflow!=visible ancestor never reaches the
  //              widget edge and is not attributed to the widget.
  //   unpainted  the author wrote geometry and the browser painted no pixel
  //              of it — a rejected attribute, or a degenerate shape. Reads
  //              in the PNG as empty space, i.e. indistinguishable from "the
  //              design has nothing here".
  //   unloaded   an <img> that never decoded.
  //   zero_size  a drawing surface with no area; nothing inside it can
  //              appear. Reported instead of its children, which would
  //              otherwise each be listed as unpainted.
  //
  // EPS is the per-side overflow tolerance in CSS pixels.
  const root = document.getElementById('widget-root');
  if (!root) return null;
  const rr = root.getBoundingClientRect();
  const EPS = 1;
  const notes = [];
  const clamp = (s, n) => String(s == null ? '' : s).trim().slice(0, n);

  // `textContent` runs the children together — a card whose lines are separate
  // <div>s reads back as "Quarterly Revenue ReportTotal 1,284,300". Joining the
  // text nodes keeps the excerpt legible to whoever has to act on the note.
  const textOf = (el) => {
    const parts = [];
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      const t = n.nodeValue.trim();
      if (t) parts.push(t);
    }
    return parts.join(' ');
  };
  const intersect = (a, b) => ({left: Math.max(a.left, b.left), top: Math.max(a.top, b.top),
                                right: Math.min(a.right, b.right), bottom: Math.min(a.bottom, b.bottom)});

  // An author who hides a subtree meant to hide it. Nothing under it can be a
  // defect, and `getComputedStyle(child).display` does not inherit the parent's
  // `none`, so the ancestor chain has to be walked explicitly.
  const hidden = (el) => {
    for (let a = el; a && a !== root.parentElement; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return true;
    }
    return false;
  };

  // ---- zero-size drawing surfaces (checked first: they explain unpainted) ----
  const deadSurfaces = new Set();
  for (const el of root.querySelectorAll(
      '[_echarts_instance_], .recharts-wrapper, .recharts-surface, canvas, svg')) {
    const r = el.getBoundingClientRect();
    if (r.width >= 2 && r.height >= 2) continue;
    // Suppress the shapes inside either way; only report a surface the author
    // expected to be visible.
    deadSurfaces.add(el);
    if (hidden(el)) continue;
    notes.push({kind: 'zero_size', tag: el.tagName.toLowerCase(),
                w: Math.round(r.width), h: Math.round(r.height)});
  }

  // ---- overflow ----
  // An element crosses the edge because its ancestor does; reporting the whole
  // subtree says one thing many times. Over a 4,210-render collection that is
  // 441 notes for 111 actual problems, and one widget alone produced 118 — 108
  // of them 1x4px tick marks under a single container overflowing by 644px.
  // Only the outermost element responsible for a side is reported; a descendant
  // is reported only for a side no ancestor is already accountable for.
  const reported = new Map();               // element -> sides attributed to it
  const covered = (el, side) => {
    for (let a = el.parentElement; a && a !== root.parentElement; a = a.parentElement) {
      const sides = reported.get(a);
      if (sides && sides.has(side)) return true;
    }
    return false;
  };

  for (const el of root.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (hidden(el)) continue;
    let skip = false, depth = 0;
    for (let a = el; a && a !== root; a = a.parentElement) {
      depth++;
      if (a.tagName.toLowerCase() === 'svg') { skip = true; break; }
      const acs = getComputedStyle(a);
      if (acs.position === 'absolute' || acs.position === 'fixed') { skip = true; break; }
      if (parseFloat(acs.marginTop)    < 0 || parseFloat(acs.marginRight)  < 0 ||
          parseFloat(acs.marginBottom) < 0 || parseFloat(acs.marginLeft)   < 0) { skip = true; break; }
    }
    if (skip) continue;
    let clip = null;
    for (let a = el.parentElement; a && a !== root; a = a.parentElement) {
      const acs = getComputedStyle(a);
      if (acs.overflowX !== 'visible' || acs.overflowY !== 'visible') {
        const ar = a.getBoundingClientRect();
        clip = clip ? intersect(clip, ar) : ar;
      }
    }
    const box = clip ? intersect(r, clip) : r;
    const sides = [['right', box.right - rr.right], ['bottom', box.bottom - rr.bottom],
                   ['left', rr.left - box.left],    ['top', rr.top - box.top]]
                  .filter(([, v]) => v > EPS);
    const own = sides.filter(([side]) => !covered(el, side));
    if (!own.length) continue;
    reported.set(el, new Set(own.map(([side]) => side)));
    for (const [side, amount] of own) {
      notes.push({kind: 'overflow', side, amount: Math.round(amount), depth,
                  tag: el.tagName.toLowerCase(), w: Math.round(r.width), h: Math.round(r.height),
                  text: clamp(textOf(el), 40)});
    }
  }

  // ---- authored geometry that paints nothing ----
  const GEOMETRY_ATTR = {path: 'd', polygon: 'points', polyline: 'points',
                         circle: 'r', ellipse: 'rx', rect: 'width', line: 'x2'};
  for (const el of root.querySelectorAll(Object.keys(GEOMETRY_ATTR).join(','))) {
    const tag = el.tagName.toLowerCase();
    if (el.closest('defs, clipPath, mask, pattern, marker, symbol')) continue;
    // A shape inside a surface that has no area cannot paint for a reason that
    // has nothing to do with its own geometry; the surface is the real note.
    let dead = false;
    for (let a = el; a && a !== root; a = a.parentElement) {
      if (deadSurfaces.has(a)) { dead = true; break; }
    }
    if (dead) continue;
    if (hidden(el)) continue;
    const cs = getComputedStyle(el);
    const noFill = cs.fill === 'none' || cs.fill === 'rgba(0, 0, 0, 0)';
    const noStroke = cs.stroke === 'none' || cs.stroke === 'rgba(0, 0, 0, 0)';
    if (noFill && noStroke) continue;
    // A zero-length stroked path with a round/square cap still paints a dot;
    // Lucide/Feather icons rely on it (`d="M12 20h.01"`).
    if (!noStroke && (cs.strokeLinecap === 'round' || cs.strokeLinecap === 'square')) continue;
    const attr = GEOMETRY_ATTR[tag];
    const written = clamp(el.getAttribute(attr), 200);
    if (!written) continue;                       // nothing was authored here
    const r = el.getBoundingClientRect();         // includes stroke width
    if (r.width >= 0.5 || r.height >= 0.5) continue;
    notes.push({kind: 'unpainted', tag, attr, value: written.slice(0, 60)});
  }

  // ---- images that never decoded ----
  for (const img of root.querySelectorAll('img')) {
    if (img.complete && img.naturalWidth > 0) continue;
    if (hidden(img)) continue;
    notes.push({kind: 'unloaded', src: clamp(img.getAttribute('src'), 70)});
  }
  return notes;
}
