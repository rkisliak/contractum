// Lets an editor frame reach down to the bottom of the window. CSS alone cannot do it: the frame's
// distance from the top of the page depends on the page header, the tabs, alerts and toolbars that
// wrap differently at every width. So the frame's document top and the space the page keeps after
// it are measured here and written as --editor-fill-top and --editor-fill-after (px); the CSS
// (`.fill` in CodeEditor.module.css) turns them into a height with a 20rem floor.
//
// The space after is measured against `[data-page-content]`, the shell's content wrapper, plus the
// bottom padding of `main`: main and the body are stretched to 100dvh by the shell grid, so their
// bottoms say nothing about where the content ends. The page-content wrapper ends exactly where the
// last element ends.
import { useLayoutEffect, type RefObject } from "react";

const TOP = "--editor-fill-top";
const AFTER = "--editor-fill-after";

export function fillOffsets(frame: HTMLElement): {
  top: number;
  after: number;
} {
  const rect = frame.getBoundingClientRect();
  const top = rect.top + window.scrollY;
  const page = frame.closest<HTMLElement>("[data-page-content]");
  const below = page
    ? Math.max(0, page.getBoundingClientRect().bottom - rect.bottom)
    : 0;
  const main = frame.closest("main");
  const padding = main
    ? Number.parseFloat(window.getComputedStyle(main).paddingBottom) || 0
    : 0;
  return { top: Math.ceil(top), after: Math.ceil(below + padding) };
}

/** When enabled, keeps --editor-fill-top and --editor-fill-after current on the frame. */
export function useViewportFill(
  ref: RefObject<HTMLElement | null>,
  enabled: boolean,
): void {
  useLayoutEffect(() => {
    const frame = ref.current;
    if (!enabled || !frame) return;
    const measure = () => {
      const { top, after } = fillOffsets(frame);
      // Only write a changed value: a write resizes the frame, which fires the observer again.
      const nextTop = `${String(top)}px`;
      const nextAfter = `${String(after)}px`;
      if (frame.style.getPropertyValue(TOP) !== nextTop)
        frame.style.setProperty(TOP, nextTop);
      if (frame.style.getPropertyValue(AFTER) !== nextAfter)
        frame.style.setProperty(AFTER, nextAfter);
    };
    measure();
    window.addEventListener("resize", measure);
    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      // Alerts appearing or leaving above the editor move it without a window resize.
      observer = new ResizeObserver(measure);
      observer.observe(
        frame.closest<HTMLElement>("[data-page-content]") ?? document.body,
      );
    }
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
      frame.style.removeProperty(TOP);
      frame.style.removeProperty(AFTER);
    };
  }, [ref, enabled]);
}
