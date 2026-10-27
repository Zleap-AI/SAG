import { open, readFile, unlink } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import sodium from 'libsodium-wrappers';

const NAMES = Object.freeze([
  'APPLE_CERTIFICATE_BASE64',
  'APPLE_CERTIFICATE_PASSWORD',
  'APPLE_ID',
  'APPLE_APP_SPECIFIC_PASSWORD',
  'APPLE_TEAM_ID',
]);
const APPROVED_TARGET = Object.freeze({
  repository: 'Zleap-AI/SAG-Wiki',
  environment: 'release',
  key_id: '3380204578043523366',
  public_key: 'hkOy5CM6+Y3LSFZADmvSnbb/MMnWTpCL+U6XQn/PzSU=',
});
const SOURCE_REPOSITORY = 'Zleap-AI/SAG';
const SOURCE_WORKFLOW = '.github/workflows/wiki-apple-secrets.yml';
const SOURCE_REF = 'refs/heads/main';
const FILE_NAME = 'apple-secrets-for-sag-wiki.json';

function exactKeys(value, keys) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}

function validateTarget(target) {
  if (!exactKeys(target, ['repository', 'environment', 'key_id', 'public_key'])
    || target.repository !== APPROVED_TARGET.repository
    || target.environment !== APPROVED_TARGET.environment
    || typeof target.key_id !== 'string' || !/^[1-9][0-9]*$/.test(target.key_id)
    || typeof target.public_key !== 'string'
    || !/^[A-Za-z0-9+/]{43}=$/.test(target.public_key)) {
    throw new Error('Invalid handoff destination.');
  }
  const publicKey = Buffer.from(target.public_key, 'base64');
  if (publicKey.length !== 32 || publicKey.toString('base64') !== target.public_key) {
    throw new Error('Invalid handoff public key.');
  }
  return publicKey;
}

export function validateApprovedTarget(manifest) {
  if (!exactKeys(manifest, ['schema', 'target']) || manifest.schema !== 1) {
    throw new Error('Invalid handoff manifest.');
  }
  validateTarget(manifest.target);
  if (!Object.keys(APPROVED_TARGET).every((key) => manifest.target[key] === APPROVED_TARGET[key])) {
    throw new Error('Handoff destination differs from the reviewed target.');
  }
  return { ...manifest.target };
}

function validateSource(source) {
  if (!exactKeys(source, ['repository', 'workflow', 'commit', 'run_id', 'run_attempt', 'ref'])
    || source.repository !== SOURCE_REPOSITORY || source.workflow !== SOURCE_WORKFLOW
    || source.ref !== SOURCE_REF || typeof source.commit !== 'string'
    || !/^[a-f0-9]{40}$/.test(source.commit)
    || !Number.isSafeInteger(source.run_id) || source.run_id < 1
    || !Number.isSafeInteger(source.run_attempt) || source.run_attempt < 1) {
    throw new Error('Invalid handoff source context.');
  }
}

function sourceFromEnvironment(env) {
  if (env.GITHUB_ACTIONS !== 'true' || env.GITHUB_EVENT_NAME !== 'workflow_dispatch'
    || env.GITHUB_REPOSITORY !== SOURCE_REPOSITORY || env.GITHUB_REF !== SOURCE_REF
    || env.GITHUB_WORKFLOW_REF !== `${SOURCE_REPOSITORY}/${SOURCE_WORKFLOW}@${SOURCE_REF}`
    || !/^[1-9][0-9]*$/.test(env.GITHUB_RUN_ID ?? '')
    || !/^[1-9][0-9]*$/.test(env.GITHUB_RUN_ATTEMPT ?? '')) {
    throw new Error('Handoff requires a manual run of the reviewed workflow on SAG main.');
  }
  const source = {
    repository: env.GITHUB_REPOSITORY,
    workflow: SOURCE_WORKFLOW,
    commit: env.GITHUB_SHA,
    run_id: Number(env.GITHUB_RUN_ID),
    run_attempt: Number(env.GITHUB_RUN_ATTEMPT),
    ref: env.GITHUB_REF,
  };
  validateSource(source);
  return source;
}

// The in-memory target parameter lets tests decrypt disposable fixtures. The CLI
// accepts no parameters and always validates the adjacent reviewed target.json.
export async function sealCredentials({ target, source, secrets }) {
  const publicKey = validateTarget(target);
  validateSource(source);
  for (const name of NAMES) {
    if (typeof secrets?.[name] !== 'string' || secrets[name].length === 0) {
      throw new Error(`Missing credential: ${name}`);
    }
  }
  await sodium.ready;
  const credentials = {};
  for (const name of NAMES) {
    // Encrypt the original UTF-8 value: never trim or decode the certificate.
    const plaintext = Buffer.from(secrets[name], 'utf8');
    try {
      credentials[name] = sodium.to_base64(
        sodium.crypto_box_seal(plaintext, publicKey),
        sodium.base64_variants.ORIGINAL,
      );
    } catch {
      throw new Error('Could not seal credentials.');
    } finally {
      sodium.memzero(plaintext);
    }
  }
  return { schema: 1, target: { ...target }, source: { ...source }, credentials };
}

async function writePrivateBundle(bundle) {
  const serialized = `${JSON.stringify(bundle, null, 2)}\n`;
  const path = join(process.cwd(), FILE_NAME);
  // Exclusive creation also rejects symlinks or stale artifacts. No plaintext
  // file, temporary file, or replacement of an existing output is permitted.
  const file = await open(path, 'wx', 0o600);
  try {
    await file.writeFile(serialized, 'utf8');
    await file.close();
  } catch (error) {
    await file.close().catch(() => {});
    await unlink(path).catch(() => {});
    throw error;
  }
}

async function main() {
  if (process.argv.length !== 2) throw new Error('No command-line inputs are permitted.');
  const source = sourceFromEnvironment(process.env);
  const target = validateApprovedTarget(JSON.parse(await readFile(new URL('./target.json', import.meta.url), 'utf8')));
  const bundle = await sealCredentials({ target, source, secrets: process.env });
  await writePrivateBundle(bundle);
  console.log(`Sealed credentials for ${target.repository} / ${target.environment}: ${NAMES.join(', ')}`);
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => {
    // Never expose dependency errors, environment values, or ciphertext in logs.
    console.error('Apple credential handoff failed; no artifact was produced.');
    process.exitCode = 1;
  });
}
