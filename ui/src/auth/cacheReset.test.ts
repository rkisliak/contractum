import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it } from "vitest";
import { clearCacheOnSessionChange } from "./cacheReset";
import { tokenStore } from "./tokenStore";

afterEach(() => {
  tokenStore.clear();
});

function jwt(payload: Record<string, unknown>): string {
  const part = (value: unknown) =>
    btoa(JSON.stringify(value))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  return `${part({ alg: "RS256", typ: "JWT" })}.${part(payload)}.signature`;
}

function seeded() {
  const client = new QueryClient();
  client.setQueryData(["folders"], ["reader's folders"]);
  return client;
}

describe("clearCacheOnSessionChange", () => {
  it("drops every cached query when the session ends", () => {
    tokenStore.set("reader-token", 900);
    const client = seeded();
    const stop = clearCacheOnSessionChange(client);
    tokenStore.clear();
    expect(client.getQueryData(["folders"])).toBeUndefined();
    stop();
  });

  it("drops every cached query when a new session starts", () => {
    const client = seeded();
    const stop = clearCacheOnSessionChange(client);
    tokenStore.set("admin-token", 900);
    expect(client.getQueryData(["folders"])).toBeUndefined();
    stop();
  });

  it("keeps the cache across a token refresh of the same session", () => {
    tokenStore.set("token-1", 900);
    const client = seeded();
    const stop = clearCacheOnSessionChange(client);
    tokenStore.set("token-2", 900);
    expect(client.getQueryData(["folders"])).toEqual(["reader's folders"]);
    stop();
  });

  it("drops the folder tree of a signed-in principal when the session ends or restarts", () => {
    tokenStore.set(jwt({ iss: "https://idp", sub: "reader" }), 900);
    const client = seeded();
    client.setQueryData(["contracts", "folder-tree", "f1"], ["Orders"]);
    const stop = clearCacheOnSessionChange(client);
    tokenStore.clear();
    expect(
      client.getQueryData(["contracts", "folder-tree", "f1"]),
    ).toBeUndefined();
    client.setQueryData(["contracts", "folder-tree", "f1"], ["Orders"]);
    tokenStore.set(jwt({ iss: "https://idp", sub: "admin" }), 900);
    expect(
      client.getQueryData(["contracts", "folder-tree", "f1"]),
    ).toBeUndefined();
    stop();
  });

  it("keeps the cache when a refreshed token names the same issuer and subject", () => {
    tokenStore.set(jwt({ iss: "https://idp", sub: "reader", jti: "1" }), 900);
    const client = seeded();
    client.setQueryData(["contracts", "folder-tree", "f1"], ["Orders"]);
    const stop = clearCacheOnSessionChange(client);
    tokenStore.set(jwt({ iss: "https://idp", sub: "reader", jti: "2" }), 900);
    expect(client.getQueryData(["folders"])).toEqual(["reader's folders"]);
    expect(client.getQueryData(["contracts", "folder-tree", "f1"])).toEqual([
      "Orders",
    ]);
    stop();
  });

  it("drops every cached query, the folder tree included, when the subject changes", () => {
    tokenStore.set(jwt({ iss: "https://idp", sub: "reader" }), 900);
    const client = seeded();
    client.setQueryData(["contracts", "folder-tree", "f1"], ["Orders"]);
    const stop = clearCacheOnSessionChange(client);
    tokenStore.set(jwt({ iss: "https://idp", sub: "admin" }), 900);
    expect(client.getQueryData(["folders"])).toBeUndefined();
    expect(
      client.getQueryData(["contracts", "folder-tree", "f1"]),
    ).toBeUndefined();
    stop();
  });

  it("drops the cache when the issuer changes for the same subject", () => {
    tokenStore.set(jwt({ iss: "https://idp-a", sub: "reader" }), 900);
    const client = seeded();
    const stop = clearCacheOnSessionChange(client);
    tokenStore.set(jwt({ iss: "https://idp-b", sub: "reader" }), 900);
    expect(client.getQueryData(["folders"])).toBeUndefined();
    stop();
  });

  it("treats a token that is not a readable JWT as no principal and never throws", () => {
    const client = seeded();
    const stop = clearCacheOnSessionChange(client);
    for (const token of [
      "a.%%%.c",
      "a.bm90LWpzb24.c",
      `a.${btoa(JSON.stringify({ iss: 1, sub: 2 }))}.c`,
      "a.b",
      "",
    ]) {
      expect(() => tokenStore.set(token, 900)).not.toThrow();
    }
    stop();
  });

  it("keeps the cache between two unreadable tokens and drops it once one names a principal", () => {
    tokenStore.set("opaque-1", 900);
    const client = seeded();
    const stop = clearCacheOnSessionChange(client);
    tokenStore.set("opaque-2", 900);
    expect(client.getQueryData(["folders"])).toEqual(["reader's folders"]);
    tokenStore.set(jwt({ iss: "https://idp", sub: "reader" }), 900);
    expect(client.getQueryData(["folders"])).toBeUndefined();
    stop();
  });
});
