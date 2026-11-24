// Admin auth for the control-plane SPA (business owners). Login-only -- admins
// are provisioned in the dedicated control-plane Cognito pool, not self-signup.
import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
  type CognitoUserSession,
} from "amazon-cognito-identity-js";

const USER_POOL_ID = import.meta.env.VITE_ADMIN_USER_POOL_ID ?? "";
const CLIENT_ID = import.meta.env.VITE_ADMIN_USER_POOL_CLIENT_ID ?? "";

// Guard against an empty UserPoolId/ClientId (which throws in CognitoUserPool)
// so a misbuilt bundle shows the login page instead of a white screen.
const pool =
  USER_POOL_ID && CLIENT_ID ? new CognitoUserPool({ UserPoolId: USER_POOL_ID, ClientId: CLIENT_ID }) : null;

function user(email: string): CognitoUser {
  return new CognitoUser({ Username: email, Pool: pool! });
}

export function login(email: string, password: string): Promise<string> {
  if (!pool) return Promise.reject(new Error("Auth is disabled (no Cognito configured)."));
  const details = new AuthenticationDetails({ Username: email, Password: password });
  // SRP (the library default): the pool client does not allow USER_PASSWORD_AUTH.
  const u = user(email);
  return new Promise((resolve, reject) => {
    u.authenticateUser(details, {
      onSuccess: (s) => resolve((s.getIdToken().decodePayload().email as string) ?? email),
      onFailure: (err) => reject(err),
      newPasswordRequired: () => reject(new Error("A new password is required for this account.")),
    });
  });
}

export function logout(): void {
  pool?.getCurrentUser()?.signOut();
}

/** Current valid id token (JWT), or null. */
export function getIdToken(): Promise<string | null> {
  const u = pool?.getCurrentUser();
  if (!u) return Promise.resolve(null);
  return new Promise((resolve) => {
    u.getSession((err: Error | null, session: CognitoUserSession | null) => {
      resolve(err || !session || !session.isValid() ? null : session.getIdToken().getJwtToken());
    });
  });
}

export function currentEmail(): string | null {
  const u = pool?.getCurrentUser();
  const s = (u as unknown as { signInUserSession?: CognitoUserSession } | null)?.signInUserSession;
  return (s?.getIdToken().decodePayload().email as string) ?? null;
}
