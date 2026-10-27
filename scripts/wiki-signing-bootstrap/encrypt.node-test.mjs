import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFile, lstat, mkdtemp, readFile, readdir, rm, stat, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import sodium from 'libsodium-wrappers';

await sodium.ready;

const CLI = fileURLToPath(new URL('./encrypt.mjs', import.meta.url));
const MODULES = fileURLToPath(new URL('./node_modules', import.meta.url));
const TARGET_FILE = new URL('./target.json', import.meta.url);
const FILE_NAME = 'apple-secrets-for-sag-wiki.json';
const FAILURE = 'Apple credential handoff failed; no artifact was produced.\n';
const NAMES = [
  'APPLE_CERTIFICATE_BASE64',
  'APPLE_CERTIFICATE_PASSWORD',
  'APPLE_ID',
  'APPLE_APP_SPECIFIC_PASSWORD',
  'APPLE_TEAM_ID',
];
const APPROVED_TARGET = {
  repository: 'Zleap-AI/SAG-Wiki',
  environment: 'release',
  key_id: '3380204578043523366',
  public_key: 'hkOy5CM6+Y3LSFZADmvSnbb/MMnWTpCL+U6XQn/PzSU=',
};
const SOURCE = {
  repository: 'Zleap-AI/SAG',
  workflow: '.github/workflows/wiki-apple-secrets.yml',
  commit: '1234567890abcdef1234567890abcdef12345678',
  run_id: 123456,
  run_attempt: 1,
  ref: 'refs/heads/main',
};
const CONTEXT = {
  GITHUB_ACTIONS: 'true',
  GITHUB_REPOSITORY: 'Zleap-AI/SAG',
  GITHUB_EVENT_NAME: 'workflow_dispatch',
  GITHUB_WORKFLOW_REF: 'Zleap-AI/SAG/.github/workflows/wiki-apple-secrets.yml@refs/heads/main',
  GITHUB_REF: 'refs/heads/main',
  GITHUB_SHA: '1234567890abcdef1234567890abcdef12345678',
  GITHUB_RUN_ID: '123456',
  GITHUB_RUN_ATTEMPT: '1',
};
const SECRETS = {
  APPLE_CERTIFICATE_BASE64: 'fixture-certificate-QUJDRA==\n',
  APPLE_CERTIFICATE_PASSWORD: ' fixture-password-中文 🔐\r\n ',
  APPLE_ID: 'fixture-apple@example.invalid',
  APPLE_APP_SPECIFIC_PASSWORD: 'fixture-xxxx-yyyy-zzzz',
  APPLE_TEAM_ID: 'FIXTURE123',
};
const implementation = () => import('./encrypt.mjs');

async function withDirectory(action) {
  const directory = await mkdtemp(join(tmpdir(), 'sag-wiki-signing-test-'));
  try {
    return await action(directory);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

function fixtureTarget() {
  const pair = sodium.crypto_box_keypair();
  return {
    pair,
    target: {
      ...APPROVED_TARGET,
      public_key: sodium.to_base64(pair.publicKey, sodium.base64_variants.ORIGINAL),
    },
  };
}

function runCli(directory, overrides = {}, args = [], cli = CLI) {
  return spawnSync(process.execPath, [cli, ...args], {
    cwd: directory,
    env: { ...process.env, ...CONTEXT, ...SECRETS, ...overrides },
    encoding: 'utf8',
  });
}

test('CLI writes only a private sealed envelope for the fixed destination', async () => {
  await withDirectory(async (directory) => {
    const result = runCli(directory);
    assert.equal(result.status, 0, 'credential sealing must succeed for valid fixture context');
    assert.equal(result.stderr, '');
    assert.deepEqual(await readdir(directory), [FILE_NAME]);
    const serialized = await readFile(join(directory, FILE_NAME), 'utf8');
    const bundle = JSON.parse(serialized);
    assert.deepEqual(Object.keys(bundle), ['schema', 'target', 'source', 'credentials']);
    assert.equal(bundle.schema, 1);
    assert.deepEqual(bundle.target, APPROVED_TARGET);
    assert.deepEqual(bundle.source, SOURCE);
    assert.deepEqual(Object.keys(bundle.credentials), NAMES);
    assert.equal((await stat(join(directory, FILE_NAME))).mode & 0o777, 0o600);
    for (const name of NAMES) {
      assert.match(bundle.credentials[name], /^[A-Za-z0-9+/]+={0,2}$/);
      assert.equal(serialized.includes(SECRETS[name]), false);
      assert.equal(result.stdout.includes(SECRETS[name]), false);
      assert.equal(result.stdout.includes(bundle.credentials[name]), false);
      assert.equal(result.stdout.includes(name), true);
    }
    assert.equal(result.stdout.includes('Zleap-AI/SAG-Wiki / release'), true);
  });
});

test('each sealed value decrypts to the original UTF-8 bytes using a disposable key', async () => {
  const { sealCredentials } = await implementation();
  const { pair, target } = fixtureTarget();
  const bundle = await sealCredentials({ target, source: SOURCE, secrets: SECRETS });
  assert.deepEqual(bundle.target, target);
  assert.deepEqual(bundle.source, SOURCE);
  assert.deepEqual(Object.keys(bundle.credentials), NAMES);
  for (const name of NAMES) {
    const sealed = sodium.from_base64(bundle.credentials[name], sodium.base64_variants.ORIGINAL);
    assert.equal(sealed.length, Buffer.byteLength(SECRETS[name], 'utf8') + sodium.crypto_box_SEALBYTES);
    assert.deepEqual(
      Buffer.from(sodium.crypto_box_seal_open(sealed, pair.publicKey, pair.privateKey)),
      Buffer.from(SECRETS[name], 'utf8'),
    );
  }
  const wrongPair = sodium.crypto_box_keypair();
  assert.throws(() => sodium.crypto_box_seal_open(
    sodium.from_base64(bundle.credentials.APPLE_ID, sodium.base64_variants.ORIGINAL),
    wrongPair.publicKey,
    wrongPair.privateKey,
  ));
});

test('approved target validation rejects changed destinations, keys, and manifest shape', async () => {
  const { validateApprovedTarget } = await implementation();
  const manifest = JSON.parse(await readFile(TARGET_FILE, 'utf8'));
  assert.deepEqual(validateApprovedTarget(manifest), APPROVED_TARGET);
  for (const change of [
    { repository: 'someone/else' },
    { environment: 'another' },
    { key_id: '3380204578043523367' },
    { public_key: fixtureTarget().target.public_key },
    { public_key: 'invalid-base64' },
    { public_key: 'AA==' },
    { key_id: 3380204578043523366 },
    { unreviewed: true },
  ]) {
    assert.throws(() => validateApprovedTarget({ schema: 1, target: { ...APPROVED_TARGET, ...change } }));
  }
  for (const invalid of [null, {}, { schema: 2, target: APPROVED_TARGET }, { schema: 1, target: APPROVED_TARGET, extra: true }]) {
    assert.throws(() => validateApprovedTarget(invalid));
  }
});

test('sealing rejects malformed public keys before any output is written', async () => {
  const { sealCredentials } = await implementation();
  for (const public_key of ['invalid-base64', 'AA==', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=', `${APPROVED_TARGET.public_key}\n`]) {
    await assert.rejects(sealCredentials({ target: { ...APPROVED_TARGET, public_key }, source: SOURCE, secrets: SECRETS }));
  }
});

test('every mandatory secret must be a nonempty string without echoing its value', async () => {
  const { sealCredentials } = await implementation();
  const { target } = fixtureTarget();
  for (const name of NAMES) {
    for (const value of [undefined, '', null, 123]) {
      await assert.rejects(sealCredentials({ target, source: SOURCE, secrets: { ...SECRETS, [name]: value } }));
    }
    await withDirectory(async (directory) => {
      const result = runCli(directory, { [name]: '' });
      assert.equal(result.status, 1);
      assert.equal(result.stderr, FAILURE);
      assert.deepEqual(await readdir(directory), []);
      for (const secret of Object.values(SECRETS)) {
        assert.equal((result.stdout + result.stderr).includes(secret), false);
      }
    });
  }
});

test('CLI refuses nonmanual, nonmain, foreign repository, and malformed source context', async () => {
  for (const change of [
    { GITHUB_ACTIONS: 'false' },
    { GITHUB_REPOSITORY: 'someone/SAG' },
    { GITHUB_REF: 'refs/heads/feature' },
    { GITHUB_REF: 'refs/tags/v1.9.2' },
    { GITHUB_EVENT_NAME: 'pull_request' },
    { GITHUB_EVENT_NAME: 'push' },
    { GITHUB_WORKFLOW_REF: 'Zleap-AI/SAG/.github/workflows/another.yml@refs/heads/main' },
    { GITHUB_WORKFLOW_REF: 'Zleap-AI/SAG/.github/workflows/wiki-apple-secrets.yml@refs/heads/feature' },
    { GITHUB_SHA: 'bad-commit' },
    { GITHUB_RUN_ID: '0' },
    { GITHUB_RUN_ID: '9007199254740992' },
    { GITHUB_RUN_ATTEMPT: '1.1' },
    { GITHUB_RUN_ATTEMPT: '-1' },
  ]) {
    await withDirectory(async (directory) => {
      const result = runCli(directory, change);
      assert.equal(result.status, 1);
      assert.equal(result.stderr, FAILURE);
      assert.deepEqual(await readdir(directory), []);
      for (const secret of Object.values(SECRETS)) {
        assert.equal((result.stdout + result.stderr).includes(secret), false);
      }
    });
  }
});

test('in-memory sealing enforces exact source metadata too', async () => {
  const { sealCredentials } = await implementation();
  const { target } = fixtureTarget();
  for (const change of [
    { repository: 'someone/SAG' },
    { workflow: '.github/workflows/another.yml' },
    { ref: 'refs/heads/feature' },
    { commit: 'bad' },
    { run_id: '123456' },
    { run_attempt: 0 },
    { unexpected: 'value' },
  ]) {
    await assert.rejects(sealCredentials({ target, source: { ...SOURCE, ...change }, secrets: SECRETS }));
  }
});

test('CLI refuses target and output arguments', async () => {
  for (const args of [['--target', '/tmp/unapproved.json'], ['--output', '/tmp/unapproved.json']]) {
    await withDirectory(async (directory) => {
      const result = runCli(directory, {}, args);
      assert.equal(result.status, 1);
      assert.equal(result.stderr, FAILURE);
      assert.deepEqual(await readdir(directory), []);
    });
  }
});

test('CLI never overwrites an existing output file', async () => {
  await withDirectory(async (directory) => {
    await writeFile(join(directory, FILE_NAME), 'existing-file-sentinel', { mode: 0o644 });
    const result = runCli(directory);
    assert.equal(result.status, 1);
    assert.equal(result.stderr, FAILURE);
    assert.equal(await readFile(join(directory, FILE_NAME), 'utf8'), 'existing-file-sentinel');
    assert.equal(result.stdout, '');
  });
});

test('CLI refuses an adjacent manifest whose public key or destination changed', async () => {
  for (const target of [
    { ...APPROVED_TARGET, public_key: fixtureTarget().target.public_key },
    { ...APPROVED_TARGET, repository: 'someone/else' },
    { ...APPROVED_TARGET, environment: 'another' },
  ]) {
    await withDirectory(async (directory) => {
      const cli = join(directory, 'encrypt.mjs');
      await copyFile(CLI, cli);
      await symlink(MODULES, join(directory, 'node_modules'), 'dir');
      await writeFile(join(directory, 'target.json'), JSON.stringify({ schema: 1, target }));
      const result = runCli(directory, {}, [], cli);
      assert.equal(result.status, 1);
      assert.equal(result.stdout, '');
      assert.equal(result.stderr, FAILURE);
      assert.equal((await readdir(directory)).includes(FILE_NAME), false);
    });
  }
});

test('CLI refuses an output symlink without altering its target', async () => {
  await withDirectory(async (directory) => {
    const sentinel = join(directory, 'sentinel');
    await writeFile(sentinel, 'symlink-target-sentinel');
    await symlink(sentinel, join(directory, FILE_NAME));
    const result = runCli(directory);
    assert.equal(result.status, 1);
    assert.equal(result.stderr, FAILURE);
    assert.equal(result.stdout, '');
    assert.equal(await readFile(sentinel, 'utf8'), 'symlink-target-sentinel');
    assert.equal((await lstat(join(directory, FILE_NAME))).isSymbolicLink(), true);
  });
});
