// fillOffsets and useViewportFill: the editor frame's document top and the space the page keeps
// after it, written as CSS variables and kept current on resize.
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fillOffsets, useViewportFill } from "./useViewportFill";

function rect(top: number, bottom: number): DOMRect {
  return {
    top,
    bottom,
    left: 0,
    right: 0,
    width: 0,
    height: bottom - top,
    x: 0,
    y: top,
    toJSON: () => ({}),
  };
}

function buildTree() {
  const main = document.createElement("main");
  main.style.paddingBottom = "48px";
  const page = document.createElement("div");
  page.setAttribute("data-page-content", "");
  const frame = document.createElement("div");
  page.append(frame);
  main.append(page);
  document.body.append(main);
  let frameRect = rect(300, 700);
  vi.spyOn(frame, "getBoundingClientRect").mockImplementation(() => frameRect);
  vi.spyOn(page, "getBoundingClientRect").mockImplementation(() =>
    rect(0, frameRect.bottom + 60),
  );
  return {
    main,
    page,
    frame,
    setFrame: (next: DOMRect) => {
      frameRect = next;
    },
  };
}

function scrollTo(y: number) {
  Object.defineProperty(window, "scrollY", { value: y, configurable: true });
}

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  scrollTo(0);
});

describe("fillOffsets", () => {
  it("measures the document top and the content plus main padding after the frame", () => {
    const { frame } = buildTree();
    scrollTo(100);
    expect(fillOffsets(frame)).toEqual({ top: 400, after: 108 });
  });

  it("uses only the main padding outside the page content and rounds up", () => {
    const main = document.createElement("main");
    main.style.paddingBottom = "48px";
    const frame = document.createElement("div");
    main.append(frame);
    document.body.append(main);
    vi.spyOn(frame, "getBoundingClientRect").mockReturnValue(rect(10.2, 400));
    expect(fillOffsets(frame)).toEqual({ top: 11, after: 48 });
    const loose = document.createElement("div");
    document.body.append(loose);
    vi.spyOn(loose, "getBoundingClientRect").mockReturnValue(rect(5, 50));
    expect(fillOffsets(loose)).toEqual({ top: 5, after: 0 });
  });
});

describe("useViewportFill", () => {
  it("writes the offsets on mount and removes them on unmount", () => {
    const { frame } = buildTree();
    scrollTo(100);
    const { unmount } = renderHook(() => {
      useViewportFill({ current: frame }, true);
    });
    expect(frame.style.getPropertyValue("--editor-fill-top")).toBe("400px");
    expect(frame.style.getPropertyValue("--editor-fill-after")).toBe("108px");
    unmount();
    expect(frame.style.getPropertyValue("--editor-fill-top")).toBe("");
    expect(frame.style.getPropertyValue("--editor-fill-after")).toBe("");
  });

  it("measures again when the observed page content resizes or the window resizes", () => {
    let fire: (() => void) | undefined;
    const observed: Element[] = [];
    const disconnect = vi.fn();
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          fire = callback;
        }
        observe(target: Element) {
          observed.push(target);
        }
        disconnect = disconnect;
      },
    );
    const { frame, page, setFrame } = buildTree();
    const { unmount } = renderHook(() => {
      useViewportFill({ current: frame }, true);
    });
    expect(observed).toEqual([page]);
    expect(frame.style.getPropertyValue("--editor-fill-top")).toBe("300px");

    setFrame(rect(360, 760));
    act(() => fire?.());
    expect(frame.style.getPropertyValue("--editor-fill-top")).toBe("360px");

    setFrame(rect(200, 600));
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    expect(frame.style.getPropertyValue("--editor-fill-top")).toBe("200px");
    unmount();
    expect(disconnect).toHaveBeenCalled();
  });

  it("writes nothing when disabled", () => {
    const { frame } = buildTree();
    renderHook(() => {
      useViewportFill({ current: frame }, false);
    });
    expect(frame.style.getPropertyValue("--editor-fill-top")).toBe("");
    expect(frame.style.getPropertyValue("--editor-fill-after")).toBe("");
  });

  it("leaves unchanged offsets alone when measured again", () => {
    const { frame } = buildTree();
    renderHook(() => {
      useViewportFill({ current: frame }, true);
    });
    const setProperty = vi.spyOn(frame.style, "setProperty");
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    expect(setProperty).not.toHaveBeenCalled();
    expect(frame.style.getPropertyValue("--editor-fill-top")).toBe("300px");
  });

  it("still measures on window resize where ResizeObserver is missing", () => {
    vi.stubGlobal("ResizeObserver", undefined);
    const { frame, setFrame } = buildTree();
    const { unmount } = renderHook(() => {
      useViewportFill({ current: frame }, true);
    });
    setFrame(rect(250, 650));
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    expect(frame.style.getPropertyValue("--editor-fill-top")).toBe("250px");
    expect(() => {
      unmount();
    }).not.toThrow();
    expect(frame.style.getPropertyValue("--editor-fill-top")).toBe("");
  });
});

describe("fillOffsets without main padding", () => {
  it("counts no padding when main has none set", () => {
    const { main, frame } = buildTree();
    main.style.removeProperty("padding-bottom");
    expect(fillOffsets(frame)).toEqual({ top: 300, after: 60 });
  });
});
