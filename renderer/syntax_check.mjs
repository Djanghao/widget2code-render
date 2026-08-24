// Parse generated JSX with the exact esbuild frozen in this renderer image.
// One JSON object is emitted per input path so Python can preserve ordering.
import { transform } from 'esbuild';
import { readFile } from 'node:fs/promises';

for (const path of process.argv.slice(2)) {
  let result;
  try {
    const source = await readFile(path, 'utf8');
    await transform(source, { loader: 'jsx', jsx: 'transform' });
    result = { path, ok: true };
  } catch (error) {
    const errors = (error.errors ?? []).map((diagnostic) => ({
      text: diagnostic.text,
      line: diagnostic.location?.line ?? null,
      column: diagnostic.location?.column ?? null,
      length: diagnostic.location?.length ?? null,
      lineText: diagnostic.location?.lineText ?? null,
    }));
    result = {
      path,
      ok: false,
      errors: errors.length ? errors : [{
        text: String(error.message ?? error),
        line: null,
        column: null,
        length: null,
        lineText: null,
      }],
    };
  }
  process.stdout.write(`${JSON.stringify(result)}\n`);
}
