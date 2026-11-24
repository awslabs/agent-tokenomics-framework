import { useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Grid from "@cloudscape-design/components/grid";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Select from "@cloudscape-design/components/select";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import Spinner from "@cloudscape-design/components/spinner";
import Alert from "@cloudscape-design/components/alert";
import BarChart from "@cloudscape-design/components/bar-chart";
import PieChart from "@cloudscape-design/components/pie-chart";
import { api, GovAnalytics } from "../api";

/**
 * Tokenomics Analytics — the control-plane HOME page (default route). The
 * analytics / FinOps view: where token spend goes + how the system is used.
 * Distinct from Token Governance (which owns the caps + governance analytics).
 *
 * V1 widgets: Total cost / Total tokens KPIs + Cost by agent (donut) + Top
 * users by cost (bar). All obey the window + user filters and show live traffic
 * only (benchmark spend is in separate BENCH# rows). Reuses the existing
 * /governance/analytics payload (cost_by_agent + top_users) — no new backend
 * route in V1. Designed to accrete more widgets (trends, cost-per-model, cache
 * savings, benchmark/total cost).
 *
 * Correctness: Total == Σ(cost by agent) == Σ(top users) at every (window,user).
 */

const usd = (n: number) => `$${n.toFixed(3)}`;
// Higher precision for the per-agent donut so tiny costs (e.g. router_judge on
// Nova Lite, ~$0.00001) are visible instead of rounding to $0.000.
const usd5 = (n: number) => `$${n.toFixed(5)}`;

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

export default function TokenomicsAnalytics() {
  const [analytics, setAnalytics] = useState<GovAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [users, setUsers] = useState<string[]>(["all"]);
  const [windowSel, setWindowSel] = useState("7d");
  const [user, setUser] = useState("all");
  const userLabel = user === "all" ? "all users" : user;

  function load() {
    setLoading(true);
    api
      .getGovAnalytics(user, windowSel)
      .then((a) => {
        setAnalytics(a);
        const emails = a.top_users.map((u) => u.user).filter((u) => u.includes("@"));
        setUsers(["all", ...Array.from(new Set(emails))]);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }
  useEffect(load, [user, windowSel]);

  const COST_BY_AGENT = analytics?.cost_by_agent ?? [];
  const TOP_USERS = analytics?.top_users ?? [];
  // Use the authoritative backend KPIs (USER# rows) for the headline totals so
  // Analytics agrees with Governance. cost_by_agent (AGENT# rows) is used only
  // for the per-agent chart below, not these totals.
  const totalCost = analytics?.kpis.total_cost_usd ?? 0;
  const totalTokens = analytics?.kpis.total_tokens ?? 0;
  const hasData = COST_BY_AGENT.length > 0 || TOP_USERS.length > 0;

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)} header="Analytics error">
          {error}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description="Where the token spend goes, across agents and users. Reflects the selected time window and user."
            actions={
              <span className="refresh-green">
                <Button iconName="refresh" loading={loading} onClick={load}>
                  Refresh
                </Button>
              </span>
            }
          >
            Usage &amp; cost
          </Header>
        }
      >
        <SpaceBetween size="l">
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
          </SpaceBetween>

          {loading ? (
            <Box textAlign="center" padding="xxl">
              <Spinner size="large" />
            </Box>
          ) : !hasData ? (
            <EmptyState hint="No usage for this window. Send some chat turns, then refresh." />
          ) : (
            <SpaceBetween size="l">
              <ColumnLayout columns={2} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Total cost ({windowSel})</Box>
                  <Box fontSize="display-l" fontWeight="bold">
                    {usd(totalCost)}
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Total tokens ({windowSel})</Box>
                  <Box fontSize="display-l" fontWeight="bold">
                    {totalTokens.toLocaleString()}
                  </Box>
                </div>
              </ColumnLayout>

              <Grid gridDefinition={[{ colspan: { default: 12, s: 6 } }, { colspan: { default: 12, s: 6 } }]}>
                <Container
                  header={
                    <Header variant="h3" description={`Cost share by agent · ${userLabel} · ${windowSel}`}>
                      Cost by agent
                    </Header>
                  }
                >
                  {/* 3dp cost everywhere via usd(); donut value is the raw $ */}
                  <PieChart
                    variant="donut"
                    hideFilter
                    size="medium"
                    data={COST_BY_AGENT.map((a) => ({ title: a.agent, value: a.cost }))}
                    detailPopoverContent={(d) => [{ key: "Cost", value: usd5(d.value) }]}
                    segmentDescription={(d, sum) =>
                      `${usd5(d.value)} (${Math.round((d.value / sum) * 100)}%)`
                    }
                    ariaLabel="Cost by agent"
                    empty={<EmptyState />}
                  />
                </Container>
                <Container
                  header={
                    <Header variant="h3" description={`${userLabel} · ${windowSel}`}>
                      Top users by cost
                    </Header>
                  }
                >
                  <BarChart
                    horizontalBars
                    hideFilter
                    hideLegend
                    height={220}
                    series={[
                      {
                        title: "Cost",
                        type: "bar",
                        data: TOP_USERS.map((u) => ({ x: u.user, y: u.cost })),
                        valueFormatter: (y: number) => usd(y),
                      },
                    ]}
                    xScaleType="categorical"
                    ariaLabel="Top users by cost"
                    xTitle="User"
                    yTitle="Cost ($)"
                    empty={<EmptyState />}
                  />
                </Container>
              </Grid>
            </SpaceBetween>
          )}
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}
