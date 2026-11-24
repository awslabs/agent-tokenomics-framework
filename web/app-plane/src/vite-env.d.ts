/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Baked in at build time from CDK outputs.
  readonly VITE_API_BASE?: string; // Chat API base URL
  readonly VITE_USER_POOL_ID?: string; // Cognito user pool id
  readonly VITE_USER_POOL_CLIENT_ID?: string; // Cognito SPA client id
  readonly VITE_AWS_REGION?: string; // AWS region
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
