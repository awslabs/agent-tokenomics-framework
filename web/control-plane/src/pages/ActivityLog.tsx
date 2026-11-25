/**
 * Activity log: the admin-facing view of the control-plane audit trail
 * (GET /governance/audit; threat model M16). Every change to feature flags,
 * usage plans and per-user overrides, and every benchmark start, is recorded by
 * the API (lambda_entry._change -> lib/audit.record) as who / when / route /
 * before / after. This page only reads it: one readable "old -> new" line per
 * change, with the raw before/after JSON available per row.
 */
import { useEffect, useMemo, useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import Button from "@cloudscape-design/components/button";
import Select from "@cloudscape-design/components/select";
import Pagination from "@cloudscape-design/components/pagination";
import Alert from "@cloudscape-design/components/alert";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { api, AuditEntry } from "../api";

type BadgeColor = "blue" | "green" | "red" | "grey";

// Audited routes (src/control-plane/lambda_entry.py) -> display type.
const TYPES: Record<string, { label: string; color: BadgeColor }> = {
  "PUT /flags": { label: "Feature flags", color: "blue" },
  "PUT /governance/config": { label: "Usage plans", color: "green" },
  "PUT /governance/user-override": { label: "User override", color: "red" },
  "POST /benchmarks": { label: "Benchmark", color: "grey" },
};

const PER_PAGE = 20;
const FETCH_LIMIT = 200; // newest entries; the API caps at 500
const MAX_DETAIL_ITEMS = 3;

const prettyLabel = (key: string) => key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

type Leaf = { path: string[]; value: unknown };

/** Flatten nested config into leaf key paths, e.g. ["agents", "order", "max_tool_calls"] -> 3.
 *  Paths stay arrays so keys that contain dots (e.g. emails) are never split. */
function flattenLeaves(
  v: unknown,
  path: string[] = [],
  out: Map<string, Leaf> = new Map(),
): Map<string, Leaf> {
  if (v && typeof v === "object" && !Array.isArray(v)) {
    for (const [k, child] of Object.entries(v as Record<string, unknown>)) {
      flattenLeaves(child, [...path, k], out);
    }
  } else if (path.length) {
    out.set(JSON.stringify(path), { path, value: v });
  }
  return out;
}

// Container keys that add nothing to a label: "agents.order.max_tool_calls" -> "Order · Max tool calls".
const LABEL_SKIP = new Set(["agents", "user_default", "user_overrides", "params"]);

/** Email recorded on a per-user override (keyed by Cognito sub), from after or, if removed, before. */
function overrideEmail(sub: string, before: unknown, after: unknown): string | undefined {
  for (const cfg of [after, before]) {
    const o = (cfg as { user_overrides?: Record<string, { email?: unknown }> } | null)?.user_overrides?.[sub];
    if (typeof o?.email === "string" && o.email) return o.email;
  }
  return undefined;
}

function changeLabel(path: string[], before: unknown, after: unknown): { label: string; isToggle: boolean } {
  // user_overrides.<sub>.<field>: show the user's email instead of the opaque sub.
  const parts =
    path[0] === "user_overrides" && path.length > 2
      ? [overrideEmail(path[1], before, after) ?? path[1], ...path.slice(2)]
      : path.filter((p) => !LABEL_SKIP.has(p));
  const isToggle = parts[parts.length - 1] === "enabled";
  if (isToggle) parts.pop();
  const last = parts.pop() ?? path.join(".");
  // Keep ids such as user emails / subs as-is; prettify config keys.
  const prefix = parts.map((p, i) => (path[0] === "user_overrides" && i === 0 ? p : prettyLabel(p)));
  return { label: [...prefix, prettyLabel(last)].join(" · "), isToggle };
}

function changeValue(v: unknown, isToggle: boolean): string {
  if (v === undefined || v === null) return "default";
  if (isToggle || typeof v === "boolean") return v ? "ON" : "OFF";
  if (typeof v === "number") return v.toLocaleString();
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** Config change -> "Prompt cache: OFF → ON; Daily max tokens per user: 1,000 → 100". */
function describeConfigChange(before: unknown, after: unknown): string {
  const b = flattenLeaves(before);
  const a = flattenLeaves(after);
  const changes: string[] = [];
  for (const key of new Set([...b.keys(), ...a.keys()])) {
    const path = (a.get(key) ?? b.get(key))!.path;
    const bv = b.get(key)?.value;
    const av = a.get(key)?.value;
    if (path[path.length - 1] === "email" || JSON.stringify(bv) === JSON.stringify(av)) continue;
    const { label, isToggle } = changeLabel(path, before, after);
    changes.push(`${label}: ${changeValue(bv, isToggle)} → ${changeValue(av, isToggle)}`);
  }
  if (changes.length === 0) return "No effective change";
  const extra = changes.length - MAX_DETAIL_ITEMS;
  return changes.slice(0, MAX_DETAIL_ITEMS).join("; ") + (extra > 0 ? ` (+${extra} more)` : "");
}

/** "prompt_cache=on+memory=on" -> "prompt cache + memory"; "" / "baseline" -> "baseline". */
function flagSummary(sig: unknown): string {
  const on = String(sig ?? "")
    .split("+")
    .filter((p) => p.endsWith("=on"))
    .map((p) => p.replace(/=on$/, "").replace(/_/g, " "));
  return on.length ? on.join(" + ") : "baseline";
}

/** Benchmark start -> "benchmark-… · A: memory vs B: prompt cache + memory · 2 sessions × 2 turns". */
function describeBenchmark(after: unknown): string {
  const bm = (after ?? {}) as {
    name?: string;
    run_a?: { flag_config?: string };
    run_b?: { flag_config?: string };
    scenario?: { sessions?: number; turns?: number };
  };
  const parts = [bm.name ?? "benchmark"];
  if (bm.run_a || bm.run_b) {
    parts.push(`A: ${flagSummary(bm.run_a?.flag_config)} vs B: ${flagSummary(bm.run_b?.flag_config)}`);
  }
  if (bm.scenario?.sessions && bm.scenario?.turns) {
    parts.push(`${bm.scenario.sessions} sessions × ${bm.scenario.turns} turns`);
  }
  return parts.join(" · ");
}

function describe(e: AuditEntry): string {
  return e.action === "POST /benchmarks"
    ? describeBenchmark(e.after)
    : describeConfigChange(e.before, e.after);
}

function RawJson({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <Box fontSize="body-s">
        <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-all", margin: 0 }}>
          {value === undefined || value === null ? "—" : JSON.stringify(value, null, 2)}
        </pre>
      </Box>
    </div>
  );
}

export default function ActivityLog() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<string | null>(null);
  const [page, setPage] = useState(1); // 1-based (Cloudscape convention)

  function load() {
    setLoading(true);
    setError(null);
    api
      .getAudit(FETCH_LIMIT)
      .then((r) => setEntries(r.entries))
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }
  useEffect(load, []);

  const filtered = useMemo(
    () => entries.filter((e) => !typeFilter || e.action === typeFilter),
    [entries, typeFilter],
  );
  useEffect(() => {
    setPage(1);
  }, [typeFilter, filtered.length]);
  const pagesCount = Math.max(1, Math.ceil(filtered.length / PER_PAGE));
  const paged = filtered.slice((page - 1) * PER_PAGE, page * PER_PAGE);

  return (
    <>
      {error && (
        <Alert type="error" header="Could not load the activity log">
          {error}
        </Alert>
      )}
      <Table
        variant="container"
        loading={loading}
        loadingText="Loading activity"
        items={paged}
        header={
          <Header
            variant="h2"
            counter={`(${filtered.length})`}
            description="Every change to feature flags, usage plans and overrides, and every benchmark start: who, when, old → new."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Select
                  selectedOption={{
                    label: typeFilter ? TYPES[typeFilter].label : "All types",
                    value: typeFilter ?? "all",
                  }}
                  onChange={(e) =>
                    setTypeFilter(
                      e.detail.selectedOption.value === "all" ? null : e.detail.selectedOption.value!,
                    )
                  }
                  options={[
                    { label: "All types", value: "all" },
                    ...Object.entries(TYPES).map(([value, t]) => ({ label: t.label, value })),
                  ]}
                />
                <Button iconName="refresh" loading={loading} onClick={load} ariaLabel="Refresh" />
              </SpaceBetween>
            }
          >
            Activity log
          </Header>
        }
        pagination={
          <Pagination
            currentPageIndex={page}
            pagesCount={pagesCount}
            onChange={(e) => setPage(e.detail.currentPageIndex)}
          />
        }
        empty={<Box color="text-body-secondary">No activity recorded yet.</Box>}
        columnDefinitions={[
          { id: "ts", header: "Time", cell: (e) => e.ts, width: 230 },
          { id: "who", header: "Changed by", cell: (e) => e.actor_email || e.actor_sub || "—" },
          {
            id: "type",
            header: "Type",
            cell: (e) => {
              const t = TYPES[e.action];
              return <Badge color={t?.color ?? "grey"}>{t?.label ?? e.action}</Badge>;
            },
          },
          {
            id: "detail",
            header: "Detail",
            cell: (e) => (
              <ExpandableSection variant="footer" headerText={describe(e)}>
                <ColumnLayout columns={2} variant="text-grid">
                  <RawJson label="Before" value={e.before} />
                  <RawJson label="After" value={e.after} />
                </ColumnLayout>
                {e.action === "POST /benchmarks" && (
                  <Box margin={{ top: "s" }}>
                    <Link href="#benchmarks">Open Benchmarking</Link>
                  </Box>
                )}
              </ExpandableSection>
            ),
          },
        ]}
      />
    </>
  );
}
