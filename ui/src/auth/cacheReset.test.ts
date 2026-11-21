import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it } from "vitest";
import { clearCacheOnSessionChange } from "./cacheReset";
import { tokenStore } from "./tokenStore";

afterEach(() => {
  tokenStore.clear();
});

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
});
