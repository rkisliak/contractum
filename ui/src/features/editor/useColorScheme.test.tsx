// useColorScheme: the OS colour scheme (prefers-color-scheme), kept live through the media query's
// change event, "light" when matchMedia is missing and on the server.
import { act, renderHook } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useColorScheme } from "./useColorScheme";

function stubMatchMedia(initiallyDark: boolean) {
  let dark = initiallyDark;
  const listeners = new Set<() => void>();
  const media = {
    get matches() {
      return dark;
    },
    addEventListener: vi.fn((_type: string, listener: () => void) => {
      listeners.add(listener);
    }),
    removeEventListener: vi.fn((_type: string, listener: () => void) => {
      listeners.delete(listener);
    }),
  };
  const matchMedia = vi.fn(() => media);
  vi.stubGlobal("matchMedia", matchMedia);
  return {
    media,
    matchMedia,
    listeners,
    setDark(next: boolean) {
      dark = next;
      for (const listener of listeners) listener();
    },
  };
}

function SchemeProbe() {
  return <span>{useColorScheme()}</span>;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useColorScheme", () => {
  it("follows the media query and stops listening on unmount", () => {
    const stub = stubMatchMedia(false);
    const { result, unmount } = renderHook(() => useColorScheme());
    expect(result.current).toBe("light");
    expect(stub.matchMedia).toHaveBeenCalledWith(
      "(prefers-color-scheme: dark)",
    );
    act(() => {
      stub.setDark(true);
    });
    expect(result.current).toBe("dark");
    unmount();
    expect(stub.media.removeEventListener).toHaveBeenCalledWith(
      "change",
      expect.any(Function),
    );
    expect(stub.listeners.size).toBe(0);
  });

  it("starts dark when the OS prefers dark", () => {
    stubMatchMedia(true);
    const { result } = renderHook(() => useColorScheme());
    expect(result.current).toBe("dark");
  });

  it("reads light without matchMedia", () => {
    vi.stubGlobal("matchMedia", undefined);
    const { result, unmount } = renderHook(() => useColorScheme());
    expect(result.current).toBe("light");
    expect(() => {
      unmount();
    }).not.toThrow();
  });

  it("renders light on the server", () => {
    stubMatchMedia(true);
    expect(renderToString(<SchemeProbe />)).toBe("<span>light</span>");
  });
});
