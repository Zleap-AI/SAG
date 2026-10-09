import assert from 'node:assert/strict'
import test from 'node:test'
import { validateRelease } from '../check-release.mjs'

const manifest = {
  name: '@zleap-ai/dsh-sag',
  version: '0.2.0',
  repository: {
    url: 'git+https://github.com/Zleap-AI/SAG.git',
    directory: 'integrations/dsh-sag/packages/dsh-sag',
  },
}

test('accepts an independent plugin release without using the SAG application version', () => {
  assert.equal(validateRelease('dsh-sag-v0.2.0', manifest), '0.2.0')
})

test('rejects SAG desktop release tags and plugin version mismatches', () => {
  assert.throws(() => validateRelease('v0.2.0', manifest), /dsh-sag-v0\.2\.0/)
  assert.throws(() => validateRelease('dsh-sag-v0.2.1', manifest), /dsh-sag-v0\.2\.0/)
})

test('rejects an unintended npm package or source repository', () => {
  assert.throws(() => validateRelease('dsh-sag-v0.2.0', { ...manifest, name: 'sag' }), /package name/)
  assert.throws(() => validateRelease('dsh-sag-v0.2.0', { ...manifest, repository: { url: 'git+https://github.com/Zleap-AI/dsh-sag.git' } }), /repository/)
})

test('rejects unstable versions rather than silently publishing them as npm latest', () => {
  assert.throws(() => validateRelease('dsh-sag-v0.2.1-alpha.1', { ...manifest, version: '0.2.1-alpha.1' }), /stable/)
})
