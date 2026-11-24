import { useEffect, useRef, useState } from "react";
import AppLayout from "@cloudscape-design/components/app-layout";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Input from "@cloudscape-design/components/input";
import Button from "@cloudscape-design/components/button";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import Alert from "@cloudscape-design/components/alert";
import Spinner from "@cloudscape-design/components/spinner";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import { chat, Usage, AgentUsage, Governance, GovernanceBlockedError } from "./api";
import AuthPage from "./AuthPage";
import { currentProfile, getIdToken, logout, type Profile } from "./auth";

function modelLabel(id: string): string {
  if (id.includes("sonnet")) return "Sonnet";
  if (id.includes("haiku")) return "Haiku";
  if (id.includes("nova-lite")) return "Nova Lite";
  if (id.includes("nova")) return "Nova";
  return id.split(".").pop() || id;
}

const AGENT_ORDER = ["orchestrator", "knowledge", "order"];

/** Total tokens for one agent's turn: input (incl. cache) + output. */
function agentTokens(a: AgentUsage): number {
  return (a.total_input_tokens ?? a.input_tokens ?? 0) + (a.output_tokens ?? 0);
}

// Starter questions shown as always-visible chips under the conversation.
// Short label on the chip; the detailed one-sentence prompt is sent on click.
// Each prompt is two-part (an order ask + a policy ask) so the orchestrator
// delegates to both specialists in one turn.
const STARTERS: { label: string; prompt: string }[] = [
  {
    label: "Return my last order",
    prompt: "Can I return my last order, and what's the return policy and refund timeline?",
  },
  {
    label: "Where are my orders?",
    prompt: "Where are my orders right now, and how long does standard shipping take?",
  },
  {
    label: "Show my orders",
    prompt:
      "Show me all my orders with their status and shipping details, and mark which ones are returnable.",
  },
  {
    label: "Cancel my order",
    prompt: "Can I still cancel my last order, and what's the cancellation policy?",
  },
  {
    label: "Orders with warranty",
    prompt: "Which of my orders are still under warranty, and how do I file a claim?",
  },
];

const newConversationId = () =>
  crypto.randomUUID?.() ?? `conv-${Date.now()}-${Math.random().toString(16).slice(2)}`;

interface Msg {
  role: "customer" | "agent";
  text: string;
  usage?: Usage;
  flagConfig?: string;
  governance?: Governance;
}

export default function App() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [checking, setChecking] = useState(true);

  // On load, restore an existing valid session (if any). Derive the profile
  // from the restored token itself — currentProfile() reads signInUserSession
  // which is null right after a page reload, which would bounce a logged-in
  // user back to the auth page.
  useEffect(() => {
    getIdToken().then((token) => {
      if (token) {
        try {
          const c = JSON.parse(atob(token.split(".")[1]));
          setProfile({
            email: c.email ?? "",
            name: c.name ?? c.email ?? "Customer",
            tier: c["custom:tier"] ?? "basic",
          });
        } catch {
          setProfile(currentProfile());
        }
      }
      setChecking(false);
    });
  }, []);

  if (checking) return null;
  if (!profile) return <AuthPage onSignedIn={setProfile} />;
  return (
    <Chat
      profile={profile}
      onSignOut={() => {
        logout();
        setProfile(null);
      }}
    />
  );
}

function Chat({ profile, onSignOut }: { profile: Profile; onSignOut: () => void }) {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState(newConversationId);
  // Source of truth for the id sent to the agent. A ref is read synchronously in
  // send(), so a "New conversation" click can't be lost to React's async state /
  // a stale closure — which would reuse the old AgentCore session and leak the
  // previous conversation's memory into the new chat.
  const conversationIdRef = useRef(conversationId);
  // Auto-scroll to the newest message. A sentinel at the end of the thread is
  // scrolled into view whenever messages change or the agent starts/stops — more
  // reliable than setting scrollTop, since Container fitHeight owns the scroller.
  const threadEnd = useRef<HTMLDivElement | null>(null);
  const threadBody = useRef<HTMLDivElement | null>(null);
  const scrollToEnd = () => threadEnd.current?.scrollIntoView({ block: "end" });
  // Scroll on message/busy change AND keep scrolling while the content height
  // changes (the stats footer renders a frame after the reply, which otherwise
  // leaves us scrolled short). A ResizeObserver on the thread body catches that.
  useEffect(() => {
    scrollToEnd();
    const el = threadBody.current;
    if (!el) return;
    const ro = new ResizeObserver(() => scrollToEnd());
    ro.observe(el);
    // Stop observing shortly after things settle so we don't fight user scroll.
    const stop = setTimeout(() => ro.disconnect(), 1200);
    return () => {
      ro.disconnect();
      clearTimeout(stop);
    };
  }, [messages, busy]);

  async function send(preset?: string) {
    const text = (preset ?? value).trim();
    if (!text || busy) return;
    setValue("");
    setError(null);
    setInfo(null);
    setMessages((m) => [...m, { role: "customer", text }]);
    setBusy(true);
    try {
      const res = await chat(text, conversationIdRef.current);
      const gov = res.governance;
      // Hard stops that produce no usable answer (e.g. per-session cap) are shown
      // as the red error banner — same treatment as quota (403) / throttle (429) —
      // rather than a chat bubble, so all governance blocks look consistent.
      if (gov?.stopped && gov.reason === "session_budget") {
        setError(
          gov.detail
            ? `Conversation limit reached — ${gov.detail}. Start a new conversation to continue.`
            : "This conversation reached its token limit. Start a new conversation to continue.",
        );
      } else if (gov?.reason === "limit_output_tokens") {
        // Per-response output cap cut the answer off. Show the red banner (like
        // other governance blocks) AND still add a bubble so the usage/cost
        // footer for this (real, billed) turn stays visible separately.
        setError(
          "The response was cut short by the per-response output-token limit. " +
            "Ask a more specific question, or an operator can raise the output cap.",
        );
        setMessages((m) => [
          ...m,
          {
            role: "agent",
            text: "",
            usage: res.usage,
            flagConfig: res.flagConfig,
            governance: res.governance,
          },
        ]);
      } else {
        // SOFT caps (tool_cap / iteration / agent-call token cap): the agent still
        // produced a usable answer, just trimmed its work. Show the answer AS a
        // normal bubble + a BLUE INFO banner naming exactly which limit applied.
        const softMsg = softCapMessage(gov);
        if (softMsg) setInfo(softMsg);
        setMessages((m) => [
          ...m,
          {
            role: "agent",
            text: res.reply,
            usage: res.usage,
            flagConfig: res.flagConfig,
            governance: res.governance,
          },
        ]);
      }
    } catch (e) {
      if (e instanceof GovernanceBlockedError) {
        // In-loop quota/throttle block -> friendly red banner, not a raw error.
        const extra = e.kind === "throttle" && e.retryAfter ? ` Try again in ${e.retryAfter}s.` : "";
        setError(e.message + extra);
      } else {
        setError((e as Error).message);
      }
    } finally {
      setBusy(false);
    }
  }

  function newConversation() {
    const id = newConversationId();
    conversationIdRef.current = id; // rotate synchronously so the next send uses it
    setMessages([]);
    setError(null);
    setInfo(null);
    setConversationId(id);
  }

  return (
    <>
      <TopNavigation
        identity={{ href: "#", title: "E-commerce Support" }}
        utilities={[
          { type: "button", text: `${profile.name} · ${profile.tier}`, iconName: "user-profile" },
          { type: "button", text: "Sign out", onClick: onSignOut },
        ]}
      />
      <AppLayout
        toolsHide
        navigationHide
        content={
          <ContentLayout
            header={
              <Header
                variant="h1"
                description="Ask about orders, returns, refunds, shipping, or your account."
                actions={
                  <SpaceBetween direction="horizontal" size="s" alignItems="center">
                    <span style={{ color: "#5f6b7a", fontSize: 13 }}>{profile.email}</span>
                    <Badge color={profile.tier === "prime" ? "green" : "grey"}>{profile.tier}</Badge>
                    <Button iconName="add-plus" onClick={newConversation}>
                      New conversation
                    </Button>
                  </SpaceBetween>
                }
              >
                Support chat
              </Header>
            }
          >
            <div style={{ maxWidth: 982, margin: "0 auto", width: "100%", height: "calc(100vh - 150px)" }}>
              <Container
                fitHeight
                footer={
                  <SpaceBetween size="xs">
                    {error && <Alert type="error">{error}</Alert>}
                    {info && (
                      <Alert type="info" dismissible onDismiss={() => setInfo(null)}>
                        {info}
                      </Alert>
                    )}
                    {/* Starter questions — outlined pill buttons (match Advisor). */}
                    <div style={{ display: "flex", gap: 8, flexWrap: "nowrap", overflowX: "auto" }}>
                      {STARTERS.map((s) => (
                        <Button key={s.label} onClick={() => send(s.prompt)} disabled={busy}>
                          <span style={{ fontSize: 12, whiteSpace: "nowrap" }}>{s.label}</span>
                        </Button>
                      ))}
                    </div>
                    {/* Input + Send button (same as the Advisor). */}
                    <div style={{ display: "flex", gap: 8 }}>
                      <div style={{ flex: 1 }}>
                        <Input
                          value={value}
                          onChange={(e) => setValue(e.detail.value)}
                          onKeyDown={(e) => {
                            if (e.detail.key === "Enter") send();
                          }}
                          placeholder={busy ? "Agent is thinking…" : "Type your message"}
                          disabled={busy}
                        />
                      </div>
                      <Button variant="primary" iconName="send" onClick={() => send()} disabled={busy}>
                        Send
                      </Button>
                    </div>
                  </SpaceBetween>
                }
              >
                {/* Message history — Container fitHeight provides the scroller. */}
                <div ref={threadBody}>
                  {messages.length === 0 ? (
                    <Box textAlign="center" color="text-body-secondary" padding="l">
                      <b>Ask about your orders, returns, refunds, shipping, or your account.</b>
                      <Box variant="p" color="text-body-secondary">
                        Pick a suggested question below, or type your own.
                      </Box>
                    </Box>
                  ) : (
                    <SpaceBetween size="l">
                      {messages.map((m, i) => (
                        <Bubble key={i} msg={m} />
                      ))}
                    </SpaceBetween>
                  )}
                  {busy && (
                    <Box color="text-body-secondary" padding={{ top: "s" }}>
                      <Spinner />
                      &nbsp;Agent is working…
                    </Box>
                  )}
                  <div ref={threadEnd} style={{ height: 1 }} />
                </div>
              </Container>
            </div>
          </ContentLayout>
        }
      />
    </>
  );
}

/** Cache status for the summary line, as JSX with the "Cache" key bolded:
 *  **Cache** ON (53.4% hit, tokens: 1,234) when on, else **Cache** OFF. */
function cacheParts(flagConfig: string | undefined, usage: Usage) {
  const on = !!flagConfig && new Set(flagConfig.split("+")).has("prompt_cache=on");
  if (!on)
    return (
      <>
        <b>Cache</b> OFF
      </>
    );
  const hit = ((usage.cache_hit_ratio ?? 0) * 100).toFixed(1);
  const cached = (usage.cache_read_tokens ?? 0).toLocaleString();
  return (
    <>
      <b>Cache</b> ON ({hit}% hit, tokens: {cached})
    </>
  );
}

/** The active tokenomics patterns for a turn (excluding cache), as JSX with each
 *  pattern name (the key) bolded. Parsed from the agent's flag_config signature. */
function patternParts(flagConfig?: string) {
  if (!flagConfig) return null;
  const active = new Set(flagConfig.split("+"));
  const memory = active.has("memory=on");
  const ctx = active.has("context_management=on");
  const routing = active.has("model_routing=on");
  return (
    <>
      {" · "}
      <b>Memory</b> {memory ? "ON" : "OFF"}
      {" · "}
      <b>Context Mgmt</b> {ctx ? "ON" : "OFF"}
      {" · "}
      <b>Model Routing</b> {routing ? "ON" : "OFF"}
    </>
  );
}

/** Message for a SOFT cap that still produced an answer (blue info banner).
 *  Names the exact limit so the operator/customer knows what applied. Returns
 *  null for reasons handled elsewhere (red banner) or when nothing acted. */
function softCapMessage(gov?: Governance): string | null {
  if (!gov || (!gov.stopped && !gov.limited)) return null;
  switch (gov.reason) {
    case "tool_cap":
      return "Tool-call limit reached (max tool calls per turn) — the agent skipped additional lookups, so this answer may be partial.";
    case "limit_turns":
      return "Iteration limit reached (max iterations) — the agent stopped reasoning early, so this answer may be partial.";
    case "limit_total_tokens":
      return "Per-agent token limit reached (max tokens per agent call) — the agent stopped early, so this answer may be partial.";
    default:
      return null; // session/quota/throttle/output → handled by the red banner
  }
}

function Bubble({ msg }: { msg: Msg }) {
  const isCustomer = msg.role === "customer";
  return (
    <div style={{ display: "flex", justifyContent: isCustomer ? "flex-end" : "flex-start", width: "100%" }}>
      <div
        style={{
          maxWidth: "80%",
          background: isCustomer ? "#0972d3" : "#f2f3f3",
          color: isCustomer ? "#ffffff" : "#16191f",
          borderRadius: isCustomer ? "14px 14px 4px 14px" : "14px 14px 14px 4px",
          padding: "10px 14px",
          boxShadow: "0 1px 2px rgba(0,0,0,0.08)",
        }}
      >
        <div style={{ whiteSpace: "pre-wrap", fontSize: 14.5, lineHeight: 1.5 }}>{msg.text}</div>
        {msg.usage && typeof msg.usage.cost_usd === "number" && (
          <div
            style={{
              marginTop: 6,
              paddingTop: 5,
              borderTop: `1px solid ${isCustomer ? "rgba(255,255,255,0.25)" : "#d5dbdb"}`,
              fontSize: 11.5,
              lineHeight: 1.4,
              fontFamily: "monospace",
            }}
          >
            {/* Cost + cache + active patterns — one amber line, keys bolded. */}
            <div style={{ color: isCustomer ? "#ffe4b3" : "#b7791f" }}>
              <b>Total:</b> ${(msg.usage.cost_usd ?? 0).toFixed(4)} · {cacheParts(msg.flagConfig, msg.usage)}
              {patternParts(msg.flagConfig)}
            </div>
            {/* Per-agent breakdown — single-column rows with a green left bar. */}
            {msg.usage.by_agent && (
              <AgentBreakdown
                byAgent={msg.usage.by_agent}
                byComponent={msg.usage.by_component}
                isCustomer={isCustomer}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function AgentBreakdown({
  byAgent,
  byComponent,
  isCustomer,
}: {
  byAgent: Record<string, AgentUsage>;
  byComponent?: Record<string, AgentUsage>;
  isCustomer: boolean;
}) {
  // The summarizer is an internal component of the orchestrator (context
  // management): its cost is inside the orchestrator's line, shown as "incl.".
  const summarizer = byComponent?.summarizer;
  const names = [
    ...AGENT_ORDER.filter((n) => byAgent[n]),
    ...Object.keys(byAgent).filter((n) => !AGENT_ORDER.includes(n)),
  ];
  if (names.length === 0) return null;
  return (
    <div
      style={{
        marginTop: 4,
        paddingLeft: 7,
        borderLeft: `2px solid ${isCustomer ? "#8fe3c6" : "#1f9d76"}`,
        color: isCustomer ? "#d7f5e9" : "#0e7f6e",
        // 2x2 grid: two equal columns.
        display: "grid",
        gridTemplateColumns: "1fr 1fr",
        columnGap: 14,
        rowGap: 1,
      }}
    >
      {names.map((n) => {
        const a = byAgent[n];
        return (
          <div key={n} style={{ whiteSpace: "nowrap" }}>
            <b>{n}</b> · {modelLabel(a.model_id ?? "")} · tokens: {agentTokens(a).toLocaleString()} · $
            {(a.cost_usd ?? 0).toFixed(6)}
            {n === "orchestrator" && summarizer && (
              <> (incl. summ ${(summarizer.cost_usd ?? 0).toFixed(6)})</>
            )}
          </div>
        );
      })}
    </div>
  );
}
