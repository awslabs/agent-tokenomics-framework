// Control-plane API client. VITE_API_BASE is baked in at build time (the API
// Gateway stage URL). Every request carries the admin Cognito JWT.
import { getIdToken } from "./auth";

const BASE = (import.meta.env.VITE_API_BASE ?? "/api").replace(/\/$/, "");

export interface FeatureFlag {
  enabled: boolean;
  params: Record<string, unknown>;
}
export interface FlagConfig {
  prompt_cache: FeatureFlag;
  memory: FeatureFlag;
  context_management: FeatureFlag;
  model_routing: FeatureFlag;
}

export interface Metrics {
  conversations: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_input_tokens: number;
  model_calls: number;
  cost_usd: number;
  cache_hit_ratio: number;
  cost_per_conversation: number;
  cache_read_cost_usd?: number;
  cache_write_cost_usd?: number;
  messages_removed?: number;
  tool_blocks_masked?: number;
  summarizations?: number;
  summarizer_cost_usd?: number;
  // model_routing pattern (zero when OFF)
  routing_model_calls?: number;
  routing_capable_calls?: number;
  routing_cheap_calls?: number;
  routing_orch_cheap_calls?: number;
  routing_escalated_calls?: number;
  routing_judge_cost_usd?: number;
}

// Average input tokens / cost at each turn index (lib/metrics.per_turn).
export interface TurnPoint {
  turn: number;
  avg_input_tokens: number;
  avg_cost_usd: number;
}

// Per-agent cost breakdown (lib/metrics.per_agent), keyed by agent name.
export interface AgentUsage {
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  model_calls: number;
}
export type PerAgent = Record<string, AgentUsage>;

export type RunStatus = "RUNNING" | "COMPLETED" | "FAILED";

// Open, ordered feature-metadata list shown under "Breakdown". Grows per enabled
// optimization strategy (e.g. prompt_cache -> cache hit rate + cache-read).
export interface FeatureStat {
  key: string;
  value: string;
}

// One side (A or B) of a benchmark, nested inside the benchmark record.
export interface RunSide {
  kind: "baseline" | "candidate";
  features: string[];
  flag_config: string;
  status: RunStatus;
  metrics?: Metrics;
  feature_stats?: FeatureStat[];
  per_agent?: PerAgent;
  per_turn?: TurnPoint[];
  error?: string | null;
}

export interface BenchProgressPhase {
  done: number;
  total: number;
}

// A benchmark is ONE record holding BOTH runs + a two-phase progress object and
// an embedded comparison. GET /benchmarks/{id} returns the full record; the list
// endpoint returns a trimmed summary (see BenchSummary).
export interface Benchmark {
  run_id: string; // benchmark id (table PK)
  name: string;
  status: RunStatus;
  scenario: {
    sessions: number;
    turns: number;
    total_turns: number;
  };
  progress?: {
    phase: "a" | "b" | "done";
    a: BenchProgressPhase;
    b: BenchProgressPhase;
  };
  run_a: RunSide;
  run_b: RunSide;
  comparison?: { cost_a: number; cost_b: number; gain_pct: number };
  created_at: string;
  completed_at?: string | null;
  error?: string | null;
}

// One summary row per benchmark (GET /benchmarks).
export interface BenchSummary {
  run_id: string;
  name: string;
  status: RunStatus;
  scenario: Benchmark["scenario"];
  progress?: Benchmark["progress"];
  features_a?: string[];
  features_b?: string[];
  cost_a?: number;
  cost_b?: number;
  comparison?: Benchmark["comparison"];
  created_at: string;
  completed_at?: string | null;
}

// POST /benchmarks body: one call creates a baseline (run_a) + candidate (run_b)
// on the identical N x M matrix, stored as one record.
export interface StartBenchmarkBody {
  name: string;
  scenario: { sessions: number; turns: number };
  run_a: { flags: Partial<FlagConfig> };
  run_b: { flags: Partial<FlagConfig> };
}

// ---- token governance ----
export interface AgentLimits {
  max_output_tokens_per_model_call: number;
  max_tool_calls: number;
  max_iterations: number;
  max_tokens_per_agent_call: number;
}
export interface UserQuota {
  daily_max_tokens_per_user: number;
  monthly_max_tokens_per_user: number;
  max_tokens_per_minute_per_user: number;
  email?: string;
}
export interface GovernanceConfig {
  agents: Record<string, AgentLimits>;
  session_max_tokens: number;
  user_default: UserQuota;
  user_overrides: Record<string, UserQuota>;
}
export interface GovernanceEvent {
  pk?: string;
  ts: string;
  type: string;
  config?: string;
  scope: string;
  severity?: "high" | "medium" | "low";
  user_id?: string;
  subject?: string;
  session_id?: string;
  message: string;
}
export interface GovAnalytics {
  window: string;
  user: string;
  cap_period: "daily" | "weekly" | "monthly";
  window_cap: number;
  kpis: {
    total_tokens: number;
    total_cost_usd: number;
    avg_tokens_per_session: number;
    avg_cost_per_session: number;
    total_violations: number;
    quota_blocks: number;
    throttles: number;
    users_near_quota: number;
    total_sessions: number;
    total_turns: number;
  };
  violations: { config: string; count: number }[];
  cost_by_agent: { agent: string; tokens: number; cost: number }[];
  top_users: { user: string; tokens: number; cost: number }[];
  throttle_by_day: { user: string; day: string; count: number }[];
  quota_block_by_day: { user: string; day: string; count: number }[];
  usage_vs_cap: { user: string; used: number; cost: number; quota: number }[];
  events: GovernanceEvent[];
}

// ---- Roles + audit trail (threat model M14/M16) ------------------------------
export interface Me {
  groups: string[];
  can_write: boolean;
}
export interface AuditEntry {
  ts: string;
  action: string;
  actor_sub: string;
  actor_email: string;
  before: unknown;
  after: unknown;
}

// ---- Tokenomics Advisor ------------------------------------------------------
export interface AdvisorFinding {
  signal: string;
  observed: string;
  value: number;
  level: string; // model | agent | user | session | fleet | config
}
export interface AdvisorCitation {
  pattern_id: string;
  name: string;
  ref_no: number;
  pillar: string; // optimisation | governance | observability
  quality_risk: string; // low | low_medium | medium | high
  summary: string;
  expected_effect: string;
}
export interface AdvisorMessage {
  msg_id: string;
  role: "user" | "assistant";
  text: string;
  status: "pending" | "complete" | "failed";
  window?: string;
  created_at?: string;
  findings?: AdvisorFinding[];
  citations?: string[];
  citation_details?: AdvisorCitation[];
  notes?: string[];
}
export interface AdvisorChatSummary {
  chat_id: string;
  title: string;
  window: string;
  created_at?: string;
  updated_at?: string;
  pending: boolean;
}
export interface AdvisorChat {
  chat_id: string;
  title: string;
  window: string;
  messages: AdvisorMessage[];
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await getIdToken();
  if (!token)
    throw Object.assign(new Error("Your session has expired. Please log in again."), { status: 401 });
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init?.headers ?? {}),
    },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw Object.assign(new Error(data.error || `HTTP ${res.status}`), { status: res.status });
  return data as T;
}

export const api = {
  getFlags: () => req<FlagConfig>("/flags"),
  putFlags: (flags: Partial<FlagConfig>) =>
    req<FlagConfig>("/flags", { method: "PUT", body: JSON.stringify(flags) }),

  listBenchmarks: () => req<{ runs: BenchSummary[] }>("/benchmarks"),
  getBenchmark: (id: string) => req<Benchmark>(`/benchmarks/${id}`),
  // One POST creates a benchmark record (baseline run_a + candidate run_b) and
  // async-runs it; the response is the RUNNING record to poll.
  startBenchmark: (body: StartBenchmarkBody) =>
    req<Benchmark>("/benchmarks", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // the caller's control-plane role + the governance change history
  getMe: () => req<Me>("/me"),
  getAudit: (limit = 50) => req<{ entries: AuditEntry[] }>(`/governance/audit?limit=${limit}`),

  // token governance
  getGovConfig: () => req<GovernanceConfig>("/governance/config"),
  putGovConfig: (cfg: Partial<GovernanceConfig>) =>
    req<GovernanceConfig>("/governance/config", { method: "PUT", body: JSON.stringify(cfg) }),
  getGovAnalytics: (user: string, window: string) =>
    req<GovAnalytics>(
      `/governance/analytics?user=${encodeURIComponent(user)}&window=${encodeURIComponent(window)}`,
    ),
  getGovEvents: (user: string, window: string, config?: string) =>
    req<{ events: GovernanceEvent[] }>(
      `/governance/events?user=${encodeURIComponent(user)}&window=${encodeURIComponent(window)}${config ? `&config=${encodeURIComponent(config)}` : ""}`,
    ),

  // Tokenomics Advisor (async: POST stores the question + a pending reply; poll the chat)
  listAdvisorChats: () => advisorReq<{ chats: AdvisorChatSummary[] }>("/advisor/chats"),
  getAdvisorChat: (chatId: string, signal?: AbortSignal) =>
    advisorReq<AdvisorChat>(`/advisor/chats/${encodeURIComponent(chatId)}`, { signal }),
  sendAdvisorMessage: (body: { message: string; window: string; chat_id?: string }) =>
    advisorReq<{ chat_id: string; message_id: string; status: string }>("/advisor/chats", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteAdvisorChat: (chatId: string) =>
    advisorReq<{ deleted: string }>(`/advisor/chats/${encodeURIComponent(chatId)}`, { method: "DELETE" }),
  deleteAllAdvisorChats: () => advisorReq<{ deleted: number }>("/advisor/chats", { method: "DELETE" }),
};

/** Advisor calls: same auth as `req`, with friendly messages for network/timeout failures. */
async function advisorReq<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    return await req<T>(path, init);
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    if (e instanceof TypeError || e instanceof SyntaxError) {
      throw new Error("The Advisor didn't respond in time. Please try again.");
    }
    throw e;
  }
}

const POLL_MS = 1500;
const POLL_REQUEST_TIMEOUT_MS = 10000;
const POLL_GIVE_UP_MS = 200000;

/** Wait until the given reply is no longer pending; returns the chat. */
export async function waitForAdvisorReply(
  chatId: string,
  messageId: string,
  cancelled: () => boolean,
): Promise<AdvisorChat | null> {
  const started = Date.now();
  while (!cancelled()) {
    const ctl = new AbortController();
    const t = window.setTimeout(() => ctl.abort(), POLL_REQUEST_TIMEOUT_MS);
    try {
      const chat = await api.getAdvisorChat(chatId, ctl.signal);
      const reply = chat.messages.find((m) => m.msg_id === messageId);
      if (reply && reply.status !== "pending") return chat;
    } catch (e) {
      // Keep polling through timeouts, network errors, throttling (408/429) and 5xx
      // until the give-up deadline; any other 4xx (session expired, no access,
      // chat gone) is final.
      const status = (e as { status?: number }).status;
      if (status !== undefined && status >= 400 && status < 500 && status !== 408 && status !== 429) throw e;
    } finally {
      window.clearTimeout(t);
    }
    if (Date.now() - started > POLL_GIVE_UP_MS) {
      throw new Error(
        "The Advisor is taking longer than expected. Reopen this chat from history in a moment.",
      );
    }
    await new Promise((r) => window.setTimeout(r, POLL_MS));
  }
  return null;
}
