import { useMemo, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import Button from "@cloudscape-design/components/button";
import Input from "@cloudscape-design/components/input";
import Grid from "@cloudscape-design/components/grid";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Select from "@cloudscape-design/components/select";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import Spinner from "@cloudscape-design/components/spinner";
import Flashbar, { FlashbarProps } from "@cloudscape-design/components/flashbar";
import Alert from "@cloudscape-design/components/alert";
import BarChart from "@cloudscape-design/components/bar-chart";
import Pagination from "@cloudscape-design/components/pagination";
import { useEffect } from "react";
import { useHelp } from "../help";
import { api, AgentLimits as ApiAgentLimits, AuditEntry, GovAnalytics } from "../api";
import { useRole } from "../role";

/**
 * Governance page — wired to the control-plane API:
 *   GET/PUT /governance/config, GET /governance/analytics, GET /governance/events,
 *   GET /governance/audit (change history; threat model M16).
 *
 * Locked config (9 fields):
 *  per-agent (x3): max_output_tokens_per_model_call, max_tool_calls,
 *                  max_iterations, max_tokens_per_agent_call
 *  per-session (global): session_max_tokens
 *  per-user   (global): daily_max_tokens_per_user, monthly_max_tokens_per_user
 *  throttling (global): max_tokens_per_minute_per_user
 */

const AGENTS = ["orchestrator", "knowledge", "order"] as const;
type Agent = (typeof AGENTS)[number];

type AgentLimits = ApiAgentLimits;

const EMPTY_LIMITS: AgentLimits = {
  max_output_tokens_per_model_call: 0,
  max_tool_calls: 0,
  max_iterations: 0,
  max_tokens_per_agent_call: 0,
};

// Human-readable label for a snake_case setting key, e.g.
// "max_output_tokens_per_model_call" -> "Max output tokens per model call".
// The underlying config key is unchanged; this is display-only.
const prettyLabel = (key: string) => key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

const AGENT_ROWS: { key: keyof AgentLimits; label: string; desc: string; applies: string }[] = [
  {
    key: "max_output_tokens_per_model_call",
    label: prettyLabel("max_output_tokens_per_model_call"),
    desc: "Output cap per model call",
    applies: "next session",
  },
  {
    key: "max_tool_calls",
    label: prettyLabel("max_tool_calls"),
    desc: "Tool calls per turn",
    applies: "next turn",
  },
  {
    key: "max_iterations",
    label: prettyLabel("max_iterations"),
    desc: "Loop iterations per agent call",
    applies: "next turn",
  },
  {
    key: "max_tokens_per_agent_call",
    label: prettyLabel("max_tokens_per_agent_call"),
    desc: "Input+output per agent call",
    applies: "next turn",
  },
];

const SCOPE_COLOR: Record<string, "blue" | "red" | "grey" | "green"> = {
  turn: "blue",
  "per-turn": "blue",
  agent: "blue",
  "per-agent": "blue",
  session: "green",
  "per-session": "green",
  user: "red",
  "per-user": "red",
  throttling: "red",
};

const fmt = (n: number) => n.toLocaleString();
const usd = (n: number) => `$${n.toFixed(3)}`;
const pctOf = (used: number, quota: number) => (quota > 0 ? Math.round((used / quota) * 100) : 0);

// Help-panel content per setting (opens in the right-hand tools drawer).
const HELP: Record<string, { header: string; body: React.ReactNode }> = {
  max_output_tokens_per_model_call: {
    header: "Max output tokens per model call",
    body: (
      <SpaceBetween size="s">
        <Box>
          Caps the number of <b>output</b> tokens a single model call may produce (Bedrock{" "}
          <code>inferenceConfig.maxTokens</code>).
        </Box>
        <Box>
          <b>Scope:</b> one model call · <b>Per agent.</b>
        </Box>
        <Box>
          <b>Enforcement:</b> soft (in-loop), set at model construction.
        </Box>
        <Box>
          <b>Applies on:</b> next session.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Lower it to shorten replies and cut output cost (output tokens are the priciest).
        </Box>
      </SpaceBetween>
    ),
  },
  max_tool_calls: {
    header: "Max tool calls",
    body: (
      <SpaceBetween size="s">
        <Box>Maximum number of tool calls an agent may make within one turn.</Box>
        <Box>
          <b>Scope:</b> one turn · <b>Per agent.</b>
        </Box>
        <Box>
          <b>Enforcement:</b> soft (in-loop hook). The next tool call past the cap is blocked.
        </Box>
        <Box>
          <b>Applies on:</b> next turn.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Guards against tool storms / redundant lookups.
        </Box>
      </SpaceBetween>
    ),
  },
  max_iterations: {
    header: "Max iterations",
    body: (
      <SpaceBetween size="s">
        <Box>
          Maximum event-loop iterations within one agent invocation (Strands <code>Limits.turns</code>).
        </Box>
        <Box>
          <b>Scope:</b> one agent call · <b>Per agent.</b>
        </Box>
        <Box>
          <b>Enforcement:</b> soft (in-loop). Stops cleanly when reached.
        </Box>
        <Box>
          <b>Applies on:</b> next turn.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Guards against reasoning loops that never resolve.
        </Box>
      </SpaceBetween>
    ),
  },
  max_tokens_per_agent_call: {
    header: "Max tokens per agent call",
    body: (
      <SpaceBetween size="s">
        <Box>
          Caps <b>input + output</b> tokens summed across every model call within one agent invocation
          (Strands <code>Limits.total_tokens</code>).
        </Box>
        <Box>
          <b>Scope:</b> one agent call · <b>Per agent.</b>
        </Box>
        <Box>
          <b>Enforcement:</b> soft (in-loop). Stops cleanly when reached.
        </Box>
        <Box>
          <b>Applies on:</b> next turn.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          A per-agent ceiling — bounds one specialist run regardless of who triggers it.
        </Box>
      </SpaceBetween>
    ),
  },
  daily_max_tokens_per_user: {
    header: "Daily max tokens per user",
    body: (
      <SpaceBetween size="s">
        <Box>
          Maximum tokens a single user may consume <b>per day</b>.
        </Box>
        <Box>
          <b>What counts:</b> every token the user's turns consume —{" "}
          <b>input + output + cache&nbsp;read + cache&nbsp;write</b>.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Per turn: <code>daily_used += input + output + cacheRead + cacheWrite</code>. Cached tokens are
          billed (write ≈1.25×, read ≈0.1× of input), so they count toward the quota at face value — the cap
          tracks tokens consumed, not dollars.
        </Box>
        <Box>
          <b>Scope:</b> per user · <b>Global default</b> (counted per user, one shared number).
        </Box>
        <Box>
          <b>Enforcement:</b> soft, in-loop — before a turn's first model call the agent compares the user's
          running daily total to the cap; at/over it the turn is blocked (chat shows 403 "usage limit
          reached"). Checks the <i>already-spent</i> total, so the turn that crosses the cap may finish and
          the next one is blocked.
        </Box>
        <Box>
          <b>Resets:</b> daily (date-keyed counter + TTL). <b>Applies on:</b> next turn (~2s).
        </Box>
      </SpaceBetween>
    ),
  },
  monthly_max_tokens_per_user: {
    header: "Monthly max tokens per user",
    body: (
      <SpaceBetween size="s">
        <Box>
          Maximum tokens a single user may consume <b>per month</b>.
        </Box>
        <Box>
          <b>What counts:</b> <b>input + output + cache&nbsp;read + cache&nbsp;write</b>, same as the daily
          cap.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Per turn: <code>monthly_used += input + output + cacheRead + cacheWrite</code>. Counts billed cache
          tokens at face value (tokens, not dollars).
        </Box>
        <Box>
          <b>Scope:</b> per user · <b>Global default</b> (counted per user).
        </Box>
        <Box>
          <b>Enforcement:</b> soft, in-loop — running monthly total vs the cap, checked before the turn's
          first model call; at/over it the turn is blocked (403).
        </Box>
        <Box>
          <b>Resets:</b> monthly (month-keyed counter + TTL). <b>Applies on:</b> next turn.
        </Box>
      </SpaceBetween>
    ),
  },
  max_tokens_per_minute_per_user: {
    header: "Max tokens per minute per user",
    body: (
      <SpaceBetween size="s">
        <Box>
          Per-user token <b>rate</b> ceiling (tokens per minute).
        </Box>
        <Box>
          <b>What counts:</b> <b>input + output + cache&nbsp;read + cache&nbsp;write</b> within the current
          minute window.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Per turn: <code>minute_used += input + output + cacheRead + cacheWrite</code>, in a rolling
          one-minute counter that resets each minute. At/over the rate the next turn is throttled.
        </Box>
        <Box>
          <b>Scope:</b> per-user rate · <b>Global default.</b>
        </Box>
        <Box>
          <b>Enforcement:</b> soft, in-loop — before the turn's first model call; at/over the rate the turn is
          throttled (chat shows 429 "sending too fast, retry in ~60s").
        </Box>
        <Box>
          <b>Resets:</b> every minute. <b>Applies on:</b> next turn.
        </Box>
      </SpaceBetween>
    ),
  },
  session_max_tokens: {
    header: "Session max tokens",
    body: (
      <SpaceBetween size="s">
        <Box>Maximum cumulative tokens across a whole conversation (all turns in one session).</Box>
        <Box>
          <b>What counts:</b> <b>input + output + cache&nbsp;read + cache&nbsp;write</b> summed over every
          turn in the session.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Per turn: <code>session_used += input + output + cacheRead + cacheWrite</code>. Checked before each
          turn; at/over it the conversation stops and the user is asked to start a new one.
        </Box>
        <Box>
          <b>Scope:</b> one session · <b>Global</b> (same for every user).
        </Box>
        <Box>
          <b>Enforcement:</b> soft, in-loop — a persisted per-session counter checked before each turn.
        </Box>
        <Box>
          <b>Resets:</b> per new session. <b>Applies on:</b> next turn.
        </Box>
      </SpaceBetween>
    ),
  },
};

// Help-panel content per settings SECTION (opened from the section's i-icon).
const SECTION_HELP: Record<string, { header: string; body: React.ReactNode }> = {
  primary: {
    header: "Primary token limits",
    body: (
      <SpaceBetween size="s">
        <Box>Per-user and per-session token quotas enforced in-loop before a turn's first model call.</Box>
        <Box>
          Each counts the <b>full turn total: input + output + cache read + cache write</b>.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          <b>0 = no cap</b> for that field.
        </Box>
      </SpaceBetween>
    ),
  },
  peragent: {
    header: "Per-agent limits",
    body: (
      <SpaceBetween size="s">
        <Box>
          Per-agent <b>safety ceilings</b> for one agent call / turn (output cap, tool calls, iterations,
          tokens per agent call).
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          <b>0 = no cap</b> for that field.
        </Box>
      </SpaceBetween>
    ),
  },
};

// Help-panel content per analytics widget.
const WIDGET_HELP: Record<string, { header: string; body: React.ReactNode }> = {
  violations: {
    header: "Limit violations",
    body: (
      <SpaceBetween size="s">
        <Box>How many times each configured limit fired in the selected window, for the selected user.</Box>
        <Box>
          A limit firing isn't money saved — the turn still ran up to the cap. Use this to spot caps that are
          too tight or a limit catching real abuse.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Sourced from the metering ledger (one row per enforcement event).
        </Box>
      </SpaceBetween>
    ),
  },
  cost_by_agent: {
    header: "Cost by agent",
    body: (
      <SpaceBetween size="s">
        <Box>Share of spend across the three agents in the selected window.</Box>
        <Box>The orchestrator (Sonnet) usually dominates; specialists (Haiku) are cheaper.</Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Cost is priced per model from measured token usage (cache buckets priced separately).
        </Box>
      </SpaceBetween>
    ),
  },
  top_users: {
    header: "Top users by cost",
    body: (
      <SpaceBetween size="s">
        <Box>
          Users ranked by total spend in the selected window. Always across all users (ranking one user is
          meaningless).
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Drives chargeback and identifies who to throttle.
        </Box>
      </SpaceBetween>
    ),
  },
  quota_blocks: {
    header: "Quota blocks (403) per day",
    body: (
      <SpaceBetween size="s">
        <Box>
          Count of <b>403</b> blocks per user, per day — a user hit their{" "}
          <code>daily_max_tokens_per_user</code> or <code>monthly_max_tokens_per_user</code> quota and the
          turn was blocked ("you've used your allowance").
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Different from throttling (429): this is running out of budget, not sending too fast. Rising bars =
          users exhausting their quota.
        </Box>
      </SpaceBetween>
    ),
  },
  throttled: {
    header: "Throttled (429) per day",
    body: (
      <SpaceBetween size="s">
        <Box>
          Count of <b>429</b> throttles per user, per day — the <code>max_tokens_per_minute_per_user</code>{" "}
          rate limit acting ("sending too fast, retry shortly").
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          Different from a quota block (403): this is bursting too fast, not running out of budget.
        </Box>
      </SpaceBetween>
    ),
  },
  usage_vs_cap: {
    header: "Token usage vs quota",
    body: (
      <SpaceBetween size="s">
        <Box>
          Per user: tokens consumed in the selected window vs the <b>matching</b> quota, plus the dollar cost
          of that usage.
        </Box>
        <Box>
          The time window picks both the usage period AND the cap to compare against, so they always align:
          <b> 24h → daily</b> cap · <b>7d → daily×7</b> (weekly) · <b>30d → monthly</b> cap.
        </Box>
        <Box>
          Tokens counted = <b>input + output + cache read + cache write</b> (the same total the quota enforces
          on). Live traffic only; obeys the User filter.
        </Box>
        <Box color="text-body-secondary" fontSize="body-s">
          The bar shows % of the matched cap. ≥95% red, ≥80% amber. Use it to spot who's close to a block
          before it happens.
        </Box>
      </SpaceBetween>
    ),
  },
  event_log: {
    header: "Enforcement event log",
    body: (
      <SpaceBetween size="s">
        <Box>
          Individual enforcement events (newest first): which limit fired, on whom, and the detail (e.g. 403
          quota block, 429 throttle, turn cutoff).
        </Box>
        <Box>Obeys the user + window filters; add a config filter with the dropdown.</Box>
      </SpaceBetween>
    ),
  },
};

/** Shared empty-state block for a widget with no data in the current window. */
function EmptyState({ hint }: { hint?: string }) {
  return (
    <Box textAlign="center" color="text-body-secondary" padding="l">
      <b>No data for this window yet.</b>
      <Box variant="p" color="text-body-secondary">
        {hint ?? "Run a few chat turns, then refresh."}
      </Box>
    </Box>
  );
}

/** One primary token-limit row: title + description on the left, the number
 * input on the right, all on a single line to save vertical space. */
function PrimaryLimitRow({
  title,
  desc,
  infoBtn,
  value,
  onChange,
}: {
  title: string;
  desc: string;
  infoBtn: React.ReactNode;
  value: number;
  onChange: (n: number) => void;
}) {
  return (
    // 2 columns: label+desc (flexes to fill) | input (fixed, right-aligned so
    // every row's box lines up vertically). Desc can wrap without shoving the input.
    <div style={{ display: "grid", gridTemplateColumns: "1fr 110px", alignItems: "center", columnGap: 12 }}>
      <div>
        <SpaceBetween direction="horizontal" size="xxs">
          <Box fontWeight="bold">{title}</Box>
          {infoBtn}
        </SpaceBetween>
        <Box fontSize="body-s" color="text-body-secondary">
          {desc}
        </Box>
      </div>
      <Input type="number" value={String(value)} onChange={(e) => onChange(Number(e.detail.value))} />
    </div>
  );
}

export default function Governance() {
  const { openHelp } = useHelp();
  const { canWrite } = useRole();
  const info = (key: string) => (
    <Button
      variant="icon"
      iconName="status-info"
      ariaLabel={`Info: ${key}`}
      onClick={() => openHelp(HELP[key])}
    />
  );
  const widgetInfo = (key: string) => (
    <Button
      variant="icon"
      iconName="status-info"
      ariaLabel={`Info: ${WIDGET_HELP[key].header}`}
      onClick={() => openHelp(WIDGET_HELP[key])}
    />
  );
  const sectionInfo = (key: string) => (
    <Button
      variant="icon"
      iconName="status-info"
      ariaLabel={`Info: ${SECTION_HELP[key].header}`}
      onClick={() => openHelp(SECTION_HELP[key])}
    />
  );

  // config state (loaded from GET /governance/config)
  const [agents, setAgents] = useState<Record<Agent, AgentLimits>>({
    orchestrator: { ...EMPTY_LIMITS },
    knowledge: { ...EMPTY_LIMITS },
    order: { ...EMPTY_LIMITS },
  });
  const [sessionMax, setSessionMax] = useState(0);
  const [dailyMax, setDailyMax] = useState(0);
  const [monthlyMax, setMonthlyMax] = useState(0);
  const [tpm, setTpm] = useState(0);
  const [configLoaded, setConfigLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);
  const [error, setError] = useState<string | null>(null);

  // change history (GET /governance/audit): who changed what, newest first
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(true);
  function loadAudit() {
    setAuditLoading(true);
    api
      .getAudit()
      .then((r) => setAudit(r.entries))
      .catch((e) => setError((e as Error).message))
      .finally(() => setAuditLoading(false));
  }
  useEffect(loadAudit, []);

  // analytics state (loaded from GET /governance/analytics)
  const [analytics, setAnalytics] = useState<GovAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [firstLoad, setFirstLoad] = useState(true);
  const [users, setUsers] = useState<string[]>(["all"]);

  // filter bar
  const [windowSel, setWindowSel] = useState("7d");
  const [user, setUser] = useState("all");
  const userLabel = user === "all" ? "all users" : user;
  const [configFilter, setConfigFilter] = useState<string | null>(null);

  const setAgentField = (a: Agent, k: keyof AgentLimits, v: number) =>
    setAgents((prev) => ({ ...prev, [a]: { ...prev[a], [k]: v } }));

  // Load config once.
  useEffect(() => {
    api
      .getGovConfig()
      .then((cfg) => {
        setAgents({
          orchestrator: { ...EMPTY_LIMITS, ...(cfg.agents?.orchestrator || {}) },
          knowledge: { ...EMPTY_LIMITS, ...(cfg.agents?.knowledge || {}) },
          order: { ...EMPTY_LIMITS, ...(cfg.agents?.order || {}) },
        });
        setSessionMax(cfg.session_max_tokens || 0);
        setDailyMax(cfg.user_default?.daily_max_tokens_per_user || 0);
        setMonthlyMax(cfg.user_default?.monthly_max_tokens_per_user || 0);
        setTpm(cfg.user_default?.max_tokens_per_minute_per_user || 0);
        setConfigLoaded(true);
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  // Load analytics on filter change.
  function loadAnalytics() {
    setLoading(true);
    api
      .getGovAnalytics(user, windowSel)
      .then((a) => {
        setAnalytics(a);
        // Build the user dropdown from whoever appears in top_users.
        const emails = a.top_users.map((u) => u.user).filter((u) => u.includes("@"));
        setUsers(["all", ...Array.from(new Set(emails))]);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => {
        setLoading(false);
        setFirstLoad(false);
      });
  }
  useEffect(loadAnalytics, [user, windowSel]);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await api.putGovConfig({
        agents,
        session_max_tokens: sessionMax,
        user_default: {
          daily_max_tokens_per_user: dailyMax,
          monthly_max_tokens_per_user: monthlyMax,
          max_tokens_per_minute_per_user: tpm,
        },
      });
      setFlash([
        {
          type: "success",
          content: "Token governance settings saved. Applies on the next turn / session.",
          dismissible: true,
          onDismiss: () => setFlash([]),
          id: "saved",
        },
      ]);
      loadAudit();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  const VIOLATIONS = analytics?.violations ?? [];
  const USAGE_VS_CAP = analytics?.usage_vs_cap ?? [];
  const capPeriod = analytics?.cap_period ?? "weekly";
  const kpis = analytics?.kpis;
  const hasData =
    !!analytics &&
    ((kpis?.total_tokens ?? 0) > 0 ||
      VIOLATIONS.some((v) => v.count) ||
      USAGE_VS_CAP.length > 0 ||
      (analytics.events?.length ?? 0) > 0);

  // Throttle-by-day -> stacked series per user.
  const throttleDays = Array.from(new Set((analytics?.throttle_by_day ?? []).map((t) => t.day))).sort();
  const throttleUsers = Array.from(new Set((analytics?.throttle_by_day ?? []).map((t) => t.user)));
  const throttleSeries = throttleUsers.map((u) => ({
    title: u,
    type: "bar" as const,
    data: throttleDays.map((day) => ({
      x: day,
      y: (analytics?.throttle_by_day ?? []).find((t) => t.user === u && t.day === day)?.count ?? 0,
    })),
  }));

  // Quota blocks (403) per user/day -> stacked series per user (mirror of throttle).
  const blockDays = Array.from(new Set((analytics?.quota_block_by_day ?? []).map((t) => t.day))).sort();
  const blockUsers = Array.from(new Set((analytics?.quota_block_by_day ?? []).map((t) => t.user)));
  const blockSeries = blockUsers.map((u) => ({
    title: u,
    type: "bar" as const,
    data: blockDays.map((day) => ({
      x: day,
      y: (analytics?.quota_block_by_day ?? []).find((t) => t.user === u && t.day === day)?.count ?? 0,
    })),
  }));

  const filteredEvents = useMemo(() => {
    const evs = analytics?.events ?? [];
    return evs.filter((e) => !configFilter || e.config === configFilter);
  }, [analytics, configFilter]);

  // Event-log pagination (client-side; the analytics payload returns the full
  // window of events at once). 20 rows/page; reset to page 1 when the filter or
  // the result set changes so we never land on a now-empty page.
  const EVENTS_PER_PAGE = 20;
  const [eventsPage, setEventsPage] = useState(1); // 1-based (Cloudscape convention)
  useEffect(() => {
    setEventsPage(1);
  }, [configFilter, filteredEvents.length]);
  const eventsPageCount = Math.max(1, Math.ceil(filteredEvents.length / EVENTS_PER_PAGE));
  const pagedEvents = filteredEvents.slice((eventsPage - 1) * EVENTS_PER_PAGE, eventsPage * EVENTS_PER_PAGE);

  // The Limit-violations widget shows ONLY the in-loop agent caps. The per-user
  // quota (daily/monthly) and TPM throttle have their own dedicated widgets
  // (403 trend, 429 trend, Quota usage vs cap), so they're excluded here.
  const QUOTA_THROTTLE_CONFIGS = new Set([
    "daily_max_tokens_per_user",
    "monthly_max_tokens_per_user",
    "max_tokens_per_minute_per_user",
  ]);
  const violationsSorted = [...VIOLATIONS]
    .filter((v) => !QUOTA_THROTTLE_CONFIGS.has(v.config))
    .sort((a, b) => b.count - a.count);

  return (
    <SpaceBetween size="l">
      {flash.length > 0 && <Flashbar items={flash} />}
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)} header="Governance error">
          {error}
        </Alert>
      )}

      {/* ============ CONFIG ============ */}
      <Container
        header={
          <Header
            variant="h2"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                {!configLoaded && (
                  <Box color="text-body-secondary" fontSize="body-s" padding={{ top: "xs" }}>
                    Loading current settings…
                  </Box>
                )}
                <Button
                  variant="primary"
                  loading={saving}
                  disabled={!configLoaded || !canWrite}
                  onClick={save}
                >
                  Save all changes
                </Button>
              </SpaceBetween>
            }
          >
            Token governance settings
          </Header>
        }
      >
        <div style={{ position: "relative" }}>
          {saving && (
            <div
              style={{
                position: "absolute",
                inset: 0,
                zIndex: 2,
                display: "flex",
                alignItems: "flex-start",
                justifyContent: "center",
                paddingTop: 60,
              }}
            >
              <Spinner size="large" />
            </div>
          )}
          <div
            style={{
              filter: saving ? "blur(4px)" : undefined,
              opacity: saving ? 0.4 : 1,
              transition: "opacity 0.15s ease",
              pointerEvents: saving ? "none" : undefined,
            }}
          >
            <SpaceBetween size="m">
              {/* ---- PRIMARY: per-user quotas + session cap (token totals) ---- */}
              <Container
                header={
                  <Header variant="h3" info={sectionInfo("primary")}>
                    Primary token limits
                  </Header>
                }
              >
                <ColumnLayout columns={2}>
                  <PrimaryLimitRow
                    title="Daily max tokens per user"
                    desc="Per user / day · input + output + cache"
                    infoBtn={info("daily_max_tokens_per_user")}
                    value={dailyMax}
                    onChange={setDailyMax}
                  />
                  <PrimaryLimitRow
                    title="Monthly max tokens per user"
                    desc="Per user / month · input + output + cache"
                    infoBtn={info("monthly_max_tokens_per_user")}
                    value={monthlyMax}
                    onChange={setMonthlyMax}
                  />
                  <PrimaryLimitRow
                    title="Max tokens per minute per user"
                    desc="Throttling (TPM) · input + output + cache"
                    infoBtn={info("max_tokens_per_minute_per_user")}
                    value={tpm}
                    onChange={setTpm}
                  />
                  <PrimaryLimitRow
                    title="Session max tokens"
                    desc="Per session (all users) · input + output + cache"
                    infoBtn={info("session_max_tokens")}
                    value={sessionMax}
                    onChange={setSessionMax}
                  />
                </ColumnLayout>
              </Container>

              {/* ---- SECONDARY: per-agent safety limits ---- */}
              <Container
                header={
                  <Header variant="h3" info={sectionInfo("peragent")}>
                    Per-agent limits
                  </Header>
                }
              >
                <Table
                  variant="borderless"
                  items={AGENT_ROWS}
                  columnDefinitions={[
                    {
                      id: "config",
                      header: "Setting",
                      isRowHeader: true,
                      width: 340,
                      cell: (r) => (
                        <SpaceBetween size="xxxs">
                          <SpaceBetween direction="horizontal" size="xs">
                            <Box fontWeight="bold">{r.label}</Box>
                            {info(r.key)}
                          </SpaceBetween>
                          <Box fontSize="body-s" color="text-body-secondary">
                            {r.desc}
                          </Box>
                        </SpaceBetween>
                      ),
                    },
                    ...AGENTS.map((a) => ({
                      id: a,
                      header: `${a.charAt(0).toUpperCase() + a.slice(1)} agent`,
                      cell: (r: { key: keyof AgentLimits; label: string; desc: string; applies: string }) => (
                        <div style={{ maxWidth: 120 }}>
                          <Input
                            type="number"
                            value={String(agents[a][r.key])}
                            onChange={(e) => setAgentField(a, r.key, Number(e.detail.value))}
                          />
                        </div>
                      ),
                    })),
                  ]}
                />
              </Container>
            </SpaceBetween>
          </div>
        </div>
      </Container>

      {/* ============ ANALYTICS ============ */}
      <Container
        header={
          <Header
            variant="h2"
            actions={
              <span className="refresh-green">
                <Button iconName="refresh" loading={loading} onClick={loadAnalytics}>
                  Refresh
                </Button>
              </span>
            }
          >
            Token governance analytics
          </Header>
        }
      >
        <SpaceBetween size="l">
          {/* Highlighted filter bar — applies to every widget below */}
          <div
            style={{
              background: "#f2f8fd",
              border: "1px solid #b4d7f5",
              borderRadius: 8,
              padding: "22px 16px",
            }}
          >
            <SpaceBetween size="xxs">
              <SpaceBetween direction="horizontal" size="s">
                <Box padding={{ top: "xxs" }} fontWeight="bold">
                  Time window
                </Box>
                <SegmentedControl
                  selectedId={windowSel}
                  onChange={(e) => setWindowSel(e.detail.selectedId)}
                  options={[
                    { id: "24h", text: "24h" },
                    { id: "7d", text: "7d" },
                    { id: "30d", text: "30d" },
                  ]}
                />
                <Box padding={{ top: "xxs" }} fontWeight="bold">
                  User
                </Box>
                <Select
                  selectedOption={{ label: user === "all" ? "All users" : user, value: user }}
                  onChange={(e) => setUser(e.detail.selectedOption.value!)}
                  options={users.map((u) => ({ label: u === "all" ? "All users" : u, value: u }))}
                />
                <Box padding={{ top: "xxs" }} color="text-body-secondary" fontSize="body-s">
                  Applies to every widget below.
                </Box>
              </SpaceBetween>
              <Box fontSize="body-s" color="text-body-secondary">
                Live user traffic only — benchmarking lives in Usage &amp; Cost Analytics.
              </Box>
            </SpaceBetween>
          </div>

          {firstLoad && loading ? (
            <Box textAlign="center" padding="xxl">
              <Spinner size="large" />
            </Box>
          ) : !hasData ? (
            <EmptyState hint="No analytics for this window. Send some chat turns, then refresh." />
          ) : (
            // Keep widgets mounted during a refresh; blur+dim them (no layout
            // collapse → no scroll jump) and overlay a small spinner.
            <div style={{ position: "relative" }}>
              {loading && (
                <div
                  style={{
                    position: "absolute",
                    inset: 0,
                    zIndex: 2,
                    display: "flex",
                    alignItems: "flex-start",
                    justifyContent: "center",
                    paddingTop: 80,
                  }}
                >
                  <Spinner size="large" />
                </div>
              )}
              <div
                style={{
                  filter: loading ? "blur(4px)" : undefined,
                  opacity: loading ? 0.4 : 1,
                  transition: "opacity 0.15s ease",
                  pointerEvents: loading ? "none" : undefined,
                }}
              >
                <SpaceBetween size="l">
                  {/* KPIs grouped in one bordered card (2 rows) */}
                  <Container header={<Header variant="h3">Summary</Header>}>
                    <SpaceBetween size="l">
                      {/* KPI row 1 — usage + cost (directly tied to the token quotas) */}
                      <ColumnLayout columns={4} variant="text-grid">
                        <div>
                          <Box variant="awsui-key-label">Total token usage</Box>
                          <Box fontSize="display-l" fontWeight="bold">
                            {fmt(kpis?.total_tokens ?? 0)}
                          </Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Total token cost</Box>
                          <Box fontSize="display-l" fontWeight="bold">
                            {usd(kpis?.total_cost_usd ?? 0)}
                          </Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Avg tokens / session</Box>
                          <Box fontSize="display-l" fontWeight="bold">
                            {fmt(kpis?.avg_tokens_per_session ?? 0)}
                          </Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Avg $ / session</Box>
                          <Box fontSize="display-l" fontWeight="bold">
                            {usd(kpis?.avg_cost_per_session ?? 0)}
                          </Box>
                        </div>
                      </ColumnLayout>
                      {/* KPI row 2 — governance activity (403/429 are charts below, not tiles) */}
                      <ColumnLayout columns={4} variant="text-grid">
                        <div>
                          <Box variant="awsui-key-label">Total violations</Box>
                          <Box fontSize="display-l" fontWeight="bold">
                            {fmt(kpis?.total_violations ?? 0)}
                          </Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Users near {capPeriod} quota</Box>
                          <Box fontSize="display-l" fontWeight="bold">
                            {fmt(kpis?.users_near_quota ?? 0)}
                          </Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Total sessions</Box>
                          <Box fontSize="display-l" fontWeight="bold">
                            {fmt(kpis?.total_sessions ?? 0)}
                          </Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Total turns</Box>
                          <Box fontSize="display-l" fontWeight="bold">
                            {fmt(kpis?.total_turns ?? 0)}
                          </Box>
                        </div>
                      </ColumnLayout>
                    </SpaceBetween>
                  </Container>

                  {/* Limit violations — all settings, sorted desc, zero rows dimmed */}
                  <Container
                    header={
                      <Header
                        variant="h3"
                        info={widgetInfo("violations")}
                        description={`How many times each configured limit fired · ${userLabel} · ${windowSel}`}
                      >
                        Limit violations
                      </Header>
                    }
                  >
                    <Table
                      variant="borderless"
                      items={violationsSorted}
                      columnDefinitions={[
                        {
                          id: "config",
                          header: "Setting",
                          isRowHeader: true,
                          width: 320,
                          cell: (v) => (
                            <Box fontWeight="bold" color={v.count ? undefined : "text-status-inactive"}>
                              {prettyLabel(v.config)}
                            </Box>
                          ),
                        },
                        {
                          id: "bar",
                          header: "Violations",
                          cell: (v) => {
                            const max = Math.max(...VIOLATIONS.map((x) => x.count), 1);
                            const pct = Math.round((v.count / max) * 100);
                            return (
                              <div style={{ display: "flex", alignItems: "center", gap: 10, height: 24 }}>
                                <div style={{ flex: 1, minWidth: 140 }}>
                                  <div
                                    style={{
                                      background: v.count ? "#0972d3" : "#e9ebed",
                                      height: 16,
                                      width: `${Math.max(pct, 2)}%`,
                                      borderRadius: 3,
                                    }}
                                  />
                                </div>
                                <Box
                                  fontWeight="bold"
                                  textAlign="right"
                                  color={v.count ? undefined : "text-status-inactive"}
                                >
                                  {v.count}
                                </Box>
                              </div>
                            );
                          },
                        },
                      ]}
                    />
                  </Container>

                  {/* Quota usage vs cap — per user, tokens used in window vs the window-matched cap + the $ it cost */}
                  <Container
                    header={
                      <Header
                        variant="h3"
                        info={widgetInfo("usage_vs_cap")}
                        description={`Usage vs the ${capPeriod} quota + cost · ${userLabel} · ${windowSel}`}
                      >
                        Token usage vs quota
                      </Header>
                    }
                  >
                    {USAGE_VS_CAP.length === 0 ? (
                      <EmptyState hint="No user usage in this window." />
                    ) : (
                      <SpaceBetween size="l">
                        {USAGE_VS_CAP.map((u) => {
                          const pct = pctOf(u.used, u.quota);
                          // green under 80%, amber 80–94%, red ≥95% (matches the status thresholds).
                          const fill =
                            u.quota <= 0
                              ? "#037f51"
                              : pct >= 95
                                ? "#d91515"
                                : pct >= 80
                                  ? "#b25f00"
                                  : "#037f51";
                          return (
                            <div key={u.user}>
                              <div
                                style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}
                              >
                                <Box fontWeight="bold">{u.user}</Box>
                                <Box fontSize="body-s" color="text-body-secondary">
                                  {u.quota > 0
                                    ? `${fmt(u.used)} / ${fmt(u.quota)} tokens (${capPeriod} cap) · ${usd(u.cost)}`
                                    : `${fmt(u.used)} tokens · ${usd(u.cost)} (no cap)`}
                                </Box>
                              </div>
                              <div style={{ display: "flex", alignItems: "center", gap: 10, height: 24 }}>
                                <div
                                  style={{
                                    flex: 1,
                                    background: "#e9ebed",
                                    height: 18,
                                    borderRadius: 4,
                                    overflow: "hidden",
                                  }}
                                >
                                  <div
                                    style={{
                                      background: fill,
                                      height: "100%",
                                      width: `${Math.min(Math.max(pct, 2), 100)}%`,
                                      borderRadius: 4,
                                      transition: "width 0.2s ease",
                                    }}
                                  />
                                </div>
                                {u.quota > 0 && (
                                  <Box fontWeight="bold" textAlign="right">
                                    {pct}%
                                  </Box>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </SpaceBetween>
                    )}
                  </Container>

                  {/* Quota blocks (403) + Throttled (429) per user / day — side by side */}
                  <Grid
                    gridDefinition={[{ colspan: { default: 12, s: 6 } }, { colspan: { default: 12, s: 6 } }]}
                  >
                    <Container
                      header={
                        <Header
                          variant="h3"
                          info={widgetInfo("quota_blocks")}
                          description={`${userLabel} · ${windowSel}`}
                        >
                          Token Quota blocks (403) trend
                        </Header>
                      }
                    >
                      <BarChart
                        stackedBars
                        hideFilter
                        height={220}
                        series={
                          blockSeries.length ? blockSeries : [{ title: "blocks", type: "bar", data: [] }]
                        }
                        xScaleType="categorical"
                        ariaLabel="Quota blocks (403) per user per day"
                        xTitle="Day"
                        yTitle="Quota blocks"
                        empty={<EmptyState hint="No quota blocks in this window." />}
                      />
                    </Container>
                    <Container
                      header={
                        <Header
                          variant="h3"
                          info={widgetInfo("throttled")}
                          description={`${userLabel} · ${windowSel}`}
                        >
                          Token Throttling (429) trend
                        </Header>
                      }
                    >
                      <BarChart
                        stackedBars
                        hideFilter
                        height={220}
                        series={
                          throttleSeries.length
                            ? throttleSeries
                            : [{ title: "throttles", type: "bar", data: [] }]
                        }
                        xScaleType="categorical"
                        ariaLabel="Throttled (429) per user per day"
                        xTitle="Day"
                        yTitle="Throttled calls"
                        empty={<EmptyState hint="No throttles in this window." />}
                      />
                    </Container>
                  </Grid>

                  {/* Event log */}
                  <Container
                    header={
                      <Header
                        variant="h3"
                        info={widgetInfo("event_log")}
                        counter={`(${filteredEvents.length})`}
                        description={`Individual enforcement events · ${userLabel} · ${windowSel}`}
                        actions={
                          <Select
                            selectedOption={{
                              label: configFilter ? prettyLabel(configFilter) : "All settings",
                              value: configFilter ?? "all",
                            }}
                            onChange={(e) =>
                              setConfigFilter(
                                e.detail.selectedOption.value === "all"
                                  ? null
                                  : e.detail.selectedOption.value!,
                              )
                            }
                            options={[
                              { label: "All settings", value: "all" },
                              ...VIOLATIONS.map((v) => ({ label: prettyLabel(v.config), value: v.config })),
                            ]}
                          />
                        }
                      >
                        Enforcement event log
                      </Header>
                    }
                  >
                    <Table
                      variant="borderless"
                      empty={
                        <Box color="text-body-secondary" padding="s">
                          No enforcement events for this filter.
                        </Box>
                      }
                      items={pagedEvents}
                      pagination={
                        <Pagination
                          currentPageIndex={eventsPage}
                          pagesCount={eventsPageCount}
                          onChange={(e) => setEventsPage(e.detail.currentPageIndex)}
                        />
                      }
                      columnDefinitions={[
                        { id: "ts", header: "Time", cell: (e) => e.ts, width: 190 },
                        { id: "user", header: "Subject", cell: (e) => e.subject || e.user_id || "—" },
                        {
                          id: "scope",
                          header: "Scope",
                          cell: (e) => <Badge color={SCOPE_COLOR[e.scope] ?? "grey"}>{e.scope}</Badge>,
                        },
                        {
                          id: "config",
                          header: "Setting",
                          cell: (e) => <Box fontSize="body-s">{e.config ? prettyLabel(e.config) : "—"}</Box>,
                        },
                        { id: "detail", header: "Detail", cell: (e) => e.message },
                      ]}
                    />
                  </Container>
                </SpaceBetween>
              </div>
            </div>
          )}
        </SpaceBetween>
      </Container>

      {/* ============ CHANGE HISTORY (audit trail) ============ */}
      <Table
        variant="container"
        loading={auditLoading}
        loadingText="Loading change history"
        items={audit}
        header={
          <Header
            variant="h2"
            description="Every change to flags, usage plans and overrides, and every benchmark start: who, when, before and after."
            actions={
              <Button iconName="refresh" loading={auditLoading} onClick={loadAudit}>
                Refresh
              </Button>
            }
          >
            Change history
          </Header>
        }
        empty={<Box color="text-body-secondary">No changes recorded yet.</Box>}
        columnDefinitions={[
          { id: "ts", header: "Time", cell: (e) => e.ts, width: 230 },
          { id: "who", header: "Changed by", cell: (e) => e.actor_email || e.actor_sub },
          { id: "action", header: "Change", cell: (e) => <code>{e.action}</code> },
          {
            id: "after",
            header: "New value",
            cell: (e) => (
              <Box fontSize="body-s">
                <code style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
                  {JSON.stringify(e.after)}
                </code>
              </Box>
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}
