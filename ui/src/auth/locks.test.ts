import { afterEach, describe, expect, it, vi } from "vitest";
import { withLock } from "./locks";

afterEach(() => {
  vi.unstubAllGlobals();
});

function gate() {
  let open: () => void = () => undefined;
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { opened, open };
}

describe("withLock", () => {
  it("asks navigator.locks for the named lock when Web Locks exist", async () => {
    const request = vi.fn((_name: string, fn: () => Promise<unknown>) => fn());
    vi.stubGlobal("navigator", { locks: { request } });
    await expect(
      withLock("dc-refresh", () => Promise.resolve(7)),
    ).resolves.toBe(7);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0]?.[0]).toBe("dc-refresh");
  });

  it("serialises two callers in this tab when Web Locks are missing", async () => {
    vi.stubGlobal("navigator", {});
    const events: string[] = [];
    const first = gate();
    const a = withLock("dc-refresh", async () => {
      events.push("a:start");
      await first.opened;
      events.push("a:end");
    });
    const b = withLock("dc-refresh", () => {
      events.push("b:start");
      return Promise.resolve();
    });
    await Promise.resolve();
    expect(events).toEqual(["a:start"]);
    first.open();
    await Promise.all([a, b]);
    expect(events).toEqual(["a:start", "a:end", "b:start"]);
  });

  it("keeps running later callers after an earlier one rejected", async () => {
    vi.stubGlobal("navigator", {});
    const failed = withLock("dc-x", () => Promise.reject(new Error("boom")));
    await expect(failed).rejects.toThrow("boom");
    await expect(withLock("dc-x", () => Promise.resolve("ok"))).resolves.toBe(
      "ok",
    );
  });

  it("falls back to the in-tab chain where there is no navigator at all", async () => {
    vi.stubGlobal("navigator", undefined);
    await expect(
      withLock("dc-worker", () => Promise.resolve("ran")),
    ).resolves.toBe("ran");
  });
});
