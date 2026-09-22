import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test, { after } from 'node:test';
import { runInNewContext } from 'node:vm';
const output = mkdtempSync(join(tmpdir(), 'sag-updater-test-'));
execFileSync(process.execPath, [fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url)), '--project', fileURLToPath(new URL('../tsconfig.json', import.meta.url)), '--outDir', output]);
const source = readFileSync(join(output, 'updater.js'), 'utf8');
after(() => rmSync(output, { recursive: true, force: true }));

// Replace only the Electron/network/installer boundary; execute the real controller.
function harness(packaged = true) {
  let checks = 0, downloads = 0, installs = 0, dialogs = 0;
  const timers: Array<() => void> = [];
  const nativeUpdater = new EventEmitter();
  const updater = Object.assign(new EventEmitter(), {
    autoDownload: true, autoInstallOnAppQuit: true,
    checkForUpdates: async () => { checks++; },
    downloadUpdate: async () => { downloads++; },
    quitAndInstall: (_silent: boolean, _runAfter: boolean) => { installs++; },
  });
  const modules: Record<string, any> = {
    'node:fs': { existsSync: () => true }, 'node:path': { join: (...p: string[]) => p.join('/') },
    electron: { autoUpdater: nativeUpdater, app: { isPackaged: packaged }, dialog: { showMessageBox: async () => { dialogs++; return { response: 0 }; } }, shell: { openExternal: async () => {} } },
    'electron-log/main': { info() {}, error() {} }, 'electron-updater': { autoUpdater: updater },
    './channels': { DESKTOP_CHANNELS: { updateState: 'state' } },
    './config': { desktopConfig: { updateCheckDelayMs: 30000, updateCheckIntervalMs: 21600000 } },
    './updater-error': { describeUpdaterError: () => ({ title: 'error', message: 'error', detail: 'error' }) },
  };
  const exports: any = {};
  runInNewContext(source, { exports, require: (id: string) => modules[id], process: { resourcesPath: '/resources' }, setTimeout: (fn: () => void) => { timers.push(fn); return { unref() {} }; }, setInterval: (fn: () => void) => { timers.push(fn); return { unref() {} }; }, clearTimeout() {}, clearInterval() {} });
  const controller = exports.createUpdaterController(() => ({ isDestroyed: () => false, webContents: { send() {} } }));
  return { updater, nativeUpdater, controller, timers, counts: () => ({ checks, downloads, installs, dialogs }) };
}

test('discovery and cached events never authorize download or installation', async () => {
  const h = harness();
  assert.equal(h.updater.autoDownload, false);
  assert.equal(h.updater.autoInstallOnAppQuit, false);
  h.timers[0](); await Promise.resolve();
  h.updater.emit('update-available', { version: '2.0.0' });
  h.updater.emit('update-downloaded', { version: '2.0.0' });
  assert.equal(h.controller.getState().status, 'available');
  assert.deepEqual(h.counts(), { checks: 1, downloads: 0, installs: 0, dialogs: 0 });
});

test('download and install require separate version-specific user actions', async () => {
  const h = harness(); h.updater.emit('update-available', { version: '2.0.0' });
  assert.equal((await h.controller.download('1.9.0')).started, false);
  assert.equal(h.controller.install('2.0.0').started, false);
  assert.equal((await h.controller.download('2.0.0')).started, true);
  h.updater.emit('update-downloaded', { version: '2.0.0' });
  assert.equal(h.controller.getState().status, 'downloaded');
  assert.equal(h.counts().installs, 0); assert.equal(h.counts().dialogs, 0);
  assert.equal(h.controller.install('1.9.0').started, false);
  assert.equal(h.controller.install('2.0.0').started, true);
  assert.equal(h.controller.install('2.0.0').started, false);
  assert.equal(h.counts().installs, 1);
});

test('pending consent cannot change target or launch concurrent work', async () => {
  const h = harness(); let finish: (() => void) | undefined;
  h.updater.downloadUpdate = () => new Promise<void>((resolve) => { finish = resolve; });
  h.updater.emit('update-available', { version: '2.0.0' });
  const download = h.controller.download('2.0.0');
  assert.equal((await h.controller.download('2.0.0')).started, false);
  await h.controller.check(); h.updater.emit('checking-for-update');
  h.updater.emit('update-available', { version: '2.1.0' }); h.updater.emit('update-not-available');
  assert.equal(h.controller.getState().status, 'downloading'); assert.equal(h.controller.getState().version, '2.0.0');
  h.updater.emit('update-downloaded', { version: '2.1.0' }); assert.equal(h.controller.getState().status, 'downloading');
  h.updater.emit('update-downloaded', { version: '2.0.0' }); finish?.(); await download;
  await h.controller.check(); assert.equal(h.counts().checks, 0); assert.equal(h.controller.getState().status, 'downloaded');
});

test('download failures retain the target and allow explicit retry', async () => {
  const h = harness(); h.updater.emit('update-available', { version: '2.0.0' });
  h.updater.downloadUpdate = async () => { throw new Error('network reset'); };
  assert.equal((await h.controller.download('2.0.0')).started, false);
  assert.equal(h.controller.getState().operation, 'download'); assert.equal(h.controller.getState().version, '2.0.0');
  h.updater.downloadUpdate = async () => {};
  assert.equal((await h.controller.download('2.0.0')).started, true);
  h.updater.emit('update-downloaded', { version: '2.0.0' }); assert.equal(h.controller.getState().status, 'downloaded');
});

test('installation failures allow explicit retry without authorizing ordinary quit', async () => {
  const h = harness(); h.updater.emit('update-available', { version: '2.0.0' });
  await h.controller.download('2.0.0'); h.updater.emit('update-downloaded', { version: '2.0.0' });
  h.updater.quitAndInstall = () => { throw new Error('installer failed'); };
  assert.equal(h.controller.install('2.0.0').started, false); assert.equal(h.controller.getState().operation, 'install');
  h.updater.quitAndInstall = () => {};
  assert.equal(h.controller.install('2.0.0').started, true); assert.equal(h.updater.autoInstallOnAppQuit, false);
});

test('unpackaged runs never check, download, install, or schedule work', async () => {
  const h = harness(false);
  assert.equal((await h.controller.check()).supported, false); assert.equal((await h.controller.download('2.0.0')).started, false);
  assert.equal(h.controller.install('2.0.0').started, false); assert.equal(h.timers.length, 0);
  assert.deepEqual(h.counts(), { checks: 0, downloads: 0, installs: 0, dialogs: 0 });
});


test('a failed macOS install does not leave a second installation callback on retry', async () => {
  const h = harness();
  let nativeInstalls = 0;
  // MacUpdater registers an install listener before Squirrel validates signatures.
  h.updater.quitAndInstall = () => {
    h.nativeUpdater.on('update-downloaded', () => { nativeInstalls++; });
  };
  h.updater.emit('update-available', { version: '2.0.0' });
  await h.controller.download('2.0.0'); h.updater.emit('update-downloaded', { version: '2.0.0' });
  assert.equal(h.controller.install('2.0.0').started, true);
  h.updater.emit('error', new Error('Squirrel validation failed'));
  assert.equal(h.controller.install('2.0.0').started, true);
  h.nativeUpdater.emit('update-downloaded');
  assert.equal(nativeInstalls, 1);
});

test('background errors do not open blocking prompts while a downloaded update waits for consent', async () => {
  const h = harness(); h.updater.emit('update-available', { version: '2.0.0' });
  await h.controller.download('2.0.0'); h.updater.emit('update-downloaded', { version: '2.0.0' });
  h.updater.emit('error', new Error('background failure'));
  assert.equal(h.counts().dialogs, 0);
  assert.equal(h.controller.getState().status, 'downloaded');
});
