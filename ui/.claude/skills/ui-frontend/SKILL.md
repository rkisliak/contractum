---
name: ui-frontend
description: Add a screen to the ui module the platform way, with a TanStack file route under the fixed URL scheme, data-contracts-enterprise tokens and CSS Modules, ProblemAlert for API problems, a NotFound for 404, capability gating that only hides or disables, Vitest and Playwright plus axe coverage, and a regenerated API client after any backend change.
---

# ui-frontend

Use for any new screen, route or component in `ui/`. Read `ui/CLAUDE.md` first (pins, exceptions, URL scheme).

1. **Route file.** Add it under `ui/src/routes/` at the path the D-UI-08-23 URL scheme gives (for example
   `folders.$folderId.contracts.tsx`). Never invent a URL outside that scheme. Run `pnpm dev` or `pnpm build` so the
   plugin regenerates `src/routeTree.gen.ts`, and commit it. Filters and cursors are typed search params. The root
   context is `{ config }`; read the runtime config from the router context, never from `window`.
2. **Design.** The design system is **data-contracts-enterprise** (`.claude/skills/typeui-design-system/`, Atlas
   retired 2026-11-15): read its module for every component you touch, all of `.claude/skills/typeui-fundamentals/`
   (fundamentals win on conflicts), and the behaviour sections of the Phase 8 UI-SPEC. Style with `src/styles/tokens.css`
   and a CSS Module per component only: no hard-coded colour, spacing, font size or shadow; radii from
   `--radius-control` (6px), `--radius-card` (8px), `--radius-overlay` (12px) with concentric inner radii
   (`--radius-overlay-inner`, `--radius-card-inner`); elevation from `--shadow-*` only (cards `xs`, menus `lg`, dialogs
   `xl`, buttons `--shadow-button`). Every colour must work in light and dark (automatic via
   `prefers-color-scheme`); a new text/background pairing goes into `tokens.test.ts` with both measured ratios.
   Pages read as dashboard panels: content in cards or `DataTable` wrappers on the page surface, KPIs with `Stat`.
3. **Problems.** Every non-2xx is parsed into a typed `Problem` and rendered through `ProblemAlert` (title, detail, slug,
   `X-Request-ID`, field errors). Never render raw JSON or a stack trace.
4. **404.** A 404 on a detail route renders the one `NotFound` page ("This item does not exist, or you do not have
   access to it."): branch on `isNotFound(error)` from `@/lib/unexpected` (as `VersionDetail` and `ContractLayout`
   do). Never say forbidden and never leak whether the item exists.
5. **Capabilities.** Gating from `/me/capabilities` (`useCapabilities(folderId).can(permission)`, `hasGlobal(me, ...)`
   from `src/features/access/`) only hides or disables a control. Never request what the capabilities rule out; a read
   refused by design renders `NotAvailable`, never a red ProblemAlert. The UI makes no authorization decision and holds
   no secret; every write is re-checked by the API. Destructive actions confirm through `ConfirmDialog`.
6. **Tests.** Vitest and Testing Library for logic and components (`src/**/*.test.{ts,tsx}`), holding the
   `test:coverage` thresholds; Playwright plus `@axe-core/playwright` for each user journey in `ui/e2e/`, walked as a
   non-admin persona too (`e2e/personas.ts`). A screen with a mutation also tests the pending, success and error states.
   Demo records a journey reads go into `e2e/demo-seed.ts`, never onto the live stack by hand. Run a spec with
   `bash scripts/e2e-ui.sh e2e/<spec>.ts`.
7. **Client.** After any backend API change, run `uv run python scripts/export_openapi.py` and
   `pnpm --dir ui gen:api`, and commit both `openapi/openapi.json` and `src/api/schema.gen.ts`; the build must still
   type-check (`gen:api:check` and `tests/repo/test_ui_openapi_snapshot.py` fail on drift). Call the API only through
   the generated client with a literal path: that is how `scripts/operation_coverage.py --gate` (full gate) finds the
   operation's `ui_client` cell. An operation deliberately left off every screen needs a `ui_client` exemption with a
   reason in `tests/repo/operation_coverage_exemptions.json`.
8. **Prove it.** Run `pnpm --dir ui test`, `typecheck`, `lint`, `build` and `format` (inside the Node container on this
   host). Dependencies stay exact-pinned and pass the age gate; add none without the legitimacy check.
