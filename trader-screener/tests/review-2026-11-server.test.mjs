/* Review 2026-11, the screener's server side.
 *
 * server.mjs starts a listener when it loads, so these tests run it the way
 * production does: as its own process, on its own port. Each test gets a
 * private copy of the service (server.mjs, lib/ and scripts/ as they are in
 * the repo) with a small public/ and data/ of its own, which the test is free
 * to rewrite. The server runs in snapshot mode and the ingest runs with fetch
 * replaced, so nothing here leaves the machine.
 *
 *   S1   a request target that is no URL gets 400, and a rejection nobody
 *        awaited is logged; the process keeps serving after both
 *   S7   static files carry a weak ETag and answer 304 to a matching one
 *   S10  /api/dataset serves one body serialised at load, with an ETag
 *   S8x  a snapshot is checked before it is written or swapped in, and the
 *        ingest replaces a file by rename
 *   S14  lib/copyscore.mjs exports the recomputation only; ranks() settles
 *        with a value whatever upstream does
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import http from 'node:http';
import net from 'node:net';
import {
  cp, mkdir, mkdtemp, readdir, readFile, rename, rm, stat, utimes, writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { assertDataset, assertSmi, writeFileAtomic } from '../lib/snapshot.mjs';
import * as copyscore from '../lib/copyscore.mjs';

// The rate gate reads its rate when the module loads; these tests are about
// what ranks() returns, so the gate is set wide open before the import.
process.env.SCREENER_POLYMARKET_RPS = '10000';
const pm = await import('../lib/polymarket.mjs');

const SCREENER = join(dirname(fileURLToPath(import.meta.url)), '..');

// --- fixtures ---------------------------------------------------------------
const wallet = (n) => `0x${String(n).repeat(40)}`;
const FIRST = '2026-11-01T08:00:00.000Z';
const SECOND = '2026-11-02T08:00:00.000Z';
const THIRD = '2026-11-03T08:00:00.000Z';

/** A cohort with every field the shape check asks for. The second name holds
 *  multi-byte characters, so a byte count and a character count differ. */
const cohort = (generatedAt, names = ['Ada', 'Zoë · 東']) => ({
  meta: { generatedAt, windowAnchor: generatedAt.slice(0, 10) },
  traders: names.map((name, i) => ({ w: wallet(i + 1), name, pnl: 1500 + i, copyClass: 'strong', copyNet: 11.5 })),
  walletMeta: {}, spark: {}, wow: {}, copyDelta: {}, angles: [], events: [],
});
const SMI = { score: 41, zone: 'Neutral' };
const APP_JS = "export const answer = 'first';\n";
const INDEX_HTML = '<!doctype html><title>board</title>\n';
const TRADER_HTML = '<!doctype html><title>wallet</title>\n';

// --- a private copy of the service -------------------------------------------
async function makeTree({ datasetText = JSON.stringify(cohort(FIRST)), smi = SMI } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'screener-review-'));
  await cp(join(SCREENER, 'server.mjs'), join(root, 'server.mjs'));
  await cp(join(SCREENER, 'lib'), join(root, 'lib'), { recursive: true });
  await cp(join(SCREENER, 'scripts'), join(root, 'scripts'), { recursive: true });
  await mkdir(join(root, 'public'));
  await mkdir(join(root, 'data'));
  await writeFile(join(root, 'public', 'app.js'), APP_JS);
  await writeFile(join(root, 'public', 'index.html'), INDEX_HTML);
  await writeFile(join(root, 'public', 'trader.html'), TRADER_HTML);
  await writeFile(join(root, 'data', 'dataset.json'), datasetText);
  await writeFile(join(root, 'data', 'smi.json'), JSON.stringify(smi));
  return root;
}

async function until(check, what, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await check()) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => { setTimeout(resolve, 20); });
  }
}

async function freePort() {
  const probe = net.createServer();
  probe.listen(0, '127.0.0.1');
  await once(probe, 'listening');
  const { port } = probe.address();
  probe.close();
  await once(probe, 'close');
  return port;
}

/** The environment a child gets: this process's, less the test runner's own marker. */
function childEnv(extra) {
  const { NODE_TEST_CONTEXT, ...env } = process.env;
  return { ...env, ...extra };
}

/** A child that ends by itself: resolves with its exit code once its output
 *  is drained. One still running after `timeoutMs` is killed, and its code is null. */
async function closed(child, timeoutMs = 8000) {
  const timer = setTimeout(() => child.kill(), timeoutMs);
  try { return await once(child, 'close'); } finally { clearTimeout(timer); }
}

/** Start the tree's server on `port`, in snapshot mode, and collect what it prints. */
function spawnServer(root, port, { entry = 'server.mjs', reloadSeconds = '0' } = {}) {
  const child = spawn(process.execPath, [entry], {
    cwd: root,
    env: childEnv({
      PORT: String(port),
      SCREENER_DATA_DIR: join(root, 'data'),
      SCREENER_DATA_MODE: 'snapshot',
      SNAPSHOT_RELOAD_SECONDS: reloadSeconds,
    }),
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const log = { out: '', err: '' };
  child.stdout.setEncoding('utf8').on('data', (text) => { log.out += text; });
  child.stderr.setEncoding('utf8').on('data', (text) => { log.err += text; });
  return { child, log };
}

async function startServer(root, options) {
  const port = await freePort();
  const { child, log } = spawnServer(root, port, options);
  const exited = once(child, 'exit');
  const stop = async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill();
    await exited;
  };
  try {
    await until(() => {
      if (child.exitCode !== null) throw new Error(`the server exited with ${child.exitCode}: ${log.err}`);
      return log.out.includes(`http://localhost:${port}`);
    }, 'the server to listen');
  } catch (error) {
    await stop();
    throw error;
  }
  return { port, child, log, stop };
}

/** Run `work` against a server over a fresh tree, then stop it and remove the tree. */
async function withService({ tree, ...server } = {}, work) {
  const root = await makeTree(tree);
  let service;
  try {
    service = await startServer(root, server);
    return await work({ root, ...service });
  } finally {
    await service?.stop();
    await rm(root, { recursive: true, force: true });
  }
}

function get(port, path, headers = {}) {
  return new Promise((resolve, reject) => {
    const request = http.get({ host: '127.0.0.1', port, path, headers, agent: false }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
    });
    request.on('error', reject);
  });
}

/** Send `text` as it is over a socket and return whatever came back before the close. */
function raw(port, text) {
  return new Promise((resolve) => {
    let answer = '';
    const socket = net.connect(port, '127.0.0.1', () => socket.write(text));
    socket.setEncoding('utf8');
    socket.on('data', (chunk) => { answer += chunk; });
    socket.on('error', () => resolve(answer));
    socket.on('close', () => resolve(answer));
  });
}

const health = async (port) => JSON.parse((await get(port, '/api/health')).body);

/** Put a snapshot file in place whole, with an mtime `aheadSeconds` from now,
 *  so the server's mtime check sees a change however fast the test runs. */
async function putSnapshot(root, name, text, aheadSeconds) {
  const target = join(root, 'data', name);
  const temp = `${target}.next`;
  await writeFile(temp, text);
  await utimes(temp, new Date(), new Date(Date.now() + aheadSeconds * 1000));
  await rename(temp, target);
}

// --- S1 ---------------------------------------------------------------------
test('S1: a request target that is no URL gets 400 and the server keeps answering', async () => {
  await withService({}, async ({ port, child }) => {
    for (const target of ['//', '///', '//[']) {
      const answer = await raw(port, `GET ${target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n`);
      assert.match(answer, /^HTTP\/1\.1 400 /, `GET ${target} was answered with ${JSON.stringify(answer.slice(0, 40))}`);
    }
    const after = await get(port, '/api/health');
    assert.equal(after.status, 200);
    assert.equal(JSON.parse(after.body).generatedAt, FIRST);
    assert.equal(child.exitCode, null, 'the server process exited');
  });
});

test('S1: a rejection nobody awaited is logged and the server keeps answering', async () => {
  const root = await makeTree();
  // The service, plus one promise that rejects with nothing awaiting it.
  await writeFile(join(root, 'with-rejection.mjs'),
    "await import('./server.mjs');\nPromise.reject(new Error('review test: nobody awaits this'));\n");
  let service;
  try {
    service = await startServer(root, { entry: 'with-rejection.mjs' });
    await until(() => service.log.err.includes('review test: nobody awaits this'), 'the rejection to be logged');
    assert.match(service.log.err, /unhandled rejection/);
    assert.equal((await get(service.port, '/api/health')).status, 200);
    assert.equal(service.child.exitCode, null, 'the server process exited');
  } finally {
    await service?.stop();
    await rm(root, { recursive: true, force: true });
  }
});

// --- S7 ---------------------------------------------------------------------
test('S7: a static file carries a weak ETag and the Cache-Control it had', async () => {
  await withService({}, async ({ port }) => {
    const file = await get(port, '/app.js');
    assert.equal(file.status, 200);
    assert.equal(file.body.toString(), APP_JS);
    assert.equal(file.headers['content-type'], 'text/javascript; charset=utf-8');
    assert.equal(file.headers['cache-control'], 'no-cache');
    assert.match(file.headers.etag, /^W\/"[0-9a-f]+-[0-9a-f]+"$/);

    // The board and wallet pages are static files served under other paths.
    const board = await get(port, '/');
    assert.equal(board.body.toString(), INDEX_HTML);
    assert.match(board.headers.etag, /^W\/"/);
    const page = await get(port, `/screener/trader/${wallet(1)}`);
    assert.equal(page.body.toString(), TRADER_HTML);
    assert.match(page.headers.etag, /^W\/"/);

    assert.equal((await get(port, '/missing.js')).status, 404);
  });
});

test('S7: a matching If-None-Match gets 304 with no body', async () => {
  await withService({}, async ({ port }) => {
    const { etag } = (await get(port, '/app.js')).headers;
    for (const sent of [etag, `"another", ${etag}`, etag.replace(/^W\//, ''), '*']) {
      const again = await get(port, '/app.js', { 'if-none-match': sent });
      assert.equal(again.status, 304, `If-None-Match: ${sent}`);
      assert.equal(again.body.length, 0);
      assert.equal(again.headers.etag, etag);
      assert.equal(again.headers['cache-control'], 'no-cache');
    }
    const other = await get(port, '/app.js', { 'if-none-match': '"another"' });
    assert.equal(other.status, 200);
    assert.equal(other.body.toString(), APP_JS);

    const board = await get(port, '/');
    assert.equal((await get(port, '/', { 'if-none-match': board.headers.etag })).status, 304);
  });
});

test('S7: a changed file answers 200 with a new ETag', async () => {
  await withService({}, async ({ port, root }) => {
    const file = join(root, 'public', 'app.js');
    const first = await get(port, '/app.js');

    const longer = `${APP_JS}// second\n`;
    await writeFile(file, longer);
    const second = await get(port, '/app.js', { 'if-none-match': first.headers.etag });
    assert.equal(second.status, 200);
    assert.equal(second.body.toString(), longer);
    assert.notEqual(second.headers.etag, first.headers.etag);

    // The same number of bytes, written later.
    await writeFile(file, longer.replace('second', 'third!'));
    await utimes(file, new Date(), new Date(Date.now() + 60_000));
    const third = await get(port, '/app.js', { 'if-none-match': second.headers.etag });
    assert.equal(third.status, 200);
    assert.equal(third.body.toString(), longer.replace('second', 'third!'));
    assert.notEqual(third.headers.etag, second.headers.etag);
  });
});

// --- S10 --------------------------------------------------------------------
test('S10: /api/dataset serves the bytes JSON.stringify gives for the loaded dataset', async () => {
  // Indented, and with a number spelled 1.5e3: the file's own text differs
  // from what the route has always sent, which is the parsed dataset serialised.
  const datasetText = JSON.stringify(cohort(FIRST), null, 2).replace('"pnl": 1500', '"pnl": 1.5e3');
  const expected = Buffer.from(JSON.stringify(JSON.parse(datasetText)));
  assert.notEqual(datasetText, expected.toString(), 'the fixture must differ from its own serialisation');

  await withService({ tree: { datasetText } }, async ({ port }) => {
    for (const path of ['/api/dataset', '/screener/api/dataset']) {
      const answer = await get(port, path);
      assert.equal(answer.status, 200);
      assert.ok(answer.body.equals(expected), `${path} sent different bytes`);
      assert.equal(answer.headers['content-type'], 'application/json; charset=utf-8');
      assert.equal(answer.headers['content-length'], String(expected.length));
    }
  });
});

test('S10: /api/dataset carries an ETag and answers 304 to a matching one', async () => {
  await withService({}, async ({ port }) => {
    const first = await get(port, '/api/dataset');
    assert.match(first.headers.etag, /^W\/"[\w-]+"$/);
    assert.equal((await get(port, '/api/dataset')).headers.etag, first.headers.etag);

    const again = await get(port, '/api/dataset', { 'if-none-match': first.headers.etag });
    assert.equal(again.status, 304);
    assert.equal(again.body.length, 0);
    assert.equal(again.headers.etag, first.headers.etag);

    const other = await get(port, '/api/dataset', { 'if-none-match': 'W/"another"' });
    assert.equal(other.status, 200);
    assert.ok(other.body.equals(first.body));
  });
});

test('S10: a reloaded snapshot changes the body and the ETag of /api/dataset', async () => {
  await withService({ reloadSeconds: '0.05' }, async ({ port, root }) => {
    const first = await get(port, '/api/dataset');
    const next = cohort(SECOND, ['Ada', 'Grace', 'Lin']);
    await putSnapshot(root, 'dataset.json', JSON.stringify(next), 60);
    await until(async () => (await health(port)).generatedAt === SECOND, 'the reload');

    const second = await get(port, '/api/dataset', { 'if-none-match': first.headers.etag });
    assert.equal(second.status, 200);
    assert.ok(second.body.equals(Buffer.from(JSON.stringify(next))));
    assert.notEqual(second.headers.etag, first.headers.etag);
  });
});

test('S10: the /api/dataset route sends the body kept from the load', async () => {
  const source = await readFile(join(SCREENER, 'server.mjs'), 'utf8');
  assert.doesNotMatch(source, /json\(res, 200, dataset\)/, 'the route serialises the dataset on a request');
});

// --- S8x: the shape check ---------------------------------------------------
test('S8x: the committed seed snapshot passes the shape check', async () => {
  const dataset = JSON.parse(await readFile(join(SCREENER, 'data', 'dataset.json'), 'utf8'));
  const smi = JSON.parse(await readFile(join(SCREENER, 'data', 'smi.json'), 'utf8'));
  assert.equal(assertDataset(dataset), dataset);
  assert.equal(assertSmi(smi), smi);
});

test('S8x: a dataset without a field the service reads is refused', () => {
  const good = cohort(FIRST);
  assert.equal(assertDataset(good), good);
  const without = (key) => { const { [key]: _, ...rest } = good; return rest; };
  const refused = [
    [null, /dataset: expected an object/],
    [[], /dataset: expected an object/],
    ['<html>', /dataset: expected an object/],
    [{ error: 'upstream is down' }, /dataset\.meta\.generatedAt/],
    [{ ...good, meta: {} }, /dataset\.meta\.generatedAt/],
    [{ ...good, meta: { generatedAt: 20261101 } }, /dataset\.meta\.generatedAt/],
    [without('traders'), /dataset\.traders: expected a list/],
    [{ ...good, traders: [] }, /dataset\.traders: expected a list/],
    [{ ...good, traders: [good.traders[0], { name: 'no wallet' }] }, /dataset\.traders\[1\]\.w/],
    [{ ...good, traders: [null] }, /dataset\.traders\[0\]\.w/],
    [without('walletMeta'), /dataset\.walletMeta: expected an object/],
    [without('spark'), /dataset\.spark: expected an object/],
    [{ ...good, wow: [] }, /dataset\.wow: expected an object/],
    [{ ...good, copyDelta: null }, /dataset\.copyDelta: expected an object/],
    [without('angles'), /dataset\.angles: expected a list/],
    [{ ...good, events: {} }, /dataset\.events: expected a list/],
  ];
  for (const [value, message] of refused) assert.throws(() => assertDataset(value), message);
  for (const value of [null, [], 'text', 7]) assert.throws(() => assertSmi(value), /smi: expected an object/);
});

// --- S8x: the server keeps a good snapshot ----------------------------------
test('S8x: a reload with the wrong shape keeps the dataset and smi already loaded', async () => {
  await withService({ reloadSeconds: '0.05' }, async ({ port, root, log, child }) => {
    const before = await get(port, '/api/dataset');

    // Valid JSON, wrong shape: what an upstream error body saved as the dataset looks like.
    await putSnapshot(root, 'dataset.json', JSON.stringify({ error: 'upstream is down' }), 60);
    await until(() => log.err.includes('snapshot reload failed'), 'the refused reload');
    const held = await get(port, '/api/health');
    assert.equal(held.status, 200);
    assert.equal(JSON.parse(held.body).generatedAt, FIRST);
    assert.equal(JSON.parse(held.body).traders, 2);
    assert.ok((await get(port, '/api/dataset')).body.equals(before.body));
    assert.deepEqual(JSON.parse((await get(port, '/api/smi')).body), SMI);

    // A good dataset beside an smi file that holds no object is refused as one snapshot.
    await putSnapshot(root, 'smi.json', 'null', 120);
    await putSnapshot(root, 'dataset.json', JSON.stringify(cohort(SECOND)), 120);
    await until(() => log.err.includes('smi: expected an object'), 'the refused smi');
    assert.equal((await health(port)).generatedAt, FIRST);
    assert.deepEqual(JSON.parse((await get(port, '/api/smi')).body), SMI);

    // The refusals did not advance the mtime the server remembers, so a whole
    // snapshot written afterwards loads. It carries its own timestamp: the
    // dataset refused above may load first, once the smi beside it is whole.
    await putSnapshot(root, 'smi.json', JSON.stringify({ score: 58 }), 180);
    await putSnapshot(root, 'dataset.json', JSON.stringify(cohort(THIRD, ['Ada', 'Grace', 'Lin'])), 180);
    await until(async () => (await health(port)).generatedAt === THIRD, 'the good reload');
    assert.equal((await health(port)).traders, 3);
    assert.deepEqual(JSON.parse((await get(port, '/api/smi')).body), { score: 58 });
    assert.equal(child.exitCode, null, 'the server process exited');
  });
});

test('S8x: a boot with no snapshot exits with the ingest hint, as it did', async () => {
  const root = await makeTree();
  try {
    await rm(join(root, 'data', 'dataset.json'));
    const { child, log } = spawnServer(root, await freePort());
    const [code] = await closed(child);
    assert.equal(code, 1);
    assert.match(log.err, /No snapshot found in .*Run: {2}node scripts\/ingest\.mjs/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// --- S8x: the ingest --------------------------------------------------------
/* fetch for the ingest: answers each of its two URLs from a JSON file named in
 * INGEST_ANSWERS, so the script runs unchanged and reaches no host. */
const STUB_FETCH = `
import { readFileSync } from 'node:fs';
const answers = JSON.parse(readFileSync(process.env.INGEST_ANSWERS, 'utf8'));
globalThis.fetch = async (url) => {
  const { status = 200, body } = answers[String(url).includes('/indexes/smi') ? 'smi' : 'dataset'];
  return { ok: status >= 200 && status < 300, status, json: async () => JSON.parse(body) };
};
`;

/** Run the tree's scripts/ingest.mjs against `answers`; it writes into the tree's data/. */
async function runIngest(root, answers) {
  const stub = join(root, 'stub-fetch.mjs');
  const answersFile = join(root, 'answers.json');
  await writeFile(stub, STUB_FETCH);
  await writeFile(answersFile, JSON.stringify(answers));
  const child = spawn(process.execPath, ['--import', pathToFileURL(stub).href, 'scripts/ingest.mjs'], {
    cwd: root, env: childEnv({ INGEST_ANSWERS: answersFile }), stdio: ['ignore', 'pipe', 'pipe'],
  });
  let out = '', err = '';
  child.stdout.setEncoding('utf8').on('data', (text) => { out += text; });
  child.stderr.setEncoding('utf8').on('data', (text) => { err += text; });
  const [code] = await closed(child);
  return { code, out, err };
}

async function withTree(work) {
  const root = await makeTree();
  try { return await work(root); } finally { await rm(root, { recursive: true, force: true }); }
}

const dataFiles = async (root) => (await readdir(join(root, 'data'))).sort();

test('S8x: the ingest replaces each file by rename and leaves no temp file', async () => {
  await withTree(async (root) => {
    const file = join(root, 'data', 'dataset.json');
    const inodeBefore = (await stat(file)).ino;
    const next = cohort(SECOND, ['Ada', 'Grace', 'Lin']);

    const run = await runIngest(root, {
      dataset: { body: JSON.stringify(next) }, smi: { body: JSON.stringify({ score: 58 }) },
    });
    assert.equal(run.code, 0, run.err);
    assert.equal(await readFile(file, 'utf8'), JSON.stringify(next));
    assert.equal(await readFile(join(root, 'data', 'smi.json'), 'utf8'), JSON.stringify({ score: 58 }));
    assert.deepEqual(await dataFiles(root), ['dataset.json', 'smi.json']);
    // A file written in place keeps its inode; one renamed into place brings its own.
    assert.notEqual((await stat(file)).ino, inodeBefore, 'dataset.json was rewritten in place');
    assert.match(run.out, /traders\s+3\b/);
  });
});

test('S8x: the ingest refuses a dataset with the wrong shape before it writes', async () => {
  await withTree(async (root) => {
    const file = join(root, 'data', 'dataset.json');
    const before = { text: await readFile(file, 'utf8'), inode: (await stat(file)).ino };

    for (const body of [JSON.stringify({ error: 'rate limited' }), JSON.stringify({ ...cohort(SECOND), traders: [] })]) {
      const run = await runIngest(root, { dataset: { body }, smi: { body: JSON.stringify({ score: 58 }) } });
      assert.notEqual(run.code, 0, 'the ingest reported success');
      assert.match(run.err, /dataset\./);
      assert.equal(await readFile(file, 'utf8'), before.text);
      assert.equal((await stat(file)).ino, before.inode);
      assert.equal(await readFile(join(root, 'data', 'smi.json'), 'utf8'), JSON.stringify(SMI));
      assert.deepEqual(await dataFiles(root), ['dataset.json', 'smi.json']);
    }
  });
});

test('S8x: the ingest refuses an smi answer that is no object and keeps smi.json', async () => {
  await withTree(async (root) => {
    const run = await runIngest(root, {
      dataset: { body: JSON.stringify(cohort(SECOND)) }, smi: { body: 'null' },
    });
    assert.notEqual(run.code, 0, 'the ingest reported success');
    assert.match(run.err, /smi: expected an object/);
    assert.equal(await readFile(join(root, 'data', 'smi.json'), 'utf8'), JSON.stringify(SMI));
    assert.deepEqual(await dataFiles(root), ['dataset.json', 'smi.json']);
  });
});

test('S8x: writeFileAtomic replaces the target whole, and a failed write leaves nothing behind', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'screener-atomic-'));
  try {
    const file = join(dir, 'dataset.json');
    await writeFile(file, 'old');
    const inodeBefore = (await stat(file)).ino;
    await writeFileAtomic(file, 'new');
    assert.equal(await readFile(file, 'utf8'), 'new');
    assert.notEqual((await stat(file)).ino, inodeBefore);
    assert.deepEqual(await readdir(dir), ['dataset.json']);

    // A directory in the target's place makes the rename fail after the temp file exists.
    await mkdir(join(dir, 'taken'));
    await assert.rejects(writeFileAtomic(join(dir, 'taken'), 'new'));
    assert.deepEqual((await readdir(dir)).sort(), ['dataset.json', 'taken']);
    assert.deepEqual(await readdir(join(dir, 'taken')), []);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

// --- S14 --------------------------------------------------------------------
test('S14: lib/copyscore.mjs exports the local recomputation and its cost model', () => {
  assert.deepEqual(Object.keys(copyscore).sort(), ['COST_MODEL', 'compute']);
  const out = copyscore.compute({ grossReturnPct: 81.37, resolved: 6, avgEntryPrice: 0.472, soldBeforeResolution: 0 });
  assert.equal(out.shrunkReturnPct, 2.58);
});

test('S14: ranks() settles with a value whatever upstream does', async () => {
  const real = globalThis.fetch;
  const aborted = new AbortController();
  aborted.abort(new Error('caller gave up'));
  const upstreams = [
    [async () => { throw new TypeError('fetch failed'); }, {}],
    [async () => ({ ok: false, status: 404, json: async () => ({}) }), {}],
    [async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token <'); } }), {}],
    [async () => ({ ok: true, status: 200, json: async () => ({ data: null }) }), { signal: aborted.signal }],
  ];
  try {
    for (const [fetchImpl, options] of upstreams) {
      globalThis.fetch = fetchImpl;
      assert.deepEqual(await pm.ranks(wallet(1), options), {});
    }
  } finally {
    globalThis.fetch = real;
  }
});
