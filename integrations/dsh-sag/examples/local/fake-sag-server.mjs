import { createServer } from 'node:http'

const ACCESS_TOKEN = 'sag_local_fixture_token'
const CONTENT = '# Uploaded\n\nhello SAG'
const SOURCE_ID = 'source-1'
const DOCUMENT_ID = 'document-1'
const CHUNK_ID = 'chunk-1'

const capabilities = [
  'sources.list', 'sources.create', 'knowledge.search', 'knowledge.read',
  'documents.list', 'documents.get', 'documents.upload', 'documents.ingest',
  'documents.reprocess', 'documents.delete',
]

const mcpTools = [
  'list_sources', 'search', 'get_entity', 'list_documents',
  'outline', 'grep', 'read', 'get_chunk',
]

function json(response, status, value, headers = {}) {
  response.writeHead(status, { 'Content-Type': 'application/json', ...headers })
  response.end(JSON.stringify(value))
}

function source() {
  return {
    id: SOURCE_ID,
    name: '个人知识库',
    description: '本地 fixture',
    source_type: 'document',
    connector_kind: 'file_upload',
    status: 'active',
    document_count: 1,
    chunk_count: 1,
    event_count: 0,
    created_at: '2026-08-25T00:00:00Z',
    updated_at: '2026-08-25T00:00:00Z',
  }
}

function document(status) {
  const ready = status === 'ready'
  return {
    id: DOCUMENT_ID,
    source_id: SOURCE_ID,
    filename: 'uploaded.md',
    content_type: 'text/markdown',
    size_bytes: Buffer.byteLength(CONTENT),
    status,
    chunk_count: ready ? 1 : 0,
    event_count: 0,
    progress: ready ? 100 : 0,
    token_usage: 0,
    error: null,
    error_layer: null,
    error_stage: null,
    parser_provider: null,
    mineru_provider: null,
    mineru_model: null,
    parser_status: null,
    fallback_from: null,
    fallback_reason: null,
    original_file_available: true,
    created_at: '2026-08-25T00:00:00Z',
    updated_at: '2026-08-25T00:00:00Z',
  }
}

async function requestBody(request) {
  const chunks = []
  for await (const chunk of request) chunks.push(chunk)
  return Buffer.concat(chunks)
}

function multipartFile(body, contentType) {
  const boundary = /boundary=(?:"([^"]+)"|([^;]+))/.exec(contentType)?.slice(1).find(Boolean)
  if (boundary === undefined) throw new Error('multipart boundary is absent')
  const headerEnd = body.indexOf(Buffer.from('\r\n\r\n'))
  if (headerEnd < 0) throw new Error('multipart headers are absent')
  const headers = body.subarray(0, headerEnd).toString('utf8')
  const fieldName = /content-disposition:\s*form-data;[^\r\n]*\bname="([^"]+)"/i.exec(headers)?.[1]
  const filename = /filename="([^"]+)"/.exec(headers)?.[1]
  const fileContentType = /^content-type:\s*([^\r\n]+)$/im.exec(headers)?.[1]?.trim().toLowerCase()
  if (fieldName === undefined) throw new Error('multipart field name is absent')
  if (filename === undefined) throw new Error('multipart filename is absent')
  if (fileContentType === undefined) throw new Error('multipart file content type is absent')
  const end = body.indexOf(Buffer.from(`\r\n--${boundary}`), headerEnd + 4)
  if (end < 0) throw new Error('multipart closing boundary is absent')
  return { fieldName, filename, contentType: fileContentType, bytes: body.subarray(headerEnd + 4, end) }
}

function mcpResult(id, result) {
  return { jsonrpc: '2.0', id, result }
}

/** Start a deterministic keyless SAG-compatible loopback service for assembled tests. */
export async function startFakeSagServer(options = {}) {
  const requests = []
  const advertisedCapabilities = options.capabilities ?? capabilities
  const advertisedMcpTools = options.mcpTools ?? mcpTools
  let uploaded = false
  const server = createServer(async (request, response) => {
    try {
      const origin = `http://${request.headers.host}`
      const url = new URL(request.url ?? '/', origin)
      const route = `${request.method} ${url.pathname}`

      if (route === 'GET /api/v1/system/dsh-connection') {
        requests.push(route)
        json(response, 200, {
          schemaVersion: 1,
          name: 'SAG 知识库',
          apiUrl: `${origin}/api/v1`,
          mcpUrl: `${origin}/mcp/`,
          accessToken: ACCESS_TOKEN,
          defaultSourceId: SOURCE_ID,
        })
        return
      }

      if (route === 'GET /api/v1/system/health') {
        requests.push(route)
        json(response, 200, { status: 'ok' })
        return
      }
      if (route === 'GET /api/v1/system/ready') {
        requests.push(route)
        json(response, 200, { status: 'ready', db: true })
        return
      }

      if (request.headers.authorization !== `Bearer ${ACCESS_TOKEN}`) {
        json(response, 401, { error: { code: 'unauthorized', message: 'invalid connector token', retryable: false } })
        return
      }

      if (url.pathname === '/mcp/' || url.pathname === '/mcp') {
        if (request.method !== 'POST') {
          response.writeHead(405, { Allow: 'POST' })
          response.end()
          return
        }
        const payload = JSON.parse((await requestBody(request)).toString('utf8'))
        const method = payload.method
        if (typeof method === 'string') requests.push(`MCP ${method}`)
        if (method === 'notifications/initialized') {
          response.writeHead(202)
          response.end()
          return
        }
        if (method === 'initialize') {
          json(response, 200, mcpResult(payload.id, {
            protocolVersion: payload.params?.protocolVersion ?? '2024-11-05',
            capabilities: { tools: {} },
            serverInfo: { name: 'SAG Knowledge MCP', version: '1.0.0' },
          }))
          return
        }
        if (method === 'tools/list') {
          json(response, 200, mcpResult(payload.id, {
            tools: advertisedMcpTools.map(name => ({ name, description: `${name} fixture`, inputSchema: { type: 'object', properties: {} } })),
          }))
          return
        }
        json(response, 200, { jsonrpc: '2.0', id: payload.id, error: { code: -32601, message: 'Method not found' } })
        return
      }

      requests.push(route)
      if (route === 'GET /api/v1/system/dsh') {
        json(response, 200, {
          schemaVersion: 1,
          capabilities: advertisedCapabilities,
          ...(advertisedCapabilities.includes('documents.upload') ? { upload: { maxMb: 100, extensions: ['md', 'pdf', 'txt'] } } : {}),
          defaultSourceId: SOURCE_ID,
        })
      } else if (route === 'GET /api/v1/sources') {
        if (options.disableSources === true) json(response, 404, { error: { code: 'not_found', message: 'sources disabled' } })
        else json(response, 200, [source()])
      } else if (route === `POST /api/v1/sources/${SOURCE_ID}/documents`) {
        const file = multipartFile(await requestBody(request), request.headers['content-type'] ?? '')
        requests.push(`UPLOAD field=${file.fieldName} filename=${file.filename} contentType=${file.contentType} bytes=${file.bytes.length}`)
        if (file.fieldName !== 'file' || file.filename !== 'uploaded.md' || file.contentType !== 'text/markdown' || file.bytes.toString('utf8') !== CONTENT) {
          json(response, 422, { error: { code: 'validation_error', message: 'unexpected upload fixture' } })
          return
        }
        uploaded = true
        json(response, 201, document('pending'))
      } else if (route === `GET /api/v1/sources/${SOURCE_ID}/documents/${DOCUMENT_ID}` && uploaded) {
        json(response, 200, document('ready'))
      } else if (route === 'POST /api/v1/search' && (uploaded || options.searchWithoutUpload === true)) {
        const body = JSON.parse((await requestBody(request)).toString('utf8'))
        if (body.query !== 'hello SAG' || body.save_exploration !== false) {
          json(response, 422, { error: { code: 'validation_error', message: 'unexpected search request' } })
          return
        }
        json(response, 200, {
          query: body.query,
          sections: [{
            chunk_id: CHUNK_ID,
            heading: 'Uploaded',
            content: CONTENT,
            score: 0.99,
            rank: 1,
            source_id: SOURCE_ID,
            source_name: '个人知识库',
          }],
          events: [], entities: [], relations: [], source_hits: [],
          summary: '', exploration_id: null, stats: { sources: 1 },
        })
      } else if (route === `GET /api/v1/sources/${SOURCE_ID}/chunks/${CHUNK_ID}` && uploaded) {
        json(response, 200, {
          chunk_id: CHUNK_ID,
          document_id: DOCUMENT_ID,
          rank: 1,
          heading: 'Uploaded',
          content: CONTENT,
          source_id: SOURCE_ID,
          source_name: '个人知识库',
        })
      } else {
        json(response, 404, { error: { code: 'not_found', message: 'fixture route not found', retryable: false } })
      }
    } catch (error) {
      json(response, 500, { error: { code: 'fixture_error', message: error instanceof Error ? error.message : String(error) } })
    }
  })

  let closePromise
  const close = () => {
    if (closePromise !== undefined) return closePromise
    closePromise = new Promise((resolve, reject) => {
      server.closeAllConnections()
      if (!server.listening) {
        resolve()
        return
      }
      server.close(error => error === undefined || error.code === 'ERR_SERVER_NOT_RUNNING' ? resolve() : reject(error))
    })
    return closePromise
  }
  let origin
  try {
    await new Promise((resolve, reject) => {
      const onError = error => {
        server.off('listening', onListening)
        reject(error)
      }
      const onListening = () => {
        server.off('error', onError)
        resolve()
      }
      server.once('error', onError)
      server.once('listening', onListening)
      server.listen(0, '127.0.0.1')
    })
    const address = server.address()
    if (address === null || typeof address === 'string') throw new Error('fake SAG did not bind a TCP port')
    origin = `http://127.0.0.1:${address.port}`
    await options.afterListen?.(origin)
  } catch (error) {
    await close()
    throw error
  }
  return {
    origin,
    requests,
    close,
  }
}

/** Own one fake server for the complete callback lifetime, including callback failures. */
export async function withFakeSagServer(run) {
  const server = await startFakeSagServer()
  try {
    return await run(server)
  } finally {
    await server.close()
  }
}
