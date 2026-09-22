import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { mkdir, readFile, readdir, stat, writeFile, mkdtemp, rm } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const MANUAL_CHANNEL = 'desktop-manual-updates';
const stableTag = /^v\d+\.\d+\.\d+$/;
const repositoryPattern = /^[A-Za-z0-9_-]+\/[A-Za-z0-9_.-]+$/;

export function releasePolicy(policy, marker) {
  if (typeof policy?.legacyBridge !== 'boolean') throw new Error('legacyBridge must be explicitly true or false');
  if (policy.legacyBridge) {
    if (marker) throw new Error('A bridge already exists. Subsequent releases must set legacyBridge=false.');
    return { makeLatest: true };
  }
  if (!stableTag.test(marker?.bridgeTag || '')) throw new Error('Publish the compatible bridge before a manual-only release.');
  return { makeLatest: false };
}

export async function buildManualFeed({ assetsDir, repository, tag }) {
  if (!repositoryPattern.test(repository) || repository.endsWith('/..')) throw new Error('Invalid GitHub repository');
  if (!stableTag.test(tag)) throw new Error('Expected stable vX.Y.Z tag');
  const version = tag.slice(1);
  const describe = async (filename) => {
    const filenamePath = path.join(assetsDir, filename);
    const info = await stat(filenamePath);
    if (!info.isFile() || info.size === 0) throw new Error(`Missing or empty release payload: ${filename}`);
    const hash = createHash('sha512');
    for await (const chunk of createReadStream(filenamePath)) hash.update(chunk);
    return {
      url: `https://github.com/${repository}/releases/download/${tag}/${encodeURIComponent(filename)}`,
      sha512: hash.digest('base64'), size: info.size,
    };
  };
  const platforms = {
    'latest-mac.yml': [`SAG-${version}-mac-arm64.zip`, `SAG-${version}-mac-arm64.dmg`],
    'latest.yml': [`SAG-Setup-${version}-win-x64.exe`],
  };
  const result = {};
  for (const [name, filenames] of Object.entries(platforms)) {
    const files = await Promise.all(filenames.map(describe));
    // JSON is valid YAML; electron-updater parses these documents with js-yaml.
    result[name] = JSON.stringify({ version, files, path: files[0].url, sha512: files[0].sha512,
      releaseDate: new Date().toISOString(), releaseName: `SAG ${tag}` }, null, 2) + '\n';
  }
  return result;
}

function gh(args) {
  try {
    return execFileSync('gh', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (error) {
    if (args[0] === 'api' && /HTTP 404/.test(String(error.stderr))) return null;
    throw error;
  }
}

export async function publishDesktopRelease({ repository, tag, assetsDir, notesFile, policy, commit = tag, run = gh }) {
  const feed = await buildManualFeed({ assetsDir, repository, tag });
  const channelJson = await run(['api', `repos/${repository}/releases/tags/${MANUAL_CHANNEL}`]);
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'sag-publish-feed-'));
  try {
    let marker = null;
    let channel = null;
    if (channelJson !== null) {
      channel = JSON.parse(channelJson);
      if (!channel.prerelease || channel.draft) throw new Error('Manual channel must be a published prerelease');
      await run(['release', 'download', MANUAL_CHANNEL, '--repo', repository, '--pattern', 'legacy-bridge.json', '--dir', temporary]);
      marker = JSON.parse(await readFile(path.join(temporary, 'legacy-bridge.json'), 'utf8'));
    }
    const { makeLatest } = releasePolicy(policy, marker);
    if (!makeLatest) {
      const latest = JSON.parse(await run(['api', `repos/${repository}/releases/latest`]));
      if (latest.tag_name !== marker.bridgeTag) throw new Error('Legacy latest has moved away from the bridge; stop publication.');
      const version = (value) => value.slice(1).split('.').map(Number);
      const left = version(tag), right = version(marker.bridgeTag);
      const comparison = left.reduce((result, value, index) => result || Math.sign(value - right[index]), 0);
      if (comparison <= 0) throw new Error('Manual release must be newer than the bridge');
    }
    const published = await run(['api', `repos/${repository}/releases/tags/${tag}`]);
    if (published === null) {
      const assets = (await readdir(assetsDir)).sort().map((name) => path.join(assetsDir, name));
      await run(['release', 'create', tag, ...assets, '--repo', repository, '--verify-tag',
        `--latest=${makeLatest}`, '--title', `SAG ${tag}`, '--notes-file', notesFile]);
    } else {
      const release = JSON.parse(published);
      if (release.draft || release.prerelease) throw new Error('Existing version release is not a published stable release');
      // Recover an interrupted index publication without mutating version assets.
      await run(['release', 'download', tag, '--repo', repository, '--pattern', 'SHA256SUMS.txt', '--dir', temporary]);
      const remote = await readFile(path.join(temporary, 'SHA256SUMS.txt'), 'utf8');
      const local = await readFile(path.join(assetsDir, 'SHA256SUMS.txt'), 'utf8');
      if (remote !== local) throw new Error('Published release checksums differ; version assets must not be overwritten');
    }
    const feedDir = path.join(temporary, 'feed');
    await mkdir(feedDir);
    for (const [name, content] of Object.entries(feed)) await writeFile(path.join(feedDir, name), content);
    if (makeLatest) {
      await writeFile(path.join(feedDir, 'legacy-bridge.json'), JSON.stringify({ bridgeTag: tag }) + '\n');
      // This non-version prerelease is an intentionally mutable index, never an installer release.
      await run(['release', 'create', MANUAL_CHANNEL, ...Object.keys(feed).map((name) => path.join(feedDir, name)),
        path.join(feedDir, 'legacy-bridge.json'), '--repo', repository, '--target', commit,
        '--prerelease', '--latest=false', '--title', 'SAG manual update channel',
        '--notes', 'Update metadata only. Installers and checksums are in immutable version releases.']);
    } else {
      const backupDir = path.join(temporary, 'backup');
      await mkdir(backupDir);
      const backupFiles = [];
      for (const name of Object.keys(feed)) {
        if (channel.assets?.some((asset) => asset.name === name)) {
          await run(['release', 'download', MANUAL_CHANNEL, '--repo', repository, '--pattern', name, '--dir', backupDir]);
          backupFiles.push(path.join(backupDir, name));
        }
      }
      const upload = async (files) => {
        let failure;
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            await run(['release', 'upload', MANUAL_CHANNEL, ...files, '--repo', repository, '--clobber']);
            return;
          } catch (error) { failure = error; }
        }
        throw failure;
      };
      try {
        await upload(Object.keys(feed).map((name) => path.join(feedDir, name)));
      } catch (error) {
        // GitHub asset replacement is not atomic: restore the previous metadata
        // when possible, but retain failure so CI requires operator attention.
        if (backupFiles.length) {
          try { await upload(backupFiles); }
          catch (recoveryError) { throw new AggregateError([error, recoveryError], 'Manual feed upload and recovery failed'); }
        }
        throw error;
      }
    }
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [tag, assetsDir = 'release-assets', notesFile = 'release-notes.md'] = process.argv.slice(2);
  const policy = JSON.parse(await readFile(new URL('../apps/desktop/release-policy.json', import.meta.url), 'utf8'));
  const commit = execFileSync('git', ['rev-parse', `${tag}^{commit}`], { encoding: 'utf8' }).trim();
  await publishDesktopRelease({ repository: process.env.GITHUB_REPOSITORY, tag, assetsDir, notesFile, policy, commit });
}
