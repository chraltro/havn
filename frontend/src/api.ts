// ---- Type definitions ----

export interface AuthStatus {
  auth_enabled: boolean;
  needs_setup?: boolean;
}

export interface UserInfo {
  username: string;
  role: string;
  display_name?: string;
}

export interface LoginResult {
  token: string;
  username: string;
  role: string;
}

export interface TableInfo {
  schema: string;
  name: string;
  type: string;
  column_count?: number;
  row_count?: number;
}

export interface TableDescription {
  schema: string;
  name: string;
  columns: ColumnInfo[];
  row_count?: number;
}

export interface ColumnInfo {
  name: string;
  type: string;
  nullable?: boolean;
}

export interface QueryResult {
  columns: string[];
  rows: unknown[][];
  row_count?: number;
  truncated?: boolean;
}

export interface TransformResult {
  results: Record<string, string>;
}

export interface ScriptResult {
  status: string;
  duration_ms: number;
  log_output?: string;
  error?: string;
  rows_affected?: number;
}

export interface StreamStep {
  action: string;
  results?: Record<string, string> | ScriptResult[];
}

export interface StreamResult {
  steps: StreamStep[];
  duration_seconds?: number;
}

export interface LintViolation {
  file: string;
  line: number;
  col: number;
  code: string;
  description: string;
  fixable?: boolean;
}

export interface LintResult {
  violations: LintViolation[];
  count: number;
  fixed?: number;
  content?: string;
}

// ---- Dashboard types ----

export interface Dashboard {
  id: string;
  name: string;
  description: string;
  created_by: string;
  updated_by?: string;
  created_at: string;
  updated_at: string;
  is_template: boolean;
  widget_count: number;
}

export interface DashboardWidget {
  id: string;
  widget_type: string;
  chart_type: string | null;
  title: string;
  sql_query: string | null;
  config: Record<string, unknown>;
  position: { x: number; y: number; w: number; h: number };
  filters: unknown[];
  cache_ttl: number;
  sort_order: number;
  created_at: string;
}

export interface DashboardFull extends Dashboard {
  layout: Record<string, unknown>;
  filters: unknown[];
  settings: Record<string, unknown>;
  widgets: DashboardWidget[];
}

export interface DashboardExport {
  dashboard: Record<string, unknown>;
  widgets: Record<string, unknown>[];
  version: number;
}

export interface WidgetQueryResult {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  error?: string;
}

export interface FileEntry {
  path: string;
  type: string;
  children?: FileEntry[];
}

export interface FileContent {
  content: string;
  language: string;
}

export interface OutputEntry {
  type: "info" | "error" | "warn" | "log" | "success";
  message: string;
  ts: string;
}

export interface ModelResult {
  name: string;
  result: string;
}

export interface RunSummary {
  type: "transform" | "stream" | "lint" | "script" | "contracts";
  status: "success" | "failed";
  models: ModelResult[];
  totalRows: number;
  duration: number;
  errors: number;
  pipelineRunId?: string | null;
}

export interface PipelineRun {
  pipeline_run_id: string;
  run_type: string;
  target: string;
  started_at: string | null;
  status: string;
  total_duration_ms: number;
  model_count: number;
  success_count: number;
  error_count: number;
  skipped_count: number;
  total_rows: number;
}

export interface RunComparison {
  models: RunComparisonModel[];
  previous_run_id: string | null;
}

export interface RunComparisonModel {
  target: string;
  duration_ms: number | null;
  rows_affected: number | null;
  status: string;
  prev_duration_ms: number | null;
  prev_rows_affected: number | null;
  prev_status: string | null;
  is_new: boolean;
}

export interface SecretEntry {
  key: string;
  is_set: boolean;
  masked_value: string;
}

export interface UserEntry {
  username: string;
  role: string;
  display_name?: string;
  created_at?: string;
  last_login?: string;
}

export interface StreamConfig {
  description?: string;
  schedule?: string;
  steps: { ingest?: string[]; transform?: string[]; export?: string[] }[];
}

// ---- Orchestration job types ----

export interface OrchestrationJob {
  name: string;
  target: string;            // legacy single-target (= targets[0])
  targets: string[];         // preferred multi-target list
  resolve: string;
  cron: string;              // legacy single-schedule (= schedules[0])
  schedules: string[];       // preferred multi-schedule list
  tags: string[];
  enabled: boolean;
  notify: string[];
  retry: number;
  retry_delay: number;
  timeout_minutes: number;
  description: string;
  file: string;
  last_run: {
    status: string;
    started_at: string | null;
    duration_ms: number | null;
    steps_completed?: number;
    steps_total?: number;
    steps_skipped?: number;
    steps_failed?: number;
  } | null;
  next_run: string | null;
  sparkline: Array<{
    status: string;
    duration_ms: number | null;
    started_at: string | null;
  }>;
}

export interface OrchestrationDagNode {
  id: string;
  kind: "ingest" | "transform" | "export";
  label: string;
  schema: string;
  path?: string | null;
  materialized?: string;
  depends_on?: string[];
  row_count?: number | null;
  last_run_at?: string | null;
  duration_ms?: number | null;
}

export interface OrchestrationDag {
  nodes: OrchestrationDagNode[];
  edges: { source: string; target: string; kind: string }[];
  schemas: string[];
}

export interface JobStepDetail {
  step: number;
  type: string;
  target: string;
  status: string;
  duration_ms?: number;
  rows_affected?: number;
  error?: string;
}

export interface JobRun {
  id: string;
  job_name: string;
  status: string;
  steps_total: number;
  steps_completed: number;
  steps_failed: number;
  steps_skipped: number;
  duration_ms: number | null;
  trigger: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  step_details: JobStepDetail[];
}

export interface JobPlanStep {
  step: number;
  type: string;
  target: string;
  estimated_duration_ms: number;
}

export interface JobPlan {
  steps: JobPlanStep[];
  total_steps: number;
  total_estimated_ms: number;
  ingest_count: number;
  transform_count: number;
  export_count: number;
}

// ---- Pull request types ----

export interface PRComment {
  id: string;
  author: string;
  body: string;
  created_at: string;
  comment_type: string;
  file: string | null;
  line: number | null;
}

export interface PullRequest {
  id: string;
  title: string;
  description: string;
  base_ref: string;
  head_ref: string;
  author: string;
  status: string;
  created_at: string;
  updated_at: string;
  comments: PRComment[];
  approvers: string[];
  change_requesters: string[];
  require_approval: boolean;
  merged_by: string | null;
  merged_at: string | null;
  closed_by: string | null;
  closed_at: string | null;
}

export interface PrBuildDiffEntry {
  status: string;
  main_rows: number;
  pr_rows: number;
  added_rows: number;
  removed_rows: number;
  schema_changes: Array<{
    type: string;
    column: string;
    from?: string;
    to?: string;
    data_type?: string;
  }>;
}

export interface PrBuild {
  id: string;
  pr_id: string;
  branch_head: string | null;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  data_diff: Record<string, PrBuildDiffEntry> | null;
  lineage_impact: { changed: string[]; impacted: string[] } | null;
  contract_results: Array<Record<string, unknown>> | null;
  error: string | null;
}

export interface PrStateStatus {
  is_git_repo: boolean;
  dirty: boolean;
  unpushed_count: number;
}

// ---- API client ----

const BASE = "/api";
const REQUEST_TIMEOUT_MS = 30000;
const MAX_RETRIES = 3;
const RETRY_BACKOFF = [1000, 2000, 4000];

let authToken: string | null = localStorage.getItem("havn_token") || null;

/** Parse an error response into a human-readable message */
async function parseErrorResponse(res: Response, path: string): Promise<string> {
  const status = res.status;
  let text = "";
  try {
    text = await res.text();
  } catch {
    // ignore read errors
  }

  // Try JSON first
  if (text) {
    try {
      const json = JSON.parse(text);
      // FastAPI/Pydantic validation errors (422)
      if (status === 422 && json.detail) {
        if (Array.isArray(json.detail)) {
          const msgs = json.detail.map((d: { loc?: string[]; msg?: string }) => {
            const loc = d.loc ? d.loc.join(" > ") : "";
            return loc ? `${loc}: ${d.msg}` : d.msg || "Validation error";
          });
          return `Validation error (422): ${msgs.join("; ")}`;
        }
        return `Validation error (422): ${typeof json.detail === "string" ? json.detail : JSON.stringify(json.detail)}`;
      }
      // Standard error/detail fields
      if (json.error) return `Error (${status}): ${json.error}`;
      if (json.detail) return `Error (${status}): ${typeof json.detail === "string" ? json.detail : JSON.stringify(json.detail)}`;
      if (json.message) return `Error (${status}): ${json.message}`;
    } catch {
      // Not JSON — check if HTML
    }
  }

  // HTML response — don't show raw HTML to users
  if (text && (text.includes("<html") || text.includes("<!DOCTYPE") || text.includes("<!doctype"))) {
    if (status >= 500) return `Server error (${status}). The server may be restarting.`;
    return `Server error (${status})`;
  }

  // Specific status code messages
  if (status === 404) return `Not found: ${path}`;
  if (status === 429) return "Rate limited. Please wait and try again.";
  if (status >= 500) return `Server error (${status}). The server may be restarting.`;

  // Fallback
  return text ? `Error (${status}): ${text}` : `Error (${status}): ${res.statusText}`;
}

/** Determine if a request is safe to retry (GET, or explicitly marked retryable) */
function isRetryable(method: string | undefined, retryable: boolean | undefined): boolean {
  if (retryable !== undefined) return retryable;
  const m = (method || "GET").toUpperCase();
  return m === "GET" || m === "HEAD" || m === "OPTIONS";
}

interface RequestOptions extends RequestInit {
  /** Whether this request can be retried on transient failure. Default: true for GET, false for POST/PUT/DELETE. */
  retryable?: boolean;
}

/** Endpoints that need a longer timeout (e.g. diff can scan many models). */
const LONG_TIMEOUT_PATHS = ["/diff", "/transform", "/stream/", "/query", "/contracts", "/docs/"];

function getTimeoutForPath(path: string): number {
  if (LONG_TIMEOUT_PATHS.some((p) => path.startsWith(p) || path === p)) {
    return 300000; // 5 minutes
  }
  return REQUEST_TIMEOUT_MS;
}

async function request<T = unknown>(path: string, options: RequestOptions = {}): Promise<T> {
  const { retryable, ...fetchOptions } = options;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(fetchOptions.headers as Record<string, string> || {}),
  };
  if (authToken) {
    headers["Authorization"] = `Bearer ${authToken}`;
  }

  const canRetry = isRetryable(fetchOptions.method, retryable);
  let lastError: Error | null = null;
  const timeoutMs = getTimeoutForPath(path);

  for (let attempt = 0; attempt <= (canRetry ? MAX_RETRIES : 0); attempt++) {
    // Wait before retry (skip first attempt)
    if (attempt > 0) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_BACKOFF[attempt - 1] || 4000));
    }

    // Set up timeout via AbortController
    const timeoutController = new AbortController();
    const existingSignal = fetchOptions.signal;
    const timeoutId = setTimeout(() => timeoutController.abort(), timeoutMs);

    try {
      // Combine existing signal (if any) with our timeout signal
      let signal = timeoutController.signal;
      if (existingSignal) {
        const combined = new AbortController();
        existingSignal.addEventListener("abort", () => combined.abort());
        timeoutController.signal.addEventListener("abort", () => combined.abort());
        signal = combined.signal;
      }

      const res = await fetch(`${BASE}${path}`, { ...fetchOptions, headers, signal });
      clearTimeout(timeoutId);

      // 401 — auth required, don't retry
      if (res.status === 401) {
        authToken = null;
        localStorage.removeItem("havn_token");
        window.dispatchEvent(new Event("havn_auth_required"));
        throw new Error("Authentication required");
      }

      // 4xx — client error, don't retry
      if (res.status >= 400 && res.status < 500) {
        const msg = await parseErrorResponse(res, path);
        throw new Error(msg);
      }

      // 5xx — server error, retry if allowed
      if (res.status >= 500) {
        const msg = await parseErrorResponse(res, path);
        lastError = new Error(msg);
        if (canRetry && attempt < MAX_RETRIES) continue;
        throw lastError;
      }

      // Success
      return res.json() as Promise<T>;
    } catch (err: unknown) {
      clearTimeout(timeoutId);
      const error = err as Error;

      // Don't retry auth errors or client errors
      if (error.message === "Authentication required" ||
          (error.message && error.message.startsWith("Validation error")) ||
          (error.message && error.message.startsWith("Not found:")) ||
          (error.message && error.message.startsWith("Rate limited"))) {
        throw error;
      }

      // Timeout
      if (error.name === "AbortError") {
        lastError = new Error(`Request timed out after ${Math.round(timeoutMs / 1000)} seconds`);
        if (canRetry && attempt < MAX_RETRIES) continue;
        throw lastError;
      }

      // Network error (TypeError from fetch) — retry if allowed
      if (error instanceof TypeError) {
        lastError = new Error(`Network error: Unable to reach the server. Check that havn is running.`);
        if (canRetry && attempt < MAX_RETRIES) continue;
        throw lastError;
      }

      // Other errors — don't retry
      throw error;
    }
  }

  // Should not reach here, but just in case
  throw lastError || new Error("Request failed");
}

export const api = {
  // Auth
  setToken: (token: string | null) => {
    authToken = token;
    if (token) localStorage.setItem("havn_token", token);
    else localStorage.removeItem("havn_token");
  },
  getToken: () => authToken,
  getAuthStatus: () => request<AuthStatus>("/auth/status"),
  login: (username: string, password: string) =>
    request<LoginResult>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  setup: (username: string, password: string, display_name?: string) =>
    request<LoginResult>("/auth/setup", {
      method: "POST",
      body: JSON.stringify({ username, password, display_name }),
    }),
  getMe: () => request<UserInfo>("/auth/me"),

  // Users
  listUsers: () => request<UserEntry[]>("/users"),
  createUser: (username: string, password: string, role: string, display_name?: string) =>
    request("/users", {
      method: "POST",
      body: JSON.stringify({ username, password, role, display_name }),
    }),
  updateUser: (username: string, data: Partial<{ role: string; password: string; display_name: string }>) =>
    request(`/users/${username}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteUser: (username: string) => request(`/users/${username}`, { method: "DELETE" }),

  // Secrets
  listSecrets: () => request<SecretEntry[]>("/secrets"),
  setSecret: (key: string, value: string) =>
    request("/secrets", { method: "POST", body: JSON.stringify({ key, value }) }),
  deleteSecret: (key: string) => request(`/secrets/${key}`, { method: "DELETE" }),

  // Files
  listFiles: () => request<FileEntry[]>("/files"),
  readFile: (path: string) => request<FileContent>(`/files/${path}`),
  saveFile: (path: string, content: string) =>
    request(`/files/${path}`, { method: "PUT", body: JSON.stringify({ content }) }),
  deleteFile: (path: string, dropObject: boolean = false) =>
    request(`/files/${path}${dropObject ? "?drop_object=true" : ""}`, { method: "DELETE" }),
  moveFile: (source: string, destination: string) =>
    request(`/files/${source}/move`, { method: "POST", body: JSON.stringify({ destination }) }),

  // Models
  listModels: () => request("/models"),

  // Transform
  runTransform: (targets: string[] | null = null, force: boolean = false) =>
    request<TransformResult>("/transform", {
      method: "POST",
      body: JSON.stringify({ targets, force }),
    }),

  // Run script
  runScript: (scriptPath: string) =>
    request<ScriptResult>("/run", {
      method: "POST",
      body: JSON.stringify({ script_path: scriptPath }),
    }),

  // Streams
  listStreams: () => request<Record<string, StreamConfig>>("/streams"),
  runStream: (name: string, force: boolean = false) =>
    request<StreamResult>(`/stream/${name}?force=${force}`, { method: "POST" }),
  cancelStream: () => request("/stream/cancel", { method: "POST" }),
  getActiveStream: () => request<{ running: boolean; operation?: string | null; operation_label?: string | null; stream_name?: string | null; started_at?: number | null; total_events: number; finished: boolean; status?: string | null; duration_seconds?: number | null }>("/stream/active"),
  startStream: (name: string, force: boolean = false) =>
    request<{ status: string; stream_name: string }>(`/stream/${name}/start?force=${force}`, { method: "POST" }),
  startLint: (fix: boolean = false) =>
    request<{ status: string; operation: string }>(`/lint/start?fix=${fix}`, { method: "POST" }),
  startContracts: () =>
    request<{ status: string; operation: string }>("/contracts/run/start", { method: "POST" }),
  startScript: (scriptPath: string) =>
    request<{ status: string; operation: string }>("/run/start", {
      method: "POST",
      body: JSON.stringify({ script_path: scriptPath }),
    }),
  startTransform: (targets: string[] | null = null, force: boolean = false) =>
    request<{ status: string; operation: string }>("/transform/start", {
      method: "POST",
      body: JSON.stringify({ targets, force }),
    }),
  startPipeline: (steps: string[] = ["ingest", "transform", "export"], force: boolean = false) =>
    request<{ status: string; operation: string }>("/pipeline/start", {
      method: "POST",
      body: JSON.stringify({ steps, force }),
    }),
  connectToStreamEvents: (
    fromEvent: number,
    onEvent: (event: string, data: Record<string, unknown>) => void,
  ): { abort: () => void; done: Promise<void> } => {
    const controller = new AbortController();
    const url = `${BASE}/stream/events?from_event=${fromEvent}`;
    const headers: Record<string, string> = {};
    if (authToken) headers["Authorization"] = `Bearer ${authToken}`;

    const done = fetch(url, { signal: controller.signal, headers })
      .then(async (res) => {
        if (!res.ok) {
          const text = await res.text();
          onEvent("error", { message: text || res.statusText });
          return;
        }
        const reader = res.body?.getReader();
        if (!reader) return;
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";
          let currentEvent = "";
          for (const line of lines) {
            if (line.startsWith("event: ")) {
              currentEvent = line.slice(7).trim();
            } else if (line.startsWith("data: ") && currentEvent) {
              try {
                const data = JSON.parse(line.slice(6));
                onEvent(currentEvent, data);
              } catch { /* skip malformed */ }
              currentEvent = "";
            }
          }
        }
      })
      .catch((err) => {
        if (err.name !== "AbortError") {
          onEvent("error", { message: String(err) });
        }
      });

    return { abort: () => controller.abort(), done };
  },

  // Query
  runQuery: (sql: string) =>
    request<QueryResult>("/query", { method: "POST", body: JSON.stringify({ sql }) }),
  explainQuery: (sql: string) =>
    request<{ plan: string }>("/query/explain", { method: "POST", body: JSON.stringify({ sql }) }),
  profileQuery: (sql: string) =>
    request<{ plan: string }>("/query/profile", { method: "POST", body: JSON.stringify({ sql }) }),
  exportCsv: async (sql: string) => {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
    const res = await fetch("/api/query/export-csv", {
      method: "POST",
      headers,
      body: JSON.stringify({ sql }),
    });
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "export.csv";
    a.click();
    URL.revokeObjectURL(url);
  },
  formatSql: (sql: string) =>
    request<{ formatted: string }>("/format-sql", { method: "POST", body: JSON.stringify({ sql }) }),
  getSlowQueries: (limit: number = 50) =>
    request("/metrics/slow-queries?limit=" + limit),

  // Tables
  listTables: (schema: string | null = null) =>
    request<TableInfo[]>(`/tables${schema ? `?schema=${schema}` : ""}`),
  describeTable: (schema: string, table: string) =>
    request<TableDescription>(`/tables/${schema}/${table}`),
  sampleTable: (schema: string, table: string, limit: number = 100) =>
    request<QueryResult>(`/tables/${schema}/${table}/sample?limit=${limit}`),

  // History
  getHistory: (limit: number = 50) => request(`/history?limit=${limit}`),
  getPipelineRuns: (limit: number = 50) => request<PipelineRun[]>(`/history/runs?limit=${limit}`),
  getPipelineRunDetail: (pipelineRunId: string) => request(`/history/runs/${pipelineRunId}`),
  getRunComparison: (pipelineRunId: string) => request<RunComparison>(`/history/runs/${pipelineRunId}/comparison`),

  // Lint
  runLint: (fix: boolean = false) =>
    request<LintResult>(`/lint?fix=${fix}`, { method: "POST" }),
  lintFile: (path: string, fix: boolean = false, content: string | null = null) =>
    request<LintResult>("/lint/file", { method: "POST", body: JSON.stringify({ path, fix, content }) }),
  getLintConfig: () => request("/lint/config"),
  saveLintConfig: (content: string) =>
    request("/lint/config", { method: "PUT", body: JSON.stringify({ content }) }),
  deleteLintConfig: () =>
    request("/lint/config", { method: "DELETE" }),

  // DAG
  getDAG: () => request("/dag"),

  // Docs
  getDocs: () => request("/docs/markdown"),
  getStructuredDocs: () => request("/docs/structured"),

  // Overview
  getOverview: () => request("/overview"),
  clearSampleProject: () => request("/project/clear-sample", { method: "POST" }),

  // Connector health
  getConnectorHealth: () => request("/connectors/health"),

  // Scheduler
  getScheduler: () => request("/scheduler"),

  // Notebooks
  listNotebooks: () => request("/notebooks"),
  getNotebook: (name: string) => request(`/notebooks/open/${name}`),
  saveNotebook: (name: string, notebook: unknown) =>
    request(`/notebooks/save/${name}`, {
      method: "POST",
      body: JSON.stringify({ notebook }),
    }),
  createNotebook: (name: string, title: string = "") =>
    request(`/notebooks/create/${name}?title=${encodeURIComponent(title)}`, {
      method: "POST",
    }),
  runNotebook: (name: string) => request(`/notebooks/run/${name}`, { method: "POST" }),
  runCell: (name: string, source: string, { reset = false, cell_type = "code" }: { reset?: boolean; cell_type?: string } = {}) =>
    request(`/notebooks/run-cell/${name}`, {
      method: "POST",
      body: JSON.stringify({ source, cell_type, reset }),
    }),

  // Import
  previewFile: (file_path: string, target_schema: string, target_table: string) =>
    request("/import/preview-file", {
      method: "POST",
      body: JSON.stringify({ file_path, target_schema, target_table }),
    }),
  importFile: (file_path: string, target_schema: string, target_table: string) =>
    request("/import/file", {
      method: "POST",
      body: JSON.stringify({ file_path, target_schema, target_table }),
    }),
  testConnection: (connection_type: string, params: Record<string, unknown>) =>
    request("/import/test-connection", {
      method: "POST",
      body: JSON.stringify({ connection_type, params }),
    }),
  importFromConnection: (connection_type: string, params: Record<string, unknown>, source_table: string, target_schema: string, target_table: string) =>
    request("/import/from-connection", {
      method: "POST",
      body: JSON.stringify({ connection_type, params, source_table, target_schema, target_table }),
    }),

  // Connectors
  listAvailableConnectors: () => request("/connectors/available"),
  listConfiguredConnectors: () => request("/connectors"),
  testConnector: (connector_type: string, config: Record<string, unknown>) =>
    request("/connectors/test", {
      method: "POST",
      body: JSON.stringify({ connector_type, config }),
    }),
  discoverConnector: (connector_type: string, config: Record<string, unknown>) =>
    request("/connectors/discover", {
      method: "POST",
      body: JSON.stringify({ connector_type, config }),
    }),
  setupConnector: (connector_type: string, connection_name: string, config: Record<string, unknown>, tables: string[], target_schema: string, schedule?: string) =>
    request("/connectors/setup", {
      method: "POST",
      body: JSON.stringify({ connector_type, connection_name, config, tables, target_schema, schedule }),
    }),
  regenerateConnector: (connection_name: string, config?: Record<string, unknown>) =>
    request(`/connectors/regenerate/${connection_name}`, {
      method: "POST",
      body: JSON.stringify(config || {}),
    }),
  syncConnector: (connection_name: string) =>
    request(`/connectors/sync/${connection_name}`, { method: "POST" }),
  removeConnector: (connection_name: string) =>
    request(`/connectors/${connection_name}`, { method: "DELETE" }),

  // Diff
  runDiff: (targets: string[] | null = null, target_schema: string | null = null, full: boolean = false, mode: string = "changed") =>
    request("/diff", {
      method: "POST",
      body: JSON.stringify({ targets, target_schema, full, mode }),
    }),

  // Git operations
  initGit: (initial_branch: string = "main") =>
    request<{ success: boolean; output?: string }>("/git/init", {
      method: "POST",
      body: JSON.stringify({ initial_branch }),
    }),
  getGitStatus: () => request("/git/status"),
  getGitLog: (limit: number = 20) => request(`/git/log?limit=${limit}`),
  getGitDiff: (file?: string, staged?: boolean) => {
    const params = new URLSearchParams();
    if (file) params.set("file", file);
    if (staged) params.set("staged", "true");
    const qs = params.toString();
    return request(`/git/diff${qs ? "?" + qs : ""}`);
  },
  getGitBranches: () => request("/git/branches"),
  getGitStash: () => request("/git/stash"),
  getGitRemote: () => request("/git/remote"),
  gitStage: (files: string[]) =>
    request("/git/stage", { method: "POST", body: JSON.stringify({ files }) }),
  gitUnstage: (files: string[]) =>
    request("/git/unstage", { method: "POST", body: JSON.stringify({ files }) }),
  gitCommit: (message: string) =>
    request("/git/commit", { method: "POST", body: JSON.stringify({ message }) }),
  gitPull: (remote?: string, branch?: string) =>
    request("/git/pull", { method: "POST", body: JSON.stringify({ remote: remote || "origin", branch }) }),
  gitPush: (remote?: string, branch?: string) =>
    request("/git/push", { method: "POST", body: JSON.stringify({ remote: remote || "origin", branch }) }),
  gitCreateBranch: (name: string, checkout: boolean = true) =>
    request("/git/branch", { method: "POST", body: JSON.stringify({ name, checkout }) }),
  gitCheckout: (branch: string) =>
    request("/git/checkout", { method: "POST", body: JSON.stringify({ branch }) }),
  gitDeleteBranch: (name: string) =>
    request(`/git/branch?name=${encodeURIComponent(name)}`, { method: "DELETE" }),
  gitStashSave: (message?: string) =>
    request("/git/stash", { method: "POST", body: JSON.stringify({ message }) }),
  gitStashPop: () =>
    request("/git/stash/pop", { method: "POST", body: JSON.stringify({}) }),
  gitDiscard: (files: string[]) =>
    request("/git/discard", { method: "POST", body: JSON.stringify({ files }) }),

  // Upload
  uploadFile: async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    const headers: Record<string, string> = {};
    if (authToken) headers["Authorization"] = `Bearer ${authToken}`;

    const timeoutController = new AbortController();
    const timeoutId = setTimeout(() => timeoutController.abort(), REQUEST_TIMEOUT_MS);

    try {
      const res = await fetch(`${BASE}/upload`, { method: "POST", body: formData, headers, signal: timeoutController.signal });
      clearTimeout(timeoutId);
      if (res.status === 401) {
        authToken = null;
        localStorage.removeItem("havn_token");
        window.dispatchEvent(new Event("havn_auth_required"));
        throw new Error("Authentication required");
      }
      if (!res.ok) {
        const msg = await parseErrorResponse(res, "/upload");
        throw new Error(msg);
      }
      return res.json();
    } catch (err: unknown) {
      clearTimeout(timeoutId);
      const error = err as Error;
      if (error.name === "AbortError") {
        throw new Error("Request timed out after 30 seconds");
      }
      if (error instanceof TypeError) {
        throw new Error("Network error: Unable to reach the server. Check that havn is running.");
      }
      throw error;
    }
  },

  // Environment
  getEnvironment: () => request("/environment"),
  switchEnvironment: (envName: string) =>
    request(`/environment/${envName}`, { method: "PUT" }),

  // Seeds
  listSeeds: () => request("/seeds"),
  runSeeds: (force: boolean = false, schema_name: string = "seeds") =>
    request("/seeds", {
      method: "POST",
      body: JSON.stringify({ force, schema_name }),
    }),

  // Sources
  listSources: () => request("/sources"),
  checkSourceFreshness: () => request("/sources/freshness"),

  // Exposures
  listExposures: () => request("/exposures"),

  // Autocomplete
  getAutocomplete: () => request("/autocomplete"),

  // Full DAG (with seeds, sources, exposures)
  getFullDAG: () => request("/dag/full"),

  // Model notebook view
  getModelNotebookView: (modelName: string) => request(`/models/${modelName}/notebook-view`),

  // Create model
  createModel: (name: string, schema_name: string = "bronze", materialized: string = "table", sql: string = "") =>
    request("/models/create", {
      method: "POST",
      body: JSON.stringify({ name, schema_name, materialized, sql }),
    }),

  // Check (validation + assertions + contracts)
  runCheck: () => request("/check", { method: "POST" }),

  // Contracts
  runContracts: () => request("/contracts/run", { method: "POST" }),

  // Lineage
  getLineage: (modelName: string) => request(`/lineage/${modelName}`),
  getAllLineage: () => request("/lineage"),

  // Alerts
  getAlertHistory: (limit: number = 50) => request(`/alerts?limit=${limit}`),
  testAlert: (channel: string, config: { slack_webhook_url?: string; webhook_url?: string }) =>
    request("/alerts/test", { method: "POST", body: JSON.stringify({ channel, ...config }) }),

  // CDC
  getCDCStatus: () => request("/cdc"),
  resetCDCWatermark: (name: string) =>
    request(`/cdc/${encodeURIComponent(name)}/reset`, { method: "POST" }),

  // Masking
  getMaskingMethods: () => request<any>("/masking/methods"),
  listMaskingPolicies: () => request("/masking/policies"),
  createMaskingPolicy: (policy: unknown) =>
    request("/masking/policies", { method: "POST", body: JSON.stringify(policy) }),
  updateMaskingPolicy: (id: number, updates: unknown) =>
    request(`/masking/policies/${id}`, { method: "PUT", body: JSON.stringify(updates) }),
  deleteMaskingPolicy: (id: number) =>
    request(`/masking/policies/${id}`, { method: "DELETE" }),

  // Quality
  getFreshness: (maxHours: number = 24) => request(`/freshness?max_hours=${maxHours}`),
  getProfiles: () => request("/profiles"),
  getAssertions: (limit: number = 100) => request(`/assertions?limit=${limit}`),
  getContracts: () => request("/contracts"),
  getContractHistory: () => request("/contracts/history"),
  getContractModelHistory: (model: string) => request(`/contracts/${encodeURIComponent(model)}/history`),

  // Anomaly Detection
  getAnomalies: (limit: number = 100, model?: string) => {
    const params = model ? `?limit=${limit}&model=${encodeURIComponent(model)}` : `?limit=${limit}`;
    return request(`/anomalies${params}`);
  },
  getModelAnomalies: (model: string, limit: number = 50) =>
    request(`/anomalies/${encodeURIComponent(model)}?limit=${limit}`),
  getAnomalyConfig: () => request("/anomalies/config"),
  updateAnomalyConfig: (config: { enabled?: boolean; lookback?: number; threshold?: number }) =>
    request("/anomalies/config", { method: "PUT", body: JSON.stringify(config) }),
  getModelProfileHistory: (model: string, limit: number = 30) =>
    request(`/anomalies/${encodeURIComponent(model)}/history?limit=${limit}`),

  // Impact analysis
  getImpactAnalysis: (model: string, column?: string) => {
    const params = column ? `?column=${encodeURIComponent(column)}` : "";
    return request(`/impact/${encodeURIComponent(model)}${params}`);
  },

  // Wiki
  listWikiPages: () => request("/wiki"),
  getWikiPage: (slug: string) => request(`/wiki/${encodeURIComponent(slug)}`),

  // Rewind (Pipeline Time Travel)
  getRewindRuns: (limit: number = 100) => request(`/rewind/runs?limit=${limit}`),
  getRewindSnapshots: (limit: number = 5000) => request(`/rewind/snapshots?limit=${limit}`),
  getRunSnapshots: (runId: string) => request(`/rewind/snapshots/${runId}`),
  getSnapshotSample: (runId: string, modelName: string, limit: number = 100) =>
    request(`/rewind/sample/${runId}/${modelName}?limit=${limit}`),
  restoreSnapshot: (runId: string, modelName: string, cascade: boolean = true) =>
    request("/rewind/restore", {
      method: "POST",
      body: JSON.stringify({ run_id: runId, model_name: modelName, cascade }),
    }),
  getDownstreamModels: (modelName: string) => request(`/rewind/downstream/${modelName}`),
  runRewindGC: () => request("/rewind/gc", { method: "POST" }),

  // Schema Sentinel
  runSentinelCheck: () => request("/sentinel/check", { method: "POST" }),
  getSentinelDiffs: (limit: number = 50) => request(`/sentinel/diffs?limit=${limit}`),
  getSentinelImpacts: (diffId: string) => request(`/sentinel/impacts/${diffId}`),
  getSentinelHistory: (sourceName: string, limit: number = 20) =>
    request(`/sentinel/history/${sourceName}?limit=${limit}`),
  getSentinelSources: () => request("/sentinel/sources"),
  applySentinelFix: (modelPath: string, oldName: string, newName: string) =>
    request("/sentinel/apply-fix", {
      method: "POST",
      body: JSON.stringify({ model_path: modelPath, old_name: oldName, new_name: newName }),
    }),
  resolveSentinelImpact: (diffId: string, modelName: string) =>
    request("/sentinel/resolve", {
      method: "POST",
      body: JSON.stringify({ diff_id: diffId, model_name: modelName }),
    }),

  // Dashboards
  listDashboards: (): Promise<Dashboard[]> => request("/dashboards"),
  listDashboardTemplates: () => request("/dashboards/templates"),
  createDashboard: (name: string, description: string = "", templateId?: string) =>
    request("/dashboards", {
      method: "POST",
      body: JSON.stringify({ name, description, template_id: templateId }),
    }),
  getDashboard: (id: string): Promise<DashboardFull> => request(`/dashboards/${id}`),
  updateDashboard: (id: string, updates: Record<string, unknown>) =>
    request(`/dashboards/${id}`, {
      method: "PUT",
      body: JSON.stringify(updates),
    }),
  deleteDashboard: (id: string) =>
    request(`/dashboards/${id}`, { method: "DELETE" }),
  cloneDashboard: (id: string, name: string = "Copy") =>
    request(`/dashboards/${id}/clone?name=${encodeURIComponent(name)}`, { method: "POST" }),
  exportDashboard: (id: string): Promise<DashboardExport> =>
    request(`/dashboards/${id}/export`),
  importDashboard: (data: { dashboard: Record<string, unknown>; widgets: Record<string, unknown>[] }) =>
    request("/dashboards/import", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  // Dashboard widgets
  addWidget: (dashboardId: string, widget: Record<string, unknown>) =>
    request(`/dashboards/${dashboardId}/widgets`, {
      method: "POST",
      body: JSON.stringify(widget),
    }),
  updateWidget: (dashboardId: string, widgetId: string, updates: Record<string, unknown>) =>
    request(`/dashboards/${dashboardId}/widgets/${widgetId}`, {
      method: "PUT",
      body: JSON.stringify(updates),
    }),
  updateWidgetPositions: (dashboardId: string, positions: { id: string; position: Record<string, number> }[]) =>
    request(`/dashboards/${dashboardId}/widgets/positions`, {
      method: "PATCH",
      body: JSON.stringify({ positions }),
    }),
  deleteWidget: (dashboardId: string, widgetId: string) =>
    request(`/dashboards/${dashboardId}/widgets/${widgetId}`, { method: "DELETE" }),

  // Dashboard widget queries
  queryWidget: (dashboardId: string, widgetId: string, filters: Record<string, unknown> = {}, parameters: Record<string, unknown> = {}): Promise<WidgetQueryResult> =>
    request(`/dashboards/${dashboardId}/widgets/${widgetId}/query`, {
      method: "POST",
      body: JSON.stringify({ filters, parameters }),
    }),
  queryDashboardBatch: (dashboardId: string, filters: Record<string, unknown> = {}, parameters: Record<string, unknown> = {}) =>
    request(`/dashboards/${dashboardId}/query-batch`, {
      method: "POST",
      body: JSON.stringify({ filters, parameters }),
    }),
  clearDashboardCache: (dashboardId: string) =>
    request(`/dashboards/${dashboardId}/cache`, { method: "DELETE" }),

  // Orchestration Jobs
  listJobs: () => request<OrchestrationJob[]>("/jobs"),
  getJob: (name: string) =>
    request<OrchestrationJob & { plan: JobPlan }>(`/jobs/${encodeURIComponent(name)}`),
  getJobPlan: (name: string) =>
    request<JobPlan>(`/jobs/${encodeURIComponent(name)}/plan`),
  runJob: (name: string) =>
    request(`/jobs/${encodeURIComponent(name)}/run`, { method: "POST" }),
  createJob: (data: Partial<OrchestrationJob>) =>
    request("/jobs", { method: "POST", body: JSON.stringify(data) }),
  updateJob: (name: string, data: Partial<OrchestrationJob>) =>
    request(`/jobs/${encodeURIComponent(name)}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  deleteJob: (name: string) =>
    request(`/jobs/${encodeURIComponent(name)}`, { method: "DELETE" }),
  getJobHistory: (name: string, limit: number = 50) =>
    request<JobRun[]>(`/jobs/${encodeURIComponent(name)}/history?limit=${limit}`),
  listJobRuns: (limit: number = 100, job?: string) =>
    request<JobRun[]>(
      `/job-runs?limit=${limit}${job ? `&job=${encodeURIComponent(job)}` : ""}`,
    ),
  getJobRun: (id: string) => request<JobRun>(`/job-runs/${id}`),
  cancelJobRun: (id: string) =>
    request(`/job-runs/${id}/cancel`, { method: "POST" }),

  // Step data preview (inline in Job Results)
  getStepPreview: (schema: string, table: string, limit: number = 10) =>
    request<{ columns: string[]; rows: any[][]; table: string }>(
      `/step-preview/${encodeURIComponent(schema)}/${encodeURIComponent(table)}?limit=${limit}`,
    ),

  // Orchestration DAG picker
  getOrchestrationDag: () => request<OrchestrationDag>("/dag/orchestration"),

  // Pull requests
  listPrs: (status?: string) =>
    request<PullRequest[]>(`/prs${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  createPr: (data: {
    title: string;
    description?: string;
    base_ref: string;
    head_ref: string;
    author?: string;
    require_approval?: boolean;
  }) => request<PullRequest>("/prs", { method: "POST", body: JSON.stringify(data) }),
  getPr: (id: string) => request<PullRequest>(`/prs/${encodeURIComponent(id)}`),
  updatePr: (
    id: string,
    data: { title?: string; description?: string; require_approval?: boolean },
  ) =>
    request<PullRequest>(`/prs/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  closePr: (id: string, user: string = "local") =>
    request<PullRequest>(`/prs/${encodeURIComponent(id)}/close`, {
      method: "POST",
      body: JSON.stringify({ user }),
    }),
  listPrComments: (id: string) =>
    request<PRComment[]>(`/prs/${encodeURIComponent(id)}/comments`),
  addPrComment: (
    id: string,
    data: {
      body: string;
      author?: string;
      comment_type?: string;
      file?: string;
      line?: number;
    },
  ) =>
    request<PRComment>(`/prs/${encodeURIComponent(id)}/comments`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  approvePr: (id: string, reviewer: string = "local") =>
    request<PullRequest>(`/prs/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      body: JSON.stringify({ reviewer }),
    }),
  requestPrChanges: (id: string, reviewer: string = "local", reason: string = "") =>
    request<PullRequest>(`/prs/${encodeURIComponent(id)}/request-changes`, {
      method: "POST",
      body: JSON.stringify({ reviewer, reason }),
    }),
  buildPr: (id: string) =>
    request<{ status: string; pr_id: string }>(`/prs/${encodeURIComponent(id)}/build`, {
      method: "POST",
    }),
  getPrBuild: (id: string) =>
    request<PrBuild | { pr_id: string; status: string }>(
      `/prs/${encodeURIComponent(id)}/build`,
    ),
  mergePr: (id: string, user: string = "local") =>
    request<{ success: boolean; merge_commit?: string; base_ref?: string; head_ref?: string }>(
      `/prs/${encodeURIComponent(id)}/merge`,
      { method: "POST", body: JSON.stringify({ user }) },
    ),
  getPrReviewPrompt: async (id: string): Promise<string> => {
    // Plain-text endpoint — bypass the JSON wrapper
    const res = await fetch(`/api/prs/${encodeURIComponent(id)}/review-prompt`, {
      headers: authToken ? { Authorization: `Bearer ${authToken}` } : {},
    });
    if (!res.ok) throw new Error(`Failed to load review prompt: ${res.status}`);
    return await res.text();
  },
  getPrDiff: (id: string) =>
    request<{ files: string[]; base_ref: string; head_ref: string }>(
      `/prs/${encodeURIComponent(id)}/diff`,
    ),
  getPrLineageImpact: (id: string) =>
    request<{ changed: string[]; impacted: string[] }>(
      `/prs/${encodeURIComponent(id)}/lineage-impact`,
    ),
  getPrStateStatus: () => request<PrStateStatus>("/prs/state-status"),
};
