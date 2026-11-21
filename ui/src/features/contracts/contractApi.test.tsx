// The contract read helpers on bodiless answers: a 204 document reads as empty text in the
// fallback media type, a bodiless contract read is an error the layout shows, and bodiless
// version and draft lists read as empty.
import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createApiClient } from "@/api/client";
import { tokenStore } from "@/auth/tokenStore";
import {
  CONTRACT_ALL,
  CONTRACT_API,
  contractFolders,
  makeContract,
} from "@/test/contracts-fixtures";
import { http, HttpResponse, server } from "@/test/msw";
import { renderApp, testConfig } from "@/test/renderApp";
import { fetchContractDocument } from "./api";

const noContent = () => new HttpResponse(null, { status: 204 });

afterEach(() => {
  tokenStore.clear();
});

describe("contract api on bodiless answers", () => {
  it("reads a 204 document as empty YAML with no ETag", async () => {
    server.use(http.get(`${CONTRACT_API}/document`, noContent));
    expect(
      await fetchContractDocument(createApiClient(testConfig), "c-1"),
    ).toEqual({ text: "", etag: null, mediaType: "application/yaml" });
  });

  it("shows a bodiless contract read as a problem", async () => {
    renderApp("/contracts/c-1", {
      folders: contractFolders,
      permissions: CONTRACT_ALL,
      handlers: [http.get(CONTRACT_API, noContent)],
    });
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(
      screen.queryByRole("heading", { level: 1, name: "Orders" }),
    ).toBeNull();
  });

  it.each([
    ["versions", "No published versions"],
    ["drafts", "No drafts"],
  ])("reads a bodiless %s list as empty", async (tab, heading) => {
    renderApp(`/contracts/c-1/${tab}`, {
      folders: contractFolders,
      permissions: CONTRACT_ALL,
      handlers: [
        http.get(CONTRACT_API, () => HttpResponse.json(makeContract())),
        http.get(`${CONTRACT_API}/versions`, noContent),
        http.get(`${CONTRACT_API}/drafts`, noContent),
      ],
    });
    expect(await screen.findByText(heading)).toBeVisible();
  });
});
