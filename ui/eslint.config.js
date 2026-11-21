import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

// Strict typed rules, strict accessibility rules, hooks rules (D-UI-08-02, A-10).
// eslint-plugin-jsx-a11y declares peer eslint <=9; it runs correctly under 10
// (verified in the Phase 8 research spike). Do not enable strictPeerDependencies.
export default defineConfig(
  {
    ignores: [
      "dist",
      "src/routeTree.gen.ts",
      "src/api/schema.gen.ts",
      "test-results",
      "playwright-report",
      "coverage",
    ],
  },
  js.configs.recommended,
  tseslint.configs.strictTypeChecked,
  jsxA11y.flatConfigs.strict,
  reactHooks.configs.flat.recommended,
  {
    languageOptions: {
      globals: { ...globals.browser },
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  {
    // TanStack Router's `redirect()` and `notFound()` return plain objects that route hooks
    // throw by design (beforeLoad, loader); every other non-Error throw stays an error.
    rules: {
      "@typescript-eslint/only-throw-error": [
        "error",
        {
          allow: [
            {
              from: "package",
              package: "@tanstack/router-core",
              name: "Redirect",
            },
            {
              from: "package",
              package: "@tanstack/router-core",
              name: "NotFoundError",
            },
          ],
        },
      ],
    },
  },
  {
    // Plain JavaScript (this file, scripts/) is outside the TypeScript project,
    // so the type-aware rules cannot run there.
    files: ["**/*.js", "**/*.mjs"],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: { globals: { ...globals.node } },
  },
);
