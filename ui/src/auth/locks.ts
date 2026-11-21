// Cross-tab mutual exclusion for the refresh call (D-UI-08-14, research Pattern 7).
//
// The refresh token is one-time use: presenting a rotated token again revokes its whole family.
// Two tabs of one browser share the cookie, so they must take turns. `navigator.locks` is the
// only primitive that spans tabs; where it is missing (an insecure context, an old browser, jsdom)
// the fallback serialises callers inside this tab only, which is all it can promise.

/** Runs `fn` while holding the named lock; callers with the same name never overlap. */
export type LockRunner = <T>(name: string, fn: () => Promise<T>) => Promise<T>;

const chains = new Map<string, Promise<unknown>>();

function inTab<T>(name: string, fn: () => Promise<T>): Promise<T> {
  const tail = chains.get(name) ?? Promise.resolve();
  // The tail never rejects (it swallows the previous caller's outcome), so `fn` always runs.
  const result = tail.then(fn);
  chains.set(
    name,
    result.then(
      () => undefined,
      () => undefined,
    ),
  );
  return result;
}

// `navigator.locks` is read at call time, so a test can install or remove a stub.
export const withLock: LockRunner = (name, fn) => {
  const locks =
    typeof navigator === "undefined"
      ? undefined
      : (navigator as { locks?: LockManager }).locks;
  if (locks === undefined) return inTab(name, fn);
  return locks.request(name, fn);
};
