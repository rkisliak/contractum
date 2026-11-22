# Accessibility

Contractum's web UI should be usable by everyone who works with data contracts, including people
who use a keyboard only, a screen reader, zoom or a high-contrast setting. This page says what the
project checks today, where the limits are, and how to report a barrier.

## Target

The target is **WCAG 2.2 level AA** for the web UI. The checks below test parts of that target
automatically. Contractum has not had a manual accessibility audit, so it makes no claim of full
conformance.

## What is checked

- **Automated axe scans.** The Playwright end-to-end journeys run
  [axe-core](https://github.com/dequelabs/axe-core) with the WCAG 2.0, 2.1 and 2.2 level A and AA
  rule sets. A serious or critical violation fails the test. The journeys run in the nightly
  end-to-end workflow and in the full local gate.
- **Light and dark themes.** The UI follows the system colour scheme. The axe scans and the layout
  audit run in both themes.
- **Colour contrast.** A unit test measures the WCAG contrast ratio of every colour pairing the UI
  ships, in light and dark.
- **Lint rules.** `eslint-plugin-jsx-a11y` (strict configuration) runs on every pull request as
  part of the UI checks.
- **Layout at three widths.** A layout audit checks every route and every dialog, menu and drawer
  at 1440, 1024 and 390 pixels wide, so the UI stays usable from a desktop to a phone. Below
  1024 pixels the side navigation moves into a drawer opened from a top bar.
- **Keyboard and focus.** Every signed-in page has a **Skip to content** link, one main landmark and
  a visible focus ring. After a page change, focus moves to the page heading and the new title is
  announced. Dialogs return focus to the control that opened them. Searchable dropdowns follow the
  WAI-ARIA combobox pattern.
- **Reduced motion.** Loading spinners respect the system's reduced-motion setting.

## Supported environments

- The automated tests run in **Chromium** (desktop Chrome settings) only. Other current browsers
  are expected to work but are not tested in CI.
- Viewport widths from 390 pixels (a phone) upwards are tested.
- The UI is in English. The page language is declared as English.

## Known limitations

- Automated tools find only part of the accessibility problems. Contractum has not been tested with
  screen readers or other assistive technology as part of its checks.
- Firefox, Safari and mobile browsers are not tested automatically.
- The contract and rule editors use the Monaco code editor. Its keyboard and screen reader support
  comes from Monaco itself and is not tested separately here.
- The API docs page (Swagger UI) and the docs site are third-party tools and are not covered by
  the checks above.

## Report a barrier

If something in Contractum stops you from doing a task, tell the maintainer through GitHub: use a
contact method on the maintainer's [GitHub profile](https://github.com/rkisliak). Please include
the page or screen, what you tried to do, what happened, and the browser and assistive technology
you use. A barrier is treated as a bug.
