import { describe, expect, it } from "vitest";
import { createLimiter } from "./limiter";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("createLimiter", () => {
  it("never runs more tasks than the limit and starts the waiting ones in order", async () => {
    const limited = createLimiter(2);
    const gates = [deferred(), deferred(), deferred(), deferred()];
    const started: number[] = [];
    let running = 0;
    let peak = 0;
    const results = gates.map((gate, index) =>
      limited(async () => {
        started.push(index);
        running += 1;
        peak = Math.max(peak, running);
        await gate.promise;
        running -= 1;
        return index;
      }),
    );

    await Promise.resolve();
    expect(started).toEqual([0, 1]);

    gates[0]?.resolve();
    await results[0];
    await Promise.resolve();
    expect(started).toEqual([0, 1, 2]);

    gates[1]?.resolve();
    gates[2]?.resolve();
    gates[3]?.resolve();
    expect(await Promise.all(results)).toEqual([0, 1, 2, 3]);
    expect(peak).toBe(2);
  });

  it("frees the slot when a task fails, so the next one still runs", async () => {
    const limited = createLimiter(1);
    const failing = limited(() => Promise.reject(new Error("boom")));
    const next = limited(() => Promise.resolve("after"));

    await expect(failing).rejects.toThrow("boom");
    expect(await next).toBe("after");
  });

  it("starts a task at once while fewer than the limit are running", async () => {
    const limited = createLimiter(3);
    expect(await limited(() => Promise.resolve(1))).toBe(1);
    expect(await limited(() => Promise.resolve(2))).toBe(2);
  });
});
