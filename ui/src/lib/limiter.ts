// A concurrency limiter for fan-out reads: a screen that asks once per row (member counts,
// contract counts, the contracts of each expanded folder) would otherwise send one request per
// row at the same moment, and the services answer them one after the other, so the last ones
// outlast the 2 second authorization call between services and come back 503.

/** Runs `task` once fewer than the limit are running; the rest wait their turn, in order. */
export type Limited = <T>(task: () => Promise<T>) => Promise<T>;

export function createLimiter(maxRunning: number): Limited {
  let running = 0;
  const waiting: (() => void)[] = [];

  async function acquire(): Promise<void> {
    if (running < maxRunning) {
      running += 1;
      return;
    }
    // A releasing task hands its slot straight to the next waiter, so the count never exceeds the limit.
    await new Promise<void>((resolve) => waiting.push(resolve));
  }

  function release(): void {
    const next = waiting.shift();
    if (next) next();
    else running -= 1;
  }

  return async function limited<T>(task: () => Promise<T>): Promise<T> {
    await acquire();
    try {
      return await task();
    } finally {
      release();
    }
  };
}
