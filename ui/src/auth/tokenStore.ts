// The access token lives here and nowhere else (D-UI-08-11, T-8-24): one module-level variable,
// never Web Storage, a cookie or the URL, so a script in another tab or a stored copy cannot read
// it. The refresh token never reaches JavaScript at all (it is an HttpOnly cookie).

export type StoredToken = { token: string; expiresAt: number };

type Listener = () => void;

let current: StoredToken | null = null;
const listeners = new Set<Listener>();

function notify(): void {
  for (const listener of [...listeners]) listener();
}

export const tokenStore = {
  get(): StoredToken | null {
    return current;
  },
  /** Keeps `token` until it is replaced or cleared; `expiresAt` is epoch milliseconds. */
  set(token: string, expiresInS: number): void {
    current = { token, expiresAt: Date.now() + expiresInS * 1000 };
    notify();
  },
  clear(): void {
    if (current === null) return;
    current = null;
    notify();
  },
  subscribe(listener: Listener): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
};
