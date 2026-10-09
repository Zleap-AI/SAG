import { readFile } from 'node:fs/promises'
import { describe, expect, it } from 'vitest'

describe('Bundle patch', () => {
  it('inserts one stable local plugin row without embedded environment defaults', async () => {
    const text = await readFile(new URL('../cordis.patch.yml', import.meta.url), 'utf8')

    expect(text).toContain('id: dsh-sag')
    expect(text).toContain("name: '@zleap-ai/dsh-sag'")
    expect(text).toContain('mode: local')
    expect(text).not.toContain('process.env.DSH_SAG_PYTHON')
    expect(text).not.toContain('process.env.DSH_SAG_ENV_FILE')
    expect(text).not.toContain('process.env.DSH_SAG_NAMESPACES')
    expect(text).not.toMatch(/\/Users\/|api[_-]?key\s*:/i)
    expect((text.match(/- id: dsh-sag/g) ?? [])).toHaveLength(1)
  })
})
