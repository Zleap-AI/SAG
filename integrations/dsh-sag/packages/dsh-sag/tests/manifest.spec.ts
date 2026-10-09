import { readFile } from 'node:fs/promises'
import { describe, expect, it } from 'vitest'

interface PackageManifest {
  readonly type: string
  readonly bin: Readonly<Record<string, string>>
  readonly files: readonly string[]
  readonly dsh: { readonly bundle: { readonly patch: string } }
  readonly dependencies: Readonly<Record<string, string>>
  readonly peerDependencies: Readonly<Record<string, string>>
  readonly devDependencies: Readonly<Record<string, string>>
  readonly scripts: Readonly<Record<string, string>>
}

async function readText(relative: string): Promise<string> {
  return readFile(new URL(relative, import.meta.url), 'utf8')
}

describe('package manifest', () => {
  it('ships an installable DSH bundle with the supported peer versions', async () => {
    const url = new URL('../package.json', import.meta.url)
    const manifest = JSON.parse(await readFile(url, 'utf8')) as PackageManifest

    expect(manifest.type).toBe('module')
    expect(manifest.dsh.bundle.patch).toBe('./cordis.patch.yml')
    expect(manifest.bin).toEqual({ 'dsh-sag': './lib/dsh-sag-cli.js' })
    expect(manifest.files).toContain('cordis.patch.yml')
    expect(manifest.files).toContain('lib/**/*.js')
    expect(manifest.files).toContain('lib/**/*.d.ts')
    expect(manifest.files).toContain('docs/embedded.md')
    expect(manifest.files).toContain('runtime')
    expect(manifest.files).toContain('README.md')
    expect(manifest.files).toContain('README.zh.md')
    expect(manifest.peerDependencies['@deepseek-ai/cordis']).toBe('~4.0.4')
    const supportedHostRange = '0.2.0-rc.2'
    expect(manifest.peerDependencies['@deepseek-ai/dsh-tools']).toBe(supportedHostRange)
    expect(manifest.peerDependencies['@deepseek-ai/dsh-subprocess']).toBe(supportedHostRange)
    for (const hostPeer of [
      '@deepseek-ai/cordis',
      '@deepseek-ai/dsh-credentials',
      '@deepseek-ai/dsh-credentials-local',
      '@deepseek-ai/dsh-settings',
      '@deepseek-ai/dsh-home-paths',
      '@deepseek-ai/dsh-atomic-write',
      '@deepseek-ai/dsh-app-boot',
    ]) {
      expect(manifest.dependencies[hostPeer]).toBeUndefined()
      expect(manifest.peerDependencies[hostPeer]).toBe(hostPeer === '@deepseek-ai/cordis' ? '~4.0.4' : supportedHostRange)
    }
    expect(Object.keys(manifest.dependencies).filter(name => name in manifest.peerDependencies)).toEqual([])
    for (const [name, version] of Object.entries(manifest.peerDependencies)) {
      expect(manifest.devDependencies[name], `${name} must mirror its host peer for development`).toBeDefined()
      if (name.startsWith('@deepseek-ai/dsh-')) expect(manifest.devDependencies[name]).toBe('0.2.0-rc.2')
    }
    expect(manifest.files).toContain('THIRD_PARTY_NOTICES')
    expect(manifest.scripts.build).toContain('build-cli.mjs')
  })

  it('preserves bundled license comments and generates notices from installed manifests', async () => {
    const builder = await readText('../../../scripts/build-cli.mjs')

    expect(builder).toContain("legalComments: 'eof'")
    expect(builder).toContain('THIRD_PARTY_NOTICES')
    expect(builder).toContain('metafile: true')
  })

  it('starts the installed web profile and exercises an invalid-mode negative control', async () => {
    const packCheck = await readText('../../../scripts/check-pack.mjs')

    expect(packCheck).toContain("['--profile', 'web', '--no-open', '--port', '0']")
    expect(packCheck).toContain("process.env.DSH_BIN ?? 'dsh'")
    expect(packCheck).not.toContain('expectedDshVersion')
    expect(packCheck).toContain('mode: invalid')
    expect(packCheck).toContain("kill('SIGTERM')")
    expect(packCheck).not.toContain("['--profile', 'web', '--help']")
  })

  it('keeps virtual environments out of the workspace and published archive', async () => {
    const ignore = await readText('../../../.gitignore')
    const packCheck = await readText('../../../scripts/check-pack.mjs')

    expect(ignore.split(/\r?\n/u)).toContain('.venv/')
    expect(packCheck).toContain('/\\.venv')
  })

  it('keeps every public README focused on quick local usage', async () => {
    const readmes = await Promise.all([
      readText('../../../README.md'),
      readText('../README.zh.md'),
      readText('../README.md'),
    ])

    for (const readme of readmes) {
      expect(readme).toContain('dsh plugin --profile web exec dsh-sag setup')
      expect(readme).toContain('dsh plugin --profile web exec dsh-sag doctor')
      expect(readme).not.toMatch(/^dsh-sag (?:setup|doctor)/gmu)
      expect(readme).not.toMatch(/实施计划|迭代计划|路线图|第一阶段|第二阶段|origin\/main/iu)
      expect(readme).not.toMatch(/implementation plan|iteration history|roadmap|test matrix/iu)
      expect(readme).not.toContain('DSH_SAG_ENV_FILE')
    }
  })

  it('documents all setup routes and keeps embedded setup in the advanced guide', async () => {
    const root = await readText('../../../README.md')
    const zh = await readText('../README.zh.md')
    const en = await readText('../README.md')
    const embedded = await readText('../docs/embedded.md')

    for (const readme of [root, zh, en]) {
      expect(readme).toContain('dsh plugin --profile web exec dsh-sag setup\n')
      expect(readme).toContain('dsh plugin --profile web exec dsh-sag setup ./sag-dsh.json')
      expect(readme).toContain('dsh plugin --profile web exec dsh-sag setup --url http://127.0.0.1:8000')
    }
    expect(zh).toContain('docs/embedded.md')
    expect(en).toContain('docs/embedded.md')
    expect(zh.indexOf('exec dsh-sag setup')).toBeGreaterThan(zh.indexOf('exec dsh-sag doctor'))
    expect(en.indexOf('exec dsh-sag setup')).toBeGreaterThan(en.indexOf('exec dsh-sag doctor'))
    expect(embedded).toContain('Python 3.11')
    expect(embedded).toContain('dsh plugin --profile web exec node node_modules/@zleap-ai/dsh-sag/scripts/setup-runtime.mjs')
    expect(embedded).not.toContain('exec node ./node_modules/')
    expect(embedded).toContain('$DSH_HOME/profiles/web/cordis.patch.yml')
    expect(embedded).toContain('dsh --profile web --dump-config')
    expect(embedded).toContain('dsh web')
    expect(embedded).toContain('DSH_SAG_PYTHON')
    expect(embedded).toContain('DSH_SAG_ENV_FILE')
    expect(embedded).toContain('DSH_SAG_NAMESPACES')
    expect(embedded).toContain('shutdown')
  })
})
