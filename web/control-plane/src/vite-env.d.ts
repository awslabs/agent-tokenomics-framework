/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string; // API Gateway stage URL
  readonly VITE_ADMIN_USER_POOL_ID?: string; // admin Cognito pool id
  readonly VITE_ADMIN_USER_POOL_CLIENT_ID?: string; // admin Cognito SPA client id
  readonly VITE_AWS_REGION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
