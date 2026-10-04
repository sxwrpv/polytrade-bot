#!/usr/bin/env node
// Pulls the screener dataset that powers the board and caches it locally.
// Source: polycopy's public (unauthenticated) discover dataset endpoint —
// the same request their own browser client makes on page load.
import { writeFile, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const DATA = join(ROOT, 'data');

const SOURCES = {
  dataset: 'https://polycopy.app/api/v2/discover/dataset',
  smi: 'https://polycopy.app/api/indexes/smi?historyDays=45',
};

async function grab(name, url) {
  process.stdout.write(`  ${name.padEnd(8)} ${url}\n`);
  const res = await fetch(url, {
    headers: { accept: 'application/json', 'user-agent': 'polycopy-clone/1.0' },
  });
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
  const json = await res.json();
  const out = join(DATA, `${name}.json`);
  await writeFile(out, JSON.stringify(json));
  const bytes = Buffer.byteLength(JSON.stringify(json));
  process.stdout.write(`  ${''.padEnd(8)} → ${out} (${(bytes / 1e6).toFixed(2)} MB)\n`);
  return json;
}

await mkdir(DATA, { recursive: true });
console.log('Ingesting screener data…');
const ds = await grab('dataset', SOURCES.dataset);
await grab('smi', SOURCES.smi);

console.log('\nSnapshot summary');
console.log('  generatedAt   ', ds.meta.generatedAt);
console.log('  windowAnchor  ', ds.meta.windowAnchor);
console.log('  traders       ', ds.traders.length.toLocaleString());
console.log('  walletMeta    ', Object.keys(ds.walletMeta).length.toLocaleString());
console.log('  sparklines    ', Object.keys(ds.spark).length.toLocaleString());
console.log('  angle boards  ', ds.angles.length.toLocaleString());
console.log('  events        ', ds.events.length.toLocaleString());
const byClass = {};
for (const t of ds.traders) byClass[t.copyClass ?? 'none'] = (byClass[t.copyClass ?? 'none'] || 0) + 1;
console.log('  copy classes  ', JSON.stringify(byClass));
