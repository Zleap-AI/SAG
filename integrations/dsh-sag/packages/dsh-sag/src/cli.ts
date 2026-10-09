#!/usr/bin/env node
import { realpathSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'
import { parseConnectionDescriptor } from './connection/descriptor.js'
import { SAG_SETUP_COMMAND } from './connection/guidance.js'
import {
  SagConnectionManager,
  type SagConnectionManagerDeps,
  type SagConnectionReport,
} from './connection/manager.js'
import type { DiscoveryResult } from './connection/discovery.js'
import type { SagConnectionDescriptor } from './connection/types.js'
import { createCliRuntime, type CliRuntime } from './cli/runtime.js'

export type { CliRuntime } from './cli/runtime.js'

/** Minimal output ports for the reusable command-line entry point. */
export interface CliIo {
  stdout(line: string): void
  stderr(line: string): void
}

/** Injectable process boundaries for focused command-line tests. */
export interface CliDependencies {
  createRuntime(): Promise<CliRuntime>
  readFile?(path: string, encoding: 'utf8'): Promise<string>
  homedir?(): string
  /** Testable hard deadline for one complete connection check. */
  requestTimeoutMs?: number
}

interface ParsedCommand {
  readonly kind: 'help' | 'setup' | 'doctor'
  readonly path?: string
  readonly url?: string
}

const HELP = `用法：
  dsh-sag setup
  dsh-sag setup --url <SAG 本机地址>
  dsh-sag setup <SAG 导出文件>
  dsh-sag doctor
`

const RECOVERY = `请启动 SAG 后重试；也可运行 ${SAG_SETUP_COMMAND} 重新发现。\n`

function usage(io: CliIo, message?: string): number {
  if (message !== undefined) io.stderr(`${message}\n`)
  io.stderr(HELP)
  return 2
}

function parse(argv: readonly string[], io: CliIo): ParsedCommand | number {
  let result: ReturnType<typeof parseArgs>
  try {
    result = parseArgs({
      args: argv,
      options: {
        help: { type: 'boolean', short: 'h' },
        url: { type: 'string' },
      },
      allowPositionals: true,
      strict: false,
    })
  } catch {
    return usage(io, '参数无效。')
  }
  const keys = Object.keys(result.values)
  if (keys.some(key => key !== 'help' && key !== 'url')) return usage(io, '参数无效。')
  if (result.values.help === true) return result.positionals.length === 0 && keys.length === 1 ? { kind: 'help' } : usage(io, '参数无效。')
  const [command, ...positionals] = result.positionals
  if (command === 'doctor' && positionals.length === 0 && keys.length === 0) return { kind: 'doctor' }
  if (command !== 'setup') return usage(io, '参数无效。')
  const url = result.values.url
  if (url !== undefined && (typeof url !== 'string' || positionals.length !== 0)) return usage(io, '参数无效。')
  if (url === undefined && positionals.length > 1) return usage(io, '参数无效。')
  if (url !== undefined) {
    try {
      const parsed = new URL(url)
      if ((parsed.protocol !== 'http:' && parsed.protocol !== 'https:') || parsed.username || parsed.password) throw new Error('invalid URL')
    } catch {
      return usage(io, 'SAG 地址必须是没有用户名或密码的 HTTP(S) 地址。')
    }
    return { kind: 'setup', url }
  }
  return { kind: 'setup', ...(positionals[0] === undefined ? {} : { path: positionals[0] }) }
}

function expandHome(path: string, getHome: () => string): string {
  if (path === '~') return getHome()
  if (path.startsWith('~/') || path.startsWith('~\\')) return `${getHome()}${path.slice(1)}`
  return path
}

function redactedMessage(error: unknown, secrets: readonly string[] = []): string {
  const message = typeof error === 'string' ? error : error instanceof Error ? error.message : '未知错误'
  return secrets.reduce((value, secret) => secret ? value.split(secret).join('<redacted>') : value, message)
    .replace(/Bearer\s+[^\s,;]+/giu, 'Bearer <redacted>')
    .replace(/([?&](?:access_)?token=)[^&#\s]+/giu, '$1<redacted>')
    .replace(/(\/\/)[^/@\s]+@/gu, '$1<redacted>@')
}

function diagnostic(report: SagConnectionReport, io: CliIo): void {
  if (report.status !== 'ready') {
    io.stderr(`SAG 未就绪：${report.status}\n`)
    for (const error of report.errors ?? []) io.stderr(`- ${redactedMessage(error)}\n`)
    for (const item of report.discovery?.diagnostics ?? []) io.stderr(`- ${redactedMessage(item.action)}\n`)
    return
  }
  io.stdout('SAG 已连接。\n')
  io.stdout(`健康检查：${report.health ? '正常' : '失败'}；就绪检查：${report.ready ? '通过' : '失败'}。\n`)
  io.stdout(`知识源：${report.sourceCount} 个。\n`)
  if (report.capabilities !== undefined) {
    if (report.capabilities.upload !== undefined) {
      io.stdout(`文件上传：最多 ${report.capabilities.upload.maxMb} MiB；支持 ${report.capabilities.upload.extensions.join('、')}。\n`)
    } else {
      io.stdout('文件上传：当前 SAG 未提供。\n')
    }
  }
}

function setupDiscovery(descriptor: SagConnectionDescriptor): DiscoveryResult {
  return { source: 'file', descriptor, diagnostics: [] }
}

function setupManager(runtime: CliRuntime, discover: (signal: AbortSignal) => Promise<DiscoveryResult>, requestTimeoutMs: number | undefined): SagConnectionManager {
  const deps: SagConnectionManagerDeps = {
    store: {
      load: async () => undefined,
    },
    discover,
    ...(runtime.inspect === undefined ? {} : { inspect: runtime.inspect }),
    ...(requestTimeoutMs === undefined ? {} : { requestTimeoutMs }),
  }
  return new SagConnectionManager(deps)
}

async function runSetup(
  command: ParsedCommand,
  runtime: CliRuntime,
  io: CliIo,
  dependencies: CliDependencies,
  rememberSecret: (secret: string) => void,
): Promise<number> {
  const controller = new AbortController()
  let discover: (signal: AbortSignal) => Promise<DiscoveryResult>
  if (command.path !== undefined) {
    const filename = expandHome(command.path, dependencies.homedir ?? homedir)
    let descriptor: SagConnectionDescriptor
    try {
      descriptor = parseConnectionDescriptor(JSON.parse(await (dependencies.readFile ?? readFile)(filename, 'utf8')))
    } catch (error) {
      io.stderr(`无法读取 SAG 导出文件：${redactedMessage(error)}\n`)
      return 1
    }
    discover = async () => setupDiscovery(descriptor)
  } else if (command.url !== undefined) {
    discover = signal => runtime.discovery.discover(signal, { paths: [], urls: [command.url!] })
  } else {
    discover = signal => runtime.discovery.discover(signal)
  }

  let report: SagConnectionReport
  try {
    report = await setupManager(runtime, discover, dependencies.requestTimeoutMs).ensureConnected(controller.signal)
  } catch (error) {
    io.stderr(`连接 SAG 失败：${redactedMessage(error)}\n`)
    return 1
  }
  if (report.status !== 'ready') {
    diagnostic(report, io)
    io.stderr(RECOVERY)
    return 1
  }
  if (report.descriptor === undefined) {
    io.stderr('SAG 连接缺少可保存的连接描述。\n')
    return 1
  }
  rememberSecret(report.descriptor.accessToken)
  try {
    await runtime.store.save(report.descriptor)
  } catch (error) {
    io.stderr(`保存 SAG 配置失败：${redactedMessage(error, [report.descriptor.accessToken])}\n`)
    return 1
  }
  diagnostic(report, io)
  return runDoctor(runtime, io, dependencies.requestTimeoutMs)
}

async function runDoctor(runtime: CliRuntime, io: CliIo, requestTimeoutMs?: number): Promise<number> {
  let report: SagConnectionReport
  try {
    report = await new SagConnectionManager({
      store: runtime.store,
      discover: signal => runtime.discovery.discover(signal),
      ...(runtime.inspect === undefined ? {} : { inspect: runtime.inspect }),
      ...(requestTimeoutMs === undefined ? {} : { requestTimeoutMs }),
    }).doctor(new AbortController().signal)
  } catch (error) {
    let secrets: readonly string[] = []
    try {
      const saved = await runtime.store.load()
      if (saved !== undefined) secrets = [saved.accessToken]
    } catch {
      // The original diagnostic remains the actionable failure.
    }
    io.stderr(`SAG 检查失败：${redactedMessage(error, secrets)}\n`)
    return 1
  }
  diagnostic(report, io)
  if (report.status === 'ready') return 0
  if (report.status === 'not-found') io.stderr(RECOVERY)
  return 1
}

/** Run setup, doctor, or help with no prompts and an explicit process exit code. */
export async function runCli(argv: readonly string[], io: CliIo, dependencies: CliDependencies = { createRuntime: createCliRuntime }): Promise<number> {
  const command = parse(argv, io)
  if (typeof command === 'number') return command
  if (command.kind === 'help') {
    io.stdout(HELP)
    return 0
  }

  let runtime: CliRuntime | undefined
  let code = 1
  let secrets: readonly string[] = []
  try {
    runtime = await dependencies.createRuntime()
    try {
      const saved = await runtime.store.load()
      if (saved !== undefined) secrets = [saved.accessToken]
    } catch {
      // The command reports saved configuration failures through its connection manager.
    }
    code = command.kind === 'setup'
      ? await runSetup(command, runtime, io, dependencies, secret => { secrets = [...secrets, secret] })
      : await runDoctor(runtime, io, dependencies.requestTimeoutMs)
  } catch (error) {
    io.stderr(`dsh-sag 失败：${redactedMessage(error, secrets)}\n`)
    code = 1
  } finally {
    if (runtime !== undefined) {
      try {
        await runtime.dispose()
      } catch (error) {
        try {
          const saved = await runtime.store.load()
          if (saved !== undefined) secrets = [...secrets, saved.accessToken]
        } catch {
          // Cleanup reporting must continue even when the backing store is unavailable.
        }
        io.stderr(`清理 dsh-sag 运行环境失败：${redactedMessage(error, secrets)}\n`)
        code = 1
      }
    }
  }
  return code
}

/** Detect direct execution after resolving npm/pnpm bin symlinks without firing when this module is imported. */
export function isDirectCliEntry(
  moduleUrl: string,
  argvEntry: string | undefined,
  resolveRealPath: (path: string) => string = realpathSync,
): boolean {
  if (argvEntry === undefined) return false
  try {
    return resolveRealPath(argvEntry) === fileURLToPath(moduleUrl)
  } catch {
    return false
  }
}

if (isDirectCliEntry(import.meta.url, process.argv[1])) {
  const code = await runCli(process.argv.slice(2), {
    stdout: line => process.stdout.write(line),
    stderr: line => process.stderr.write(line),
  })
  process.exitCode = code
}
