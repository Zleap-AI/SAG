import type { FileSystem } from '@deepseek-ai/dsh-fs'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { ResolvedLocalConfig } from '../config.js'
import { jsonRender, localInputError, localOperation, requireToolCapability, selectSource, type ConnectionManager } from './local.js'

const ACCEPTED_OUTPUT = {
  type: 'object', additionalProperties: false, properties: {
    accepted: { type: 'boolean', required: true }, documentId: { type: 'string', required: true },
    sourceId: { type: 'string', required: true }, status: { type: 'string', required: true },
  },
} as const

function basename(path: string): string {
  return path.split(/[\\/]/).at(-1) ?? path
}

function extension(filename: string): string {
  const index = filename.lastIndexOf('.')
  return index < 0 ? '' : filename.slice(index + 1).toLowerCase()
}

function contentType(ext: string): string {
  if (ext === 'md' || ext === 'markdown') return 'text/markdown'
  if (ext === 'txt') return 'text/plain'
  if (ext === 'json') return 'application/json'
  if (ext === 'pdf') return 'application/pdf'
  return 'application/octet-stream'
}

/** Build a bounded file-upload tool that reads exclusively through dsh FS. */
export function createUploadTool(manager: ConnectionManager, config: ResolvedLocalConfig, fs: FileSystem) {
  return defineTool({
    name: 'sag_upload_file',
    description: 'Upload one admitted local file to a SAG knowledge base and return its asynchronous processing status.',
    parameters: {
      path: { type: 'string', required: true, description: 'File path resolved through the dsh filesystem.' },
      source_id: { type: 'string', description: 'Target knowledge-base id; omitted only when a default or single source exists.' },
    },
    output: { schema: ACCEPTED_OUTPUT, render: (_args, value) => jsonRender(value) },
    timeoutMs: config.requestTimeoutMs,
    async execute(args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        requireToolCapability(connection, 'sag_upload_file')
        const upload = connection.capabilities.upload
        if (upload === undefined) localInputError('This SAG version does not provide documents.upload limits. Upgrade SAG and retry.')
        const sourceId = await selectSource(connection, args.source_id, exec.signal)
        const target = await fs.resolve(args.path, { signal: exec.signal })
        const info = await fs.stat(target, exec.signal)
        if (info?.type !== 'file') localInputError('sag_upload_file 只能上传普通文件。')
        const filename = basename(target.displayPath)
        const ext = extension(filename)
        const allowed = upload.extensions.map(value => value.toLowerCase())
        if (!allowed.includes(ext)) localInputError(`SAG 不允许 .${ext || '(无扩展名)'} 文件；可用扩展名：${allowed.join('、')}`)
        const sagLimit = upload.maxMb * 1024 * 1024
        const limit = Math.min(sagLimit, config.maxUploadBytes)
        if (info.size !== undefined && info.size > limit) localInputError(`文件超过上传上限 ${Math.floor(limit / 1024 / 1024)} MiB。`)
        const bytes = await fs.readBytes(target, exec.signal, limit)
        if (bytes.length > limit) localInputError(`文件超过上传上限 ${Math.floor(limit / 1024 / 1024)} MiB。`)
        const document = await connection.gateway.uploadFile({ sourceId, filename, contentType: contentType(ext), bytes }, exec.signal)
        return { accepted: true, documentId: document.id, sourceId, status: document.status ?? 'pending' }
      })
    },
    presentCall: args => ({ card: 'generic', title: 'Upload file to SAG', kind: 'edit', rawInput: args.path, locations: [{ path: args.path }] }),
  })
}
