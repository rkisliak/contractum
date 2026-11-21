import { afterEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "./tokenStore";

afterEach(() => {
  tokenStore.clear();
  vi.useRealTimers();
});

describe("tokenStore", () => {
  it("holds a token with its expiry and clears it", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    tokenStore.set("abc", 900);
    expect(tokenStore.get()).toEqual({ token: "abc", expiresAt: 1_900_000 });
    tokenStore.clear();
    expect(tokenStore.get()).toBeNull();
    vi.useRealTimers();
  });

  it("notifies subscribers on set and clear until they unsubscribe", () => {
    const listener = vi.fn();
    const unsubscribe = tokenStore.subscribe(listener);
    tokenStore.set("abc", 60);
    tokenStore.clear();
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
    tokenStore.set("def", 60);
    expect(listener).toHaveBeenCalledTimes(2);
  });
});

describe("tokenStore expiry arithmetic", () => {
  it("computes expiresAt from expires_in seconds at the moment of set", () => {
    vi.useFakeTimers();
    vi.setSystemTime(5_000);
    tokenStore.set("t", 1);
    expect(tokenStore.get()?.expiresAt).toBe(6_000);
  });

  it("does not notify when clearing an already empty store", () => {
    const listener = vi.fn();
    const unsubscribe = tokenStore.subscribe(listener);
    tokenStore.clear();
    unsubscribe();
    expect(listener).not.toHaveBeenCalled();
  });
});
