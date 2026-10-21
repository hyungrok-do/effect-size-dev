// Builds site/vendor/: the Anthropic TypeScript SDK bundled for the browser, pdf.js, and their
// licenses. Run with `npm ci && npm run build` in site/. The Pages workflow runs it on every deploy.

import { build } from 'esbuild';
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';

const vendor = new URL('./vendor/', import.meta.url);
const modules = new URL('./node_modules/', import.meta.url);
await mkdir(vendor, { recursive: true });

await build({
  stdin: { contents: 'export { default as Anthropic } from "@anthropic-ai/sdk";', resolveDir: new URL('.', import.meta.url).pathname },
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: 'es2022',
  minify: true,
  legalComments: 'eof',
  outfile: new URL('anthropic-sdk.mjs', vendor).pathname,
});

for (const name of ['pdf.min.mjs', 'pdf.worker.min.mjs']) {
  await copyFile(new URL(`pdfjs-dist/build/${name}`, modules), new URL(name, vendor));
}

const licenses = [];
for (const pkg of ['@anthropic-ai/sdk', 'pdfjs-dist']) {
  const meta = JSON.parse(await readFile(new URL(`${pkg}/package.json`, modules), 'utf8'));
  const text = await readFile(new URL(`${pkg}/LICENSE`, modules), 'utf8');
  licenses.push(`${meta.name} ${meta.version}\n${'='.repeat(60)}\n${text.trim()}\n`);
}
await writeFile(new URL('LICENSES.txt', vendor), licenses.join('\n\n'));
console.log('built site/vendor/');
