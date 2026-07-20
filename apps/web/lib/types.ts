import type { SearchStrategy } from "./retrieval-config";

export interface User {
  id: string;
  email: string;
  name: string;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export type SourceStatus = "active" | "paused" | "error";
export type SourceType = "document" | "web" | "message" | "audio";
export type DocumentParser = "auto" | "markitdown" | "mineru";
export type EffectiveDocumentParser = Exclude<DocumentParser, "auto">;
export interface Source {
  id: string;
  name: string;
  description: string;
  source_type: SourceType;
  connector_kind: string;
  status: SourceStatus;
  document_count: number;
  chunk_count: number;
  event_count: number;
  created_at: string;
  updated_at: string;
}

export interface Connector {
  kind: string;
  title: string;
  description: string;
  supports_sync: boolean;
  config_fields: Array<Record<string, unknown>>;
}

export type DocumentStatus =
  | "pending"
  | "loading"
  | "extracting"
  | "paused"
  | "ready"
  | "failed";

export interface Doc {
  id: string;
  source_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  status: DocumentStatus;
  chunk_count: number;
  event_count: number;
  progress: number;
  token_usage: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface CitationEventRef {
  id?: string | null;
  title: string;
  /** Extracted event body. This is the citation card copy. */
  content?: string | null;
  /** Retained for persisted data and non-visual consumers; never used as event body. */
  summary?: string | null;
  category?: string | null;
}

export interface Citation {
  n: number;
  /** Missing on legacy messages; legacy citations are internal knowledge references. */
  kind?: "internal" | "external";
  chunk_id: string | null;
  /** Source section heading; never treat this as an extracted event title. */
  heading: string;
  /** Explicit external-result summary. Legacy internal values are not event summaries. */
  summary?: string;
  /** Real extracted events associated with this knowledge chunk, ordered by relevance. */
  event_refs?: CitationEventRef[];
  /** Source chunk used to locate the original passage; not rendered as event copy. */
  snippet: string;
  score: number;
  source_id: string | null;
  source_name?: string | null;
  /** Present for external tool/web references only. */
  url?: string | null;
  title?: string | null;
  source?: string | null;
  /** Whether the answer placed this reference next to a specific claim. */
  mapped?: boolean;
  claim_level?: "claim" | "run";
}

export type BindingTargetType = "source" | "mcp_server";

export interface Persona {
  system_prompt?: string;
  greeting?: string;
  tools?: string[];
}

export interface ActivityItem {
  type: "document" | "thread";
  id: string;
  source_id?: string;
  title: string;
  subtitle: string | null;
  status: DocumentStatus | null;
  at: string;
}

export interface Agent {
  id: string;
  name: string;
  avatar: string;
  persona: Persona;
  is_default?: boolean;
  created_at: string;
  updated_at: string;
}

export interface Binding {
  id: string;
  target_type: BindingTargetType;
  target_id: string;
  config: Record<string, unknown>;
}

export type ModelProviderId = "openai" | "anthropic" | "gemini";

export interface ModelProviderSpec {
  id: ModelProviderId;
  display_name: string;
  protocol: string;
  default_model: string;
  default_base_url: string | null;
  default_context_window: number;
  default_temperature: number;
  temperature_configurable: boolean;
  can_reuse_embedding_credentials: boolean;
  api_key_placeholder: string;
}

export interface ModelConfig {
  llm_provider: ModelProviderId;
  llm_base_url: string | null;
  llm_model: string;
  llm_context_window: number;
  llm_temperature: number;
  llm_max_tokens: number;
  llm_timeout_ms: number;
  llm_max_retries: number;
  llm_api_key_set: boolean;
  embedding_model: string;
  embedding_base_url: string | null;
  embedding_dimensions: number | null;
  embedding_api_key_set: boolean;
  document_parser: DocumentParser;
  mineru_base_url: string | null;
  mineru_version: "2.0" | "2.5";
  mineru_api_key_set: boolean;
  effective_document_parser: EffectiveDocumentParser;
  document_extract_concurrency: number;
  document_chunk_max_tokens: number;
  document_chunk_mode: "standard" | "heading_strict";
  search_strategy: SearchStrategy;
  search_top_k: number;
  sag_language: "zh" | "en";
}

export type ModelConfigPatch = Partial<{
  llm_provider: ModelConfig["llm_provider"];
  llm_base_url: string | null;
  llm_api_key: string;
  llm_model: string;
  llm_context_window: number;
  llm_temperature: number;
  llm_max_tokens: number;
  llm_timeout_ms: number;
  llm_max_retries: number;
  embedding_model: string;
  embedding_base_url: string;
  embedding_api_key: string;
  embedding_dimensions: number | null;
  document_parser: DocumentParser;
  mineru_base_url: string | null;
  mineru_version: "2.0" | "2.5";
  mineru_api_key: string;
  document_extract_concurrency: number;
  document_chunk_max_tokens: number;
  document_chunk_mode: "standard" | "heading_strict";
  search_strategy: SearchStrategy;
  search_top_k: number;
  sag_language: "zh" | "en";
}>;

export interface ModelSetupStatus {
  required: boolean;
  environment_configured: boolean;
  database_configured: boolean;
}

export interface SystemPreferences {
  timezone: string;
}

export interface McpToolDetail {
  name: string;
  label: string;
  description: string;
}

export interface SourceMcpDescriptor {
  source_id: string;
  source_name: string;
  tools: string[];
  tool_details: McpToolDetail[];
  http: {
    transport: string;
    url: string;
    headers?: Record<string, string>;
    note: string;
  };
  stdio: { command: string; args: string[]; env: Record<string, string>; note: string };
}

export interface KnowledgeMcpDescriptor {
  name: string;
  scope: "knowledge_base";
  source_count: number;
  tools: string[];
  tool_details: McpToolDetail[];
  http: {
    transport: string;
    url: string;
    headers: Record<string, string>;
    note: string;
  };
  stdio: { command: string; args: string[]; env: Record<string, string>; note: string };
}

export interface Thread {
  id: string;
  agent_id: string;
  archived?: boolean;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface MessageStep {
  kind: "thinking" | "tool" | "answer";
  step: number;
  name?: string;
  label?: string;
  args?: string;
  arguments?: Record<string, unknown>;
  details?: {
    count?: number;
    scope?: "knowledge" | "internet";
    sources?: { id?: string; name?: string }[];
    matches?: {
      n?: number;
      chunk_id?: string | null;
      heading?: string;
      snippet?: string;
      score?: number;
      source_id?: string | null;
      source_name?: string;
    }[];
    output_preview?: string;
  };
  ms?: number;
  count?: number;
  error?: string;
}

export interface MessageAttachment {
  id: string;
  name?: string;
  media_type?: string;
}

export interface Message {
  id: string;
  thread_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  citations: Citation[];
  attachments?: MessageAttachment[];
  steps?: MessageStep[];
  prompt_preview?: string;
  created_at: string;
}

export interface MessagePage {
  items: Message[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface Entity {
  id: string;
  name: string;
  type: string;
  description: string;
  heat: number;
}

export interface SourceGraphDocument {
  id: string;
  filename: string;
  status: DocumentStatus;
  chunk_count: number;
  event_count: number;
  created_at: string;
}

export interface SourceGraphEvent {
  id: string;
  document_id: string | null;
  title: string;
  summary: string;
  category: string;
  rank: number;
  parent_id: string | null;
  chunk_id: string | null;
  start_time: string | null;
}

export type SourceGraphNodeKind = "document" | "event" | "entity";
export type SourceGraphRelationKind = "contains" | "subevent" | "mentions";

export interface SourceGraphRelation {
  source_id: string;
  source_kind: SourceGraphNodeKind;
  target_id: string;
  target_kind: SourceGraphNodeKind;
  kind: SourceGraphRelationKind;
  weight: number;
  description: string;
}

export interface SourceGraphResponse {
  documents: SourceGraphDocument[];
  events: SourceGraphEvent[];
  entities: Entity[];
  relations: SourceGraphRelation[];
  counts: {
    documents: number;
    events: number;
    entities: number;
    shown_documents: number;
    shown_events: number;
    shown_entities: number;
    shown_relations: number;
  };
  truncated: boolean;
}

export interface Section {
  chunk_id: string | null;
  heading: string;
  content: string;
  score: number;
  rank: number;
  source_id: string | null;
  source_name?: string | null;
}

export interface SearchEvent extends SourceGraphEvent {
  source_id: string | null;
  source_name?: string | null;
  score: number;
}

export interface SearchResponse {
  query: string;
  sections: Section[];
  events: SearchEvent[];
  entities: Entity[];
  relations: SourceGraphRelation[];
  source_hits: SearchSourceHit[];
  summary: string;
  exploration_id: string | null;
  stats: Record<string, unknown>;
}

export interface SearchSourceHit {
  source_id: string;
  source_name: string | null;
  event_hits: number;
  max_score: number;
  latest_event_time: string | null;
}

export type UniverseNodeKind = "event" | "entity";
export type UniverseActivationOrigin = "search" | "assistant" | "browse";

export interface UniversePartition {
  id: string;
  source_id: string;
  parent_id: string | null;
  kind: "source" | "topic";
  key: string;
  label: string;
  x: number;
  y: number;
  z: number;
  radius: number;
  node_count: number;
  event_count: number;
  entity_count: number;
  relation_count: number;
  density: number;
  time_buckets: Array<{ start: string; end: string; count: number }>;
  importance: number;
}

export interface UniverseManifest {
  version: string | null;
  status: "empty" | "building" | "ready" | "stale" | "failed";
  stale: boolean;
  as_of: string | null;
  bounds: {
    min_x?: number;
    min_y?: number;
    min_z?: number;
    max_x?: number;
    max_y?: number;
    max_z?: number;
  };
  partitions: UniversePartition[];
  counts: {
    sources?: number;
    partitions?: number;
    events?: number;
    entities?: number;
    nodes?: number;
    relations?: number;
  };
  policy: UniversePolicy;
}

export interface UniversePolicy {
  source_limit: number;
  timeline_event_page_size: number;
  event_entity_limit: number;
  lod_orbit_px: number;
  lod_near_px: number;
  lod_deep_px: number;
  lod_hysteresis_px: number;
  lod_debounce_ms: number;
  proxy_budget_desktop: number;
  proxy_budget_mobile: number;
  node_budget_desktop: number;
  node_budget_mobile: number;
  edge_budget_desktop: number;
  edge_budget_mobile: number;
}

export interface UniverseRelation {
  source_id: string;
  from_id: string;
  to_id: string;
  kind: "mentions" | "subevent";
  weight: number;
  description: string;
}

export interface UniverseEvidence {
  source_id: string;
  source_name: string;
  document_id: string | null;
  document_name: string | null;
  chunk_id: string | null;
  heading: string;
  content: string;
}

export interface UniverseNodeDetail {
  id: string;
  kind: UniverseNodeKind;
  source_id: string;
  source_name: string;
  label: string;
  description: string;
  category: string;
  start_time: string | null;
  evidence: UniverseEvidence | null;
}

export interface UniverseActivationNode {
  id: string;
  kind: UniverseNodeKind;
  source_id?: string | null;
  label: string;
  description?: string;
  category?: string;
  chunk_id?: string | null;
  start_time?: string | null;
  importance?: number;
  related_count?: number;
  citation_numbers?: number[];
  state?: "latent" | "active";
}

export interface UniverseActivation {
  epoch?: number;
  origin?: UniverseActivationOrigin;
  query: string;
  nodes: UniverseActivationNode[];
  relations: UniverseRelation[];
  source_hits?: SearchSourceHit[];
}

export interface UniversePatchNode {
  id: string;
  kind: UniverseNodeKind;
  source_id: string;
  label: string;
  description: string;
  category: string;
  chunk_id: string | null;
  start_time: string | null;
  importance: number;
  related_count: number;
  state: "latent" | "active";
}

export interface UniverseGraphPatch {
  schema_version: 2;
  epoch: number;
  source_id: string;
  source_revision: string;
  snapshot_id: string;
  request_cursor: string | null;
  page_id: string;
  bundle_id: string;
  anchor: UniversePatchNode;
  nodes: UniversePatchNode[];
  relations: UniverseRelation[];
  page: {
    returned: number;
    has_more: boolean;
    next_cursor: string | null;
  };
  as_of: string;
}

export interface UniverseTimelineEventNode extends UniversePatchNode {
  kind: "event";
}

export interface UniverseTimelineEntityNode extends UniversePatchNode {
  kind: "entity";
}

export interface UniverseTimelineRelation extends UniverseRelation {
  kind: "mentions";
}

export type UniverseTimelineDirection = "older" | "newer";

export interface UniverseTimelineSlice {
  schema_version: 3;
  epoch: number;
  source_id: string;
  source_revision: string;
  snapshot_id: string;
  request_direction: UniverseTimelineDirection;
  request_cursor: string | null;
  page_id: string;
  bundles: Array<{
    bundle_id: string;
    /** Snapshot-stable position in the source's exploration order; 0 = newest. */
    ordinal: number;
    event: UniverseTimelineEventNode;
    nodes: UniverseTimelineEntityNode[];
    relations: UniverseTimelineRelation[];
    neighbor_page: {
      total_unique: number;
      returned_unique: number;
      complete: boolean;
      next_cursor: string | null;
    };
    cursor_before: string | null;
    cursor_after: string | null;
  }>;
  /** Snapshot-stable event total: the counting axis' length for this source. */
  total_events: number;
  page: {
    returned_bundles: number;
    returned_unique_nodes: number;
    returned_relations: number;
    direction: UniverseTimelineDirection;
    has_newer: boolean;
    newer_cursor: string | null;
    has_older: boolean;
    older_cursor: string | null;
    has_more: boolean;
    next_cursor: string | null;
  };
  as_of: string;
}

export interface BackgroundJob {
  id: string;
  type: string;
  status: "queued" | "running" | "succeeded" | "failed";
  source_id: string | null;
  document_id: string | null;
  progress: number;
  attempts: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ExplorationSession {
  id: string;
  title: string;
  source_ids: string[];
  created_at: string;
  updated_at: string;
  step_count: number;
}

export interface ExplorationStep {
  id: string;
  session_id: string;
  query: string;
  summary: string;
  source_ids: string[];
  event_refs: SearchEvent[];
  entity_refs: Entity[];
  relation_refs: SourceGraphRelation[];
  evidence_refs: Array<Record<string, unknown>>;
  camera: Record<string, unknown>;
  created_at: string;
}

export interface ExplorationDetail {
  session: ExplorationSession;
  steps: ExplorationStep[];
}

export interface Capabilities {
  llm_configured: boolean;
  llm_provider: ModelProviderId;
  llm_model: string;
  context_window?: number;
  embedding_model: string;
  vector_provider: string;
  language: string;
  search_strategy: SearchStrategy;
  document_parser: DocumentParser;
  effective_document_parser: EffectiveDocumentParser;
  mineru_configured: boolean;
  max_upload_mb: number;
  allowed_upload_exts?: string[];
  timezone: string;
}
