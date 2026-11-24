import { useState } from "react";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Button from "@cloudscape-design/components/button";
import Alert from "@cloudscape-design/components/alert";
import { login } from "./auth";

export default function LoginPage({ onSignedIn }: { onSignedIn: (email: string) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function doLogin() {
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await login(email.trim(), password));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Box padding={{ top: "xxl" }}>
      <div style={{ maxWidth: 440, margin: "0 auto" }}>
        <Container
          header={
            <Header variant="h1" description="Admin sign in">
              Tokenomics Control Plane
            </Header>
          }
        >
          <SpaceBetween size="l">
            {error && <Alert type="error">{error}</Alert>}
            <FormField label="Email">
              <Input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.detail.value)}
                placeholder="admin@example.com"
              />
            </FormField>
            <FormField label="Password">
              <Input type="password" value={password} onChange={(e) => setPassword(e.detail.value)} />
            </FormField>
            <Button variant="primary" loading={busy} onClick={doLogin}>
              Sign in
            </Button>
          </SpaceBetween>
        </Container>
      </div>
    </Box>
  );
}
