// Flat ESLint config (ESLint 9) for the control-plane SPA.
// TypeScript + React Hooks rules, with Prettier owning formatting (eslint only
// lints correctness, prettier formats -- they don't fight). Kept identical to
// the app-plane SPA's config on purpose: two independent bundles, one standard.
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import prettier from "eslint-config-prettier";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "*.config.js", "vite-env.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // Allow intentionally-unused args prefixed with _ (e.g. event handlers).
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
  prettier,
);
