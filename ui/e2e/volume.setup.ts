// The volume-setup project (quick 261120-6i0, D-03, D-06): seeds the compose test stack with the
// volume dataset through the admin API, asserts the D-01 minimums by listing, and when this pass
// created anything runs the seed once more and asserts that the second pass created nothing.
import { request } from "@playwright/test";
import { expect, test } from "./fixtures";
import { adminPassword } from "./journey-helpers";
import { Client } from "./seed-client";
import { ensureVolumeData, TEAM_SLUGS, type Folder } from "./volume-seed";

const GATEWAY_URL = process.env.DC_UI_E2E_SETUP_URL ?? "https://traefik";

async function assertMinimums(api: Client): Promise<void> {
  const folders = (await api.all<Folder>("identity/folders")).filter((f) =>
    f.slug.startsWith("vol-"),
  );
  expect(folders.length, "volume folders").toBeGreaterThanOrEqual(40);
  const users = await api.all<{ username: string }>("identity/users");
  expect(
    users.filter((u) => u.username.startsWith("vol-")).length,
    "volume users",
  ).toBeGreaterThanOrEqual(320);
  const groups = await api.all<{ id: string; name: string }>("identity/groups");
  const volGroups = groups.filter((g) => g.name.startsWith("vol-grp-"));
  expect(volGroups.length, "volume groups").toBeGreaterThanOrEqual(160);
  let big = 0;
  for (const g of volGroups.slice(0, 5)) {
    const members = await api.all<unknown>(`identity/groups/${g.id}/members`);
    if (members.length >= 120) big += 1;
  }
  expect(big, "groups with 120 or more members").toBeGreaterThanOrEqual(5);
  const accounts = await api.all<{ name: string }>("identity/service-accounts");
  expect(
    accounts.filter((a) => a.name.startsWith("vol-sa-")).length,
    "volume service accounts",
  ).toBeGreaterThanOrEqual(30);
  const entra = [
    ...(await api.all<{ object_id: string }>("identity/entra/users")),
    ...(await api.all<{ object_id: string }>("identity/entra/groups")),
    ...(await api.all<{ object_id: string }>(
      "identity/entra/service-principals",
    )),
  ].filter((r) => r.object_id.startsWith("ba5e000"));
  expect(entra.length, "Entra registrations").toBeGreaterThanOrEqual(55);
  const access = folders.find((f) => f.slug === "vol-access-heavy");
  const grants = await api.all<unknown>(
    `identity/folders/${access?.id ?? ""}/grants`,
  );
  expect(
    grants.length,
    "grants on the access-heavy team",
  ).toBeGreaterThanOrEqual(55);
  let contracts = 0;
  let cards = 0;
  for (const slug of TEAM_SLUGS) {
    const team = folders.find((f) => f.slug === slug);
    contracts += (
      await api.all<unknown>(`contracts/contracts?folder_id=${team?.id ?? ""}`)
    ).length;
    cards += (
      await api.all<unknown>(`contracts/folders/${team?.id ?? ""}/cards`)
    ).length;
  }
  expect(contracts, "contracts in the volume teams").toBeGreaterThanOrEqual(
    210,
  );
  expect(cards, "subscriber cards in the volume teams").toBeGreaterThanOrEqual(
    110,
  );
  let sets = 0;
  let versions = 0;
  for (const f of folders.slice(0, 40)) {
    const rows = await api.all<{ name: string; publish_counter: number }>(
      `validation/folders/${f.id}/rule-sets`,
    );
    for (const r of rows.filter((x) => x.name.startsWith("vol-rs-"))) {
      sets += 1;
      versions += r.publish_counter;
    }
  }
  expect(sets, "volume rule sets").toBeGreaterThanOrEqual(40);
  expect(versions, "volume rule set versions").toBeGreaterThanOrEqual(110);
  const templates = await api.all<{ name: string }>("contracts/templates");
  expect(
    templates.filter((t) => t.name.startsWith("vol-tpl-")).length,
    "volume templates",
  ).toBeGreaterThanOrEqual(20);
  const pats = await api.all<{ name: string }>("identity/pats");
  expect(
    pats.filter((t) => t.name.startsWith("vol-pat-")).length,
    "admin tokens",
  ).toBeGreaterThanOrEqual(110);
}

test("seed the volume dataset (idempotent)", async () => {
  test.setTimeout(45 * 60_000);
  const ctx = await request.newContext({
    baseURL: GATEWAY_URL,
    ignoreHTTPSErrors: true,
  });
  try {
    const login = await ctx.post("/api/v1/identity/auth/login", {
      data: { username: "admin", password: adminPassword() },
    });
    expect(login.ok(), "admin sign-in").toBe(true);
    const { access_token } = (await login.json()) as { access_token: string };
    const first = await ensureVolumeData(ctx, access_token);
    console.log(`volume seed: created ${JSON.stringify(first.ledger.created)}`);
    console.log(
      `volume seed: existing ${JSON.stringify(first.ledger.existing)}`,
    );
    if (first.ledger.refused.length > 0)
      console.log(`volume seed refused:\n${first.ledger.refused.join("\n")}`);
    await assertMinimums(new Client(ctx, access_token, "volume minimums"));
    if (Object.values(first.ledger.created).some((n) => n > 0)) {
      const second = await ensureVolumeData(ctx, access_token);
      expect(
        second.ledger.created,
        "a second seed pass writes nothing",
      ).toEqual({});
    }
  } finally {
    await ctx.dispose();
  }
});
