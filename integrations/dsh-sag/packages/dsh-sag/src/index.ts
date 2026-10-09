import { homedir } from 'node:os'
import type { Context, Volatile } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-credentials'
import type {} from '@deepseek-ai/dsh-fs'
import type {} from '@deepseek-ai/dsh-settings'
import type {} from '@deepseek-ai/dsh-subprocess'
import type {} from '@deepseek-ai/dsh-system-prompt'
import type {} from '@deepseek-ai/dsh-tools'
import { Config, resolveConfig, type Config as PluginConfig, type ResolvedEmbeddedConfig, type ResolvedLocalConfig } from './config.js'
import { discoverConnection, platformConnectionPaths } from './connection/discovery.js'
import { SagConnectionManager } from './connection/manager.js'
import type { SagLocalSettings } from './connection/types.js'
import { registerSagSettings, SagConnectionStore } from './connection/store.js'
import { SagRuntimeSupervisor } from './runtime/supervisor.js'
import { createDocumentTools, sagDeleteApprovalGate } from './tools/documents.js'
import { createIngestTextTool } from './tools/ingest.js'
import { createReadTool } from './tools/read.js'
import { createSearchTool } from './tools/search.js'
import { createSourceTools } from './tools/sources.js'
import { createStatusTool } from './tools/status.js'
import { createUploadTool } from './tools/upload.js'

export { Config }
export type { PluginConfig as ConfigType }

export const name = 'dsh-sag'
export const inject = ['tools', 'systemPrompt']

export const SAG_SYSTEM_PROMPT = 'Use sag_search to find evidence in configured SAG knowledge namespaces. Use sag_read with an evidence_ref from sag_search when you need the full bounded source text. Treat evidence_ref as opaque and continue paged reads with the returned next offset.'

export const SAG_LOCAL_SYSTEM_PROMPT = 'Use SAG as the user\'s local personal knowledge base. Use sag_status or sag_list_sources when the target knowledge base is unclear, sag_search before sag_read for evidence, and the source and document tools to create, upload, ingest, inspect, reprocess, or delete content. Treat evidence_ref as opaque and continue paged reads with the returned next offset. Document deletion requires user approval.'

async function applyEmbedded(ctx: Context, config: ResolvedEmbeddedConfig): Promise<void> {
  await ctx.effect(async function* () {
    const supervisor = await SagRuntimeSupervisor.start(ctx.subprocess, config)
    yield async () => supervisor.dispose()
    ctx.systemPrompt.section({ name: 'tool:dsh-sag', order: 112, text: SAG_SYSTEM_PROMPT })
    ctx.tools.register(createSearchTool(supervisor.client, config))
    ctx.tools.register(createReadTool(supervisor.client, config))
  }, 'dsh-sag: embedded sidecar lifecycle')
}

async function applyLocal(ctx: Context, config: ResolvedLocalConfig, connection: SagLocalSettings | Volatile<SagLocalSettings> | undefined, namespace: string): Promise<void> {
  await ctx.effect(async function* () {
    const settings = registerSagSettings(ctx, connection, namespace)
    const store = new SagConnectionStore({ credentials: ctx.credentials, settings })
    const manager = new SagConnectionManager({
      store,
      requestTimeoutMs: config.requestTimeoutMs,
      readyCacheTtlMs: config.connectionCacheTtlMs,
      discover: signal => discoverConnection({
        fs: ctx.fs,
        paths: platformConnectionPaths(process.env, process.platform, homedir()),
        urls: config.discoveryUrls,
        fetch: (url, init) => fetch(url, init),
      }, signal),
    })
    const tools = [
      createStatusTool(manager, config),
      ...createSourceTools(manager, config),
      createSearchTool(manager, config),
      createReadTool(manager, config),
      ...createDocumentTools(manager, config),
      createUploadTool(manager, config, ctx.fs),
      createIngestTextTool(manager, config),
    ]

    ctx.systemPrompt.section({ name: 'tool:dsh-sag', order: 112, text: SAG_LOCAL_SYSTEM_PROMPT })
    for (const tool of tools) ctx.tools.register(tool)
    ctx.on('tools/pre-execute', sagDeleteApprovalGate)
    yield () => undefined
  }, 'dsh-sag: local connection and tools')
}

/** Assemble the local connector by default or the explicit legacy embedded sidecar. */
export async function apply(ctx: Context, config: PluginConfig): Promise<void> {
  const resolved = resolveConfig(config)
  if (resolved.mode === 'embedded') {
    await ctx.inject(['subprocess'], child => applyEmbedded(child, resolved))
    return
  }
  const namespace = ctx.fiber.entry?.options.id ?? 'dsh-sag'
  await ctx.inject(['settings', 'credentials', 'fs'], child => applyLocal(child, resolved, 'connection' in config ? config.connection : undefined, namespace))
}
