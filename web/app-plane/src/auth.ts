// Cognito auth for the chat SPA (custom UI, no hosted pages).
// Config is baked in at build time from CDK outputs (VITE_*).
import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
  CognitoUserAttribute,
  type CognitoUserSession,
} from "amazon-cognito-identity-js";

const USER_POOL_ID = import.meta.env.VITE_USER_POOL_ID ?? "";
const CLIENT_ID = import.meta.env.VITE_USER_POOL_CLIENT_ID ?? "";

// Guard against an empty UserPoolId/ClientId (which throws in CognitoUserPool) so
// a misbuilt bundle shows the login page instead of a white screen -- same guard
// the control-plane auth uses. The functions below null-check `pool`.
const pool =
  USER_POOL_ID && CLIENT_ID ? new CognitoUserPool({ UserPoolId: USER_POOL_ID, ClientId: CLIENT_ID }) : null;

export interface Profile {
  email: string;
  name: string;
  tier: string;
}

function user(email: string): CognitoUser {
  return new CognitoUser({ Username: email, Pool: pool! });
}

const NO_POOL = new Error("Auth is disabled (no Cognito configured).");

/** Register a new user with a tier choice (prime | basic). Requires email verify. */
export function register(
  email: string,
  password: string,
  name: string,
  tier: "prime" | "basic",
): Promise<void> {
  const attrs = [
    new CognitoUserAttribute({ Name: "email", Value: email }),
    new CognitoUserAttribute({ Name: "name", Value: name }),
    new CognitoUserAttribute({ Name: "custom:tier", Value: tier }),
  ];
  if (!pool) return Promise.reject(NO_POOL);
  return new Promise((resolve, reject) => {
    pool.signUp(email, password, attrs, [], (err) => (err ? reject(err) : resolve()));
  });
}

/** Confirm sign-up with the emailed verification code. */
export function confirm(email: string, code: string): Promise<void> {
  return new Promise((resolve, reject) => {
    user(email).confirmRegistration(code, true, (err) => (err ? reject(err) : resolve()));
  });
}

/** Resend the verification code. */
export function resendCode(email: string): Promise<void> {
  return new Promise((resolve, reject) => {
    user(email).resendConfirmationCode((err) => (err ? reject(err) : resolve()));
  });
}

/** Log in; resolves with the user's profile. Uses the SRP flow (the library
 *  default) -- the password never leaves the browser; the pool client does not
 *  allow plaintext USER_PASSWORD_AUTH. */
export function login(email: string, password: string): Promise<Profile> {
  if (!pool) return Promise.reject(NO_POOL);
  const details = new AuthenticationDetails({ Username: email, Password: password });
  const u = user(email);
  return new Promise((resolve, reject) => {
    u.authenticateUser(details, {
      onSuccess: (session) => resolve(profileFromSession(session)),
      onFailure: (err) => reject(err),
      // Any of these unhandled would otherwise hang the promise silently.
      newPasswordRequired: () => reject(new Error("A new password is required for this account.")),
      mfaRequired: () => reject(new Error("MFA is required but not supported here.")),
      totpRequired: () => reject(new Error("MFA is required but not supported here.")),
    });
  });
}

function profileFromSession(session: CognitoUserSession): Profile {
  const p = session.getIdToken().decodePayload();
  return {
    email: (p.email as string) ?? "",
    name: (p.name as string) ?? (p.email as string) ?? "Customer",
    tier: (p["custom:tier"] as string) ?? "basic",
  };
}

export function logout(): void {
  pool?.getCurrentUser()?.signOut();
}

/** Current valid session's id token (JWT), or null. Refreshes if needed. */
export function getIdToken(): Promise<string | null> {
  const u = pool?.getCurrentUser();
  if (!u) return Promise.resolve(null);
  return new Promise((resolve) => {
    u.getSession((err: Error | null, session: CognitoUserSession | null) => {
      if (err || !session || !session.isValid()) return resolve(null);
      resolve(session.getIdToken().getJwtToken());
    });
  });
}

/** Read the signed-in user's profile from the cached id-token claims. */
export function currentProfile(): Profile | null {
  const u = pool?.getCurrentUser();
  if (!u) return null;
  const session = (u as unknown as { signInUserSession?: CognitoUserSession }).signInUserSession;
  const payload = session?.getIdToken().decodePayload();
  if (!payload) return null;
  return {
    email: (payload.email as string) ?? "",
    name: (payload.name as string) ?? (payload.email as string) ?? "Customer",
    tier: (payload["custom:tier"] as string) ?? "basic",
  };
}
