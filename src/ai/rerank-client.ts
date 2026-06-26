import { config } from "../config/env.js";
import { aiSettingsService, type AiRuntimeSettings } from "../services/ai-settings-service.js";
import { createModelCallLogger } from "../observability/model-call-log.js";
import type { EventRecord } from "../types.js";

export interface RerankClient {
  rerankEvents(input: {
    query: string;
    candidates: EventRecord[];
    topK: number;
  }): Promise<string[]>;
}

export class QwenRerankClient implements RerankClient {
  async rerankEvents(input: {
    query: string;
    candidates: EventRecord[];
    topK: number;
  }): Promise<string[]> {
    if (input.candidates.length === 0 || input.topK <= 0) {
      return [];
    }
    const settings = await aiSettingsService.getRuntimeSettings();
    if (!settings.hasRemoteLlm) {
      const ids = localScoreRerank(input.query, input.candidates, input.topK);
      const log = createModelCallLogger({
        kind: "llm",
        operation: "rerankEvents.local",
        request: {
          model: "local-lexical-rerank",
          query: input.query,
          topK: input.topK,
          candidateCount: input.candidates.length
        }
      });
      log.succeed({ useful_event_ids: ids });
      return ids;
    }

    return this.remoteRerank(settings, input);
  }

  private async remoteRerank(settings: AiRuntimeSettings, input: {
    query: string;
    candidates: EventRecord[];
    topK: number;
  }): Promise<string[]> {
    const url = buildRerankUrl(config.RERANK_BASE_URL ?? settings.llmBaseUrl);
    const documents = input.candidates.map((candidate) => eventToRerankDocument(candidate, input.candidates.length));
    const body = {
      model: config.RERANK_MODEL,
      documents,
      query: input.query,
      top_n: Math.min(input.topK, input.candidates.length),
      instruct: config.RERANK_INSTRUCT
    };

    let lastError: unknown;
    const maxAttempts = settings.llmMaxRetries + 1;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), settings.llmTimeoutMs);
      const log = createModelCallLogger({
        kind: "llm",
        operation: "rerankEvents.qwen3Rerank",
        request: {
          url,
          method: "POST",
          attempt,
          maxAttempts,
          headers: {
            "Content-Type": "application/json"
          },
          body: {
            ...body,
            documents: documents.map((document) => previewText(document, 600))
          }
        }
      });
      let logged = false;
      try {
        const response = await fetch(url, {
          method: "POST",
          signal: controller.signal,
          headers: {
            Authorization: `Bearer ${settings.llmApiKey}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify(body)
        });
        const { responseText, responseBody } = await readResponseBody(response);
        if (!response.ok) {
          const error = new Error(`rerank request failed: ${response.status} ${responseText.slice(0, 500)}`);
          log.fail(error, {
            status: response.status,
            body: responseBody
          });
          logged = true;
          lastError = error;
          if (attempt < maxAttempts && isRetryableHttpStatus(response.status)) {
            await waitBeforeRetry(attempt);
            continue;
          }
          throw error;
        }

        const ids = parseRerankResponse(responseBody, input.candidates, input.topK);
        log.succeed({
          status: response.status,
          body: responseBody,
          useful_event_ids: ids
        });
        return ids.length > 0 ? ids : localScoreRerank(input.query, input.candidates, input.topK);
      } catch (error) {
        lastError = error;
        if (!logged) {
          log.fail(error);
        }
        if (attempt < maxAttempts && isRetryableFetchError(error)) {
          await waitBeforeRetry(attempt);
          continue;
        }
        throw error;
      } finally {
        clearTimeout(timeout);
      }
    }
    throw lastError instanceof Error ? lastError : new Error(String(lastError));
  }
}

export function buildRerankUrl(baseUrl: string): string {
  const base = baseUrl.replace(/\/$/, "");
  if (base.endsWith("/v1")) {
    return `${base}/reranks`;
  }
  if (base.endsWith("/rerank") || base.endsWith("/reranks")) {
    return base;
  }
  return `${base}/v1/reranks`;
}

function eventToRerankDocument(event: EventRecord, candidateCount: number): string {
  const maxChars = Math.max(500, Math.min(2400, Math.floor(28000 / Math.max(candidateCount, 1))));
  return previewText([
    `标题：${event.title}`,
    event.summary ? `摘要：${event.summary}` : "",
    event.content ? `内容：${event.content}` : ""
  ].filter(Boolean).join("\n"), maxChars);
}

function parseRerankResponse(responseBody: unknown, candidates: EventRecord[], topK: number): string[] {
  const records = extractRerankRecords(responseBody);
  if (records.length === 0) {
    return [];
  }
  return records
    .map((record, position) => ({
      index: readIndex(record),
      score: readScore(record, position)
    }))
    .filter((record): record is { index: number; score: number } => (
      Number.isInteger(record.index)
      && record.index >= 0
      && record.index < candidates.length
      && Number.isFinite(record.score)
    ))
    .sort((a, b) => b.score - a.score)
    .slice(0, topK)
    .map((record) => candidates[record.index]?.id)
    .filter((id): id is string => Boolean(id));
}

function extractRerankRecords(value: unknown): unknown[] {
  if (!isRecord(value)) {
    return [];
  }
  for (const key of ["results", "data", "documents"]) {
    const records = value[key];
    if (Array.isArray(records)) {
      return records;
    }
  }
  return [];
}

function readIndex(value: unknown): number {
  if (!isRecord(value)) {
    return Number.NaN;
  }
  const raw = value.index ?? value.document_index ?? value.documentIndex;
  return typeof raw === "number" ? raw : Number(raw);
}

function readScore(value: unknown, position: number): number {
  if (!isRecord(value)) {
    return Number.NaN;
  }
  const raw = value.relevance_score ?? value.relevanceScore ?? value.score;
  const score = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(score) ? score : -position;
}

function localScoreRerank(query: string, candidates: EventRecord[], topK: number): string[] {
  const terms = query.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter((term) => term.length > 1);
  return [...candidates]
    .map((candidate) => {
      const text = `${candidate.title} ${candidate.summary} ${candidate.content}`.toLowerCase();
      const lexicalScore = terms.reduce((score, term) => score + (text.includes(term) ? 1 : 0), 0);
      return {
        id: candidate.id,
        score: lexicalScore + (candidate.score ?? 0)
      };
    })
    .sort((a, b) => b.score - a.score)
    .slice(0, topK)
    .map((candidate) => candidate.id);
}

function parseJsonOrText(text: string): unknown {
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function readResponseBody(response: Response): Promise<{ responseText: string; responseBody: unknown }> {
  const maybeText = (response as Response & { text?: () => Promise<string> }).text;
  if (typeof maybeText === "function") {
    const responseText = await maybeText.call(response);
    return {
      responseText,
      responseBody: parseJsonOrText(responseText)
    };
  }
  const responseBody = await (response as Response & { json: () => Promise<unknown> }).json();
  return {
    responseText: typeof responseBody === "string" ? responseBody : JSON.stringify(responseBody),
    responseBody
  };
}

function isRetryableHttpStatus(status: number): boolean {
  return status === 408 || status === 429 || status >= 500;
}

function isRetryableFetchError(error: unknown): boolean {
  if (!(error instanceof Error)) {
    return false;
  }
  return error.name === "AbortError" || error.message.includes("fetch failed");
}

async function waitBeforeRetry(attempt: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * attempt, 3000)));
}

function previewText(text: string, limit: number): string {
  const cleaned = text.replace(/\s+/g, " ").trim();
  return cleaned.length > limit ? `${cleaned.slice(0, limit - 1)}…` : cleaned;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export const rerankClient = new QwenRerankClient();
