import { useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Toggle from "@cloudscape-design/components/toggle";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Button from "@cloudscape-design/components/button";
import { api, FlagConfig, FeatureFlag } from "../api";
import { useRole } from "../role";

// Defaults mirror ContextManagementParams in src/shared/flags_schema.py.
const CTX_DEFAULTS = { window_size: 12, compress_at_tokens: 20000 };
const CTX_LIMITS = { window_size: 2, compress_at_tokens: 500 };

export default function Flags() {
  const { canWrite } = useRole();
  const [flags, setFlags] = useState<FlagConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Draft value for the active context manager's tunable (saved on "Save").
  const [ctxDraft, setCtxDraft] = useState<string>("");

  useEffect(() => {
    api
      .getFlags()
      .then(setFlags)
      .catch((e) => setError(e.message));
  }, []);

  async function toggleCache(enabled: boolean) {
    if (!flags) return;
    setCtxDraft(""); // the tunable shown depends on the cache flag
    setSaving(true);
    setError(null);
    try {
      const updated = await api.putFlags({
        prompt_cache: { enabled, params: flags.prompt_cache.params },
      });
      setFlags(updated);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function toggleMemory(enabled: boolean) {
    if (!flags) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await api.putFlags({
        memory: { enabled, params: flags.memory.params },
      });
      setFlags(updated);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function updateCtx(next: FeatureFlag) {
    if (!flags) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await api.putFlags({ context_management: next });
      setFlags(updated);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function toggleRouting(enabled: boolean) {
    if (!flags) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await api.putFlags({
        model_routing: { enabled, params: flags.model_routing?.params ?? {} },
      });
      setFlags(updated);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  if (error)
    return (
      <Alert type="error" header="Could not load flags">
        {error}
      </Alert>
    );
  if (!flags) return <Spinner size="large" />;

  const on = flags.prompt_cache.enabled;
  const memOn = flags.memory.enabled;
  const ctxFlag = flags.context_management ?? { enabled: false, params: {} };
  const ctxOn = ctxFlag.enabled;
  const routingOn = (flags.model_routing ?? { enabled: false }).enabled;
  // Strategy is always "auto": sliding window when caching is off, summarize when on.
  const ctxResolved = on ? "summarize" : "sliding window";
  // The one tunable for the active manager: window_size (cache OFF) or
  // compress_at_tokens (cache ON). Stored in the flag's params; defaults load
  // when unset.
  const ctxKey = on ? "compress_at_tokens" : "window_size";
  const ctxSaved = Number(ctxFlag.params?.[ctxKey] ?? CTX_DEFAULTS[ctxKey]);
  const ctxValue = ctxDraft === "" ? String(ctxSaved) : ctxDraft;
  const ctxNum = Number(ctxValue);
  const ctxInvalid = !Number.isInteger(ctxNum) || ctxNum < CTX_LIMITS[ctxKey];

  // One list row per pattern: bold name + description on the left, toggle + status
  // on the right; context management renders its tunable below when enabled.
  const row = (
    key: string,
    name: string,
    description: string,
    control: React.ReactNode,
    status: React.ReactNode,
    extra?: React.ReactNode,
  ) => (
    // Row divider is a CSS top-border on every row except the first
    // (.tk-flags-list .tk-flag-row:first-child), so no per-row style is needed.
    <div key={key} className="tk-flag-row">
      <div className="tk-flag-row__main">
        <div className="tk-flag-row__body">
          <Box variant="strong">{name}</Box>
          <Box color="text-body-secondary" fontSize="body-s" padding={{ top: "xxs" }}>
            {description}
          </Box>
          <Box padding={{ top: "xs" }}>{status}</Box>
        </div>
        <div className="tk-flag-row__control">{control}</div>
      </div>
      {extra && <div className="tk-flag-row__extra">{extra}</div>}
    </div>
  );

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header variant="h2" actions={saving ? <Spinner /> : undefined}>
            Tokenomics features
          </Header>
        }
      >
        <div className="tk-flags-list">
          {/* Conversation memory */}
          {row(
            "memory",
            "Conversation memory",
            "Give the orchestrator AgentCore short-term memory so it recalls earlier turns. Multi-turn context accumulates per session (history is re-sent) — pair with prompt-cache to offset the cost.",
            <Toggle
              checked={memOn}
              disabled={saving || !canWrite}
              onChange={(e) => toggleMemory(e.detail.checked)}
            >
              {memOn ? "Enabled" : "Disabled"}
            </Toggle>,
            memOn ? (
              <StatusIndicator type="success">
                On — remembers earlier turns (short-term). Applies next session.
              </StatusIndicator>
            ) : (
              <StatusIndicator type="stopped">Off — baseline. Each turn is stateless.</StatusIndicator>
            ),
          )}

          {/* Prompt-cache placement */}
          {row(
            "prompt_cache",
            "Prompt-cache placement",
            "Reuse the stable prompt prefix (system + tool schemas) across calls so Bedrock bills cached tokens at a reduced rate.",
            <Toggle
              checked={on}
              disabled={saving || !canWrite}
              onChange={(e) => toggleCache(e.detail.checked)}
            >
              {on ? "Enabled" : "Disabled"}
            </Toggle>,
            on ? (
              <StatusIndicator type="success">
                On — caching applies next turn; cache-read tokens billed at a discount.
              </StatusIndicator>
            ) : (
              <StatusIndicator type="stopped">
                Off — baseline. Every request reprocesses the full prompt.
              </StatusIndicator>
            ),
          )}

          {/* Context management (with tunable) */}
          {row(
            "context_management",
            "Context management",
            "Keep the orchestrator's history lean — earlier tool calls/answers are masked; with caching off a sliding window keeps the latest messages, with caching on history is summarized rarely (by Haiku) only when the prompt gets large. Only has an effect with memory on.",
            <Toggle
              checked={ctxOn}
              disabled={saving || !canWrite}
              onChange={(e) => updateCtx({ enabled: e.detail.checked, params: ctxFlag.params })}
            >
              {ctxOn ? "Enabled" : "Disabled"}
            </Toggle>,
            ctxOn ? (
              <StatusIndicator type={memOn ? "success" : "warning"}>
                {memOn
                  ? `On — ${ctxResolved}, applies next turn.`
                  : "On, but memory is off — nothing to trim."}
              </StatusIndicator>
            ) : (
              <StatusIndicator type="stopped">
                Off — baseline. Strands default (trims at 40 messages / on overflow).
              </StatusIndicator>
            ),
            ctxOn ? (
              <FormField
                label={on ? "Max tokens before summarizing" : "Window size (messages)"}
                description={
                  on
                    ? `Caching is on, so history is summarized once the whole prompt reaches this many tokens. Keep well above ~1,500 or it re-summarizes every call. Default ${CTX_DEFAULTS.compress_at_tokens.toLocaleString()}.`
                    : `Caching is off, so the orchestrator keeps only this many recent messages (~4 per tool-using turn). Default ${CTX_DEFAULTS.window_size}.`
                }
                errorText={ctxInvalid ? `Enter a whole number of at least ${CTX_LIMITS[ctxKey]}.` : undefined}
                secondaryControl={
                  <Button
                    disabled={saving || !canWrite || ctxInvalid || ctxNum === ctxSaved}
                    onClick={async () => {
                      await updateCtx({ enabled: ctxOn, params: { ...ctxFlag.params, [ctxKey]: ctxNum } });
                      setCtxDraft("");
                    }}
                  >
                    Save
                  </Button>
                }
              >
                <Input
                  type="number"
                  inputMode="numeric"
                  value={ctxValue}
                  disabled={saving || !canWrite}
                  onChange={(e) => setCtxDraft(e.detail.value)}
                />
              </FormField>
            ) : undefined,
          )}

          {/* Model routing */}
          {row(
            "model_routing",
            "Model routing",
            "Pick the model per step by complexity instead of a fixed model per agent. The orchestrator (Sonnet) routes simple steps down to Haiku via an LLM judge (Nova Lite); specialists (Haiku) escalate hard steps up to Sonnet. On a classifier miss it keeps the safe default.",
            <Toggle
              checked={routingOn}
              disabled={saving || !canWrite}
              onChange={(e) => toggleRouting(e.detail.checked)}
            >
              {routingOn ? "Enabled" : "Disabled"}
            </Toggle>,
            routingOn ? (
              <StatusIndicator type="success">
                On — each agent picks its model by step complexity next session.
              </StatusIndicator>
            ) : (
              <StatusIndicator type="stopped">
                Off — baseline. Each agent runs its single static model.
              </StatusIndicator>
            ),
          )}
        </div>
      </Container>

      <Alert type="info" header="How the toggles apply">
        The agent reads these flags from the shared store. Flipping one here changes behavior on the agent's
        next turn/session — no redeploy. Baseline (off) vs enabled (on) is exactly what the benchmark compares
        on identical inputs.
      </Alert>
    </SpaceBetween>
  );
}
