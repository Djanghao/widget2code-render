import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [
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
