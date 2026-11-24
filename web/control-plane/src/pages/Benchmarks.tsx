import { useEffect, useRef, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Checkbox from "@cloudscape-design/components/checkbox";
import Grid from "@cloudscape-design/components/grid";
import Link from "@cloudscape-design/components/link";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Slider from "@cloudscape-design/components/slider";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Spinner from "@cloudscape-design/components/spinner";
import Pagination from "@cloudscape-design/components/pagination";
import LineChart from "@cloudscape-design/components/line-chart";
import { useCollection } from "@cloudscape-design/collection-hooks";

import {
  api,
  type Benchmark,
  type BenchSummary,
  type RunSide,
  type RunStatus,
  type FeatureFlag,
  type FlagConfig,
} from "../api";
import { useRole } from "../role";

// Feature catalog drives the A/B selectors and feature badges. Mirrors
// src/shared/flags_schema.py; the catalog is the one place to add a new
// strategy's checkbox.
const FEATURE_CATALOG = [
  {
    id: "memory",
    name: "Short-term memory",
    description:
      "Give the orchestrator AgentCore short-term memory so it recalls earlier turns. Multi-turn context accumulates per session (history is re-sent) — pair with prompt-cache to offset the cost.",
  },
  {
    id: "prompt_cache",
    name: "Prompt caching",
    description:
      "Bedrock caches the stable prefix (system prompt + tool schemas). Cache reads replace full input re-sends on repeat turns.",
  },
  {
    id: "context_management",
    name: "Context management",
    description:
      "Keeps the orchestrator's history lean (needs memory). Auto: sliding window when caching is off, summarize rarely when caching is on.",
  },
  {
    id: "model_routing",
    name: "Model routing",
    description:
      "Routes each step to a model tier by complexity: the orchestrator drops simple steps to Haiku (Nova-Lite judge), specialists escalate hard steps to Sonnet (heuristic). Fails to the safe default.",
  },
] as const;

const featureName = (id: string) => FEATURE_CATALOG.find((f) => f.id === id)?.name ?? id;
const featureNames = (ids: string[]) => ids.map(featureName);

const usd = (n: number | undefined | null) => (typeof n === "number" ? `$${n.toFixed(4)}` : "—");
const pct = (n: number | undefined | null) => (typeof n === "number" ? `${(n * 100).toFixed(1)}%` : "—");
const compact = (n: number | undefined | null) =>
  typeof n !== "number"
    ? "—"
    : n >= 1_000_000
      ? `${(n / 1_000_000).toFixed(1)}M`
      : n >= 1_000
        ? `${(n / 1_000).toFixed(0)}k`
        : `${n}`;

function statusIndicator(s: RunStatus) {
  if (s === "COMPLETED") return <StatusIndicator type="success">Completed</StatusIndicator>;
  if (s === "FAILED") return <StatusIndicator type="error">Failed</StatusIndicator>;
  return <StatusIndicator type="in-progress">Running</StatusIndicator>;
}

function defaultRunName() {
  const d = new Date();
  const p = (n: number) => `${n}`.padStart(2, "0");
  return `benchmark-${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
}

// Build a FlagConfig fragment from a selected set of feature ids.
function flagsFromSet(ids: Set<string>): Partial<FlagConfig> {
  const flags: Record<string, FeatureFlag> = {};
  for (const id of ids) flags[id] = { enabled: true, params: {} };
  return flags as Partial<FlagConfig>;
}

const POLL_MS = 2000;

export default function Benchmarks() {
  const { canWrite } = useRole();
  const [history, setHistory] = useState<BenchSummary[]>([]);
  const [name, setName] = useState(defaultRunName());
  const [sessions, setSessions] = useState(3);
  const [turns, setTurns] = useState(3);
  // Memory is always on by default; prompt_cache is the candidate pattern.
  const [selectedA, setSelectedA] = useState<Set<string>>(new Set(["memory"]));
  const [selectedB, setSelectedB] = useState<Set<string>>(new Set(["memory", "prompt_cache"]));
  const [busy, setBusy] = useState(false);
  // Live progress of the in-flight benchmark (the whole record, polled).
  const [running, setRunning] = useState<Benchmark | null>(null);
  // The benchmark currently loaded in the results panel.
  const [openId, setOpenId] = useState<string | null>(null);
  const [openBenchmark, setOpenBenchmark] = useState<Benchmark | null>(null);
  const [loadingResult, setLoadingResult] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pollRef = useRef<number | null>(null);
  const clearPoll = () => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  async function refreshHistory() {
    try {
      const { runs } = await api.listBenchmarks();
      setHistory(runs);
      return runs;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return [];
    }
  }

  useEffect(() => {
    refreshHistory();
    return clearPoll;
  }, []);

  async function openById(id: string | null) {
    if (!id) {
      setOpenId(null);
      setOpenBenchmark(null);
      return;
    }
    setLoadingResult(true);
    setOpenId(id);
    try {
      setOpenBenchmark(await api.getBenchmark(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingResult(false);
    }
  }

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, id: string) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setter(next);
  };

  // Run-history pagination (standard Cloudscape, 20 rows per page).
  const { items: pageItems, paginationProps } = useCollection(history, {
    pagination: { pageSize: 20 },
  });

  // Poll the ONE benchmark record until it is terminal.
  function pollBenchmark(id: string) {
    clearPoll();
    pollRef.current = window.setInterval(async () => {
      let bm: Benchmark;
      try {
        bm = await api.getBenchmark(id);
      } catch (e) {
        clearPoll();
        setBusy(false);
        setRunning(null);
        setError(e instanceof Error ? e.message : String(e));
        return;
      }
      setRunning(bm);
      if (bm.status === "COMPLETED") {
        clearPoll();
        setRunning(null);
        setBusy(false);
        setName(defaultRunName());
        await refreshHistory();
        setOpenId(bm.run_id);
        setOpenBenchmark(bm);
      } else if (bm.status === "FAILED") {
        clearPoll();
        setRunning(null);
        setBusy(false);
        setError(bm.error || `Benchmark ${bm.name} failed.`);
        await refreshHistory();
      }
    }, POLL_MS);
  }

  async function runBenchmark() {
    const runName = name.trim() || defaultRunName();
    setError(null);
    setBusy(true);
    setOpenId(null);
    setOpenBenchmark(null);
    try {
      const bm = await api.startBenchmark({
        name: runName,
        scenario: { sessions, turns },
        run_a: { flags: flagsFromSet(selectedA) },
        run_b: { flags: flagsFromSet(selectedB) },
      });
      setRunning(bm);
      pollBenchmark(bm.run_id);
    } catch (e) {
      setBusy(false);
      setRunning(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)} header="Benchmark error">
          {error}
        </Alert>
      )}

      {/* ============ 1. Configure ============ */}
      <div
        style={{
          // Dim (not blur) while running so the selected checkboxes stay readable;
          // the running section below also lists each run's enabled features.
          opacity: busy ? 0.75 : 1,
          pointerEvents: busy ? "none" : "auto",
          transition: "filter 0.15s ease, opacity 0.15s ease",
        }}
        aria-disabled={busy}
      >
        <Container
          header={
            <Header
              variant="h2"
              description="Benchmarks run ungoverned (quotas and caps are disabled) and never touch your live flag or governance settings."
            >
              Configure benchmark run
            </Header>
          }
        >
          <SpaceBetween size="l">
            <FormField label="Run name" description="Shown in the run history. Defaults to a timestamp.">
              <Input
                value={name}
                onChange={(e) => setName(e.detail.value)}
                placeholder={defaultRunName()}
                disabled={busy}
              />
            </FormField>

            <Box>
              <Box variant="awsui-key-label" padding={{ bottom: "xs" }}>
                Multi-turn conversation settings
              </Box>
              <Grid gridDefinition={[{ colspan: { default: 12, s: 6 } }, { colspan: { default: 12, s: 6 } }]}>
                <FormField
                  label={`No. of parallel sessions: ${sessions}`}
                  description="Independent conversations run in parallel (1–5)."
                >
                  <Slider
                    value={sessions}
                    onChange={(e) => setSessions(e.detail.value)}
                    min={1}
                    max={5}
                    step={1}
                    tickMarks
                    disabled={busy}
                  />
                </FormField>
                <FormField
                  label={`No. of turns per session: ${turns}`}
                  description="Sequential multi-turn messages per session (1–5)."
                >
                  <Slider
                    value={turns}
                    onChange={(e) => setTurns(e.detail.value)}
                    min={1}
                    max={5}
                    step={1}
                    tickMarks
                    disabled={busy}
                  />
                </FormField>
              </Grid>
              <Box variant="small" color="text-body-secondary" padding={{ top: "xs" }}>
                {sessions} session{sessions === 1 ? "" : "s"} × {turns} turn{turns === 1 ? "" : "s"} ·{" "}
                {sessions * turns} total turns · governance disabled
              </Box>
            </Box>

            <Grid gridDefinition={[{ colspan: { default: 12, s: 6 } }, { colspan: { default: 12, s: 6 } }]}>
              <FeatureColumn
                tag="A"
                title="Baseline (Run A)"
                description="Reference run. Default: memory on, everything else off."
                selected={selectedA}
                onToggle={(id) => toggle(selectedA, setSelectedA, id)}
                disabled={busy}
              />
              <FeatureColumn
                tag="B"
                title="Candidate (Run B)"
                description="Features the control plane enables for this run only."
                selected={selectedB}
                onToggle={(id) => toggle(selectedB, setSelectedB, id)}
                disabled={busy}
              />
            </Grid>
          </SpaceBetween>
        </Container>
      </div>

      {/* Run button stays OUTSIDE the dimmed wrapper so it (and its loader) stay
          at full opacity while the config fields above are dimmed during a run. */}
      <Button
        variant="primary"
        iconName="play"
        loading={busy}
        disabled={busy || !canWrite}
        onClick={runBenchmark}
      >
        Run benchmark
      </Button>

      {/* ============ 2. Running progress (two side-by-side bars: A then B) ============ */}
      {running && running.progress && (
        <Container
          header={
            <Header
              variant="h2"
              description={`${running.scenario.sessions} sessions × ${running.scenario.turns} turns · governance disabled`}
            >
              Running benchmark — {running.name}
            </Header>
          }
        >
          <Grid gridDefinition={[{ colspan: { default: 12, m: 6 } }, { colspan: { default: 12, m: 6 } }]}>
            <PhaseBar
              tag="A"
              title="Baseline (Run A)"
              done={running.progress.a.done}
              total={running.progress.a.total}
              state={running.progress.phase === "a" ? "in-progress" : "success"}
              features={running.run_a?.features}
            />
            <PhaseBar
              tag="B"
              title="Candidate (Run B)"
              done={running.progress.b.done}
              total={running.progress.b.total}
              state={
                running.progress.phase === "a"
                  ? "pending"
                  : running.progress.phase === "b"
                    ? "in-progress"
                    : "success"
              }
              features={running.run_b?.features}
            />
          </Grid>
        </Container>
      )}

      {/* ============ 3. Result (loaded benchmark) ============ */}
      {!running && loadingResult && (
        <Container header={<Header variant="h2">Benchmark Results</Header>}>
          <Box textAlign="center" padding="xxl" color="text-body-secondary">
            <SpaceBetween size="s" alignItems="center">
              <Spinner size="large" />
              <span>Loading run…</span>
            </SpaceBetween>
          </Box>
        </Container>
      )}
      {!running && !loadingResult && openBenchmark && <BenchmarkResult bm={openBenchmark} />}

      {/* ============ 4. Run history (one row per benchmark) ============ */}
      <Table<BenchSummary>
        header={
          <Header
            variant="h2"
            counter={`(${history.length})`}
            description="Select a benchmark to load its summary above."
            actions={
              <Button
                iconName="refresh"
                loading={refreshing}
                onClick={async () => {
                  setRefreshing(true);
                  try {
                    await refreshHistory();
                  } finally {
                    setRefreshing(false);
                  }
                }}
              >
                Refresh
              </Button>
            }
          >
            Run history
          </Header>
        }
        variant="container"
        selectionType="single"
        selectedItems={history.filter((r) => r.run_id === openId)}
        onSelectionChange={(e) => openById(e.detail.selectedItems[0]?.run_id ?? null)}
        trackBy="run_id"
        columnDefinitions={[
          {
            id: "name",
            header: "Benchmark",
            cell: (r) => <Link onFollow={() => openById(r.run_id)}>{r.name}</Link>,
            isRowHeader: true,
          },
          {
            id: "candidate",
            header: "Candidate features",
            cell: (r) =>
              (r.features_b?.length ?? 0) === 0 ? (
                <Badge color="grey">none</Badge>
              ) : (
                <SpaceBetween direction="horizontal" size="xxs">
                  {featureNames(r.features_b ?? []).map((n) => (
                    <Badge key={n} color="blue">
                      {n}
                    </Badge>
                  ))}
                </SpaceBetween>
              ),
          },
          {
            id: "scn",
            header: "Scenario",
            cell: (r) => `${r.scenario?.sessions ?? "?"}×${r.scenario?.turns ?? "?"}`,
          },
          { id: "status", header: "Status", cell: (r) => statusIndicator(r.status) },
          {
            id: "gain",
            header: "Gain",
            cell: (r) => {
              const g = r.comparison?.gain_pct;
              if (typeof g !== "number") return "—";
              const cheaper = g >= 0;
              return (
                <Box color={cheaper ? "text-status-success" : "text-status-error"}>
                  {cheaper ? "▼" : "▲"} {pct(Math.abs(g))}
                </Box>
              );
            },
          },
          { id: "cost_a", header: "Cost (A)", cell: (r) => usd(r.cost_a) },
          { id: "cost_b", header: "Cost (B)", cell: (r) => usd(r.cost_b) },
          {
            id: "when",
            header: "Finished",
            cell: (r) => (r.completed_at ? new Date(r.completed_at).toLocaleString() : "—"),
          },
        ]}
        items={pageItems}
        pagination={<Pagination {...paginationProps} />}
        empty={<Box>No benchmarks yet.</Box>}
      />
    </SpaceBetween>
  );
}

/* ======================= running-phase progress bar ======================= */

function PhaseBar({
  tag,
  title,
  done,
  total,
  state,
  features,
}: {
  tag: "A" | "B";
  title: string;
  done: number;
  total: number;
  state: "pending" | "in-progress" | "success";
  features?: string[];
}) {
  const pctValue = total > 0 ? (done / total) * 100 : 0;
  const status =
    state === "pending" ? (
      <StatusIndicator type="pending">Waiting</StatusIndicator>
    ) : state === "success" ? (
      <StatusIndicator type="success">Done</StatusIndicator>
    ) : (
      <StatusIndicator type="in-progress">Running</StatusIndicator>
    );
  const names = featureNames(features ?? []);
  return (
    <Container
      header={
        <Header variant="h3" actions={status}>
          <SpaceBetween direction="horizontal" size="xs">
            <Badge color={tag === "A" ? "grey" : "green"}>Run {tag}</Badge>
            <span>{title}</span>
          </SpaceBetween>
        </Header>
      }
    >
      <SpaceBetween size="xs">
        {/* The features this run enabled — so the choices stay visible while the
            config panel above is blurred during the run. */}
        <Box variant="small" color="text-body-secondary">
          Enabled features:{" "}
          {names.length === 0 ? (
            <Badge color="grey">none</Badge>
          ) : (
            <SpaceBetween direction="horizontal" size="xxs">
              {names.map((n) => (
                <Badge key={n} color="blue">
                  {n}
                </Badge>
              ))}
            </SpaceBetween>
          )}
        </Box>
        <div className="bench-progress">
          <ProgressBar
            value={state === "pending" ? 0 : state === "success" ? 100 : pctValue}
            label={state === "pending" ? "Waiting for Run A to finish" : `${done} / ${total} conversations`}
            description={state === "success" ? "All conversations complete" : undefined}
          />
        </div>
      </SpaceBetween>
    </Container>
  );
}

/* ======================= feature selector column ======================= */

function FeatureColumn({
  tag,
  title,
  description,
  selected,
  onToggle,
  disabled,
}: {
  tag: "A" | "B";
  title: string;
  description: string;
  selected: Set<string>;
  onToggle: (id: string) => void;
  disabled?: boolean;
}) {
  return (
    <Container
      header={
        <Header
          variant="h3"
          description={description}
          actions={<Badge color={tag === "A" ? "grey" : "green"}>Run {tag}</Badge>}
        >
          {title}
        </Header>
      }
    >
      <SpaceBetween size="xs">
        {FEATURE_CATALOG.map((f) => (
          <Box key={f.id} padding="xs">
            <Checkbox checked={selected.has(f.id)} onChange={() => onToggle(f.id)} disabled={disabled}>
              <Box variant="strong">{f.name}</Box>
              <Box variant="small" color="text-body-secondary">
                {f.description}
              </Box>
            </Checkbox>
          </Box>
        ))}
      </SpaceBetween>
    </Container>
  );
}

/* ============================ result view ============================ */

function BenchmarkResult({ bm }: { bm: Benchmark }) {
  const a = bm.run_a;
  const b = bm.run_b;
  const costA = a.metrics?.cost_usd;
  const costB = b.metrics?.cost_usd;
  const gain =
    typeof costA === "number" && typeof costB === "number" && costA > 0 ? (costA - costB) / costA : undefined;
  const cheaper = (gain ?? 0) >= 0;
  const scenarioLine = `${bm.scenario.sessions} sessions × ${bm.scenario.turns} turns · ${bm.scenario.total_turns} total turns`;

  return (
    <Container
      header={
        <Header variant="h2" description={scenarioLine}>
          Benchmark Results — {bm.name}
        </Header>
      }
    >
      <SpaceBetween size="l">
        {typeof gain === "number" && (
          <Alert
            type={cheaper ? "success" : "warning"}
            header={`${pct(Math.abs(gain))} cost ${cheaper ? "reduction" : "increase"}`}
          >
            The control plane enabled {(b.features ?? []).length} feature
            {(b.features ?? []).length === 1 ? "" : "s"} for Run B. It spent {usd(costB)} vs {usd(costA)}{" "}
            baseline on identical inputs.
          </Alert>
        )}

        <Grid gridDefinition={[{ colspan: { default: 12, m: 6 } }, { colspan: { default: 12, m: 6 } }]}>
          <RunCard side={a} tag="A" />
          <RunCard side={b} tag="B" gain={gain} />
        </Grid>

        <PerTurnChart a={a} b={b} />
      </SpaceBetween>
    </Container>
  );
}

// Input tokens per turn, A vs B: climbs with memory alone, flattens with
// context management.
function PerTurnChart({ a, b }: { a: RunSide; b: RunSide }) {
  const toSeries = (side: RunSide) =>
    (side.per_turn ?? []).map((p) => ({ x: p.turn, y: p.avg_input_tokens }));
  const seriesA = toSeries(a);
  const seriesB = toSeries(b);
  if (seriesA.length < 2 && seriesB.length < 2) return null;
  const turns = Math.max(seriesA.length, seriesB.length);
  const maxY = Math.max(1, ...seriesA.map((p) => p.y), ...seriesB.map((p) => p.y));
  return (
    <div>
      <Box variant="awsui-key-label" padding={{ bottom: "xs" }}>
        Input tokens per turn (avg across sessions) (shown only when a run has at least 2 turns per session)
      </Box>
      <LineChart
        series={[
          { title: "Run A", type: "line", data: seriesA },
          { title: "Run B", type: "line", data: seriesB },
        ]}
        xDomain={[1, turns]}
        yDomain={[0, Math.ceil(maxY * 1.1)]}
        xScaleType="linear"
        xTitle="Turn"
        yTitle="Input tokens"
        i18nStrings={{ xTickFormatter: (x) => `${x}`, yTickFormatter: (y) => compact(y) }}
        height={220}
        hideFilter
      />
    </div>
  );
}

function RunCard({ side, tag, gain }: { side: RunSide; tag: "A" | "B"; gain?: number }) {
  const m = side.metrics;
  const showGain = typeof gain === "number";
  const cheaper = (gain ?? 0) >= 0;

  // Core stats (always) + the server-provided dynamic feature_stats (grows per
  // enabled strategy, e.g. prompt_cache adds cache hit rate + cache-read).
  const coreStats = m
    ? [
        { label: "Tokens in", value: compact(m.input_tokens) },
        { label: "Tokens out", value: compact(m.output_tokens) },
        { label: "Cost / session", value: usd(m.cost_per_conversation) },
        { label: "Model calls", value: `${m.model_calls}` },
      ]
    : [];
  const featureStats = (side.feature_stats ?? []).map((s) => ({ label: s.key, value: s.value }));

  return (
    <Container
      header={
        <Header variant="h3" actions={<Badge color={tag === "A" ? "grey" : "green"}>Run {tag}</Badge>}>
          {tag === "A" ? "Baseline" : "Candidate"}
        </Header>
      }
    >
      <SpaceBetween size="m">
        <div>
          <Box variant="awsui-key-label">Cost — Run {tag}</Box>
          <SpaceBetween direction="horizontal" size="s" alignItems="center">
            <Box fontSize="display-l" fontWeight="bold">
              {usd(m?.cost_usd)}
            </Box>
            {showGain && (
              <Box
                fontSize="heading-m"
                fontWeight="bold"
                color={cheaper ? "text-status-success" : "text-status-error"}
              >
                {cheaper ? "▼" : "▲"} {pct(Math.abs(gain!))} vs A
              </Box>
            )}
          </SpaceBetween>
        </div>

        <div>
          <Box variant="awsui-key-label">Features enabled for this run</Box>
          {(side.features ?? []).length === 0 ? (
            <Badge color="grey">None (baseline)</Badge>
          ) : (
            <SpaceBetween direction="horizontal" size="xxs">
              {featureNames(side.features ?? []).map((n) => (
                <Badge key={n} color="blue">
                  {n}
                </Badge>
              ))}
            </SpaceBetween>
          )}
        </div>

        <div>
          <Box variant="awsui-key-label" padding={{ bottom: "xs" }}>
            Breakdown
          </Box>
          <KeyValuePairs columns={2} items={[...coreStats, ...featureStats]} />
        </div>
      </SpaceBetween>
    </Container>
  );
}
