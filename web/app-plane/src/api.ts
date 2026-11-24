// Chat SPA API client -> app-plane Chat API (Cognito-authorized).
// VITE_API_BASE is baked in at build time. Requests carry the Cognito JWT.
import { getIdToken } from "./auth";

const BASE = (import.meta.env.VITE_API_BASE ?? "/api").replace(/\/$/, "");

export interface AgentUsage {
  model_calls: number;
  cost_usd: number;
  cache_hit_ratio: number;
  model_id: string;
  // Token buckets (present per agent in by_agent; the backend emits these).
  input_tokens?: number;
  output_tokens?: number;
  total_input_tokens?: number;
}

export interface Usage {
  cost_usd: number;
  cache_hit_ratio: number;
  cache_read_tokens: number;
  total_input_tokens: number;
  model_calls: number;
  by_agent?: Record<string, AgentUsage>;
  // Internal components (e.g. the context-management summarizer). Their spend is
  // already included in the owning agent's by_agent entry.
  by_component?: Record<string, AgentUsage>;
}

/** Token-governance enforcement outcome for a turn (from the agent's response). */
export interface Governance {
  stopped?: boolean;
  limited?: boolean;
  reason?: string | null;
  scope?: string | null;
  detail?: string | null;
}

export interface ChatResult {
  reply: string;
  usage: Usage;
  /** Which tokenomics patterns were active on this response, e.g.
   *  "prompt_cache=on+memory=on" | "baseline". From the agent's identity block. */
  flagConfig?: string;
  /** Enforcement outcome (in-loop caps). Present when governance is configured. */
  governance?: Governance;
}

/** Raised when the in-loop quota hook blocks the turn (per-user quota / throttle),
 *  surfaced by the Chat API as 403 (quota) / 429 (throttle). */
export class GovernanceBlockedError extends Error {
  kind: "quota" | "throttle";
  retryAfter?: number;
  constructor(kind: "quota" | "throttle", message: string, opts?: { retryAfter?: number }) {
    super(message);
    this.name = "GovernanceBlockedError";
    this.kind = kind;
    this.retryAfter = opts?.retryAfter;
  }
}

/** Send one chat turn. Identity comes from the JWT; the body carries only the
 *  message + the conversation id (which pins the AgentCore session). */
export async function chat(message: string, conversationId: string): Promise<ChatResult> {
  const token = await getIdToken();
  if (!token) throw new Error("Your session has expired. Please log in again.");

  const res = await fetch(`${BASE}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};

  if (!res.ok) {
    // In-loop quota hook block: per-user quota (403 BUDGET_EXCEEDED) or throttle (429 TPM).
    if (res.status === 403 && (data.error === "BUDGET_EXCEEDED" || data.limit_hit)) {
      throw new GovernanceBlockedError("quota", data.message || "You've reached your usage limit.");
    }
    if (res.status === 429) {
      const retryAfter = Number(data.retryAfter ?? res.headers.get("Retry-After") ?? 0) || undefined;
      throw new GovernanceBlockedError(
        "throttle",
        data.message || "You're sending requests too fast. Please wait a moment.",
        { retryAfter },
      );
    }
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return {
    reply: data.reply,
    usage: data.usage,
    flagConfig: data.identity?.flag_config,
    governance: data.governance,
  };
}
