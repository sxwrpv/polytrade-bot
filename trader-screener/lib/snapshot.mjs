/* The cohort snapshot on disk: the shape the service reads off it, and the
 * write that replaces one of its files in a single step.
 *
 * scripts/ingest.mjs checks a download here before anything is written, and
 * server.mjs checks a re-read snapshot here before it replaces the one in
 * memory. A wrong answer from upstream then costs one refresh: the files on
 * disk and the dataset being served stay as they were. */
import { randomUUID } from 'node:crypto';
import { rename, rm, writeFile } from 'node:fs/promises';

const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

/** Throws unless `ds` carries every field server.mjs and the ingest summary
 *  read: meta.generatedAt, at least one trader, a wallet on every trader, and
 *  the per-wallet maps and board lists. Returns `ds`. */
export function assertDataset(ds) {
  if (!isObject(ds)) throw new Error('dataset: expected an object');
  if (!isObject(ds.meta) || typeof ds.meta.generatedAt !== 'string' || !ds.meta.generatedAt) {
    throw new Error('dataset.meta.generatedAt: expected a timestamp');
  }
  if (!Array.isArray(ds.traders) || !ds.traders.length) {
    throw new Error('dataset.traders: expected a list with at least one trader');
  }
  const bad = ds.traders.findIndex((t) => !isObject(t) || typeof t.w !== 'string' || !t.w);
  if (bad >= 0) throw new Error(`dataset.traders[${bad}].w: expected a wallet address`);
  for (const key of ['walletMeta', 'spark', 'wow', 'copyDelta']) {
    if (!isObject(ds[key])) throw new Error(`dataset.${key}: expected an object`);
  }
  for (const key of ['angles', 'events']) {
    if (!Array.isArray(ds[key])) throw new Error(`dataset.${key}: expected a list`);
  }
  return ds;
}

/** Throws unless `smi` is a JSON object, which is all /api/smi asks of it.
 *  Returns `smi`. */
export function assertSmi(smi) {
  if (!isObject(smi)) throw new Error('smi: expected an object');
  return smi;
}

/** Replace `path` with `body` in one step: the bytes go to a temp file beside
 *  the target, and a rename puts it in the target's place. A reader sees the
 *  old file or the new one, whole. A failed write removes the temp file and
 *  leaves the target as it was. */
export async function writeFileAtomic(path, body) {
  const temp = `${path}.${randomUUID()}.tmp`;
  try {
    await writeFile(temp, body);
    await rename(temp, path);
  } catch (error) {
    await rm(temp, { force: true });
    throw error;
  }
}
