import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";
import { server } from "./msw";

// A browser resolves a same-origin path such as "/api/v1/identity/me" against the page URL;
// Node's Request and fetch do not (they throw "Invalid URL"). The UI only ever calls
// same-origin paths (apiBase is validated as one), so the test environment resolves them
// against the jsdom origin the way a browser would. Installed before msw patches the globals.
function resolveAgainstPage(input: RequestInfo | URL): RequestInfo | URL {
  if (
    typeof input === "string" &&
    input.startsWith("/") &&
    !input.startsWith("//")
  ) {
    return new URL(input, window.location.href).href;
  }
  return input;
}

const NativeRequest = globalThis.Request;
globalThis.Request = class PageRequest extends NativeRequest {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    super(resolveAgainstPage(input), init);
  }
};

const nativeFetch = globalThis.fetch.bind(globalThis);
globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit) =>
  nativeFetch(resolveAgainstPage(input), init);

// jsdom has no ResizeObserver; Radix popper surfaces (a focused tooltip's arrow) construct one.
// Nothing here depends on measured sizes, so an inert implementation is enough.
if (!("ResizeObserver" in globalThis)) {
  globalThis.ResizeObserver = class InertResizeObserver {
    observe(): void {
      return undefined;
    }
    unobserve(): void {
      return undefined;
    }
    disconnect(): void {
      return undefined;
    }
  };
}

// jsdom never runs CSS animations, so it never fires animationend. Since jsdom 31 it computes
// animation-name from the CSS modules, and Radix Presence then keeps a closing dialog mounted
// until an animationend that cannot come. The test DOM therefore takes the app's own
// reduced-motion path (Dialog.module.css): no animation, so Radix unmounts at once.
const noAnimation = document.createElement("style");
noAnimation.textContent =
  "*, *::before, *::after { animation: none !important; transition: none !important; }";
document.head.append(noAnimation);

// Screen tests render the whole shell; under coverage on a loaded machine the default 1 s wait for
// findBy*/waitFor is shorter than a cold render. A test file may still set its own value.
configure({ asyncUtilTimeout: 15_000 });

// msw's "error" strategy (msw 3 `onUnhandledFrame`, formerly `onUnhandledRequest`) only logs the
// unmocked call and fails the request, and the screens absorb a failed request, so the test would
// still pass. Every unmocked call is therefore also recorded and fails the running test in
// afterEach: a test must mock every request its screen makes.
const unhandledFrames: string[] = [];

function describeFrame(frame: { protocol: string; data: unknown }): string {
  const data = frame.data as { request?: unknown };
  if (data.request instanceof Request) {
    const url = new URL(data.request.url);
    return `${data.request.method} ${url.pathname}${url.search}`;
  }
  return `${frame.protocol} frame`;
}

beforeAll(() => {
  server.listen({
    onUnhandledFrame({ frame, defaults }) {
      unhandledFrames.push(describeFrame(frame));
      defaults.error();
    },
  });
});

// Vitest runs without globals, so Testing Library cannot register its own cleanup.
afterEach(() => {
  cleanup();
  server.resetHandlers();
  const unhandled = unhandledFrames.splice(0);
  if (unhandled.length > 0) {
    throw new Error(
      `[MSW] unmocked request(s), add a handler:\n  ${unhandled.join("\n  ")}`,
    );
  }
});

afterAll(() => {
  server.close();
});
