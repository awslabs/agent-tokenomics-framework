import { useState } from "react";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Button from "@cloudscape-design/components/button";
import Tiles from "@cloudscape-design/components/tiles";
import Alert from "@cloudscape-design/components/alert";
import { login, register, confirm, resendCode, type Profile } from "./auth";

// Self-registration is currently disabled at the Cognito pool level (AWS abuse
// mitigation force-disables open self sign-up on this account), so the register
// entry point is hidden. Demo users are provisioned by the seed. Flip this to
// true and re-enable self_sign_up_enabled in the CDK to bring registration back.
const SELF_SIGNUP_ENABLED = false;

type Mode = "login" | "register" | "verify";

export default function Auth({ onSignedIn }: { onSignedIn: (p: Profile) => void }) {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [tier, setTier] = useState<"prime" | "basic">("basic");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function doLogin() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const profile = await login(email.trim(), password);
      onSignedIn(profile);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function doRegister() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await register(email.trim(), password, name.trim() || email.trim(), tier);
      setNotice("Account created. Check your email for a verification code.");
      setMode("verify");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function doVerify() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await confirm(email.trim(), code.trim());
      setNotice("Email verified. You can now log in.");
      setMode("login");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Box padding={{ top: "xxl" }}>
      <div style={{ maxWidth: 460, margin: "0 auto" }}>
        <Container
          header={
            <Header
              variant="h1"
              description={
                mode === "login"
                  ? "Sign in to the support chat"
                  : mode === "register"
                    ? "Create your account"
                    : "Verify your email"
              }
            >
              E-commerce Support
            </Header>
          }
        >
          <SpaceBetween size="l">
            {error && <Alert type="error">{error}</Alert>}
            {notice && <Alert type="success">{notice}</Alert>}

            {mode === "login" && (
              <SpaceBetween size="m">
                <FormField label="Email">
                  <Input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.detail.value)}
                    placeholder="you@example.com"
                  />
                </FormField>
                <FormField label="Password">
                  <Input type="password" value={password} onChange={(e) => setPassword(e.detail.value)} />
                </FormField>
                <Button variant="primary" loading={busy} onClick={doLogin}>
                  Sign in
                </Button>
                {SELF_SIGNUP_ENABLED && (
                  <Box textAlign="center">
                    <Button
                      variant="link"
                      onClick={() => {
                        setMode("register");
                        setError(null);
                      }}
                    >
                      New here? Create an account
                    </Button>
                  </Box>
                )}
              </SpaceBetween>
            )}

            {mode === "register" && (
              <SpaceBetween size="m">
                <FormField label="Name">
                  <Input value={name} onChange={(e) => setName(e.detail.value)} placeholder="Your name" />
                </FormField>
                <FormField label="Email">
                  <Input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.detail.value)}
                    placeholder="you@example.com"
                  />
                </FormField>
                <FormField
                  label="Password"
                  description="At least 8 chars, with a lowercase letter and a digit."
                >
                  <Input type="password" value={password} onChange={(e) => setPassword(e.detail.value)} />
                </FormField>
                <FormField label="Membership">
                  <Tiles
                    value={tier}
                    onChange={(e) => setTier(e.detail.value as "prime" | "basic")}
                    items={[
                      { value: "basic", label: "Basic", description: "Standard shipping" },
                      { value: "prime", label: "Prime", description: "Free 2-day shipping" },
                    ]}
                  />
                </FormField>
                <Button variant="primary" loading={busy} onClick={doRegister}>
                  Create account
                </Button>
                <Box textAlign="center">
                  <Button
                    variant="link"
                    onClick={() => {
                      setMode("login");
                      setError(null);
                    }}
                  >
                    Already have an account? Sign in
                  </Button>
                </Box>
              </SpaceBetween>
            )}

            {mode === "verify" && (
              <SpaceBetween size="m">
                <FormField label="Email">
                  <Input type="email" value={email} onChange={(e) => setEmail(e.detail.value)} />
                </FormField>
                <FormField label="Verification code" description="Sent to your email.">
                  <Input value={code} onChange={(e) => setCode(e.detail.value)} placeholder="123456" />
                </FormField>
                <Button variant="primary" loading={busy} onClick={doVerify}>
                  Verify
                </Button>
                <Box textAlign="center">
                  <Button
                    variant="link"
                    onClick={async () => {
                      try {
                        await resendCode(email.trim());
                        setNotice("Code resent.");
                      } catch (e) {
                        setError((e as Error).message);
                      }
                    }}
                  >
                    Resend code
                  </Button>
                </Box>
              </SpaceBetween>
            )}
          </SpaceBetween>
        </Container>
      </div>
    </Box>
  );
}
