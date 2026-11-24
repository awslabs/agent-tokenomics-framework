import { useEffect, useState, useCallback } from "react";
import AppLayout from "@cloudscape-design/components/app-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import HelpPanel from "@cloudscape-design/components/help-panel";
import Alert from "@cloudscape-design/components/alert";
import Flags from "./pages/Flags";
import Benchmarks from "./pages/Benchmarks";
import Governance from "./pages/Governance";
import Advisor, { ADVISOR_HELP } from "./pages/Advisor";
import TokenomicsAnalytics from "./pages/TokenomicsAnalytics";
import LoginPage from "./LoginPage";
import { currentEmail, getIdToken, logout } from "./auth";
import { HelpContext, type HelpContent } from "./help";
import { api } from "./api";
import { RoleContext } from "./role";

type Page = "analytics" | "advisor" | "flags" | "benchmarks" | "governance";
const PAGES: Page[] = ["analytics", "advisor", "flags", "benchmarks", "governance"];
// The analytics page is the default/home page (loaded on no/unknown hash).
const DEFAULT_PAGE: Page = "analytics";

const PAGE_TITLE: Record<Page, string> = {
  analytics: "Tokenomics Analytics",
  advisor: "Tokenomics Advisor",
  flags: "Feature flags",
  benchmarks: "Benchmarks",
  governance: "Token governance",
};

const PAGE_DESC: Record<Page, string> = {
  analytics: "Where the token spend goes and how the system is used — cost by agent, top users, and more.",
  advisor: "Ask an AI advisor what's driving cost and what to optimise — grounded in your metrics and plans.",
  flags: "Toggle tokenomics features and prove the savings with before/after benchmarks.",
  benchmarks: "Run before/after benchmarks and compare cost across flag configurations.",
  governance: "Set the token limits the running agent enforces in-loop, and watch them work.",
};

function parseHash(): Page {
  const h = window.location.hash.replace("#", "").split("/")[0] as Page;
  return PAGES.includes(h) ? h : DEFAULT_PAGE;
}

// #advisor/<chat_id> opens one Advisor chat.
function parseAdvisorChat(): string | null {
  const [p, sub] = window.location.hash.replace("#", "").split("/");
  return p === "advisor" && sub ? sub : null;
}

// Dev-only: VITE_NO_AUTH=1 skips Cognito login so the UI can be viewed with no
// backend. Never enabled in a production build.
const DEV_NO_AUTH = import.meta.env.VITE_NO_AUTH === "1";

export default function App() {
  const [page, setPage] = useState<Page>(parseHash);
  const [advisorChat, setAdvisorChat] = useState<string | null>(parseAdvisorChat);
  const [navOpen, setNavOpen] = useState(true);
  const [email, setEmail] = useState<string | null>(DEV_NO_AUTH ? "dev@local" : null);
  const [checking, setChecking] = useState(!DEV_NO_AUTH);
  // Control-plane role (GET /me): null = not loaded, "none" = no role assigned.
  const [role, setRole] = useState<"admin" | "viewer" | "none" | null>(DEV_NO_AUTH ? "admin" : null);

  // help/tools drawer: each page starts with its own help (the Advisor's page help, else empty)
  const [toolsOpen, setToolsOpen] = useState(false);
  const pageHelp = (p: Page): HelpContent =>
    p === "advisor" ? ADVISOR_HELP : { header: "Help", body: null };
  const [help, setHelp] = useState<HelpContent>(() => pageHelp(parseHash()));
  const openHelp = useCallback((c: HelpContent) => {
    setHelp(c);
    setToolsOpen(true);
  }, []);

  // Deep links: keep page state <-> location.hash in sync (back/forward works).
  useEffect(() => {
    const onHash = () => {
      const next = parseHash();
      setPage(next);
      // Remember the open Advisor chat across page switches: only update it while
      // on an Advisor hash (leaving to another page must NOT forget it, so the
      // nav link can bring the admin back to the same conversation). It is cleared
      // only by New chat (sets #advisor) or a page refresh (App state resets).
      if (next === "advisor") setAdvisorChat(parseAdvisorChat());
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const goto = (p: string) => {
    window.location.hash = p; // triggers hashchange -> setPage
  };

  // Keep the left navigation open on every page (including Advisor); the user
  // can still collapse it manually via the menu button.
  useEffect(() => {
    setHelp(pageHelp(page));
  }, [page]);

  useEffect(() => {
    if (DEV_NO_AUTH) return;
    getIdToken().then((t) => {
      // Derive email from the (restored) token itself — currentEmail() reads
      // signInUserSession which is null right after a page reload.
      if (t) {
        try {
          const claims = JSON.parse(atob(t.split(".")[1]));
          setEmail(claims.email ?? currentEmail() ?? "admin");
        } catch {
          setEmail(currentEmail() ?? "admin");
        }
      }
      setChecking(false);
    });
  }, []);

  useEffect(() => {
    if (!email || DEV_NO_AUTH) return;
    api
      .getMe()
      .then((me) => setRole(me.can_write ? "admin" : "viewer"))
      .catch((e) => setRole((e as { status?: number }).status === 403 ? "none" : "viewer"));
  }, [email]);

  if (checking) return null;
  if (!email) return <LoginPage onSignedIn={setEmail} />;

  // One Advisor href for both the nav item and activeHref (so the item highlights).
  const advisorHref = advisorChat ? `#advisor/${advisorChat}` : "#advisor";

  return (
    <HelpContext.Provider value={{ openHelp }}>
      <TopNavigation
        identity={{ href: "#", title: "Tokenomics Control Plane" }}
        utilities={[
          { type: "button", text: email, iconName: "user-profile" },
          {
            type: "button",
            text: "Sign out",
            onClick: () => {
              logout();
              setEmail(null);
            },
          },
        ]}
      />
      <RoleContext.Provider value={{ canWrite: role === "admin" }}>
        <AppLayout
          navigation={
            <SideNavigation
              // The Advisor link carries the open chat so returning to it resumes the
              // conversation; activeHref must use the SAME href or the item won't highlight.
              activeHref={page === "advisor" ? advisorHref : `#${page}`}
              header={{ href: "#analytics", text: "Tokenomics" }}
              onFollow={(e) => {
                e.preventDefault();
                goto(e.detail.href.replace("#", ""));
              }}
              items={[
                { type: "link", text: "Analytics", href: "#analytics" },
                { type: "link", text: "Governance", href: "#governance" },
                // Preserve the open chat: returning to Advisor from another page
                // reopens the same conversation (cleared only by refresh or New chat).
                { type: "link", text: "Advisor", href: advisorHref },
                { type: "link", text: "Feature flags", href: "#flags" },
                { type: "link", text: "Benchmarking", href: "#benchmarks" },
              ]}
            />
          }
          // The Advisor is a chat workspace: let it use the full width of the screen.
          maxContentWidth={page === "advisor" ? Number.MAX_VALUE : undefined}
          navigationOpen={navOpen}
          onNavigationChange={(e) => setNavOpen(e.detail.open)}
          tools={<HelpPanel header={<h2>{help.header}</h2>}>{help.body}</HelpPanel>}
          toolsOpen={toolsOpen}
          onToolsChange={(e) => setToolsOpen(e.detail.open)}
          content={
            <ContentLayout
              header={
                <Header variant="h1" description={PAGE_DESC[page]}>
                  {PAGE_TITLE[page]}
                </Header>
              }
            >
              {role === "none" ? (
                <Alert type="error" header="No control-plane role">
                  Your account isn&apos;t in the <b>governance-admin</b> or <b>viewer</b> group. Ask an
                  administrator to add you in the control-plane Cognito user pool.
                </Alert>
              ) : (
                <>
                  {role === "viewer" && (
                    <Alert type="info" header="Read-only access">
                      You have the <b>viewer</b> role: you can see everything but not change flags, usage
                      plans or run benchmarks. Changes need the <b>governance-admin</b> role.
                    </Alert>
                  )}
                  {page === "analytics" ? (
                    <TokenomicsAnalytics />
                  ) : page === "advisor" ? (
                    <Advisor chatId={advisorChat} />
                  ) : page === "governance" ? (
                    <Governance />
                  ) : page === "flags" ? (
                    <Flags />
                  ) : (
                    <Benchmarks />
                  )}
                </>
              )}
            </ContentLayout>
          }
        />
      </RoleContext.Provider>
    </HelpContext.Provider>
  );
}
