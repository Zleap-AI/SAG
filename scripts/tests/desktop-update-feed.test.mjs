import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, writeFile, rm, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

const root = new URL('../../', import.meta.url);
const script = new URL('../desktop-update-feed.mjs', import.meta.url);

function builder(env) {
  return spawnSync(process.execPath, ['-e', 'console.log(JSON.stringify(require("./apps/desktop/electron-builder.config.cjs").publish))'], {
    cwd: root, encoding: 'utf8', env: { ...process.env, SAG_UPDATE_BASE_URL: '', SAG_UPDATE_GITHUB_REPOSITORY: '', ...env },
  });
}

test('official packages discover only the separate manual feed', () => {
  const result = builder({ SAG_UPDATE_GITHUB_REPOSITORY: 'Zleap-AI/SAG' });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    provider: 'generic', url: 'https://github.com/Zleap-AI/SAG/releases/download/desktop-manual-updates',
    useMultipleRangeRequest: false,
  });
});

test('self-hosted feed remains available and conflicting providers are rejected', () => {
  assert.equal(JSON.parse(builder({ SAG_UPDATE_BASE_URL: 'https://updates.example/sag/' }).stdout).url, 'https://updates.example/sag');
  assert.notEqual(builder({ SAG_UPDATE_BASE_URL: 'https://updates.example/sag', SAG_UPDATE_GITHUB_REPOSITORY: 'Zleap-AI/SAG' }).status, 0);
  assert.notEqual(builder({ SAG_UPDATE_GITHUB_REPOSITORY: '../SAG' }).status, 0);
});

async function fixture(t) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'sag-manual-feed-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  for (const filename of ['SAG-1.8.10-mac-arm64.zip', 'SAG-1.8.10-mac-arm64.dmg', 'SAG-Setup-1.8.10-win-x64.exe']) {
    await writeFile(path.join(dir, filename), `signed payload ${filename}`);
  }
  await writeFile(path.join(dir, 'SHA256SUMS.txt'), 'test checksums\n');
  return dir;
}

test('manual feed uses immutable release assets and correct SHA512 integrity metadata', async (t) => {
  const { buildManualFeed } = await import(script);
  const dir = await fixture(t);
  const feed = await buildManualFeed({ assetsDir: dir, repository: 'Zleap-AI/SAG', tag: 'v1.8.10' });
  for (const [name, count] of [['latest-mac.yml', 2], ['latest.yml', 1]]) {
    const info = JSON.parse(feed[name]);
    assert.equal(info.version, '1.8.10');
    assert.equal(info.files.length, count);
    for (const file of info.files) {
      const filename = decodeURIComponent(new URL(file.url).pathname.split('/').at(-1));
      const payload = `signed payload ${filename}`;
      assert.equal(file.url, `https://github.com/Zleap-AI/SAG/releases/download/v1.8.10/${filename}`);
      assert.equal(file.sha512, createHash('sha512').update(payload).digest('base64'));
      assert.equal(file.size, Buffer.byteLength(payload));
    }
    assert.equal(info.path, info.files[0].url);
    assert.equal(info.sha512, info.files[0].sha512);
  }
});

test('invalid targets, empty or incomplete payloads fail before publication', async (t) => {
  const { buildManualFeed } = await import(script);
  const dir = await fixture(t);
  for (const tag of ['../main', 'v1.8.10-beta', '1.8.10']) {
    await assert.rejects(buildManualFeed({ assetsDir: dir, repository: 'Zleap-AI/SAG', tag }));
  }
  await assert.rejects(buildManualFeed({ assetsDir: dir, repository: '../SAG', tag: 'v1.8.10' }));
  await writeFile(path.join(dir, 'SAG-Setup-1.8.10-win-x64.exe'), '');
  await assert.rejects(buildManualFeed({ assetsDir: dir, repository: 'Zleap-AI/SAG', tag: 'v1.8.10' }));
});

test('only the first compatible bridge can become legacy latest', async () => {
  const { releasePolicy } = await import(script);
  assert.equal(releasePolicy({ legacyBridge: true }, null).makeLatest, true);
  assert.throws(() => releasePolicy({ legacyBridge: true }, { bridgeTag: 'v1.8.10' }));
  assert.throws(() => releasePolicy({ legacyBridge: false }, null));
  assert.equal(releasePolicy({ legacyBridge: false }, { bridgeTag: 'v1.8.10' }).makeLatest, false);
  assert.throws(() => releasePolicy({}, { bridgeTag: 'v1.8.10' }));
});

async function publication(t, { bridge = false, existingBridge = 'v1.8.9', latest = 'v1.8.9', published = false, remoteChecksums = 'test checksums\n', uploadFailures = 0 } = {}) {
  const { publishDesktopRelease } = await import(script);
  const assetsDir = await fixture(t);
  const calls = [];
  const uploaded = [];
  let failures = uploadFailures;
  const run = async (args) => {
    calls.push(args);
    if (args[0] === 'api') {
      if (args[1].endsWith('/tags/desktop-manual-updates')) return existingBridge ? JSON.stringify({ prerelease: true, draft: false, assets: [{name: 'latest.yml'}, {name: 'latest-mac.yml'}] }) : null;
      if (args[1].endsWith('/latest')) return JSON.stringify({ tag_name: latest });
      if (args[1].endsWith('/tags/v1.8.10')) return published ? JSON.stringify({ draft: false, prerelease: false }) : null;
      throw new Error(`Unexpected API call ${args}`);
    }
    if (args[1] === 'download') {
      const dir = args[args.indexOf('--dir') + 1];
      const filename = args[args.indexOf('--pattern') + 1];
      await writeFile(path.join(dir, filename), filename === 'legacy-bridge.json' ? JSON.stringify({ bridgeTag: existingBridge }) : filename === 'SHA256SUMS.txt' ? remoteChecksums : 'previous metadata');
    }
    if (args[1] === 'upload') {
      uploaded.push(await readFile(args[3], 'utf8'));
      if (failures-- > 0) throw new Error('simulated interrupted upload');
    }
    return '';
  };
  let error;
  try { await publishDesktopRelease({ repository: 'Zleap-AI/SAG', tag: 'v1.8.10', assetsDir, notesFile: 'notes.md', policy: { legacyBridge: bridge }, run }); }
  catch (value) { error = value; }
  return { calls, error, uploaded };
}

test('first bridge publishes legacy latest and seeds a non-latest prerelease index', async (t) => {
  const { calls, error } = await publication(t, { bridge: true, existingBridge: null });
  assert.equal(error, undefined);
  const creates = calls.filter((args) => args[1] === 'create');
  assert.equal(creates.length, 2);
  assert.ok(creates[0].includes('--latest=true'));
  assert.equal(creates[1][2], 'desktop-manual-updates');
  assert.ok(creates[1].includes('--prerelease'));
  assert.ok(creates[1].includes('--latest=false'));
});

test('manual releases never promote legacy latest and never rewrite the bridge marker', async (t) => {
  const { calls, error } = await publication(t);
  assert.equal(error, undefined);
  const creates = calls.filter((args) => args[1] === 'create');
  assert.equal(creates.length, 1);
  assert.ok(creates[0].includes('--latest=false'));
  const upload = calls.find((args) => args[1] === 'upload');
  assert.equal(upload[2], 'desktop-manual-updates');
  assert.equal(upload.some((arg) => arg.endsWith('legacy-bridge.json')), false);
});

test('missing bridge, repeated bridge or displaced latest fail before any publication', async (t) => {
  for (const options of [{ existingBridge: null }, { bridge: true }, { latest: 'v1.8.8' }]) {
    const { calls, error } = await publication(t, options);
    assert.ok(error);
    assert.equal(calls.some((args) => ['create', 'upload', 'edit'].includes(args[1])), false);
  }
});


test('publication retry resumes feed upload without overwriting a verified version release', async (t) => {
  const { calls, error } = await publication(t, { published: true });
  assert.equal(error, undefined);
  assert.equal(calls.some((args) => args[1] === 'create'), false);
  assert.ok(calls.some((args) => args[1] === 'upload'));
});

test('publication retry rejects changed checksums before updating the feed', async (t) => {
  const { calls, error } = await publication(t, { published: true, remoteChecksums: 'different artifacts' });
  assert.ok(error);
  assert.equal(calls.some((args) => ['create', 'upload', 'edit'].includes(args[1])), false);
});


test('transient metadata upload failure is retried', async (t) => {
  const { calls, error } = await publication(t, { uploadFailures: 1 });
  assert.equal(error, undefined);
  assert.equal(calls.filter((args) => args[1] === 'upload').length, 2);
});

test('exhausted metadata upload restores previous files and reports the failed publication', async (t) => {
  const { error, uploaded } = await publication(t, { uploadFailures: 3 });
  assert.ok(error);
  assert.equal(uploaded.length, 4);
  assert.equal(uploaded.at(-1), 'previous metadata');
});
