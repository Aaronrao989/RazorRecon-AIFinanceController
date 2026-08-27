// Typed client for the AI Finance Controller FastAPI backend.
// Adapted for Next.js: reads NEXT_PUBLIC_API_URL and guards all browser APIs so
// it is safe during server rendering. The backend contract is unchanged.

export type Health = {
  service?: string;
  status?: string;
  groq_key_present?: boolean;
  groq_model?: string;
  groq_model_fallback?: string;
  confidence_threshold?: number;
  amount_delta_cap?: number;
  data_ready?: boolean;
  data_dir?: string;
};

export type AgentMetrics = {
  records_investigated?: number;
  avg_steps?: number | null;
  total_tool_calls?: number;
  verdicts_honored?: number;
  verdicts_overridden_by_guardrail?: number;
  verdicts_errored?: number;
  groq?: {
    total_calls?: number;
    peak_rpm?: number;
    rpm_cap?: number;
    min_interval_seconds?: number;
  };
};

export type Metrics = {
  total_records?: number;
  auto_matched?: number;
  match_rate?: number;
  exceptions?: number;
  matched_precision?: number;
  matched_recall?: number;
  matched_f1?: number;
  confusion?: { tp?: number; fp?: number; fn?: number; tn?: number };
  status_accuracy?: number | null;
  false_positive_count?: number;
  false_positive_cost?: number;
  false_positives?: Array<Record<string, unknown>>;
  false_negatives?: Array<Record<string, unknown>>;
  by_status?: Record<string, number>;
  wall_clock_seconds?: number;
  throughput_rps?: number | null;
  llm_calls?: number;
  load_errors?: number;
  mode?: "single-shot" | "agent" | string;
  agent?: AgentMetrics;
};

export type Decision = {
  txn_id?: string;
  order_id?: string;
  status?: string;
  source?: string;
  reason?: string;
  confidence?: number;
  amount_delta?: number | null;
  matched_utr?: string | null;
  llm_used?: boolean;
};

export type AuditTrace = {
  provider?: string;
  model?: string;
  resolution?: string;
  reason?: string;
  confidence?: number;
  raw?: unknown;
  guardrail?: unknown;
  // The backend returns the investigation trail as `evidence` (an array of
  // step objects), with `agent_steps` / `agent_tool_calls` as numeric counts.
  evidence?: Array<Record<string, unknown>>;
  agent_steps?: number;
  agent_tool_calls?: number;
  [key: string]: unknown;
};

export type AuditEntry = {
  txn_id?: string;
  order_id?: string;
  timestamp?: string;
  inputs_seen?: unknown;
  rule_or_layer?: "deterministic" | "llm-resolver" | string;
  ambiguity_reason?: string | null;
  llm_trace?: AuditTrace | null;
  decision?: Decision;
};

export type Results = {
  job_id?: string;
  status?: string;
  ready?: boolean;
  metrics?: Metrics;
  decisions?: Decision[];
  exceptions?: Decision[];
};

export type CompareItem = {
  txn_id?: string;
  single?: string;
  agent?: string;
  truth?: string;
};

export type CompareResult = {
  single?: Metrics;
  agent?: Metrics;
  lift?: CompareItem[];
  regressions?: CompareItem[];
};

export type JobStatus = {
  job_id?: string;
  status?: "pending" | "running" | "done" | "error" | string;
  events?: number;
  latest?: ProgressEvent | null;
  error?: string | null;
};

export type ProgressEvent = {
  phase?: string;
  seq?: number;
  t?: string | number;
  processed?: number;
  total?: number;
  llm_calls?: number;
  last_txn?: string;
  last_status?: string;
  message?: string;
  status?: string;
};

export const DEFAULT_API =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const STORAGE_KEY = "finance-api-url";

export const getApiUrl = () => {
  if (typeof window === "undefined") return DEFAULT_API;
  try {
    return localStorage.getItem(STORAGE_KEY) || DEFAULT_API;
  } catch {
    return DEFAULT_API;
  }
};

export const setApiUrl = (url: string) => {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, url.trim().replace(/\/$/, ""));
  } catch {
    /* ignore storage failures (private mode, etc.) */
  }
};

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${getApiUrl()}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/"),
  generate: (body: {
    seed: number;
    total: number;
    inject_investigable: boolean;
  }) =>
    request<Record<string, unknown>>("/generate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  reconcile: (body: {
    use_llm: boolean;
    simulate: boolean;
    use_agent: boolean;
  }) =>
    request<{ job_id: string; status: string }>("/reconcile", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  status: (id: string) =>
    request<JobStatus>(`/status/${encodeURIComponent(id)}`),
  results: (id: string) =>
    request<Results>(`/results/${encodeURIComponent(id)}`),
  audit: (id: string) =>
    request<AuditEntry[]>(`/audit/${encodeURIComponent(id)}`),
  compare: (body: { simulate: boolean; use_llm: boolean }) =>
    request<CompareResult>("/compare", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  csv: (name: string) => `${getApiUrl()}/data/${encodeURIComponent(name)}.csv`,
  auditUrl: (id: string) => `${getApiUrl()}/audit/${encodeURIComponent(id)}`,
};

export function subscribeProgress(
  id: string,
  onMessage: (data: ProgressEvent) => void,
  onEnd: (status?: string) => void,
  onError: () => void,
) {
  const source = new EventSource(
    `${getApiUrl()}/progress/${encodeURIComponent(id)}`,
  );

  source.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data) as ProgressEvent;
      onMessage(data);
      if (data.phase === "stream_end") {
        source.close();
        onEnd(data.status);
      }
    } catch {
      // Keep the stream alive for heartbeats or malformed non-terminal events.
    }
  };
  source.onerror = () => {
    source.close();
    onError();
  };
  return () => source.close();
}
