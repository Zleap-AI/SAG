import type { ToolCallView } from '@deepseek-ai/dsh-tools/presentation'

export function presentSearchCall(args: { readonly query: string }): ToolCallView {
  return { card: 'generic', title: 'Search SAG knowledge', kind: 'search', rawInput: args.query }
}

export function presentReadCall(args: { readonly evidence_ref: string }): ToolCallView {
  return { card: 'generic', title: 'Read SAG evidence', kind: 'read', rawInput: args.evidence_ref }
}
