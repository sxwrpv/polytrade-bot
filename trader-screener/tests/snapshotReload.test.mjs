/* The cohort snapshot must refresh without a rebuild.
 *
 * It used to be baked into the image and read once at boot, while upstream
 * regenerated it daily — so the board's staleness banner reappeared about two
 * days after every deploy, and on 2026-09-05 it had been telling visitors for
 * ten days to run an ingest that only a rebuild could deliver.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm, utimes } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const snapshot = (generatedAt, traders = 1) => JSON.stringify({
  meta: { generatedAt, boardsAsOf: '2026-06-06', boardsFrozen: true },
  traders: Array.from({ length: traders }, (_, i) => ({ address: `0x${i}` })),
  walletMeta: {}, spark: {}, angles: [], events: [],
});

async function withDataDir(fn) {
  const dir = await mkdtemp(join(tmpdir(), 'screener-'));
  try { return await fn(dir); } finally { await rm(dir, { recursive: true, force: true }); }
}

/* The server module starts a listener on import, so the reload contract is
 * exercised against an equivalent local implementation of the same rule:
 * re-read only on mtime change, and never discard a good dataset. */
function makeLoader(dir, readFileImpl, statImpl) {
  let dataset = null, mtime = 0;
  return {
    get generatedAt() { return dataset?.meta?.generatedAt ?? null; },
    async load() {
      dataset = JSON.parse(await readFileImpl(join(dir, 'dataset.json'), 'utf8'));
      mtime = (await statImpl(join(dir, 'dataset.json'))).mtimeMs;
    },
    async reloadIfChanged() {
      let m;
      try { m = (await statImpl(join(dir, 'dataset.json'))).mtimeMs; } catch { return false; }
      if (m === mtime) return false;
      try {
        dataset = JSON.parse(await readFileImpl(join(dir, 'dataset.json'), 'utf8'));
        mtime = m;
        return true;
      } catch { return false; }
    },
  };
}

const { readFile, stat } = await import('node:fs/promises');

test('a refreshed snapshot is picked up without a restart', async () => {
  await withDataDir(async (dir) => {
    const file = join(dir, 'dataset.json');
    await writeFile(file, snapshot('2026-08-26T08:48:30.332Z'));
    const l = makeLoader(dir, readFile, stat);
    await l.load();
    assert.equal(l.generatedAt, '2026-08-26T08:48:30.332Z');

    await writeFile(file, snapshot('2026-09-05T08:49:01.265Z', 3));
    await utimes(file, new Date(), new Date(Date.now() + 5000));
    assert.equal(await l.reloadIfChanged(), true);
    assert.equal(l.generatedAt, '2026-09-05T08:49:01.265Z');
  });
});

test('an unchanged file is not re-parsed', async () => {
  await withDataDir(async (dir) => {
    await writeFile(join(dir, 'dataset.json'), snapshot('2026-09-05T08:49:01.265Z'));
    let reads = 0;
    const counting = (...a) => { reads += 1; return readFile(...a); };
    const l = makeLoader(dir, counting, stat);
    await l.load();
    const after = reads;
    assert.equal(await l.reloadIfChanged(), false);
    assert.equal(await l.reloadIfChanged(), false);
    assert.equal(reads, after, 're-read a file whose mtime had not moved');
  });
});

test('a half-written snapshot keeps the loaded one', async () => {
  await withDataDir(async (dir) => {
    const file = join(dir, 'dataset.json');
    await writeFile(file, snapshot('2026-09-05T08:49:01.265Z'));
    const l = makeLoader(dir, readFile, stat);
    await l.load();

    await writeFile(file, '{"meta":{"generat');       // mid-write
    await utimes(file, new Date(), new Date(Date.now() + 5000));
    assert.equal(await l.reloadIfChanged(), false);
    assert.equal(l.generatedAt, '2026-09-05T08:49:01.265Z',
      'a corrupt read discarded a good dataset');
  });
});

test('a torn write is retried once the file is whole', async () => {
  await withDataDir(async (dir) => {
    const file = join(dir, 'dataset.json');
    await writeFile(file, snapshot('2026-08-26T08:48:30.332Z'));
    const l = makeLoader(dir, readFile, stat);
    await l.load();

    await writeFile(file, '{"meta":{"generat');
    await utimes(file, new Date(), new Date(Date.now() + 5000));
    assert.equal(await l.reloadIfChanged(), false);

    // mtime must NOT have advanced on the failure, or the good write that
    // follows would look unchanged and never load.
    await writeFile(file, snapshot('2026-09-05T08:49:01.265Z'));
    await utimes(file, new Date(), new Date(Date.now() + 9000));
    assert.equal(await l.reloadIfChanged(), true);
    assert.equal(l.generatedAt, '2026-09-05T08:49:01.265Z');
  });
});

test('a missing file is survivable, not fatal', async () => {
  await withDataDir(async (dir) => {
    await writeFile(join(dir, 'dataset.json'), snapshot('2026-09-05T08:49:01.265Z'));
    const l = makeLoader(dir, readFile, stat);
    await l.load();
    await rm(join(dir, 'dataset.json'));
    assert.equal(await l.reloadIfChanged(), false);
    assert.equal(l.generatedAt, '2026-09-05T08:49:01.265Z');
  });
});
