/** Deterministic OpenAI-compatible model fixture; SAG itself stays the real API/engine. */
import { createServer } from 'node:http'
import { appendFile } from 'node:fs/promises'

const port = Number(process.env.SAG_FIXTURE_PORT ?? 18247)
const evidenceFile = process.env.SAG_FIXTURE_EVIDENCE_FILE
let sequence = 0

function responseForSchema(schema, request) {
  let input
  for (const message of [...(request.messages ?? [])].reverse()) {
    if (message.role !== 'user') continue
    try { input = JSON.parse(message.content); break } catch { /* prompt text */ }
  }
  const items = input?.data?.items ?? []
  if (schema?.properties?.type?.const === 'response' && schema.properties.data) {
    const eventDefinition = schema.$defs.FullExtractedEvent ?? schema.$defs.ExtractedEvent
    const content = items.map(item => item.content).join('\n') || 'Bounded SAG acceptance content.'
    const entityType = input?.data?.meta?.entity_types?.[0]?.type ?? 'concept'
    const event = {
      title: 'SAG integration acceptance', content,
      entities: [{ type: entityType, name: 'ORBIT-7246', description: 'The bounded integration acceptance marker.' }],
      is_valid: true, children: [],
      ...(eventDefinition.properties.reason ? { reason: 'Deterministic extraction from the provided test document.' } : {}),
      ...(eventDefinition.properties.summary ? { summary: content.slice(0, 120) } : {}),
      ...(eventDefinition.properties.references ? { references: input?.data?.meta?.source_type === 'parent' ? [] : items.map(item => item.id) } : {}),
    }
    return { type: 'response', data: { items: [event] } }
  }
  function fill(node, depth = 0) {
    if (depth > 8) return null
    if (node.$ref) node = schema.$defs[node.$ref.split('/').at(-1)]
    if (Object.hasOwn(node, 'const')) return node.const
    if (node.enum) return node.enum[0]
    if (node.anyOf || node.oneOf) return fill((node.anyOf ?? node.oneOf).find(option => option.type !== 'null') ?? { type: 'null' }, depth + 1)
    if (node.type === 'object') return Object.fromEntries(Object.entries(node.properties ?? {}).map(([key, value]) => [key, key === 'rewritten_query' ? 'SAG acceptance' : fill(value, depth + 1)]))
    if (node.type === 'array') return Array.from({ length: node.minItems ?? 0 }, () => fill(node.items, depth + 1))
    if (node.type === 'boolean') return true
    if (node.type === 'integer' || node.type === 'number') return node.minimum ?? 1
    if (node.type === 'null') return null
    return 'SAG acceptance'
  }
  return schema ? fill(schema) : { title: 'SAG acceptance', summary: 'Bounded fixture document.' }
}

const server = createServer(async (request, response) => {
  try {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    const id = ++sequence
    let result
    if (request.url?.endsWith('/embeddings')) {
      const inputs = Array.isArray(body.input) ? body.input : [body.input]
      const dimensions = body.dimensions ?? 16
      result = {
        object: 'list', model: body.model,
        data: inputs.map((input, index) => ({ object: 'embedding', index, embedding: Array.from({ length: dimensions }, (_, n) => n === 0 ? 1 : 0.01) })),
        usage: { prompt_tokens: inputs.length, total_tokens: inputs.length },
      }
    } else if (request.url?.endsWith('/chat/completions')) {
      const schema = body.response_format?.json_schema?.schema
      const content = JSON.stringify(responseForSchema(schema, body))
      result = {
        id: `fixture-${id}`, object: 'chat.completion', created: Math.floor(Date.now() / 1000), model: body.model,
        choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
        usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 },
      }
    } else {
      response.writeHead(404).end(); return
    }
    if (evidenceFile) await appendFile(evidenceFile, JSON.stringify({ id, path: request.url, model: body.model, inputCount: Array.isArray(body.input) ? body.input.length : 1 }) + '\n')
    response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(result))
  } catch (error) {
    response.writeHead(500, { 'Content-Type': 'application/json' }).end(JSON.stringify({ error: { message: String(error) } }))
  }
})
server.listen(port, '127.0.0.1', () => console.log(`Deterministic model fixture: http://127.0.0.1:${port}/v1`))
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => server.close(() => process.exit(0)))
