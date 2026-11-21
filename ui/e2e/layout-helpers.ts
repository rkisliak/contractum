// Generic, exhaustive layout assertions for the layout audit (`layout.spec.ts`).
//
// `measureLayout(page)` runs one pass in the page and enumerates EVERY visible element of the
// relevant kinds (headings, breadcrumbs, labels, inputs, selects, textareas, checkboxes, radios,
// buttons, links, badges, icons, cards, table headers and cells, tabs, dialog headers and
// footers, menu items, tree rows). For each it checks the rules below and returns one violation
// string per failure, naming the element (role + accessible name) and the measured vs expected
// values. `checkLayout(page, label)` turns them into soft assertions labelled with the page or
// state, so one run reports everything at once.
//
// Rules (tolerance 1px unless stated):
//  R1 row       controls (field shells, selects, buttons) that sit in the same visual row of the
//               same flex/grid container share the same top and the same height;
//  R2 size      every single-line field control is 36px tall, every Button its size token
//               (small 36, large 46), every IconButton its square hit area (24, 32, 36);
//  R3 label     every field label sits directly above its control, left-aligned, with the same
//               8px gap everywhere;
//  R4 stack     stacked fields in one container are spaced evenly (one gap per container);
//  R5 viewport  nothing visible is wider than or outside the viewport width (and the document
//               never scrolls horizontally), except inside a horizontal scroll container;
//  R6 contain   no element overflows its card, dialog, drawer, menu, table wrapper or the main
//               region, unless a scroll container sits between them;
//  R7 clip      no text is clipped (scrollWidth/scrollHeight beyond the box of a non-scrolling
//               element), unless the element carries a `title` (deliberate truncation with the
//               full text on hover);
//  R8 cards     cards (and stat tiles) in the same grid row have equal heights;
//  R9 centre    icons and text inside buttons, links, tabs, menu items, nav items and badges are
//               vertically centred on each other (centre delta <= 1px);
//  R10 overlap  no two interactive elements overlap;
//  R12 breathe  a field never touches the block above it (at least 8px between them);
//  R11 cells    in every table row, the first text line, buttons and badges of each cell share
//               one vertical centre (a row action or status sits level with the row's text);
//  R13 frame    a page's primary block (a top-level card or `[data-primary-block]` in the page
//               content column) that sits alone in its row spans the full content column width;
//               blocks sharing a row (an intentional two-column layout) are exempt.
//  R14 popup    every visible `[role=listbox]`, `[role=menu]`, `[role=tooltip]` and Radix popper
//               content lies inside the viewport (1px tolerance) and, when its content is taller
//               than its box, is itself the scroll container (`overflow-y` auto or scroll);
//  R15 dialog   every visible `[role=dialog]` and `[role=alertdialog]` lies inside the viewport,
//               its header and footer actions are visible inside it, content that overflows
//               scrolls in an element inside the dialog, and the document behind it does not
//               scroll horizontally;
//  R16 one-line Buttons, Badges (`data-badge`, pin chips), tabs, menu items, nav items and
//               breadcrumb items never wrap to a second line; a long label ellipsises (with the
//               full text in `title`) or the control wraps as a whole within its container;
//  R17 bidi     an element whose text holds a strong right-to-left character or a bidi control
//               is bidi-isolated (`dir` on it or on an ancestor up to its block, or a computed
//               `unicode-bidi` of isolate, isolate-override or plaintext), so a user-supplied
//               name can never reorder the text around it.
// (Added in quick 261120-6i0 for the high-volume audit; both audits apply them.)
import type { Page } from "@playwright/test";
import { expect } from "./fixtures";

export type LayoutOptions = {
  /** Rules to skip for this state, with the reason in the caller. */
  skip?: string[];
};

/** Expected heights. */
export const TOKENS = {
  control: 36,
  buttonSmall: 36,
  buttonLarge: 46,
  labelGap: 8,
} as const;

/** One in-page pass; returns the violations. */
export async function measureLayout(
  page: Page,
  options: LayoutOptions = {},
): Promise<string[]> {
  return page.evaluate(
    ({ tokens, skip }) => {
      const out: string[] = [];
      const skipped = new Set(skip);
      const seenMsg = new Set<string>();
      const add = (rule: string, msg: string) => {
        const line = `${rule}: ${msg}`;
        if (skipped.has(rule) || seenMsg.has(line)) return;
        seenMsg.add(line);
        out.push(line);
      };
      const TOL = 1.01;
      const vw = document.documentElement.clientWidth;

      const r = (n: number) => String(Math.round(n * 10) / 10);

      // ---------- naming ----------
      const textOf = (el: Element) =>
        el.textContent.replace(/\s+/g, " ").trim().slice(0, 48);
      const nameOf = (el: Element): string => {
        const h = el as HTMLElement;
        const role =
          el.getAttribute("role") ??
          (
            {
              A: "link",
              BUTTON: "button",
              INPUT: "textbox",
              SELECT: "combobox",
              TEXTAREA: "textbox",
              H1: "heading",
              H2: "heading",
              H3: "heading",
              H4: "heading",
              TH: "columnheader",
              TD: "cell",
              LABEL: "label",
              LI: "listitem",
              NAV: "navigation",
            } as Record<string, string | undefined>
          )[el.tagName] ??
          el.tagName.toLowerCase();
        let name = el.getAttribute("aria-label") ?? "";
        const lb = el.getAttribute("aria-labelledby");
        if (!name && lb)
          name = lb
            .split(" ")
            .map((id) => document.getElementById(id)?.textContent ?? "")
            .join(" ");
        if (!name && h.id) {
          const label = document.querySelector(
            `label[for="${CSS.escape(h.id)}"]`,
          );
          if (label) name = textOf(label);
        }
        if (
          !name &&
          el.querySelector("[data-control]") === null &&
          el.hasAttribute("data-control")
        ) {
          const field = el.closest("[data-field]");
          const label = field?.querySelector("label");
          if (label) name = textOf(label);
        }
        if (!name) name = textOf(el);
        const cls =
          typeof h.className === "string"
            ? (h.className.split(" ")[0] ?? "").replace(
                /^_|_[a-z0-9]{5}$/gi,
                "",
              )
            : "";
        return `${role}${cls ? `.${cls}` : ""} "${name.replace(/\s+/g, " ").trim().slice(0, 48)}"`;
      };

      // ---------- visibility ----------
      const visible = (el: Element): boolean => {
        const h = el as HTMLElement;
        if (
          !h.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })
        )
          return false;
        const rect = h.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) return false;
        // Visually hidden helpers (1px clip) are not layout.
        const cs = getComputedStyle(h);
        if (cs.clipPath === "inset(50%)") return false;
        if (
          h.closest("[aria-hidden='true'] , [inert]") &&
          !h.closest("[role=dialog],[role=alertdialog]")
        )
          return false;
        // Monaco draws its own scrollable surface; it is checked as one box.
        if (
          h.closest(".monaco-editor") &&
          !h.classList.contains("monaco-editor")
        )
          return false;
        return true;
      };

      // Scope: with a modal open only the top-most modal is interactive.
      const modals = [
        ...document.querySelectorAll(
          "[role=dialog][aria-modal=true], [role=alertdialog], [role=dialog][data-state=open]",
        ),
      ].filter(visible);
      const menus = [...document.querySelectorAll("[role=menu]")].filter(
        visible,
      );
      const scope: Element = menus.at(-1) ?? modals.at(-1) ?? document.body;
      const inScope = (el: Element) =>
        scope === document.body || scope.contains(el);

      const all = (sel: string, root: Element | Document = document) =>
        [...root.querySelectorAll(sel)].filter(visible);

      const rects = new Map<Element, DOMRect>();
      const rect = (el: Element) => {
        let b = rects.get(el);
        if (!b) {
          b = el.getBoundingClientRect();
          rects.set(el, b);
        }
        return b;
      };
      /** The element's box cut by every clipping ancestor; null when nothing of it shows. */
      const clippedRect = (el: Element) => {
        const b = rect(el);
        let left = b.left;
        let top = b.top;
        let right = b.right;
        let bottom = b.bottom;
        for (let p = el.parentElement; p; p = p.parentElement) {
          const cs = getComputedStyle(p);
          if (cs.overflowX === "visible" && cs.overflowY === "visible")
            continue;
          const pb = rect(p);
          left = Math.max(left, pb.left);
          top = Math.max(top, pb.top);
          right = Math.min(right, pb.right);
          bottom = Math.min(bottom, pb.bottom);
        }
        return right - left > 1 && bottom - top > 1
          ? { left, top, right, bottom }
          : null;
      };
      const isScroller = (el: Element, axis: "x" | "y") => {
        const cs = getComputedStyle(el);
        const v = axis === "x" ? cs.overflowX : cs.overflowY;
        return v === "auto" || v === "scroll";
      };
      const clipsAxis = (el: Element, axis: "x" | "y") => {
        const cs = getComputedStyle(el);
        const v = axis === "x" ? cs.overflowX : cs.overflowY;
        return v !== "visible";
      };
      /** True when a scroll/clip container sits strictly between el and ancestor. */
      const scrollerBetween = (
        el: Element,
        ancestor: Element,
        axis: "x" | "y",
      ) => {
        for (
          let p = el.parentElement;
          p && p !== ancestor;
          p = p.parentElement
        ) {
          if (clipsAxis(p, axis)) return true;
        }
        return false;
      };

      // ---------- R2 size ----------
      if (!skipped.has("R2")) {
        for (const el of all("[data-control=single]")) {
          if (!inScope(el)) continue;
          const h = rect(el).height;
          const coarse = matchMedia("(pointer: coarse)").matches;
          const want = coarse ? 44 : tokens.control;
          if (Math.abs(h - want) > TOL)
            add(
              "R2",
              `${nameOf(el)} field height ${r(h)}px, expected ${r(want)}px`,
            );
        }
        for (const el of all("input, select")) {
          const type = (el as HTMLInputElement).type;
          if (
            ["checkbox", "radio", "hidden", "file", "range", "color"].includes(
              type,
            )
          )
            continue;
          if (el.closest("[data-control]")) continue;
          const h = rect(el).height;
          if (Math.abs(h - tokens.control) > TOL)
            add(
              "R2",
              `${nameOf(el)} unframed control height ${r(h)}px, expected ${r(tokens.control)}px`,
            );
        }
        for (const el of all("[data-button]")) {
          const kind = el.getAttribute("data-button") ?? "";
          const b = rect(el);
          if (kind === "small" && Math.abs(b.height - tokens.buttonSmall) > TOL)
            add(
              "R2",
              `${nameOf(el)} small button height ${r(b.height)}px, expected ${r(tokens.buttonSmall)}px`,
            );
          // Large is the sign-in card size only (buttons.md); inside the app shell it is Small.
          if (kind === "large" && document.querySelector("main#main-content"))
            add(
              "R2",
              `${nameOf(el)} is a Large button inside the app shell, expected Small`,
            );
          if (kind === "large" && Math.abs(b.height - tokens.buttonLarge) > TOL)
            add(
              "R2",
              `${nameOf(el)} large button height ${r(b.height)}px, expected ${r(tokens.buttonLarge)}px`,
            );
          const m = /^icon-(\d+)$/.exec(kind);
          if (m) {
            const n = Number(m[1]);
            if (Math.abs(b.height - n) > TOL || Math.abs(b.width - n) > TOL)
              add(
                "R2",
                `${nameOf(el)} icon button ${r(b.width)}x${r(b.height)}px, expected ${r(n)}x${r(n)}px`,
              );
          }
        }
        for (const el of all("input[type=checkbox], input[type=radio]")) {
          const b = rect(el);
          if (Math.abs(b.height - 16) > TOL || Math.abs(b.width - 16) > TOL)
            add(
              "R2",
              `${nameOf(el)} ${(el as HTMLInputElement).type} ${r(b.width)}x${r(b.height)}px, expected 16x16px`,
            );
        }
      }

      // ---------- R1 row ----------
      if (!skipped.has("R1")) {
        const controls = [
          ...all("[data-control=single]"),
          ...all(
            "[data-button=small], [data-button=large], [data-button=icon-36]",
          ),
          // A checkbox in a row of controls is compared by its first line's centre.
          ...all("[data-checkbox] > :first-child"),
        ].filter(inScope);
        const flexOrGridRow = (el: Element) => {
          const cs = getComputedStyle(el);
          if (cs.display.includes("grid")) return true;
          if (cs.display.includes("flex"))
            return !cs.flexDirection.startsWith("column");
          return false;
        };
        const lca = (a: Element, b: Element) => {
          const set = new Set<Element>();
          for (let p: Element | null = a; p; p = p.parentElement) set.add(p);
          for (let p: Element | null = b; p; p = p.parentElement)
            if (set.has(p)) return p;
          return document.body;
        };
        const seen = new Set<string>();
        for (let i = 0; i < controls.length; i++) {
          for (let j = i + 1; j < controls.length; j++) {
            const a = controls[i];
            const b = controls[j];
            if (!a || !b) continue;
            if (a.contains(b) || b.contains(a)) continue;
            const ra = rect(a);
            const rb = rect(b);
            const overlap =
              Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
            if (overlap < Math.min(ra.height, rb.height) * 0.5) continue;
            const c = lca(a, b);
            if (!flexOrGridRow(c)) continue;
            // Two different regions of the app shell (the sidebar search and a page button) are
            // not one row of controls.
            if (c === document.body || c.querySelector("main")) continue;
            // A checkbox sits centred on the controls of its row (it is shorter than 36px).
            if (
              a.parentElement?.hasAttribute("data-checkbox") ||
              b.parentElement?.hasAttribute("data-checkbox")
            ) {
              const dc = Math.abs(
                ra.top + ra.height / 2 - (rb.top + rb.height / 2),
              );
              if (dc > 1.5)
                add(
                  "R1",
                  `${nameOf(a)} and ${nameOf(b)} in one row have centres ${r(dc)}px apart, expected level`,
                );
              continue;
            }
            // Controls inside different table cells of one row are compared too (same row).
            const dt = Math.abs(ra.top - rb.top);
            const dh = Math.abs(ra.height - rb.height);
            if (dt > TOL || dh > TOL) {
              const key = `${nameOf(a)}|${nameOf(b)}`;
              if (seen.has(key)) continue;
              seen.add(key);
              add(
                "R1",
                `${nameOf(a)} (top ${r(ra.top)}, h ${r(ra.height)}) vs ${nameOf(b)} (top ${r(rb.top)}, h ${r(rb.height)}) in one row: expected the same top and height`,
              );
            }
          }
        }
      }

      // ---------- R3 label ----------
      const fields = all("[data-field]").filter(inScope);
      if (!skipped.has("R3")) {
        for (const field of fields) {
          const label = field.querySelector(":scope > label");
          const control = label?.nextElementSibling;
          if (!label || !control || !visible(label) || !visible(control))
            continue;
          const rl = rect(label);
          const rc = rect(control);
          const gap = rc.top - rl.bottom;
          if (Math.abs(gap - tokens.labelGap) > TOL)
            add(
              "R3",
              `${nameOf(label)} label to control gap ${r(gap)}px, expected ${r(tokens.labelGap)}px`,
            );
          if (Math.abs(rl.left - rc.left) > TOL)
            add(
              "R3",
              `${nameOf(label)} label left ${r(rl.left)} vs control left ${r(rc.left)}, expected aligned`,
            );
        }
      }

      // ---------- R4 stack ----------
      if (!skipped.has("R4")) {
        const parents = new Set(
          fields.map((f) => f.parentElement).filter(Boolean) as Element[],
        );
        for (const parent of parents) {
          const kids = [...parent.children].filter(visible);
          const stacked = kids.filter((k) => k.hasAttribute("data-field"));
          if (stacked.length < 2) continue;
          // A multi-column grid aligns rows to its tallest field; only a single column of fields
          // must be evenly spaced.
          const lefts = new Set(stacked.map((k) => Math.round(rect(k).left)));
          if (lefts.size > 1) continue;
          // Gaps between consecutive vertically stacked siblings that are both fields.
          const gaps: number[] = [];
          for (let i = 1; i < kids.length; i++) {
            const prev = kids[i - 1];
            const cur = kids[i];
            if (!prev || !cur) continue;
            if (
              !prev.hasAttribute("data-field") ||
              !cur.hasAttribute("data-field")
            )
              continue;
            const rp = rect(prev);
            const rc = rect(cur);
            if (rc.top < rp.bottom - 1) continue; // same row
            gaps.push(rc.top - rp.bottom);
          }
          if (gaps.length > 1 && Math.max(...gaps) - Math.min(...gaps) > TOL)
            add(
              "R4",
              `fields in ${nameOf(parent)} are stacked with uneven gaps ${gaps.map(r).join(", ")}px`,
            );
        }
      }

      // ---------- element kinds for R5 to R7 ----------
      const KIND_SEL = [
        "h1",
        "h2",
        "h3",
        "h4",
        "nav[aria-label=Breadcrumb] li",
        "label",
        "legend",
        "input",
        "select",
        "textarea",
        "[data-control]",
        "button",
        "a",
        "[role=tab]",
        "[role=menuitem]",
        "[role=treeitem]",
        "[role=row][aria-level]",
        "[data-card]",
        "th",
        "td",
        "dt",
        "dd",
        "p",
        "[class*=badge i]",
        "[class*=Badge]",
        "svg",
        "[role=dialog] > *",
        "[role=alertdialog] > *",
        "[class*=footer i]",
        "[class*=header i]",
        "li",
        "code",
        "pre",
        "[role=status]",
        "[role=alert]",
        "img",
      ].join(",");
      const elements = all(KIND_SEL);

      // ---------- R5 viewport ----------
      if (!skipped.has("R5")) {
        const sw = document.documentElement.scrollWidth;
        if (sw > vw + 0.5)
          add(
            "R5",
            `document scrolls horizontally: scrollWidth ${r(sw)}px > viewport ${r(vw)}px`,
          );
        let reported = 0;
        for (const el of elements) {
          const b = rect(el);
          if (b.right <= vw + TOL && b.left >= -TOL) continue;
          if (scrollerBetween(el, document.body, "x")) continue;
          // Elements of the off-canvas drawer while it slides are hidden by checkVisibility.
          if (reported++ > 400) break;
          add(
            "R5",
            `${nameOf(el)} spans ${r(b.left)}..${r(b.right)}px outside the ${r(vw)}px viewport`,
          );
        }
      }

      // ---------- R6 contain ----------
      if (!skipped.has("R6")) {
        const containers = all(
          "[data-card], [role=dialog], [role=alertdialog], [role=menu], main, table, [role=tabpanel], [role=region], form, section, header, footer, aside, fieldset, [class*=card i], [class*=panel i], [class*=Panel]",
        );
        let reported = 0;
        for (const c of containers) {
          const rc = rect(c);
          // A horizontal scroll container (a table region) may scroll on a phone or a laptop
          // (tables.md), never on a 1440px desktop: there a scrollbar means cramped columns.
          if (isScroller(c, "x")) {
            const h = c as HTMLElement;
            // Only a table region counts; code and JSON blocks scroll by design.
            if (
              vw >= 1440 &&
              c.querySelector("table") &&
              h.scrollWidth > h.clientWidth + 1
            ) {
              if (reported++ > 400) break;
              add(
                "R6",
                `${nameOf(c)} scrolls horizontally at ${r(vw)}px: content ${r(h.scrollWidth)}px in ${r(h.clientWidth)}px`,
              );
            }
            continue;
          }
          // Descendants that are themselves layout-relevant kinds.
          for (const el of c.querySelectorAll(KIND_SEL)) {
            if (!visible(el)) continue;
            if (scrollerBetween(el, c, "x")) continue;
            // A sibling popper portalled inside (menus in dialogs) is its own container.
            if (
              el.closest(
                "[role=menu],[role=listbox],[data-radix-popper-content-wrapper]",
              ) &&
              !c.closest("[role=menu]") &&
              c !== el.closest("[role=menu]")
            )
              continue;
            const b = rect(el);
            const over = Math.max(rc.left - b.left, b.right - rc.right);
            if (over > TOL) {
              if (reported++ > 400) break;
              add(
                "R6",
                `${nameOf(el)} overflows ${nameOf(c)} by ${r(over)}px horizontally`,
              );
            }
            if (
              !isScroller(c, "y") &&
              !scrollerBetween(el, c, "y") &&
              c.tagName !== "MAIN"
            ) {
              const overY = Math.max(rc.top - b.top, b.bottom - rc.bottom);
              if (overY > TOL && clipsAxis(c, "y")) {
                if (reported++ > 400) break;
                add(
                  "R6",
                  `${nameOf(el)} is cut off by ${nameOf(c)} by ${r(overY)}px vertically`,
                );
              }
            }
          }
        }
      }

      // ---------- R7 clip ----------
      if (!skipped.has("R7")) {
        let reported = 0;
        const textEls = all("body *").filter((el) =>
          [...el.childNodes].some(
            (n) =>
              n.nodeType === Node.TEXT_NODE &&
              (n.textContent ?? "").trim() !== "",
          ),
        );
        for (const el of textEls) {
          if (
            [
              "INPUT",
              "TEXTAREA",
              "SELECT",
              "OPTION",
              "SCRIPT",
              "STYLE",
              "PRE",
              "CODE",
            ].includes(el.tagName)
          )
            continue;
          if (el.closest("[title]")) continue;
          // Clipping comes from this element or a clipping ancestor that is narrower than its text.
          let clipper: Element | null = null;
          for (
            let p: Element | null = el;
            p && p !== document.body;
            p = p.parentElement
          ) {
            const cs = getComputedStyle(p);
            if (isScroller(p, "x") || isScroller(p, "y")) break;
            if (
              cs.overflowX !== "visible" ||
              cs.overflowY !== "visible" ||
              cs.textOverflow === "ellipsis"
            ) {
              clipper = p;
              break;
            }
            if (
              p !== el &&
              p.matches(
                "[data-card], [role=dialog], td, th, li, button, a, label",
              )
            )
              break;
          }
          if (!clipper) continue;
          const h = clipper as HTMLElement;
          const dx = h.scrollWidth - h.clientWidth;
          const dy = h.scrollHeight - h.clientHeight;
          if (dx > 1 || dy > 1) {
            if (reported++ > 400) break;
            add(
              "R7",
              `${nameOf(el)} is clipped by ${nameOf(clipper)}: content ${r(h.scrollWidth)}x${r(h.scrollHeight)}px in a ${r(h.clientWidth)}x${r(h.clientHeight)}px box`,
            );
          }
        }
      }

      // ---------- R8 cards ----------
      if (!skipped.has("R8")) {
        const isCard = (el: Element) =>
          el.hasAttribute("data-card") ||
          /(^|[_\s])(card|tile|stat)[A-Za-z]*_/i.test(
            ` ${(el as HTMLElement).className}`,
          ) ||
          el.querySelector(":scope > [data-card]") !== null;
        for (const grid of all("*")) {
          const cs = getComputedStyle(grid);
          if (!cs.display.includes("grid")) continue;
          const cards = [...grid.children].filter(
            (k) => visible(k) && isCard(k),
          );
          if (cards.length < 2) continue;
          const rows = new Map<number, Element[]>();
          for (const k of cards) {
            const t = Math.round(rect(k).top);
            const key = [...rows.keys()].find((x) => Math.abs(x - t) <= 1) ?? t;
            rows.set(key, [...(rows.get(key) ?? []), k]);
          }
          for (const row of rows.values()) {
            if (row.length < 2) continue;
            const hs = row.map((k) => rect(k).height);
            if (Math.max(...hs) - Math.min(...hs) > TOL)
              add(
                "R8",
                `cards in one grid row of ${nameOf(grid)} have heights ${hs.map(r).join(", ")}px, expected equal`,
              );
          }
        }
      }

      // ---------- R9 centre ----------
      if (!skipped.has("R9")) {
        const hosts = all(
          "button, a, [role=tab], [role=menuitem], [role=treeitem] > *, [role=row][aria-level] > [role=gridcell], [class*=badge i], [class*=Badge], [role=status], [role=alert] > *",
        ).filter(inScope);
        for (const host of hosts) {
          const svgs = [...host.querySelectorAll(":scope svg")].filter(visible);
          if (svgs.length === 0) continue;
          // The text runs inside the host (excluding nested interactive children).
          const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
          let textNode: Node | null;
          while ((textNode = walker.nextNode())) {
            if (!(textNode.textContent ?? "").trim()) continue;
            const parent = textNode.parentElement;
            if (!parent || !visible(parent)) continue;
            if (
              parent.closest("button, a, [role=menuitem], [role=tab]") !==
                host.closest("button, a, [role=menuitem], [role=tab]") &&
              host.matches("button, a, [role=menuitem], [role=tab]")
            )
              continue;
            const range = document.createRange();
            range.selectNodeContents(textNode);
            const first = range.getClientRects()[0];
            if (!first) continue;
            const textCentre = first.top + first.height / 2;
            // Only the icon on the same line as the first text line is compared.
            for (const svg of svgs) {
              const s = rect(svg);
              if (s.bottom < first.top || s.top > first.bottom) continue;
              if (
                svg.closest("button, a, [role=menuitem], [role=tab]") !==
                  host.closest("button, a, [role=menuitem], [role=tab]") &&
                host.matches("button, a, [role=menuitem], [role=tab]")
              )
                continue;
              const d = Math.abs(s.top + s.height / 2 - textCentre);
              if (d > TOL)
                add(
                  "R9",
                  `${nameOf(host)} icon centre is ${r(d)}px off the text centre, expected <= 1px`,
                );
            }
            break;
          }
        }
      }

      // ---------- R10 overlap ----------
      if (!skipped.has("R10")) {
        const interactive = all(
          "a[href], button, input:not([type=hidden]), select, textarea, [role=tab], [role=menuitem], [role=treeitem], [role=row][aria-level], [tabindex='0']",
        )
          .filter(inScope)
          // The skip link shows over the page only while it has keyboard focus, by design.
          .filter((el) => el.getAttribute("href") !== "#main-content");
        for (let i = 0; i < interactive.length; i++) {
          for (let j = i + 1; j < interactive.length; j++) {
            const a = interactive[i];
            const b = interactive[j];
            if (!a || !b) continue;
            if (a.contains(b) || b.contains(a)) continue;
            // A native control drawn by its label (radio cards) is part of that label.
            if (
              a.closest("label")?.contains(b) ||
              b.closest("label")?.contains(a)
            )
              continue;
            // Only the part left visible by clipping (scrolled) ancestors can overlap.
            const ra = clippedRect(a);
            const rb = clippedRect(b);
            if (!ra || !rb) continue;
            const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
            const h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
            if (w > 1 && h > 1)
              add(
                "R10",
                `${nameOf(a)} overlaps ${nameOf(b)} by ${r(w)}x${r(h)}px`,
              );
          }
        }
      }

      // ---------- R12 breathe ----------
      if (!skipped.has("R12")) {
        for (const field of fields) {
          let prev = field.previousElementSibling;
          while (prev && !visible(prev)) prev = prev.previousElementSibling;
          if (!prev) continue;
          const rp = rect(prev);
          const rf = rect(field);
          if (rp.bottom > rf.top + 1) continue; // beside it, not above
          const gap = rf.top - rp.bottom;
          if (gap < 7.5)
            add(
              "R12",
              `${nameOf(field.querySelector("label") ?? field)} is ${r(gap)}px below ${nameOf(prev)}, expected at least 8px`,
            );
        }
      }

      // ---------- R11 cells ----------
      if (!skipped.has("R11")) {
        for (const row of all("tbody tr").filter(inScope)) {
          const centres: { name: string; c: number }[] = [];
          for (const cell of row.children) {
            if (!visible(cell)) continue;
            // The first thing in the cell: a button, a badge or the first text line.
            const first = [
              ...cell.querySelectorAll("[data-button], [data-badge], *"),
            ]
              .filter(visible)
              .find(
                (el) =>
                  el.matches("[data-button], [data-badge]") ||
                  [...el.childNodes].some(
                    (n) =>
                      n.nodeType === Node.TEXT_NODE &&
                      (n.textContent ?? "").trim() !== "",
                  ),
              );
            const direct = [...cell.childNodes].some(
              (n) =>
                n.nodeType === Node.TEXT_NODE &&
                (n.textContent ?? "").trim() !== "",
            );
            let c: number | null = null;
            let name = "";
            if (first && first.matches("[data-button], [data-badge]")) {
              const b = rect(first);
              c = b.top + b.height / 2;
              name = nameOf(first);
            } else {
              const host = direct ? cell : first;
              if (!host) continue;
              const walker = document.createTreeWalker(
                host,
                NodeFilter.SHOW_TEXT,
              );
              let n: Node | null;
              while ((n = walker.nextNode())) {
                if (!(n.textContent ?? "").trim()) continue;
                const range = document.createRange();
                range.selectNodeContents(n);
                const line = range.getClientRects()[0];
                if (line) {
                  // The line box centre: the text's content area centred in its line height.
                  c = line.top + line.height / 2;
                  name = nameOf(n.parentElement ?? host);
                }
                break;
              }
            }
            if (c !== null) centres.push({ name, c });
          }
          if (centres.length < 2) continue;
          const ref = centres[0];
          if (!ref) continue;
          for (const other of centres.slice(1)) {
            const d = Math.abs(other.c - ref.c);
            if (d > 1.5)
              add(
                "R11",
                `${other.name} centre is ${r(d)}px off the row's first line (${ref.name}) in one table row, expected level`,
              );
          }
        }
      }

      // ---------- R13 frame ----------
      const frame = document.querySelector("[data-page-content]");
      if (!skipped.has("R13") && frame && scope === document.body) {
        const fw = rect(frame).width;
        const blocks = all("[data-card], [data-primary-block]", frame).filter(
          (el) =>
            !el.parentElement?.closest(
              "[data-card], [data-primary-block], [role=dialog]",
            ),
        );
        for (const el of blocks) {
          const b = rect(el);
          const shared = blocks.some((other) => {
            if (other === el) return false;
            const o = rect(other);
            return o.top < b.bottom - 1 && o.bottom > b.top + 1;
          });
          if (shared) continue;
          if (fw - b.width > TOL)
            add(
              "R13",
              `${nameOf(el)} is ${r(b.width)}px wide alone in its row, expected the content column width ${r(fw)}px`,
            );
        }
      }

      // ---------- R14 popup ----------
      if (!skipped.has("R14")) {
        const vh = document.documentElement.clientHeight;
        const popups = all(
          "[role=listbox], [role=menu], [role=tooltip], [data-radix-popper-content-wrapper] > *",
        );
        for (const el of popups) {
          const b = rect(el);
          if (
            b.left < -TOL ||
            b.top < -TOL ||
            b.right > vw + TOL ||
            b.bottom > vh + TOL
          )
            add(
              "R14",
              `${nameOf(el)} popup spans ${r(b.left)},${r(b.top)}..${r(b.right)},${r(b.bottom)}px outside the ${r(vw)}x${r(vh)}px viewport`,
            );
          const h = el as HTMLElement;
          if (h.scrollHeight > h.clientHeight + 1 && !isScroller(el, "y"))
            add(
              "R14",
              `${nameOf(el)} popup content is ${r(h.scrollHeight)}px tall in a ${r(h.clientHeight)}px box but is not itself a scroll container`,
            );
        }
      }

      // ---------- R15 dialog-fit ----------
      if (!skipped.has("R15")) {
        const vh = document.documentElement.clientHeight;
        for (const dlg of all("[role=dialog], [role=alertdialog]")) {
          const b = rect(dlg);
          if (
            b.left < -TOL ||
            b.top < -TOL ||
            b.right > vw + TOL ||
            b.bottom > vh + TOL
          )
            add(
              "R15",
              `${nameOf(dlg)} dialog spans ${r(b.left)},${r(b.top)}..${r(b.right)},${r(b.bottom)}px outside the ${r(vw)}x${r(vh)}px viewport`,
            );
          for (const action of all(
            "[class*=footer i] button, [class*=footer i] a, [class*=header i] button, [class*=header i] a",
            dlg,
          )) {
            if (scrollerBetween(action, dlg, "y")) continue;
            const a = rect(action);
            if (
              a.left < b.left - TOL ||
              a.right > b.right + TOL ||
              a.top < b.top - TOL ||
              a.bottom > b.bottom + TOL
            )
              add(
                "R15",
                `${nameOf(action)} in ${nameOf(dlg)} is not fully inside the dialog (${r(a.left)},${r(a.top)}..${r(a.right)},${r(a.bottom)} vs ${r(b.left)},${r(b.top)}..${r(b.right)},${r(b.bottom)}px)`,
              );
          }
          if (!isScroller(dlg, "y")) {
            for (const el of dlg.querySelectorAll(KIND_SEL)) {
              if (!visible(el) || scrollerBetween(el, dlg, "y")) continue;
              // A popup (a combobox list) floats over the dialog on its own: R14 measures it.
              if (el.closest("[role=listbox], [class*=popup i]")) continue;
              const e = rect(el);
              if (e.bottom > b.bottom + TOL)
                add(
                  "R15",
                  `${nameOf(el)} extends ${r(e.bottom - b.bottom)}px below ${nameOf(dlg)} and no element inside the dialog scrolls it`,
                );
            }
          }
          const sw = document.documentElement.scrollWidth;
          if (sw > vw + 0.5)
            add(
              "R15",
              `the document behind ${nameOf(dlg)} scrolls horizontally: scrollWidth ${r(sw)}px > viewport ${r(vw)}px`,
            );
        }
      }

      // ---------- R16 one-line chrome ----------
      if (!skipped.has("R16")) {
        const lineCount = (el: Element): number => {
          const tops: number[] = [];
          const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
          let n: Node | null;
          while ((n = walker.nextNode())) {
            if (!(n.textContent ?? "").trim()) continue;
            // Visually hidden helper text (sr-only) is not a line of the control.
            if (!n.parentElement || !visible(n.parentElement)) continue;
            const range = document.createRange();
            range.selectNodeContents(n);
            for (const line of range.getClientRects()) {
              if (line.width < 2 || line.height < 2) continue;
              if (!tops.some((t) => Math.abs(t - line.top) < line.height / 2))
                tops.push(line.top);
            }
          }
          return tops.length;
        };
        const chrome = all(
          "[data-button], [data-badge], [role=tab], [role=menuitem], nav[aria-label=Sections] a, nav[aria-label=Sections] button, nav[aria-label=Breadcrumb] li, nav[aria-label=Breadcrumb] a",
        );
        for (const el of chrome) {
          const lines = lineCount(el);
          if (lines > 1)
            add(
              "R16",
              `${nameOf(el)} wraps to ${String(lines)} lines, expected one line (ellipsis with the full text in title)`,
            );
        }
      }

      // ---------- R17 bidi ----------
      if (!skipped.has("R17")) {
        const strong =
          /[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF\u202A-\u202E\u2066-\u2069]/u;
        const isolates = (el: Element): boolean => {
          const bidi = getComputedStyle(el).unicodeBidi;
          if (
            bidi === "isolate" ||
            bidi === "isolate-override" ||
            bidi === "plaintext"
          )
            return true;
          const dir = el.getAttribute("dir");
          return dir === "auto" || dir === "rtl" || dir === "ltr";
        };
        const blockLike = (el: Element) => {
          const d = getComputedStyle(el).display;
          return ![
            "inline",
            "inline-block",
            "inline-flex",
            "contents",
          ].includes(d);
        };
        let reported = 0;
        for (const el of all("body *")) {
          if (["SCRIPT", "STYLE", "OPTION"].includes(el.tagName)) continue;
          const direct = [...el.childNodes].some(
            (n) =>
              n.nodeType === Node.TEXT_NODE && strong.test(n.textContent ?? ""),
          );
          if (!direct) continue;
          if (el.closest(".monaco-editor")) continue;
          let ok = false;
          for (let p: Element | null = el; p; p = p.parentElement) {
            if (isolates(p)) {
              ok = true;
              break;
            }
            if (blockLike(p)) break;
          }
          if (!ok) {
            if (reported++ > 200) break;
            add(
              "R17",
              `${nameOf(el)} holds right-to-left or bidi-control text but is not bidi-isolated (no dir attribute or unicode-bidi isolate up to its block)`,
            );
          }
        }
      }

      return out;
    },
    { tokens: TOKENS, skip: options.skip ?? [] },
  );
}

/** Measures the current state and records every violation as one soft assertion. */
export async function checkLayout(
  page: Page,
  label: string,
  options: LayoutOptions = {},
): Promise<string[]> {
  const violations = await measureLayout(page, options);
  expect.soft(violations, `layout of ${label}`).toEqual([]);
  return violations;
}
