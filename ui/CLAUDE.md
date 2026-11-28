# ui (React single-page app, no Python package)

The browser UI for the platform: a single-page app styled with the data-contracts-enterprise design system served by its own `ui` container behind the gateway. It
is not a uv workspace member; it is a pnpm project with its own lockfile.

## What this module owns

- The SPA under `src/` (React 19, TypeScript 6 strict, Vite 8, TanStack Router file routes, TanStack Query), its design
  tokens and CSS Modules, and its unit tests (Vitest, Testing Library, jsdom).
- The runtime-config boot path: `src/config.ts` fetches `/config.json` (`no-store`) and validates every key and type
  before anything else renders (D-UI-08-05, T-8-04). `src/main.tsx` shows `BootScreen` until it resolves. In `pnpm dev` a
  serve-only Vite middleware in `vite.config.ts` answers it from `dev/config.json`; there is no `public/config.json` and
  none ships in `dist`. In the container the nginx entrypoint writes it from `DC_UI_*` variables.
- The nginx runtime image configuration (`nginx/`, Plan 08), the generated API client snapshot (`openapi/`,
  `src/api/schema.gen.ts`) and the Playwright specs (`e2e/`).
- Generated files that are committed and never hand-edited: `src/routeTree.gen.ts` (TanStack Router plugin, written on
  `pnpm dev` and `pnpm build`) and `src/api/schema.gen.ts`. Both are ignored by ESLint and Prettier.

## What it must never import

No Python, no `dc_*` library. The UI talks to the platform only over HTTP, through the generated client. It holds no
secret (`config.json` carries public identifiers only) and it makes no authorization decision: capabilities from
`/me/capabilities` only hide or disable controls, and every write is re-checked by the API (D-UI-08-21). A 404 on a
detail route is rendered as "Not found", never as forbidden (D-UI-08-25).

## Key files and entry points

- `src/main.tsx`: boot (loading, config, router, or the failed boot screen with a reload button).
- `src/config.ts`: `RuntimeConfig`, `loadRuntimeConfig(fetchImpl?)`, `RuntimeConfigError`.
- `src/router.ts`: `createAppRouter(context)` and the router `Register` declaration; the root context is
  `{ config, queryClient, api, session }`, built once in `main.tsx` (`QueryClientProvider` around the `RouterProvider`).
- Routes: `src/routes/__root.tsx` (`createRootRouteWithContext<RouterContext>()`), `signin.tsx` (typed search
  `{ redirect?, notice? }`, renders `features/signin/SignInPage`), the pathless authenticated layout `_app.tsx` (its
  `beforeLoad` awaits `session.restore()` and otherwise throws a redirect to `/signin?redirect=<location>`; skeleton
  `pendingComponent`) and `_app/index.tsx` (`/`, the `["me"]` query and the "Welcome, {display name}" h1). Screen
  components live in `src/features/<screen>/` so their tests never sit in the file-route directory; route files own the
  search schema and `beforeLoad`. `@typescript-eslint/only-throw-error` allows throwing TanStack's `Redirect`.
- `src/auth/tokenStore.ts` (the access token: one module-level variable, `get`/`set`/`clear`/`subscribe`, never Web
  Storage, a cookie or the URL; a grep acceptance check guards `src/auth` and `src/api`) and `src/auth/session.ts`
  (`createSession(api, { navigate })` gives `signIn` (login with `X-Token-Delivery: cookie`; a `password:rotate` scope
  is kept in a separate restricted slot and resolves `rotation-required`), `refresh` (cookie POST, 401 gives false, other
  failures throw, concurrent callers share one request, run inside the `dc-refresh` Web Lock via `src/auth/locks.ts`
  `withLock` with an in-tab fallback), `rotate(newPassword)` (posts `/auth/password` with the restricted bearer, clears
  the restricted slot, then signs in again on the cookie path with the same username), `restore`, `signOut` (cookie-path logout, then clear, `BroadcastChannel("dc-session")`
  `logout` and navigate; a failed logout rejects and changes nothing), `expire` (clear, then `/signin` with the current path
  and `session-expired` only when a session existed in this page), a proactive refresh at 80% of `expires_in`,
  `restrictedToken`, `redirectToSignIn` and `dispose`. Options: `{ navigate, withLock?, channel?, now?, currentPath? }`.
  The refresh token is the HttpOnly `__Secure-dc_refresh` cookie and is never visible to the UI. `createApiClient` injects
  `Authorization: Bearer <token>` from the store unless the caller set one; `installAuthRetry` (`src/api/client.ts`, called
  by `createSession`) replays a request once after a refresh on a 401, never for `/api/v1/identity/auth/` paths or a
  caller-set `Authorization`, and a second 401 expires the session. `src/auth/notices.ts` is the one definition of the
  sign-in notice enum (`NOTICES`, `Notice`, `isNotice`; `/signin` validates against it and `SignInPage` renders the fixed
  copy as an info Alert) and `src/auth/redirect.ts` is `safeRedirect` (single-slash same-origin paths only). `/signin`'s
  `beforeLoad` sends a signed-in user to `safeRedirect(search.redirect)` (it skips the cookie probe when a notice is set).
- **Entra sign-in** (D-UI-08-16, A-3, A-4, A-7): `src/auth/msal.ts` (`buildMsalConfig`, memoised `getMsal`,
  `startEntraSignIn`, `completeEntraSignIn`, `hasEntraAccount`, `acquireEntraToken`, `entraSignOut`; the library is
  loaded on demand), the redirect bridge `redirect.html` + `src/redirect.ts` (second Vite entry calling
  `broadcastResponseToMainFrame`), `/auth/callback` (`routes/auth.callback.tsx`, `features/signin/CallbackPage.tsx`:
  `?start=1` begins `loginRedirect` there so the bridge returns there; completion adopts the token through
  `session.adoptEntraToken`, checks `GET /me`, then follows `safeRedirect(state)`; "Signing you in…", a 10 s "taking
  longer" backstop and a failure card). `createSession` takes an optional `entra` adapter (built in `main.tsx` only when
  `entraEnabled`): an Entra session renews through MSAL, restores from MSAL's per-tab cache and signs out through
  `logoutRedirect`; `session.idp()` tells which IdP is in use. `SignInPage` takes `methods` from the runtime config.
- **Forced bootstrap rotation** (UI-SPEC 7.3, D-UI-08-15): `routes/signin_.rotate.tsx` (`/signin/rotate?redirect=`;
  `beforeLoad` sends a visit without `session.restrictedToken()`, such as a reload, to `/signin?notice=rotation-restart`)
  and `features/signin/RotatePage.tsx` (strings in `copy.ts` `rotateCopy`; length and confirm checks on submit then per
  field, `/new_password` 422 pointers on the field and the rest in a ProblemAlert, a 401 restarts sign-in; success toasts
  "Password set. You are signed in." and follows `safeRedirect`). `SignInPage` navigates there on `rotation-required`.
  `RotatePage.test.tsx` covers the whole journey with msw. Never run the rotation against the live warm stack: the
  bootstrap secret is consumed; `e2e/global-setup.ts` drives it in a browser only on a cold stack and sets
  `DC_UI_E2E_ROTATED=1`, and the smoke test "the bootstrap rotation landed the admin signed in" skips otherwise.
- `src/components/Toast` (`ToastProvider`, mounted in `__root.tsx`; `useToast()({ title, description?, intent? })`):
  Radix Toast top-right, "Notifications" region, 6 s paused on hover or focus, at most three (oldest leaves first).
  Success and neutral confirmations only; errors stay in flow as Alerts. `useToast` is a no-op outside the provider.
- Playwright projects: `chromium` (every spec) and `chromium-dark` (`colorScheme: "dark"`, runs only the read-only
  "every area renders cleanly" journey, `e2e/design-screens.spec.ts` and the layout audit, so axe and the layout rules
  also guard the dark theme).
  `design-screens.spec.ts` writes full-page screenshots of home, folder, folder contracts, contract versions, the rule
  set editor and audit plus /folders, /contracts and a contract page to `test-results/design/<page>-<project>.png` for visual review.
- **Layout audit** (`e2e/layout.spec.ts`, `e2e/layout-helpers.ts`, `e2e/layout-coverage.ts`): rules R1 to R12 (row
  alignment, token sizes, label gaps, stacking, viewport, containment, clipping, card heights, icon centring, overlap,
  table row centres, field spacing) measured on every route at 1440/1024/390 and on every dialog, menu, drawer, tab and
  form state a page button opens; screenshots in `test-results/audit/<project>/<width>/`. Register a new route or
  overlay component in `e2e/layout-coverage.ts` (`src/test/layoutCoverage.test.ts` fails otherwise). Shared layout
  hooks the rules read: `data-field`, `data-control`, `data-checkbox`, `data-button`, `data-card`, `data-badge`. Use
  `Identifier` (one-line id, ellipsis + title), `Breakable` (wraps at `. _ : /`) and `ButtonLink` (a link sized like a
  Button) instead of ad-hoc monospace cells or icon links in toolbars. **Monospace is only for real code** (quick
  261119-r5g, D-03): Monaco editors and diffs, YAML/JSON blocks, CEL and JSONPath expressions, JSON pointers and values,
  the "How to fetch" URL and header snippets, webhook and fetch URLs, hashes, machine codes (problem slugs, rule keys,
  event types, permission ids) and the `SecretReveal` token. Every ID shown as data (object, tenant, principal, app,
  record, request, event, run and version IDs, the ODCS id), plus semvers, folder slugs and paths, reads in the normal
  Inter font (no tabular figures: `tabular-nums` widens a UUID's hyphens from 6px to 9px so it looks monospaced; each
  `.ident` class and `Identifier` set `font-variant-numeric: normal` to opt out of the DataTable cell default): the
  `Identifier` component and each module's `.ident` class (`styles.ident`), never a `<code className={styles.mono}>` (the global `code` rule is monospace, so use a `span`).
- **Volume audit** (quick 261120-6i0; `e2e/volume.spec.ts`, `e2e/volume-seed.ts`, `e2e/seed-client.ts`,
  `e2e/volume.setup.ts`, `e2e/layout-crawl.ts`): the layout rules run again over a large, hostile-but-valid dataset (40
  folders, 320 users, 160 groups, 30 service accounts, 55 Entra registrations, 120 grants, 40 rule sets with 127
  versions, 210 contracts (ten with 25 versions), 20 templates, 110 subscriber cards, 120 tokens; 200-character names
  without spaces, Arabic, Hebrew, CJK, emoji and bidi controls) seeded through the admin API. **Compose test stack
  only**: the Playwright projects `volume-setup` (idempotent seed, asserts every minimum, runs itself twice and demands
  zero writes the second time), `volume` and `volume-dark` (`dependencies: ["volume-setup"]`); `chromium` ignores both
  files. Run with project flags: `bash scripts/e2e-ui.sh --project=volume-setup`, then
  `bash scripts/e2e-ui.sh --project=volume --project=volume-dark` (about 25 minutes for the route test per theme) or
  `--project=volume --no-deps -g "<title>"` to iterate. Never run the seed against the Kubernetes release.
  `seed-client.ts` is the paced admin client (at most 6 requests in flight and 40 per second, retry on 429 and 503
  with `Retry-After`) that `demo-seed.ts` shares. The fake Graph (`infra/fake-graph/directory.json` `generated`) adds
  60 groups, 60 users and 40 service principals plus the 600-member "Volume Entra big group"; restart the fake-graph
  container after editing it. `VOLUME_WANDERER` (`personas.ts`, exported, not in `PERSONAS`) is the non-admin persona
  with 24 folders of access ("Show access at" lists them). `layout-crawl.ts` holds the shared settle, audit, crawl,
  dismiss and `DENY`; its context object carries the screenshot root, the `opened` and `crawled` sets and a per-state
  hook (the volume spec runs axe and the issue check there). Rules R14 to R17 (`layout-helpers.ts`, both audits):
  popups and menus stay in the viewport and scroll themselves, dialogs fit with reachable header and footer actions,
  buttons, badges, tabs, menu items, nav items and crumbs never wrap to a second line, and user text is bidi-isolated.
  Register a new route in `VOLUME_ROUTES` and a new searchable popup (a file rendering `<Combobox`, `<PrincipalPicker`
  or `<EntraDirectorySearch`) in `POPUPS` (`e2e/layout-coverage.ts`; `layoutCoverage.test.ts` fails otherwise). A
  native `select` is measured as an element and by option count; its browser-drawn list cannot be measured. Measured exemptions
  (comments in `layout-helpers.ts`): R15 ignores a combobox popup floating over its dialog (R14 measures it) and R16
  ignores visually hidden helper text; the crawl opens a table row action once per verb (`oncePerRowAction`) and the
  spec scrolls to the top before measuring a long list on a phone. Shared
  behaviour the fixes added: `DataTable` marks a cell with a word of 40 or more characters (or 80 characters of text)
  `data-longword` (layout effect plus a `MutationObserver`), and only such cells break anywhere and drop `nowrap`; the
  caption wraps; `Badge` caps at min(100%, 16rem) and `Button` label at 22rem and ends its label (a child span with the full text as `title`) in
  an ellipsis; `Breadcrumb` crumbs are one line each with an ellipsis and a `title`; `PageHeader`'s grid track is
  `minmax(0, 1fr)`; `global.css` gives every text container `unicode-bidi: isolate`. CSS-only fixes are pinned by
  `src/styles/volumeText.test.ts`.
- The Monaco editor follows the colour scheme: `features/editor/useColorScheme.ts` + `editorTheme(scheme)` in
  `monacoSetup.ts` define `dc-light`/`dc-dark` from the live tokens.
- `e2e/full-journey.spec.ts` (+ `e2e/journey-helpers.ts`): the whole UI against the real stack as admin, every area and
  tab (h1, no ProblemAlert, no console/page/API error, axe) plus real writes across areas; `cleanupJourneyTeams` (run
  in `beforeAll` and `afterAll`) archives the cards, soft-deletes the contracts and archives every "Journey ..." team
  under Acme > Payments; `e2e/non-admin-journey.spec.ts` (+ `e2e/personas.ts`): the same checks as the Ledger Team
  editor (`ledger-editor`, through the group `demo-ledger-editors`), Team reader (`ledger-reader`, direct grant) and
  consumer (`ledger-subscriber`, direct `consumer` grant: published reads plus `card:create`), recreated
  idempotently by `ensurePersonas(adminApi)` and signed in with `signInAs`; no API error is allow-listed,
  admin-only navigation must be absent and "Your access" collapsed until "See details". "subscriber flow" creates a
  card in Ledger as the consumer, subscribes it to the published Orders version, finds it listed and archives it
  (leftover "Subscriber journey ..." cards are archived as admin before and after). The layout audit also runs the
  editor's and the subscriber's pages ("the editor persona's pages", "the subscriber persona's pages"). Add a persona
  in `personas.ts`, never in a spec; `e2e/session-switch.spec.ts`: users switching in one tab (reader to admin, admin
  to reader, editor to subscriber, subscriber to admin; sign out through the account menu, sign in as the next user,
  same pages, no ProblemAlert or API error, the next user's own nav and folders) and a sign-out in a second tab
  sending the first to /signin (guards `src/auth/cacheReset.ts`); `e2e/entra-search.spec.ts`: the Register Entra dialogs search the fake Graph (user by name in a dropdown attached under the input, already-registered option disabled, Escape closes only the list, keyboard pick, register, group search, object-ID toggle, plus the live Entra group members card: links, a 200-member cut, the 503 warning with Retry loading); `e2e/entra.spec.ts`: Entra
  sign-in as Alice, the failure card, bridge headers. Specs must look up the local submit with
  `{ name: "Sign in", exact: true }` (the Microsoft button also matches "Sign in").
- `e2e/demo-seed.ts` (`ensureDemoData`, called by `global-setup.ts`) creates whatever part of the demo dataset the
  journeys read is missing (Acme > Payments > Ledger/Fraud, Orders 1.0.0 + 1.1.0 + a 1.2.0 draft, the three-rule
  `acme-baseline` set, the "Acme batch table" template, the demo-analytics-team card, demo-alice, the
  acme-data-engineers group, the acme-pipeline service account and the Entra Alice and "Demo pipeline" registrations), so the
  suite also runs on the cold stack of `CI_FULL=1 scripts/ci.sh`. Each step looks before it creates; add new demo
  records there, never by hand on the live stack.
- `e2e/global-setup.ts` exports `DC_UI_E2E_ADMIN_PASSWORD` (from `/dev-out/admin_password`, bootstrapping through the API when
  it is absent or stale, like `tests/e2e/conftest.py::admin_password`); it reaches the gateway as `https://traefik`
  because Playwright's Node client ignores the Chromium host mapping.
- `src/components/Logo` (`LogoMark size`, `Logo` with the one-word "Contractum" wordmark; glyph in `LogoGlyph.tsx`): the brand tile
  (control radius, button glint) with the contract-sheet glyph (folded corner, two schema rows, a check). Used by the
  sidebar, top bar, sign-in panel (which adds the tagline "Data contract governance" under the wordmark) and boot screen; `public/favicon.svg` is the same mark with hex colours (linked from
  `index.html` and `redirect.html`). Never draw a text "DC" tile again.
- **Versions** (`features/shell/VersionsDialog.tsx`, `src/buildInfo.ts`): the UI build (the commit and build time from
  `__DC_BUILD__`, defined in `vite.config.ts` from the `GIT_SHA`/`BUILD_TIME` build args of `ui.Dockerfile`, never
  `package.json`; the platform version from `config.version`, which the nginx entrypoint writes from `DC_UI_VERSION`
  and `config.ts` validates, "dev" when absent; read through `useUiBuild()`; "dev" under Vitest and `pnpm dev`) plus `GET /api/v1/{identity,contracts,validation,audit}/version` (public,
  `ServiceVersionOut`) and `GET /api/v1/docs/version` (the api-docs container, same shape, typed locally because it is
  not in the merged document). The dialog (title "Versions") leads with "Contractum <UI version>"; a row whose version
  differs from the UI's carries a warning Badge "Different version" (text, not colour only). Settings > Platform >
  About does the same. A failed component reads "Unavailable", never an alert. One release gives every image the same
  platform version, supplied at startup (the image holds none, so the tested build is the released one).
  `scripts/e2e-ui.sh` exports `GIT_SHA` (12-character HEAD) for the build.
- `src/components/BootScreen/index.tsx`: logo mark plus `Loading` status, or the "Can't reach the server" state, styled
  from tokens (`BootScreen.module.css`).
- `src/components/Button` (`Button`, `IconButton`), `src/components/form` (`FormField`, `TextField`, `PasswordField`,
  `useFormFieldControl`), `Card`, `TextLink`, `VisuallyHidden`, `Stat`/`StatGrid` (KPI tiles), `SegmentedControl` (a `role=group` of toggle Buttons, `aria-pressed`
  on the current one, button-group.md: wrapper shadow, 1px overlap; the Source filter of the admin lists): the shared controls (UI-SPEC 5.1 to 5.3 and 5.10), each a
  folder with `index.tsx` and a CSS Module, covered by `src/components/controls.test.tsx`. `src/lib/cx.ts` joins CSS
  Module class names (module lookups are `string | undefined` under `noUncheckedIndexedAccess`). A busy `Button` is not
  `disabled`: it keeps focus, sets `aria-busy` and swallows the click (and a submit). `IconButton` requires `aria-label`
  by type. `TextLink` takes `to` (router `Link`) or `href`; `external` adds `target=_blank`, `rel="noopener noreferrer"`
  and a visually hidden "(opens in a new tab)".
- `src/components/Alert` (five intents, `role="alert"` only for danger, hidden intent prefix, optional `Dismiss alert`),
  `Badge` (Large only) with `Badge/status.ts` (the shared `statusBadges` map and `StatusBadge`; later phases add rows,
  never styling), `Avatar` (`initialsOf`), `CopyButton` (3 s confirmation, polite live region that never holds the copied
  value, `onError` when the clipboard rejects) and `src/components/states` (`EmptyState`, `Skeleton`, `Spinner`, the last
  reading `prefers-reduced-motion` through `matchMedia`): UI-SPEC 5.4, 5.7 and 5.9, covered by
  `src/components/feedback.test.tsx`. Under Vitest, `userEvent.setup()` replaces `navigator.clipboard`, so install a
  clipboard stub after it; with fake timers use `fireEvent` inside `act` (Testing Library only waits on real timers).
- `src/components/Combobox` (`Combobox`, quick 261119-r5g): the searchable dropdown, a WAI-ARIA combobox (input
  `role="combobox"`, `aria-expanded`/`aria-controls`/`aria-activedescendant`, `aria-autocomplete="list"`) over a
  `role="listbox"` of options attached under the input while typing. Props: `id`, `label`, `helper`, `error`, `value`
  - `onValueChange` (typing and the clear button only), `options: { key, label, detail?, meta?, note?, disabled? }[]`
    (line 1 the name, line 2 the secondary text and the object ID in the normal font, a disabled option with a `note`
    such as "Already registered"), `selectedKey`, `onSelect` (the caller puts the display name in the box and keeps the
    record, the list closes), `status` (a line in the popup: searching, no matches, "Type more to narrow the list"),
    `listLabel`, `controlSize`. Arrow keys skip disabled options, Enter picks without submitting the form, Escape closes
    the list only (a window capture listener runs before the Dialog's own Escape, so the dialog stays open), blur closes,
    typing reopens, the clear button resets. The popup is `position: fixed` from the input's box in a portal into
    `useDialogContainer()` (document body outside a dialog), so a Dialog's scrolling body never clips it, and flips
    above the input when there is no room below. It is the one search control of `EntraDirectorySearch` and
    `PrincipalPicker`; never put a radio list under a search box again. Covered by `Combobox/Combobox.test.tsx`.
- `src/components/Dialog` (`Dialog`, `useDialogContainer`), `ConfirmDialog`, `SecretRevealDialog` and `Checkbox` (from
  `@/components/form`): UI-SPEC 5.5 and 5.2, covered by `ConfirmDialog.test.tsx` and `SecretRevealDialog.test.tsx`. `Dialog` is
  controlled with no Radix Trigger, so any button opens it: the element focused at open is remembered and refocused on close.
  Sizes 448, 512, 896 and 1280; `dismissible={false}` drops the close button and ignores Esc, backdrop and outside focus.
  A popper opened inside a dialog portals into `useDialogContainer()` (C-24). `ConfirmDialog` is the destructive pattern for
  Phases 9 to 13 (Radix AlertDialog, focus on "Keep {object}" or "Go back", Danger confirm, optional `typeToConfirm`, busy
  label, stays open with a `ProblemAlert` when `onConfirm` rejects; closing is inert while it runs). `SecretRevealDialog`
  (`kind` token, key or webhook secret) renders nothing after "Close after storing {kind}"; it keeps no copy of the secret,
  and the caller must clear its own in `onClosed`. `CopyButton` gained `variant` and `announcement` for it.
- `src/styles/tokens.css`: the **data-contracts-enterprise** token sheet (`.claude/skills/typeui-design-system`, adopted
  2026-11-15, replacing Atlas): colors.md registry hex in `:root` (light) and under `@media (prefers-color-scheme: dark)`
  (automatic dark theme, never swapped manually), Inter 12/14/16/20/24/32, radii 6px controls / 8px cards / 12px
  overlays with concentric inner radii, the shadows.md scale plus `--shadow-button` (glint) and the input focus rings,
  uppercase hex (excluded from Prettier). Fundamentals exceptions are listed at its top (gray-500
  `--color-border-control` for interactive boundaries, one 2px `--color-focus-ring` outline, dark `--color-border-danger`,
  no warning or success button). `tokens.test.ts` asserts every hex against the registry in both themes and the measured
  WCAG contrast of every shipped pairing in light and dark. `global.css` holds the universal focus ring and resets. `fonts.css` declares Inter latin and
  latin-ext only (the package's `wght.css` would ship all 42 subsets). All three are imported first in `main.tsx`.
- `.stylelintrc.json`: forbids raw hex and colour functions outside `tokens.css`, the glint parts (`--color-1-*`),
  `fg-disabled`, the retired Atlas `--radius-xxl`/`--radius-none`/`--elevation-*`, px font sizes and any `box-shadow`
  other than `none` or one `var(--shadow-*)`; `src/styles/stylelint.test.ts`
  proves each rule. `pnpm lint` runs ESLint then stylelint.
- `vite.config.ts`: router plugin (before `react()`), dev `/config.json` middleware, `@` alias, dev server and `/api` proxy.
- `dev/config.json`: development runtime config (local login on, Entra off, `apiBase` `/api/v1`).
- `nginx/default.conf`, `nginx/40-ui-config.sh`, `public/healthz.txt`, `../infra/docker/ui.Dockerfile`: the `ui` container
  (deps, build, runtime stages; unprivileged nginx on 8080, read-only root with a tmpfs `/tmp`, no secret). The entrypoint
  validates every `DC_UI_*` value against an allow-list and exits 1 on a bad one (the container then does not start), then
  writes `config.json` (scopes as one space-separated string, as `src/config.ts` reads it), `headers.conf` (CSP with
  `frame-ancestors 'none'`, nosniff, Referrer-Policy, Permissions-Policy, COOP) and `headers-bridge.conf` (for
  `/redirect.html`: `frame-ancestors 'self'`, no COOP). Every nginx location includes a header file, because any
  `add_header` in a location drops the server-level ones; the immutable asset `Cache-Control` has no `always`.
  `tests/repo/test_ui_entrypoint.py` runs the script without Docker; `tests/e2e/test_gateway.py -k ui` proves it through Traefik.
- `openapi/openapi.json`, `src/api/schema.gen.ts`, `src/api/client.ts`: the generated-client chain (UI-06, D-UI-08-30).
  `uv run python scripts/export_openapi.py` builds the apps in-process (no stack), merges the four routed documents and
  writes the byte-stable snapshot; `pnpm gen:api` turns it into `schema.gen.ts`. `createApiClient(config, { fetch? })`
  is openapi-fetch typed with `paths`; its base URL is `apiBase` minus a trailing `/api/v1` (the document's paths already
  carry `/api/v1`). Call the API only through it. Two drift layers fail CI after a backend route change without
  regeneration: `tests/repo/test_ui_openapi_snapshot.py` (unit step) and `pnpm gen:api:check` (`scripts/gen-api-check.mjs`,
  byte comparison, run by `pnpm ci:static`). `scripts/ci.sh` runs `pnpm ci:static` natively when Node and pnpm match the
  pins, otherwise through the Dockerfile `ci` stage (`CI_UI=0` skips it).
- `src/api/problem.ts`, `src/api/fieldErrors.ts`, `src/components/ProblemAlert` (UI-05, D-UI-08-24): `createApiClient` throws
  an `ApiError` (`isApiError`) for every non-2xx (`parseProblem` into a `Problem`: slug only from `urn:dc:problem:<slug>`,
  title, detail, `errors[{pointer, detail}]`, `X-Request-ID`; no other body text is ever copied) and for a rejected fetch
  (`networkProblem()`; an `AbortError` is left alone). `copyFor(problem, context)` gives the fixed Copywriting Contract
  wording (sign-in 401, 429, 503, network, 403 on an action) or null. `mapFieldErrors(problem, { "/pointer": "fieldName" })`
  returns per-field messages plus the unmapped rest for the alert list. `ProblemAlert` is danger, or warning for 503, 429
  and network; `context`, `onRetry` ("Retry loading") and `fieldLabels` are optional. Tests: `problem.test.ts`,
  `ProblemAlert.test.tsx`.
- `src/test/msw.ts` (`server`, `http`, `HttpResponse`) is started by `src/test/setup.ts` with the "error" strategy for
  unhandled requests (msw 3 `onUnhandledFrame`); `setup.ts` also records every unmocked call and fails the running test in
  `afterEach`, because the screens absorb a failed request. Its base handlers answer the public `GET /auth/policy` and
  `GET /settings/public` (`defaultAuthPolicy`, `defaultPublicSettings`); it is reset after each test; add handlers with
  `server.use(...)`. `setup.ts`
  also resolves same-origin paths against the jsdom origin for `Request` and `fetch`, as a browser does (Node throws on a
  relative URL). It also turns off CSS animations and transitions in the test DOM: jsdom (31 and later) computes
  `animation-name` but never fires `animationend`, so a closing Radix dialog would otherwise stay mounted.
- **App shell** (`src/features/shell/`, Enterprise preview layout): `_app.tsx` renders `AppShell`: skip link; at 1024px and
  up the slate icon `Rail` (`nav` "Sections", one icon per registry section, the current section a brand tile plus a
  light edge bar; at the bottom "Versions" (info icon, `VersionsDialog`), the account avatar (`AccountMenu
variant="rail"`, tooltip = display name, menu opens to the right with the name, "Role · Folder", the sign-in method,
  then "Account settings", "Access tokens", "Versions", "Sign out"), API docs and the panel toggle; tokens
  `--color-rail-*`) and the white navigation panel `Sidebar` (starts with the product mark `LogoMark`; `GlobalSearch` to `/contracts?q=`; uppercase section labels; active item brand-softer + fg-brand; no
  folder tree: `/folders` is the folder navigator), the panel hideable from the rail and remembered in `localStorage` key
  `dc-sidebar-collapsed`; under 1024px a compact `TopBar` (toggle, mark, and `AccountMenu variant="topbar"` at the end)
  opens a start `Drawer` with the panel. Specs open the account menu with `openAccountMenu`/`signOut` from
  `e2e/personas.ts` (rail at 1024px and up, top bar below); one
  `<main id="main-content">` on the page surface and route-change focus (to the
  `[data-page-heading]` h1, else main, plus a polite title announcement). **Navigation registry:**
  `src/features/shell/nav.ts` `NAV_SECTIONS` (sections Explorer, Governance, Subscriptions, Audit, Admin; each entry
  `{ id, label, to, icon, visible?(access) }`, `access` = `HeldAccess` from `useHeldAccess`). Add or edit entries
  there; never hand-write sidebar markup. `visible` only hides; gate on `holdsAny(access, ...)` with the permission the
  area's first request needs, never on a baseline principal permission (`audit:list`, `group:list`, `entra:list`,
  `service-account:list`, ...: every signed-in principal holds those, so they say nothing).
  The Admin section lists exactly Users (`user:read`, `user:create`), Groups, Service accounts, Access tokens and Settings (`settings:read`, Global admin only; quick 261120-0yc): there is no
  Identities entry (the Entra registrations live on the merged Groups, Service accounts and Users lists), so Groups and
  Service accounts are also shown to holders of `entra:read` or `entra:manage` (quick 261119-l30).
- **Placeholders:** none left; every nav area has a real screen. `src/features/placeholder/BeingBuilt.tsx` remains for future areas. `/contracts` must keep its `q` search param (the top bar search sends it).
- **Shared page building blocks** (reuse these, do not re-create them):
  - **One page frame.** Every route renders `PageHeader` (`title`, optional `documentTitle`, `crumbLabel`,
    `description`, `breadcrumbs: Crumb[]`, `badges`, `actions` on the right): the breadcrumb always starts at Home and
    ends at the current page (`frameCrumbs` adds both; pages pass only the trail between, with a section crumb such
    as "Administration" or "Governance"), sets `document.title` to "{title} · Contractum" and marks the h1 for
    route focus. Tabs (`TabNav`) follow the header directly; the shell owns width (content = 80% of the main area,
    `padding-inline: max(32px, 4%)`, no cap). `NotFound` renders the same frame (`nested` gives an h2 inside a page
    that already has one). `features/shell/pageFrame.test.tsx` renders every leaf route under `/_app` and asserts a
    Home breadcrumb and exactly one frame h1. `Breadcrumb` (`items: { label,
to? }[]`, the last is the current page).
  - `DataTable<T>` (`caption`, `captionHidden`, `columns: Column<T>[]` with `cell`, `rowHeader`, `align`, optional
    `sortValue` for client sorting, `rows`, `getRowId`, `compact`, `loadMore: { hasMore, isLoadingMore, onLoadMore,
error }`) plus `orNotSet(value)`. Pair it with `useCursorList` (`src/lib/useCursorList.ts`: `{ queryKey,
fetchPage(cursor) }` over `{ items, next_cursor }` pages, returns `rows`, `isPending`, `error`, `pageError`,
    `hasMore`, `isLoadingMore`, `loadMore`, `refetch`). The footer, "N more loaded" announcement and focus on the
    first new row are built in.
  - `Tabs` (Radix, in-page panels) and `TabNav` (route links, `aria-current` from the router); `Menu` (Radix dropdown:
    `trigger`, `items: { label, icon?, onSelect? | to?, danger?, disabled?, unavailableReason? }[]`, optional `header`;
    `unavailableReason` keeps the item focusable but `aria-disabled`, with the reason in a tooltip below the item and `aria-describedby`;
    selecting it does nothing and keeps the menu open, unlike `disabled`, which Radix skips); `Drawer`
    (`side` start or end, `width` default or wide); `Tooltip` (the shell renders the provider); `NotFound` (detail 404
    and unknown URLs, optional `back`); `SelectField` and `TextareaField` in `@/components/form` (same frame as
    `TextField`; `SelectField` takes `options` and an optional `placeholder`, `TextareaField` takes `monospace`;
    `labelHidden` keeps a label for assistive technology only, for a compact header control). `IconButtonLink` is a
    router link drawn like an `IconButton` (row quick actions that navigate).
  - `src/lib/unexpected.ts`: `problemOf(error)` (the Problem for any error, generic for non-API errors) and
    `isNotFound(error)`. `src/api/types.ts`: short aliases of generated schema types (`Me`, `Folder`, `Capabilities`,
    `ContractSummary`); add aliases there rather than hand-writing shapes.
- **Settings** (`src/features/settings/`, quick 261120-0yc, route `/admin/settings?tab=entra|notifications|governance|security|platform`,
  file `routes/_app/admin/settings/index.tsx`, nav entry gated by `settings:read`): `SettingsPage` renders one tab per settings section the API
  returns, from the fixed `TAB_REGISTRY` (Entra, Notifications, Governance, Sign-in & security, Platform; an unknown or unavailable `tab`
  falls back to the first available), with Export
  and Import as page-level actions. A principal without `settings:read` sees `NotAvailable` and no settings request is made (`useSettings`,
  `useEntraOverview` and `useAbout` are `enabled` only for `hasGlobal(me, "settings:read")`). `api.ts` holds the keys under `["settings", ...]`
  (`useSettings`, `usePublicSettings` key `["settings","public"]`, `useSaveSection` with `If-Match` = the section etag, `useEntraOverview`,
  `useEntraTest`, `useAbout`, `useExportSettings` (blob download, file name from `Content-Disposition`), `useImportSettings` (the file's own
  text as the body; dry run first, Apply sends the combined etag)). Every tab form shows "Default: X" beside each field and a "Reset to
  default" that clears the override and saves; a 412 reads "These settings changed elsewhere" with "Reload settings" (`SaveProblem`),
  422 pointers land on fields, a 429 or 503 on Test connection is a warning, never a red alert. `EntraTab`: default-tenant Select
  (configured tenants only), default-owner `FolderSelect` (ROOT and Organizations), the "Managed in Helm (view only)" card (tenants,
  Graph client, secret metadata, expiry with a rotation reminder when unknown or under 30 days, Test connection). `PlatformTab`: the banner
  form (plain text, 500 characters, severity, optional window in local time sent as UTC, live preview) and About as a `DataTable` (the UI build
  plus the five services; "Unreachable" is a neutral badge). `ImportSettingsDialog` (title "Import settings", size 896, refuses a file over
  256 KiB). `features/shell/AnnouncementBanner.tsx` is mounted by `AppShell` above the page content: the active banner from
  `GET /settings/public` as React text (never HTML), info or warning Alert, dismissible per browser session (`sessionStorage` key
  `dc-banner-dismissed:<hash of severity, start and text>`), nothing when absent or failed. The Register Entra dialogs pre-fill the default
  tenant and, only when it is one of the caller's own `entra:manage` owner options, the default owner. `renderApp` answers
  `/settings/public` with an empty answer by default. Specs: `e2e/settings.spec.ts` (admin journey plus the `ledger-editor` refusal).
  `NotificationsTab` (quick 261120-0yd, N1 to N6): one form over the whole `notifications` section (saved with `If-Match`; a field equal to
  its default is not stored). Email templates: a Notice Select with plain labels (Version published, Published with breaking changes, Latest
  version changed, Version deprecated, Sunset approaching, Unpublish blocked, Pin removed, Other notices), Subject and Body in the normal font
  (plain text), the allow-listed placeholders as `code` tokens, Preview (`usePreviewTemplate`, POST `/settings/notifications/preview`, shown
  as plain text with `white-space: pre-wrap`; a 422 pointer lands on the field) and a per-template "Reset to default" that saves the stored
  values without that template. A save 422 at `/email_templates/<event>/subject` selects that event. Sender name and address with "Default:
  <address>" and "Send test email", which opens `SendTestEmailDialog` ("Send a test email", `useSendTestEmail`, POST
  `/api/v1/contracts/settings/test-email`; the outcome is an info Alert (sent) or a warning Alert (refused, unavailable, not configured, or
  a 429 with the wait from the `retry_after_s` problem member); it uses the saved sender). Retries: delays in seconds, comma separated, with
  a plain-unit echo ("1 min, 5 min, 30 min", `humanSeconds`), maximum attempts; sunset lead days; Webhook security: HTTPS only as an
  "Enforced" badge (no control) and the allowed hosts, one per line, a mandatory list (quick 261120-s0p): helper "Webhooks are sent only to these
  hosts, one per line (hooks.example.com or *.example.com).", "Default: no host is allowed" and, with the list empty, a warning Alert "No webhook host
  is allowed, so card owners cannot add webhooks and none are sent." `CardFormDialog` maps a 422 `webhook-host-not-allowed` to the Webhook URL field
  ("This host is not on the list of allowed webhook hosts.") with, for `hasGlobal(me, "settings:read")`, the link "Add it in Settings"
  (`/admin/settings?tab=notifications`) and for anyone else "Ask a Global admin to add it in Administration > Settings > Notifications."; a 503
  `settings-unavailable` stays the warning `ProblemAlert`. `e2e/layout.spec.ts` never clicks "Reset to default" (it writes).
- **Governance, Sign-in & security and retention** (quick 261120-0ye): `GovernanceTab` (grace period in days with "0 enforces new mandatory
  versions at once", the default ODCS version Select from `choices.odcs_api_versions`, the "Require a reason to unpublish or deprecate"
  checkbox) and `SecurityTab` (the sign-in methods card with Microsoft disabled and a reason when `choices.microsoft_signin_available` is
  false; a `signin-lockout-risk` refusal (409 or 422) is a warning Alert "This change could lock you out" with the problem detail and
  "Nothing was changed.", never a red alert and the stored value stays; the access-token ceiling in days; session length in hours and idle
  timeout in minutes; lockout thresholds and the longest lock in minutes; password rules with "Applies to new passwords only"). Hours and
  minutes convert to the API's seconds in `settings/units.ts` (`toDisplay`, `fromDisplay`, `rangeHint`; unit tests in `units.test.ts`); a
  field equal to its default is not stored; each card has "Reset to default for ...". `PlatformTab` gained the Audit retention card
  ("Keep forever" or "Delete after N days", at least 90, "Settings changes are always kept"); the banner and retention forms each send the
  whole `platform` section (the other field rides along). `GET /api/v1/identity/auth/policy` (public) is read by `signin/passwordRules.tsx`
  (`useAuthPolicy`, `usePasswordRules`, `describeRules`, `unmetRule`, `PasswordRulesList`): `SignInPage` offers a method only when the
  runtime config enables it AND the policy says it is on (nothing while loading, the config alone when the call fails), `RotatePage` and
  `ChangePasswordPage` list the rules ("At least 14 characters", ...) and pre-check them (the server still decides). The token and key
  expiry picker caps at `credential_max_ttl_days` of `/settings/public` (`expiresAtFor(value, now, maxDays)`, 365 until it is read);
  `NewContractPage` starts a blank document with `default_odcs_api_version` (v3.2.0 when the call fails); the deprecate and unpublish
  dialogs (`VersionActions`, titles unchanged) have a Reason field, optional or required by `require_lifecycle_reason`, sent as
  `{reason}`. `renderApp` answers `/auth/policy` and the extended `/settings/public` by default.
- **Access** (`src/features/access/`): `useMe()` (query key `["me"]`) and `hasGlobal(me, permission)`;
  `useCapabilities(folderId?, { enabled? })` (key `["capabilities", folderId ?? "global"]`) returns `{ can(permission), data,
isPending }` and only hides or disables controls; `useHeldAccess()` (`useHeldAccess.ts`) unions `/me` global
  permissions with the capabilities at every folder holding a grant (same cache keys) into `HeldAccess`, plus
  `holdsAny`, `isBaselinePermission` (dc_auth target `principal`/`special`) and `grantedGlobalPermissions` (what the
  Home "Global permissions" stat counts). **Non-admin rule:** never request what the capabilities rule out (`enabled:`
  on the query, or leave the section out); a read refused by design renders `NotAvailable` (`@/components/states`,
  "Not available at your access level"), never a red ProblemAlert. Admin and non-admin get the same layout and
  defaults; only the content differs (for example `EffectiveAccess` is collapsed behind "See details" for everyone); `roles.ts` has `roleLabel`, `roleMeaning`, `kindLabel` and
  `childKindOf`. `permissionRegistry.ts` is the snapshot of dc_auth `PERMISSIONS`/`Role` (guarded by
  `tests/repo/test_ui_permission_registry.py`); `permissionLabels.ts` gives every id a label, tooltip, area and
  capability column (`permissionLabels.test.ts` fails on an unlabelled id) and `summarizeAccess`; `EffectiveAccess`
  (+ `effectiveAccessModel.ts`: `folderRoles`, `folderInheritance`, `homeRoles`, `readableFolderPath`) is the one
  plain-language "Your effective access" view, used by the folder Access tab and the home "Your access" card.
- **Home** (`src/features/home/HomePage.tsx`): "Welcome, {name}", the account card, the full-width "Your access" card
  (`EffectiveAccess` over `/me` plus the capabilities at each folder holding a grant) and quick links.
  **Folder switcher** (quick 261119-rtc): the role lines stay the full list, but inside "See details" a caller with
  grants on 2+ folders gets a "Show access at" `SelectField` (Root first, then readable paths; default = the folder of
  the first role line) and the matrix, the "No access:" line and the technical names describe only that folder and
  below ("What you can do at {folder} and below"), never the union of every grant (an Org admin on one organization
  plus an Org reader on another used to merge into one misleading table). `useHeldAccess` returns `byFolder`
  (`{ folderId, permissions }[]`, the same capability queries and cache keys; the capability answer already includes
  inherited roles, so it is used as is and never unioned across folders) next to the unchanged `permissions` union that
  the navigation reads. `EffectiveAccess` takes optional `scopes: AccessScope[]` and `initialScopeId`; with one scope
  or none it behaves as before (the folder Access tab passes none).
  **Account** (`src/features/account/`): `ProfilePage.tsx` at `/account/profile` (the first tab and the account
  menu's "Account settings" target; tabs Profile, Password, Access tokens) edits the caller's own display name with
  `PATCH /api/v1/identity/me` (success toast, then `settle(users, principalName, principalNames)` which also
  refreshes `["me"]`: Home, Your account and the account menu; a local interactive user edits, an Entra user sees the
  read-only field with "Your name comes from Microsoft Entra ID"; 422 pointers arrive as `/body/display_name`).
  `SessionsCard.tsx` (quick 261120-s0p) is the Sessions card below the Profile card (`mode="self"`, `GET /me/sessions`: device, signed in, last active, ends, a "This session" badge, no ids shown; an Entra account sees "Microsoft Entra ID manages your sign-in sessions." and requests nothing) and, as `mode="admin"`, on `/admin/users/$userId` (listed with `user:read`, sign-out controls with `user:deactivate`). Interactive sign-ins only get "Sign out" per row and "Sign out everywhere else" (ConfirmDialogs, success toasts); the note says a sign-out ends at the next refresh and the issued access token lives up to `access_token_lifetime_s`. `e2e/sessions.spec.ts` walks it as the dedicated persona `sessions-user` (Team reader, in `PERSONAS`) in two browser contexts and as the Global admin; the layout crawl's DENY only skips an exact "Sign out" button so these dialogs open.
  `ChangePasswordPage.tsx` at `/account/password`: current, new and confirm fields; success clears the token and goes
  to `/signin?notice=password-changed` (the API revokes every session).
- **Folder explorer** (`src/features/folders/`): `api.ts` (`useFolderList(includeArchived)` pages `GET /folders` up to
  2,000 and is shared by the folders page, the header picker and breadcrumbs; `useContractCount(folderId)` one cached
  contract list page per visible navigator row (it and the tree's per-folder pages share one four-at-a-time limiter, `lib/limiter.ts`
  `createLimiter`, also used by the group member counts, so a page with many rows never queues the services into a 503); `useFolder(id)`; `useFolderWrites()`
  create, rename, archive, unarchive, each invalidating `["folders"]` and `["capabilities"]`), `tree.ts` (built from
  `parent_id`; a readable ROOT is the single top node "Root", open by default, holding organizations, then projects,
  then teams; `folderName` gives "Root"), `FolderTree` (WAI-ARIA tree with roving
  tabindex, arrows, Home, End, type-ahead, Enter opens; optional `renderMeta`/`renderActions` per visible row),
  `FoldersPage` (`/folders`, `?archived=true`: the primary navigator, rows with kind badge, subfolder and contract
  counts, "Create {child}" and open quick actions), `FolderHeader` (every ancestor crumb a link, a compact "Switch
  folder" `FolderSelect` that keeps the tab, kind and Archived badges, "Create {kind}", actions menu, archived banner, tab nav;
  **Delete folder…** (quick 261119-0v1) is a danger menu item shown only for `folder:delete` holders on non-ROOT folders:
  `useFolderDeletionCheck` (key `folderKeys.deletionCheck(id)` under `["folders"]`, `enabled` only for those holders, so
  ROOT and non-admin personas never request it, 30 s stale) feeds the item: deletable opens a destructive `ConfirmDialog`
  (title names the folder, body the path and "Also permanently removes ..." from `purges`), blocked or failed turns it into
  an `unavailableReason` listing the blockers (`copy.ts` `blockerPhrase`, `deleteUnavailable`). `useFolderWrites().remove`
  does not invalidate: the header navigates to the parent (`/folders` for a ROOT parent) first and then calls `settle`. The
  tree rows deliberately have no delete action: a per-row state would need one deletion check per visible row, each
  fanning out to identity, contracts and validation), `FolderOverview`,
  **"Show contracts in tree"** (`/folders?contracts=true`, off by default, next to `archived`; the `showContracts`
  FolderTree prop): `folderContractsOptions(api, folderId)` (key `["contracts","folder-tree",id]`, `include_subtree=false`,
  `sort=name`, `limit` 100, `staleTime` 30 s) and `useFolderContractQueries` make one list call per visible expanded
  folder and none per contract. `tree.ts` `visibleRows` emits the `TreeRow` union (`folder`, `contract`, `overflow`,
  `loading`, `error`) with unique key prefixes `f:`, `c:`, `o:`, `l:`, `e:`; subfolders come first, then the
  contracts in the server's order (never re-sorted), then one state row; a folder with no contracts gets no row, and a leaf folder whose list came back empty loses its arrow (`isExpandableFolder`). `headBadges(row)` in
  `features/contracts/versionStatus.tsx` is the only lifecycle mapping of a list row (Draft, Preview, Latest, Unpublished,
  Deprecated); `contractVersionSlot` and `contractRowLabel` (`versionSlot.ts`) build the version cell and the row's accessible
  name. FolderTree hooks: `contractLabel`, `renderContract` and `narrowInline` (a column whose badge is repeated in the name
  cell, hidden by CSS outside the narrow container query). The overflow row reads "More contracts — open folder" (no count:
  the list API has no total) and links to `/folders/{id}/contracts?subtree=false`. **Recorded exception:** a per-folder
  tree error is an inline row with **Retry loading**, not a `ProblemAlert` (like the member count's "Not available"), so
  one failing folder does not replace the whole tree. A metadata-only contract (`content_redacted`, the Policy Manager)
  opens the normal page with an info notice and only the Overview and Rule sets tabs (`contractAccess` in
  `ContractLayout.tsx`). `clearCacheOnSessionChange` clears the query cache when the token's principal (`principalOf`:
  issuer and subject) changes, not only on sign-out, so a tree never shows another principal's contracts.
  `FolderContracts` (`/folders/$folderId/contracts`, `?q=&subtree=`), `FolderChildren` (`/folders/$folderId/folders`),
  `CreateFolderDialog` and `RenameFolderDialog`. **Folder tabs registry:** `tabs.ts` `FOLDER_TABS` (`{ id, label,
segment, visible?(folder), requires? }`, `tabVisible(tab, folder, caps)`: `requires` is any-of at the folder and
  hides the tab once capabilities are known); a later phase adds its child route file under
  `routes/_app/folders/$folderId/` and one entry there. `OwningFolder` (`features/folders/OwningFolder.tsx`) renders an
  owning folder in body text ("Platform", the path, or a muted `HIDDEN_FOLDER_LABEL` "Folder you cannot see"); use it
  instead of a monospace cell.
- **Contracts** (`src/features/contracts/`, routes under `routes/_app/contracts/`): `/contracts` (global list, `q`,
  `status`, `domain`, plus `deleted*` params for the restore notice), `/contracts/new?folder=` (five sources; the Folder select lists every non-archived folder, Root first, with a "You cannot create contracts in this folder." helper when `contract:create` is not held there; New contract and Use in new contract show on any non-archived folder where it is held),
  `/contracts/$contractId` layout with Overview, `document`, `drafts`, `drafts/$draftId` (editor, conflict dialog,
  publish gate), `versions`, `versions/$ref`, `diff?from=&to=`, `rule-sets`, `subscribers`. `api.ts` holds the query
  keys (`["contracts", ...]` lists, `["contract", id, ...]` per contract), raw-text document fetches with `Accept`
  (`parseAs: "text"`) and raw-body writes (`bodySerializer`). `report.ts` reads the untyped validation report into one
  finding list (`flattenReport`, grouped by the server's `blocking` flag); `FindingsPanel` renders it; `versionStatus.tsx`
  maps a version to text-plus-icon badges. The Publish dialog (`PublishDialog.tsx`) shows an advisory warning Alert
  when `documentStatus(saved)` (the saved bytes, which are exactly what publish sends) is not `active` (the real
  status, or "sets no status"); it never disables Publish and mirrors the platform rule `published-status-active`,
  which warns only on publish and never blocks. Clearer refusals (quick 261119-wk8): a draft save refused with
  `odcs-id-mismatch` shows "This contract's id is X. The document says Y." (ids in the normal font; the problem's
  `expected_odcs_id` and `found_odcs_id` are allow-listed in `api/problem.ts`) and, when the buffer allows it, a
  "Put the contract's id back" button that rewrites only the top-level `id` value (`idEdit.ts`, YAML and JSON) and
  never saves. Findings computed for text that has since changed render as stale: a warning Alert ("These findings
  are for revision N. Save and validate to check the current text.") with the page's save-and-validate action, the
  list collapsed behind "Show previous findings", headings "Earlier errors/warnings/info" (never "Blocking") and
  no locations. Structural unknown-property findings name the offending keys in the API message.
- **Editor** (`src/features/editor/`): `CodeEditor` and `DiffView` (lazy Monaco chunk; under Vitest a plain textarea with
  the same props, so screen tests can type into it) and `pointerMap.ts` (`locatePointer(text, pointer, "yaml"|"json")`
  to a line range, nearest-ancestor fallback, null when the buffer does not parse). Reuse them for templates and rule
  checks.
- `HttpProblem.extensions` (`src/api/problem.ts`) carries an allow-list of problem members screens may read (`report`,
  `run_id`, `referrers`, `hidden_referrer_count`, `deleted_target_count`, `reasons`, `highest_semver`,
  `existing_semver`, `blockers` (the `folder-not-empty` list `[{kind, count}]`)); add a member there before reading it, never render the object raw.
- **Governance** (Phase 11 screens): `src/features/templates/` (`api.ts` keys under `["templates"]`, raw YAML/JSON
  bodies via `bodySerializer` and `parseAs: "text"`; `TemplatesPage` `/templates?folder_id=`, `TemplatePage`
  `/templates/$templateId` with Draft and Versions tabs, `FolderTemplates` tab), `src/features/rule-sets/` (`api.ts`
  keys under `["rule-sets"]`, draft writes send `If-Match: "r<draft_revision>"` and a 412 shows "Reload draft";
  `check.ts` parses the untyped check report; `rollout.ts` formats the server mode, never the clock; `ruleValues.ts`
  form conversion with fixtures as JSON arrays; `RuleSetLists` (Defined here / Available here, also the folder tab),
  `RuleSetPage` `/rule-sets/$ruleSetId?via=&tab=` with Draft, Versions, Rollout and Impact; platform and inherited
  sets fall back to the read-only `available-rule-sets` views through `via` or ROOT; Rollout offers Pause/Resume for
  platform sets (ROOT `ruleset:mandate`) and Change date… for any resolved version, gated by capabilities) and `src/features/governance/`
  (`DryRunPage` `/governance/dry-run?rule_set_id=&version=&folder_id=`, `DriftPage` `/governance/drift?folder_id=`,
  `DryRunResults`, client-side `filters.ts`, shared `gov.module.css`). `src/features/folders/FolderSelect.tsx`
  (`FolderSelect`, `useFolderPathLabel`, `useRootFolder`) is the folder picker for any screen.
- **Subscriber cards** (`src/features/cards/`, replaces the `/cards` placeholder): `api.ts` (`cardKeys`, `useCard`,
  `usePublishedVersions(contractId)`, `useCardWrites()`: create, update, archive, members, subscribe, move,
  unsubscribe, rotateSecret, each invalidating `["cards"]`), `CardIndex` (shared all-cards list over `GET
/contracts/cards`: scope slot, search, archived and active filters, "N cards in M folders", folder path column, pin
  chips; `folderLabel`), `CardsPage` (`/cards?folder=&subtree=&q=&archived=&active=`, scope "All folders (Root)" first,
  "Include subfolders" on by default), `FolderCards` (the folder tab `/folders/$folderId/cards` "Subscribers", scoped
  to the folder with subtree on), `CardPage`
  (`/cards/$cardId?tab=`, tabs Overview, Members, Subscriptions, Notices, Webhook), `CardFormDialog` (create from any folder kind: the Folder select lists every non-archived folder in scope, Root first, preselects the scope, helper "You cannot create cards in this folder." without `card:create`), `SubscribeDialog`
  (`VersionSelect`, `MoveDialog`). The rotated webhook secret only lives in component state until
  `SecretRevealDialog` closes.
- **Audit explorer** (`src/features/audit/`, replaces the `/audit` placeholder): `query.ts` (`AuditSearch`,
  `validateAuditSearch`, `toApiQuery` with RFC 3339 UTC times, local `datetime-local` helpers, presets), `changes.ts`
  (RFC 6902 renderer: `resolvePointer`, `describeChanges`), `AuditFilters`, `AuditPage` (keyset table) and
  `AuditEventDrawer` (`?event=<id>`; envelope, change list, before/after, detail). Deep link from other screens with
  `<Link to="/audit" search={{ aggregate_type, aggregate_id }}>`.
- **Access administration and identities** (`src/features/admin/`, Phase 12; reorganised by WHAT in quick 261119-l30):
  `shared.tsx` (`idKeys` under
  `["identity", ...]`, `useSettle(...keys)` which also invalidates `/me` and capabilities, `ActiveBadge`,
  `CredentialStatus`, `useFolderLookup().ownerLabel`, `ownerOptions(me, folders, permission)`, `useRowFilter` +
  `RowFilter` ("Filter loaded rows", "Hide deactivated"; still used by `TokensPages`), `dateInDays`/`expiresAtFor`/`daysUntil` with the 365-day cap),
  `principal.ts` (`PRINCIPAL_ID_PATTERNS`, `principalIdOf`, `principalTypeLabel(type, principalId?)` the one plain-word
  label helper: "Local user", "Entra user", "Service account", "Entra service principal", "Local group", "Entra group", never a
  raw id such as `entra_user`; role descriptions as copy only),
  `idKeys.principalName` / `idKeys.principalNames` (prefixes of the `PrincipalName` and `usePrincipalNames` query
  keys: invalidate them after any rename so "by whom" cells refresh), `usePrincipalNames` (id to name for
  grant rows), `CreateCredentialDialog` (PAT/key create without useMutation so the token never enters a TanStack cache;
  pair with `SecretRevealDialog`).
  **Merged lists:** `UsersPage` (`/admin/users`), `GroupsPage` (`/admin/groups`) and `ServiceAccountsPage`
  (`/admin/service-accounts`) each show the local and the Microsoft Entra records in ONE `DataTable` with a Source column
  (`StatusBadge` rows `source-local` / `source-entra`), the segmented All | Local | Entra filter stored in the typed
  search param `source` (absent = All), a search box stored in `q` (`validateSourceSearch`: source allow-listed, `q`
  trimmed and capped at 200) and one **New** menu (`NewMenu`) whose entries reuse the existing dialogs: Users "Create local
  user" / "Register Entra user", Groups "Create local group" / "Register Entra group", Service accounts "Create service
  account" / "Register Entra service principal" (`EntraRegisterDialog kind=...` in `EntraRegisterDialog.tsx`, which also
  exports `EntraProblem` and `ENTRA_UNREACHABLE`; titles and submit labels equal the menu labels). Columns: Users Name,
  Username/email, Source, Status; Groups Name, Source, Owner, Members; Service accounts Name, Source, Owner, Status.
  `SourceList.tsx` holds the shared parts: `SourceToolbar` (SegmentedControl, search, optional "Hide deactivated"; stays
  mounted while a source loads), `MergedListView` (skeleton, empty state, toolbar, table), `useMergedCursorLists` (the two
  `useCursorList` queries under their existing keys, rows sorted by name, one "Load more" that loads each list that has more;
  when only one source fails the loaded rows stay and `PartialFailure` names it, "Entra groups could not be loaded",
  with Retry loading), `sourcesShown`, the column and menu helpers `sourceColumn<T>()`, `ownerColumn<T>(lookup)` and
  `localEntraMenuItems({...})` (quick 261121-0sh; Groups and Service accounts both use them, and the local create dialogs are one
  `CreateOwnedDialog.tsx` parameterised by title, copy, id prefix and the create call), and the member count (`useMemberCount`, key `[...idKeys.members(id), "count"]`,
  pages `GET /groups/{id}/members` at 100 per page, stops after 10 pages and shows "1,000+", every request goes through one
  module-level limiter of 4 concurrent requests because the gateway's default limit is 50/s; counts are requested only for
  loaded local rows, never for `source=entra`; a failed count reads "Not available", never an alert).
  **Gating (never request what the capabilities rule out):** the Entra filter, rows and menu entries need
  `config.entraEnabled` AND the caller's right: Users need the root-level `user:read` (both user lists need it; without it the
  page is `NotAvailable` and nothing is requested) and `user:create` for both menu entries; Groups and Service accounts
  need `entra:read` or `entra:manage` held anywhere (`useHeldAccess` + `holdsAny`) for the list, and an `entra:manage`
  owner option (`ownerOptions`) for the register entry; nothing is requested until the caller's access is known. Without
  Entra the Source filter is not drawn, the Source column still reads "Local" and a `source=entra` URL is treated as All.
  **Searching dialog (quick 261119-ofa):** `EntraRegisterDialog` opens in search mode: for groups and service principals the
  Owner select comes first, then `EntraDirectorySearch.tsx` (debounced 300 ms, 2+ characters after the same stripping the
  server applies, `GET /api/v1/identity/entra/directory/search` through `useQuery` keyed `[...idKeys.entraDirectory, kind,
owner, tenant, q]`), a `Combobox` dropdown under the box (display name, then the secondary line and object ID in the normal
  font, already-registered options disabled with "Already registered"; a pick puts the display name in the box and the
  secondary text and object ID in the helper line), the empty, truncated and "Searching…" states in the popup, a warning Alert with "Retry search" when the
  directory is unreachable or not configured (503), and the "Enter an object ID instead" / "Search the directory instead"
  toggle (the object-ID mode is the old GUID field and still works without a directory). Submitting posts the picked
  `object_id` and `tenant_id` to the unchanged register routes and settles `idKeys.entraDirectory`. `MergedListView`'s
  `empty.action` is optional and the three pages pass none: the header **New** is the only entry (exactly one New button,
  also when empty). Elsewhere an `EmptyState` action stays only when its screen has no header action of its own.
  **Live group members (quick 261119-wp6):** the Entra group detail page shows a "Members" card
  (`EntraGroupMembers.tsx`) from `GET /api/v1/identity/entra/groups/{registration_id}/members`, read LIVE from Microsoft
  Graph because Contractum never stores Entra membership (key `[...idKeys.entraGroups, registrationId, "members"]`). A
  `DataTable` of Name (a link to `/admin/entra/users/$entraUserId` or `/service-principals/$registrationId` only when the
  response carries `registered` and a `record_id`, i.e. the caller may open that record; nested groups and unknown users are
  plain text), Email or app ID (normal font), Type (User, Group, Service principal) and Status (Enabled, Disabled, Not
  applicable for a group), under the note "Live from Microsoft Entra ID; nested members included." States: skeleton,
  "This group has no members", "Showing the first N of M members." (200 shown at most), a **warning** Alert with "Retry
  loading" for 503 (never a red ProblemAlert), a warning for 404 (Entra no longer has the group) and a ProblemAlert for
  anything else. Covered by `entraAccess.test.tsx` ("Entra group members") and `e2e/entra-search.spec.ts`.
  **/admin/entra no longer has a page:** `routes/_app/admin/entra/index.tsx` only redirects in `beforeLoad`
  (`?tab=groups` to `/admin/groups?source=entra`, `?tab=service-principals` to `/admin/service-accounts?source=entra`,
  anything else to `/admin/users?source=entra`; fixed internal targets, so no open redirect). Detail URLs are unchanged
  (`/admin/users/$userId`, `/admin/entra/users/$entraUserId`, `/admin/groups/$groupId`, `/admin/entra/groups/$registrationId`,
  `/admin/service-accounts/$serviceAccountId`, `/admin/entra/service-principals/$registrationId`); every detail header shows a
  Source badge and the breadcrumb Home > Administration > Users | Groups | Service accounts > name.
  Other screens: `UserDetailPage` (an "Edit name" button and "Edit display
  name" dialog for `user:update` holders, i.e. Global admins; the Entra user detail explains the name comes from
  Microsoft), `GroupDetailPage` (members), `FolderAccess` (folder `access` tab: grants, `GrantDialog` fed by `grantable_roles`,
  block inheritance via parent capabilities, effective access), `EntraDetailPages`, `ServiceAccountDetailPage` (keys),
  `TokensPages` (`MyTokensPage` at `/account/tokens`, `AllPatsPage`, `AccountTabs`).
  **`PrincipalPicker`** (type select and a `Combobox` search dropdown; grant dialog, group members, card members) is search-only: until
  two characters are typed it shows "Type at least 2 characters: name, username, email or object ID" and requests nothing;
  it then matches display name, username, email, preferred username, object ID and record id client-side over the type's
  list (one request per type, query key `["identity","picker",type,folderId]`), shows at most 20 matches in the dropdown and then "Showing N
  of M matches. Type more to narrow the list." as its status line; a pasted full object ID shows exactly that item. The user type needs the
  root-level `user:read` (else no user list is requested); for a caller without it, `allowUsername` offers one dropdown option
  `Use local username “{typed}”` ("The server checks that this username exists") that yields `{ username }` so an Org admin
  can still name a local user by exact username. There is no free-text username or group object ID field: an unregistered
  Entra group is registered first on Groups > New > Register Entra group.
- `src/test/renderApp.tsx`: mounts the real router signed in with msw defaults for `/me`, `/me/capabilities`, `GET
/folders` and `GET /folders/{id}` (`renderApp(path, { me, folders, permissions, handlers })`, plus `makeMe`,
  `makeFolder`, `rootFolder`). Screen tests use it instead of building their own router.
- `scripts/check-pin-age.mjs`: exact-pin and 14-day-age gate. `src/test/pins.test.ts`: offline pin assertions.
- Skill: `.claude/skills/ui-frontend/SKILL.md` (adding a screen).
- Gap fixes (2026-11-16):
  - `src/lib/fetchAllPages.ts` (`PAGE_LIMIT` 100, `MAX_PAGES` 50) follows `next_cursor` for lookups that need every record (the identity lists have no server search). `PrincipalPicker`, `usePrincipalNames` and the template list on `/contracts/new` use it. Past 5,000 records they say the list could not be fully loaded.
  - `PrincipalPicker` is search-only (quick 261119-l30): it lists nothing before 2 characters and renders at most 20 matches ("Type more to narrow the list"). `renderApp` answers the six identity principal lists with empty pages by default (and `GET /groups/:id/members`, which the Groups list asks per row) and takes `config: { entraEnabled: true }` to switch Entra on. Card members are picked with `PrincipalPicker` (local group, Entra group, service account as `sa:<uuid>`).
  - The contract Subscribers tab removes a pin through `POST .../subscriptions/{subscription_id}/remove` with the `subscription_id` from `GET /contracts/{id}/subscribers`, shown only with `contract:publish`.
  - The audit drawer offers "Restore contract" on `contract.deleted` events (`features/audit/RestoreContract.tsx`, `contract:restore` at the folder); `entra_registration` events link to the Entra group or service-principal page and `grant` events to the folder's Access tab.
  - A 412 on a contract draft shows the server copy beside the unsaved edits (`DiffView` with `originalLabel`/`modifiedLabel`, which `MonacoDiff` passes as the editors' aria labels at construction); a 412 on a rule-set draft shows the server's rule or draft beside the refused change.

## Pins

Every dependency is an exact `x.y.z` (no `^` or `~`), and `pnpm-workspace.yaml` enforces `minimumReleaseAge: 20160`
(14 days), `blockExoticSubdeps` and `strictDepBuilds`. Never install a package without the legitimacy check and the pin-age
check (`node scripts/check-pin-age.mjs`).

| Tool                 | Pin                                                                                   |
| -------------------- | ------------------------------------------------------------------------------------- |
| Node                 | 26.13.1 (`.nvmrc`, `engines.node`, Dockerfile `FROM`); no corepack on Node 26 (A-6)   |
| pnpm                 | 12.23.0 (`packageManager`; installed with `npm i -g pnpm@12.23.0`, never on the host) |
| nginx runtime        | `nginxinc/nginx-unprivileged:1.31.7-alpine-slim`                                      |
| Playwright runner    | `mcr.microsoft.com/playwright:v1.65.1-noble` (equals `@playwright/test`)              |
| monaco-editor        | 0.57.0 (installed with the contract screens)                                          |
| @monaco-editor/react | 4.7.0 (installed with the contract screens)                                           |

`src/features/editor/monacoSetup.ts` calls `loader.config({ monaco })` and sets `MonacoEnvironment.getWorker` to the
Vite `?worker` bundles (editor and JSON), so the editor is bundled and no CDN script loads (CSP `script-src 'self'`,
`worker-src 'self' blob:`; proven by `e2e/contracts.spec.ts` under the CSP fixture).

Runtime dependencies (`dependencies`):

| Package                         | Version  |
| ------------------------------- | -------- |
| `@azure/msal-browser`           | 5.30.0   |
| `@fontsource-variable/inter`    | 5.3.0    |
| `@monaco-editor/react`          | 4.7.0    |
| `@radix-ui/react-alert-dialog`  | 1.2.1    |
| `@radix-ui/react-dialog`        | 1.3.1    |
| `@radix-ui/react-dropdown-menu` | 2.2.1    |
| `@radix-ui/react-popover`       | 1.3.1    |
| `@radix-ui/react-tabs`          | 1.2.1    |
| `@radix-ui/react-toast`         | 1.3.1    |
| `@radix-ui/react-tooltip`       | 1.4.1    |
| `@tanstack/react-query`         | 5.108.3  |
| `@tanstack/react-router`        | 1.170.46 |
| `jsonc-parser`                  | 3.3.1    |
| `lucide-react`                  | 1.77.0   |
| `monaco-editor`                 | 0.57.0   |
| `openapi-fetch`                 | 0.17.0   |
| `react`                         | 19.3.1   |
| `react-dom`                     | 19.3.1   |
| `yaml`                          | 2.9.1    |

Tooling (`devDependencies`):

| Package                       | Version |
| ----------------------------- | ------- |
| `@axe-core/playwright`        | 4.14.0  |
| `@eslint/js`                  | 10.0.1  |
| `@playwright/test`            | 1.65.1  |
| `@tanstack/router-plugin`     | 1.169.0 |
| `@testing-library/dom`        | 10.4.2  |
| `@testing-library/jest-dom`   | 8.0.0   |
| `@testing-library/react`      | 16.3.3  |
| `@testing-library/user-event` | 14.6.13 |
| `@types/node`                 | 26.6.6  |
| `@types/react`                | 19.3.0  |
| `@types/react-dom`            | 19.3.0  |
| `@vitejs/plugin-react`        | 6.3.0   |
| `eslint`                      | 10.15.0 |
| `eslint-plugin-jsx-a11y`      | 6.10.2  |
| `eslint-plugin-react-hooks`   | 7.1.1   |
| `globals`                     | 17.14.0 |
| `jsdom`                       | 31.0.1  |
| `msw`                         | 3.0.7   |
| `openapi-typescript`          | 7.13.0  |
| `prettier`                    | 3.10.1  |
| `stylelint`                   | 18.1.0  |
| `typescript`                  | 6.0.3   |
| `typescript-eslint`           | 8.73.0  |
| `vite`                        | 8.3.8   |
| `vitest`                      | 5.1.1   |

## Known exceptions

- **CSP `style-src 'unsafe-inline'`** (D-UI-08-07). Radix primitives and Monaco set inline `style` attributes, so the
  nginx CSP keeps `style-src 'self' 'unsafe-inline'`. It is the only relaxation: `script-src` is `'self'` with no inline
  script and no `unsafe-eval`, and the build emits one external module script and no inline script or style.
- **Expected peer warnings** (A-5, A-10): `pnpm peers check` lists two unmet peers, `eslint` for
  `eslint-plugin-jsx-a11y` (its range stops at ESLint 9; it runs correctly under 10) and `typescript` for
  `openapi-typescript` (range stops before 6). Both are verified. Never enable strict peer dependencies; upgrade them
  deliberately.
- **TypeScript stays on 6.x** (A-5): 7.x ships no JavaScript compiler API, so `openapi-typescript` (and
  `typescript-eslint`, peer `<6.1.0`) cannot run on it.
- **Node tooling runs in a container on this host**: the host has no pnpm and none may be installed globally. Use
  `node:26.13.1-trixie-slim` with `npm i -g pnpm@12.23.0`, named volumes for `node_modules` and the pnpm store, or the
  Docker `ci` stage once it exists.

## URL scheme (D-UI-08-23)

Route files live under `src/routes/`; later phases conform to this list.

- Sign-in: `/signin`, `/signin/rotate`, `/auth/callback`.
- Account: `/account/profile`, `/account/password`, `/account/tokens` (Phase 12).
- Folders and contracts (Phases 9 and 10): `/folders/$folderId` with tab child routes `contracts`, `templates`,
  `rule-sets`, `cards`, `access`; `/contracts/$contractId` with child routes `drafts/$draftId`, `versions`,
  `versions/$ref`, `diff`, `rule-sets`, `subscribers`.
- Governance (Phase 11): `/templates/$templateId`, `/rule-sets/$ruleSetId`, `/governance/dry-run`, `/governance/drift`.
- Admin (Phase 12): `/admin/users`, `/admin/groups` and `/admin/service-accounts` (each with `?source=local|entra&q=`;
  absent source = All), their details `/admin/users/$userId`, `/admin/groups/$groupId`,
  `/admin/service-accounts/$serviceAccountId` and the Entra registration details `/admin/entra/users/$entraUserId`,
  `/admin/entra/groups/$registrationId`, `/admin/entra/service-principals/$registrationId`, `/admin/pats`.
  `/admin/entra` itself only redirects (`?tab=` to the merged page with `source=entra`).
- Subscriptions and audit (Phase 13): `/cards/$cardId`, `/audit`.

Filters and cursors live in typed search params so views can be bookmarked and shared.

## Local development

- `pnpm --dir ui dev` serves on **`http://127.0.0.1:5173`**. Never open it as `localhost`: HSTS is host-based, not
  port-based, so after the browser sees `Strict-Transport-Security` from `https://localhost:8443` it can upgrade
  `http://localhost:5173` to HTTPS, where Vite speaks plain HTTP. IP literals are not eligible for HSTS (Pitfall 6).
- The dev server forwards `/api` to `https://localhost:8443` (`secure: false`, self-signed certificate), so calls are
  same-origin from the browser's view.
- Safari refuses a `Secure` cookie from the plain-HTTP dev origin, so sign-in through the dev server is not supported
  there; Playwright over HTTPS through the gateway is the contract (Pitfall 7).
- Checks: `pnpm --dir ui test`, `typecheck`, `lint`, `build`, `format`.

## Commands and gates

| Command                                                                | What it does                                                                                                                                                     |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pnpm --dir ui dev`                                                    | Vite dev server on `http://127.0.0.1:5173` (see above)                                                                                                           |
| `pnpm --dir ui test` / `typecheck` / `lint` / `format` / `build`       | Vitest, `tsc --noEmit`, ESLint plus Stylelint, Prettier check, production build                                                                                  |
| `pnpm --dir ui test:coverage`                                          | Vitest with V8 coverage and the `COVERAGE_THRESHOLDS` of `vitest.config.ts` (report in `ui/coverage/`)                                                           |
| `uv run python scripts/export_openapi.py` then `pnpm --dir ui gen:api` | Re-export the merged OpenAPI snapshot and regenerate `src/api/schema.gen.ts` after a backend route change                                                        |
| `pnpm --dir ui ci:static`                                              | `gen:api:check`, format, lint, typecheck, test, build: the UI step of `bash scripts/ci.sh`                                                                       |
| `pnpm --dir ui ci:full`                                                | `ci:static` with `test:coverage` instead of `test`: the UI step of the full gate (`CI_FULL=1`)                                                                   |
| `bash scripts/e2e-ui.sh [spec] [playwright args]`                      | Playwright in the pinned `ui-e2e` container against the warm stack (rebuilds only changed images), for example `bash scripts/e2e-ui.sh e2e/smoke.spec.ts -g axe` |

- **Operation coverage.** There is no `pnpm coverage:ops` and no `ui/coverage/operations.json` (planned in Plan 26,
  superseded). The full gate runs `uv run python scripts/operation_coverage.py --gate`, which fails when an operation of
  `openapi/openapi.json` lacks a backend 2xx test (`backend_ok`), a backend refusal test (`backend_refused`), a row in
  the authz matrix (`authz_matrix`) or a UI call site (`ui_client`: a literal `/api/v1/...` path passed to the generated
  client in `src/`, tests excluded). It also reports, ungated, `e2e`, `ui_unit` (msw handlers) and `ui_e2e` (the
  `apiCalls` fixture of `e2e/fixtures.ts`). Justified exemptions, one reason per column, go in
  `tests/repo/operation_coverage_exemptions.json`. A new screen therefore makes its operations' `ui_client` cell green;
  an operation deliberately left off every screen needs a `ui_client` exemption with the reason.
- **Demo data.** The UI e2e suite seeds what it reads through `e2e/demo-seed.ts` (see above), so it runs on a cold
  stack; never create demo records by hand.
- **Performance tests.** Tests marked `performance` (throughput and scale budgets) are skipped by `scripts/ci.sh` and
  `scripts/e2e.sh` unless `DC_PERFORMANCE=1`.
- **No raw HTML.** `dangerouslySetInnerHTML` is not used anywhere under `src/` (`grep -rl dangerouslySetInnerHTML
ui/src` is empty); keep it that way.

## Skills

- `.claude/skills/ui-frontend/SKILL.md`: adding a screen the platform way.

The design system is binding: read `.claude/skills/typeui-design-system/` (the component files you touch) and
`.claude/skills/typeui-fundamentals/` at the repo root, and the Phase 8 UI-SPEC.
