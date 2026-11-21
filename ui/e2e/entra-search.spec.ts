// Searching the Entra directory in the Register Entra dialogs (quick 261119-ofa), on the compose
// stack with the fake Graph: search a user by name, see an already registered one disabled, pick
// an unregistered one, register it and find the row. Also the group search, and (quick
// 261119-wp6) the live member list on an Entra group's detail page.
import {
  expect,
  expectNoHorizontalOverflow,
  expectNoSeriousA11yViolations,
  test,
} from "./fixtures";
import { adminApi, signIn } from "./journey-helpers";
import { checkLayout } from "./layout-helpers";

test.use({ actionTimeout: 15_000 });

test("an admin registers an Entra user found by name", async ({ page }) => {
  test.setTimeout(120_000);
  await signIn(page);
  await page.goto("/admin/users");
  await expect(page.getByRole("table", { name: "Users" })).toBeVisible();
  await page.getByRole("button", { name: "New" }).click();
  await page.getByRole("menuitem", { name: "Register Entra user" }).click();
  const dialog = page.getByRole("dialog", { name: "Register Entra user" });
  const search = dialog.getByRole("combobox", { name: "Search Entra users" });

  // Alice is registered by the demo seed: found, but not pickable.
  await search.fill("Alice");
  const list = dialog.getByRole("listbox", { name: "Directory matches" });
  const alice = list.getByRole("option", { name: /Alice/ }).first();
  await expect(alice).toHaveAttribute("aria-disabled", "true");
  await expect(list.getByText("Already registered").first()).toBeVisible();

  // The results are a dropdown attached under the input: same left edge and width, directly below.
  const field = await search
    .locator("xpath=ancestor::*[@data-control][1]")
    .boundingBox();
  const popup = await list.boundingBox();
  expect(field).not.toBeNull();
  expect(popup).not.toBeNull();
  if (field && popup) {
    expect(Math.abs(popup.x - field.x)).toBeLessThan(2);
    expect(popup.y).toBeGreaterThanOrEqual(field.y + field.height);
    expect(popup.y - (field.y + field.height)).toBeLessThan(24);
  }

  // Escape closes the list and only the list.
  await page.keyboard.press("Escape");
  await expect(list).toBeHidden();
  await expect(dialog).toBeVisible();
  // The list stays closed until the next keystroke: nothing covers the buttons below the field.
  await expect(
    dialog.getByRole("button", { name: "Enter an object ID instead" }),
  ).toBeVisible();

  // Re-runs on a warm stack still find an unregistered Searchable user.
  await search.fill("Searchable");
  const free = list
    .locator('[role="option"]:not([aria-disabled="true"])')
    .first();
  await expect(free).toBeVisible();
  const label = ((await free.innerText()).split("\n")[0] ?? "").trim();
  expect(label).toMatch(/^Searchable /);
  await expectNoSeriousA11yViolations(page);
  await checkLayout(page, "Register Entra user dialog with results");
  await expectNoHorizontalOverflow(page);

  // Arrow keys and Enter pick the same way a click does.
  await search.press("ArrowDown");
  await search.press("Enter");
  await expect(list).toBeHidden();
  await expect(search).toHaveValue(label);
  // The helper line under the box now holds the object ID.
  await expect(
    dialog
      .getByText(
        /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
      )
      .first(),
  ).toBeVisible();

  await dialog.getByRole("button", { name: "Register Entra user" }).click();
  await expect(dialog).toBeHidden();
  await page.goto("/admin/users?source=entra");
  await expect(page.getByRole("link", { name: label }).first()).toBeVisible();
});

test("an admin finds an Entra group by name and can still paste an object ID", async ({
  page,
}) => {
  await signIn(page);
  await page.goto("/admin/groups");
  await expect(page.getByRole("table", { name: "Groups" })).toBeVisible();
  await page.getByRole("button", { name: "New" }).click();
  await page.getByRole("menuitem", { name: "Register Entra group" }).click();
  const dialog = page.getByRole("dialog", { name: "Register Entra group" });
  await dialog.getByLabel("Search Entra groups").fill("Searchable");
  await expect(
    dialog.getByRole("option", { name: /Searchable Analysts/ }),
  ).toBeVisible();
  // The dropdown covers what is below the field until it is closed.
  await page.keyboard.press("Escape");
  await expect(dialog.getByRole("listbox")).toBeHidden();
  await dialog
    .getByRole("button", { name: "Enter an object ID instead" })
    .click();
  await expect(dialog.getByLabel("Object ID")).toBeVisible();
  await dialog
    .getByRole("button", { name: "Search the directory instead" })
    .click();
  await expect(dialog.getByLabel("Search Entra groups")).toBeVisible();
  await expectNoSeriousA11yViolations(page);
});

type Registration = { id: string; object_id: string; display_name: string };

test("an Entra group's detail page lists its live members", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const api = await adminApi();
  const groups = await api.get<{ items: Registration[] }>(
    "identity/entra/groups?limit=100",
  );
  await api.dispose();
  const dataEng = groups.items.find((g) => g.display_name === "Data Eng");
  if (!dataEng) throw new Error("the Data Eng registration is missing");

  await signIn(page);
  await page.goto(`/admin/entra/groups/${dataEng.id}`);
  await expect(
    page.getByRole("heading", { level: 2, name: "Members" }),
  ).toBeVisible();
  await expect(
    page.getByText("Live from Microsoft Entra ID; nested members included."),
  ).toBeVisible();
  const table = page.getByRole("table", {
    name: "Members of this Entra group",
  });
  await expect(table).toBeVisible();
  const row = (name: string) =>
    table.getByRole("row").filter({ hasText: name });

  // A user with a Contractum record links to it; a nested group and a disabled user do not.
  await expect(row("Alice").getByRole("link", { name: "Alice" })).toBeVisible();
  await expect(row("Data Eng Platform")).toContainText("Group");
  await expect(row("Data Eng Platform").getByRole("link")).toHaveCount(0);
  await expect(row("Platform Pat")).toContainText("User");
  await expect(row("Dormant Dee")).toContainText("Disabled");
  await expect(row("Group ingest")).toContainText("Service principal");
  await expectNoSeriousA11yViolations(page);
  await expectNoHorizontalOverflow(page);

  await row("Alice").getByRole("link", { name: "Alice" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Alice" }),
  ).toBeVisible();
});

test("a group with more than 200 members shows the first 200 and says so", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const api = await adminApi();
  const root = (
    await api.get<{ items: { id: string; kind: string }[] }>(
      "identity/folders?limit=100",
    )
  ).items.find((f) => f.kind === "root");
  if (!root) throw new Error("no ROOT folder");
  const created = await api.post<Registration>("identity/entra/groups", {
    owner_folder_id: root.id,
    object_id: "c70e0000-0000-4000-8000-0000000000c1",
  });
  try {
    await signIn(page);
    await page.goto(`/admin/entra/groups/${created.id}`);
    await expect(
      page.getByText("Showing the first 200 of 250 members."),
    ).toBeVisible();
    await expect(
      page
        .getByRole("table", { name: "Members of this Entra group" })
        .getByRole("row"),
    ).toHaveCount(201); // 200 members and the header row
    await expectNoSeriousA11yViolations(page);
  } finally {
    await api.delete(`identity/entra/groups/${created.id}`);
    await api.dispose();
  }
});

test("a directory outage on the members card is a warning with Retry loading", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const api = await adminApi();
  const groups = await api.get<{ items: Registration[] }>(
    "identity/entra/groups?limit=100",
  );
  await api.dispose();
  const dataEng = groups.items.find((g) => g.display_name === "Data Eng");
  if (!dataEng) throw new Error("the Data Eng registration is missing");
  let refuse = true;
  await page.route("**/api/v1/identity/entra/groups/*/members", (route) =>
    refuse
      ? route.fulfill({
          status: 503,
          contentType: "application/problem+json",
          body: JSON.stringify({
            type: "urn:dc:problem:service-unavailable",
            title: "Service Unavailable",
            status: 503,
            detail: "The directory is temporarily unavailable.",
          }),
        })
      : route.continue(),
  );
  await signIn(page);
  await page.goto(`/admin/entra/groups/${dataEng.id}`);
  const retry = page.getByRole("button", { name: "Retry loading" });
  await expect(retry).toBeVisible();
  await expect(page.getByText(/could not be reached/)).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  refuse = false;
  await retry.click();
  await expect(
    page.getByRole("table", { name: "Members of this Entra group" }),
  ).toBeVisible();
});
