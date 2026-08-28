import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

const rendererEntry = path.resolve('src/main.jsx');

const resolveFrozenPackage = {
  name: 'w2c-resolve-frozen-package',
  enforce: 'pre',
  async resolveId(source, importer, options) {
    if (!importer || importer.startsWith(path.resolve('.')) || source.startsWith('.') || source.startsWith('/')) {
      return null;
    }
    // A generated JSX lives in the daemon's temporary directory. Resolve its
    // bare imports as if they came from this frozen Vite project, whose lockfile
    // and node_modules are part of the image. The Python source policy decides
    // which packages are allowed before the file reaches Vite.
    return this.resolve(source, rendererEntry, { ...options, skipSelf: true });
  },
};

export default defineConfig({
  plugins: [
    resolveFrozenPackage,
    react({
      // Treat .jsx files outside the renderer's own source tree as JSX.
      include: ['**/*.jsx', '**/*.tsx'],
    }),
  ],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    // The renderer always navigates to a cache-busted module URL, so it does
    // not need HMR or filesystem invalidation. Disabling the watcher avoids
    // exhausting the host-wide inotify limit during large trajectory runs.
    watch: null,
    fs: {
      // Allow Vite to serve files anywhere on disk via /@fs/<abs_path>.
      // The server is bound to 127.0.0.1, so this is local-only access.
      strict: false,
    },
    // HMR error overlay is broadcast over the WebSocket to *every* open
    // page. For headless rendering of many widgets in parallel, one broken
    // jsx would pollute screenshots of the rest. Disable.
    hmr: { overlay: false },
  },
});
