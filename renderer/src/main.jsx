import React from 'react';
import { createRoot } from 'react-dom/client';
import ReactECharts from 'echarts-for-react';
import * as echarts from 'echarts';
import * as Recharts from 'recharts';

// The widget jsx files have NO imports and reference React, ReactECharts,
// and Recharts as free identifiers. JS resolves free variables on
// globalThis, so exposing them here is enough.
window.React = React;
window.ReactECharts = ReactECharts;
// Recharts is a single namespace; widgets use `Recharts.LineChart`,
// `Recharts.XAxis`, etc. Saves polluting window with ~40 names.
window.Recharts = Recharts;
// Exposed for the render service's force_resize path (and any other
// caller that needs to walk the page's chart instances).
window.echarts = echarts;

const params = new URLSearchParams(location.search);
const jsxPath = params.get('path');  // absolute filesystem path
const jsxVersion = params.get('v') || '0';

const status = (msg) => {
  console.log('[renderer]', msg);
  window.__widget_status = msg;
};

// One short line, no stack. The service turns this into the model-visible
// render error, and a stack of minified React frames localizes nothing.
const describe = (error) => {
  if (!error) return 'RuntimeError: unknown error';
  const name = error.name || 'RuntimeError';
  const message = String(error.message || error).split('\n')[0];
  return `${name}: ${message}`;
};

// First error wins: React reports the same failure through the boundary and
// through the window handler, and the boundary's message is the specific one.
//
// Once the widget is ready its PNG is obtainable, so a later throw — an event
// handler, a timer, a settled promise — is a diagnostic, not a render failure.
// Promoting those to __widget_error would fail widgets that screenshot fine.
window.__widget_late_errors = [];
const fail = (message) => {
  if (window.__widget_ready === true) {
    window.__widget_late_errors.push(message);
    return;
  }
  if (window.__widget_error) return;
  window.__widget_error = message;
  status('error');
};

// createRoot().render() is asynchronous, so a throw inside the component
// never reaches mount()'s try/catch. Without this boundary React unmounted
// the tree, `page-root` was left empty, and the only surviving evidence was
// an uncaught `pageerror` the service discarded — the render then failed as
// a 30s "waiting for #widget-root" screenshot timeout with no cause attached.
class WidgetBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error) {
    fail(describe(error));
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}

// Anything the boundary cannot see: module evaluation, event handlers,
// async callbacks, rejected promises.
window.addEventListener('error', (event) => {
  fail(event.error ? describe(event.error) : `RuntimeError: ${event.message || 'script error'}`);
});
window.addEventListener('unhandledrejection', (event) => {
  fail(describe(event.reason));
});

async function mount() {
  if (!jsxPath) {
    fail('RuntimeError: missing ?path=');
    return;
  }
  try {
    // Vite serves any file via /@fs/<abs_path> when server.fs.strict is
    // disabled. The render service spawns vite with that setting.
    // RenderService supplies the file mtime as a cache-buster. This keeps
    // successive edits fresh even though the Vite filesystem watcher is off.
    const url = `/@fs${jsxPath}?v=${encodeURIComponent(jsxVersion)}`;
    const mod = await import(/* @vite-ignore */ url);
    const Widget = mod.default;
    if (!Widget) throw new Error(`No default export in ${jsxPath}`);

    const pageRoot = document.getElementById('page-root');
    const root = createRoot(pageRoot);
    root.render(React.createElement(WidgetBoundary, null, React.createElement(Widget)));
    status('rendered');

    // Two RAFs so layout + initial echarts paint settle before screenshot.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (window.__widget_error) {
        status('error');
        return;
      }
      // Tag the widget's actual outer element so #widget-root selectors
      // target the real widget (with its inline width/height/overflow/
      // border-radius), not this page-level mount wrapper.
      const widget = pageRoot.firstElementChild;
      if (!widget) {
        // Never announce readiness without a screenshot target: the service
        // would wait out its full locator timeout for an element that can
        // never appear.
        fail('EmptyRender: the component produced no DOM element');
        return;
      }
      const box = widget.getBoundingClientRect();
      if (box.width < 1 || box.height < 1) {
        fail(
          `EmptyRender: the widget root element has zero size ` +
          `(${Math.round(box.width)}x${Math.round(box.height)}px)`
        );
        return;
      }
      widget.id = 'widget-root';
      window.__widget_ready = true;
      status('ready');
    }));
  } catch (e) {
    console.error(e);
    fail(describe(e));
  }
}

mount();
