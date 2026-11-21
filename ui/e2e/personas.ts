// Non-admin personas for the UI journeys, recreated idempotently through the API as admin: the
// user is created when absent, then its role on the folder is ensured (through a local group or a
// direct grant). Nothing is removed afterwards; the personas are part of the demo dataset.
import type { Page } from "@playwright/test";
import { expect } from "./fixtures";
import type { Api } from "./journey-helpers";

export type Persona = {
  username: string;
  password: string;
  displayName: string;
  /** The folder path (as `GET /folders` reports it) where the role is held. */
  folderPath: string;
  role: string;
  /** Held through this local group (owned by the folder's organization) instead of directly. */
  group?: string;
};

export const LEDGER_EDITOR: Persona = {
  username: "ledger-editor",
  password: "Demo-Editor-2026-pass!",
  displayName: "Lena Ledger (editor)",
  folderPath: "acme/payments/ledger",
  role: "team_editor",
  group: "demo-ledger-editors",
};

export const LEDGER_READER: Persona = {
  username: "ledger-reader",
  password: "Demo-Reader-2026-pass!",
  displayName: "Rita Ledger (reader)",
  folderPath: "acme/payments/ledger",
  role: "team_reader",
};

/** A consuming team member: reads published contracts and manages its subscriber cards. */
export const LEDGER_SUBSCRIBER: Persona = {
  username: "ledger-subscriber",
  password: "Demo-Subscriber-2026-pass!",
  displayName: "Sam Subscriber (consumer)",
  folderPath: "acme/payments/ledger",
  role: "consumer",
};

/** Administers the Acme organization: folders, access, groups, accounts and its audit trail. */
export const ACME_ORG_ADMIN: Persona = {
  username: "acme-org-admin",
  password: "Demo-OrgAdmin-2026-pass!",
  displayName: "Olga Acme (org admin)",
  folderPath: "acme",
  role: "org_admin",
};

/** Authors Acme's rule sets and templates; reads contract metadata, never content. */
export const ACME_POLICY_MANAGER: Persona = {
  username: "acme-policy-manager",
  password: "Demo-Policy-2026-pass!",
  displayName: "Pia Acme (policy manager)",
  folderPath: "acme",
  role: "policy_manager",
};

/**
 * Quick 261120-s0p: the persona the sessions journey signs in and out. Dedicated, so revoking its
 * sessions can never disturb another spec's persona. A Team reader holds no admin permission.
 */
export const SESSIONS_USER: Persona = {
  username: "sessions-user",
  password: "Demo-Sessions-2026-pass!",
  displayName: "Sessa Sessions (reader)",
  folderPath: "acme/payments/ledger",
  role: "team_reader",
};

export const PERSONAS = [
  LEDGER_EDITOR,
  LEDGER_READER,
  LEDGER_SUBSCRIBER,
  ACME_ORG_ADMIN,
  ACME_POLICY_MANAGER,
  SESSIONS_USER,
];

type Page_<T> = { items: T[]; next_cursor?: string | null };
type FolderRow = { id: string; path: string; kind: string };

async function all<T>(api: Api, path: string): Promise<T[]> {
  const out: T[] = [];
  let cursor: string | null | undefined = null;
  do {
    const sep = path.includes("?") ? "&" : "?";
    const page: Page_<T> = await api.get<Page_<T>>(
      `${path}${sep}limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    );
    out.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return out;
}

/** Creates each persona's user when absent and ensures its role (group membership or grant). */
export async function ensurePersonas(
  api: Api,
  personas: Persona[] = PERSONAS,
): Promise<void> {
  const folders = await all<FolderRow>(api, "identity/folders");
  const users = await all<{ id: string; username: string }>(
    api,
    "identity/users",
  );
  const groups = await all<{ id: string; name: string }>(
    api,
    "identity/groups",
  );
  for (const persona of personas) {
    const folder = folders.find((f) => f.path === persona.folderPath);
    if (!folder)
      throw new Error(`persona folder ${persona.folderPath} missing`);
    let user = users.find((u) => u.username === persona.username);
    if (!user) {
      user = await api.post<{ id: string; username: string }>(
        "identity/users",
        {
          username: persona.username,
          password: persona.password,
          display_name: persona.displayName,
        },
      );
    }
    const userPrincipal = `local:${user.id}`;
    let principalType = "user";
    let principalId = userPrincipal;
    if (persona.group) {
      let group = groups.find((g) => g.name === persona.group);
      if (!group) {
        const org = folders.find(
          (f) => f.path === persona.folderPath.split("/")[0],
        );
        if (!org) throw new Error(`no organization for ${persona.folderPath}`);
        group = await api.post<{ id: string; name: string }>(
          "identity/groups",
          { name: persona.group, owner_folder_id: org.id },
        );
      }
      const members = await all<{ member_id: string }>(
        api,
        `identity/groups/${group.id}/members`,
      );
      if (!members.some((m) => m.member_id === userPrincipal))
        await api.post(`identity/groups/${group.id}/members`, {
          username: persona.username,
        });
      principalType = "local_group";
      principalId = group.id; // a local group's principal id is its bare id
    }
    const grants = await all<{ principal_id: string; role: string }>(
      api,
      `identity/folders/${folder.id}/grants`,
    );
    if (
      grants.some(
        (g) => g.principal_id === principalId && g.role === persona.role,
      )
    )
      continue;
    await api.post(`identity/folders/${folder.id}/grants`, {
      principal_type: principalType,
      principal_id: principalId,
      role: persona.role,
    });
  }
}

/** Signs in through the local form as the persona and waits for the welcome heading. */
export async function signInAs(page: Page, persona: Persona): Promise<void> {
  await page.goto("/signin");
  await page.getByLabel("Username").fill(persona.username);
  await page.getByLabel("Password", { exact: true }).fill(persona.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: `Welcome, ${persona.displayName}`,
    }),
  ).toBeVisible();
}

/** Opens the account menu: the rail's avatar at 1024px and up, the top bar's below. */
export async function openAccountMenu(page: Page): Promise<void> {
  const wide = (page.viewportSize()?.width ?? 1280) >= 1024;
  const scope = wide
    ? page.getByRole("navigation", { name: "Sections" })
    : page.getByRole("banner");
  await scope.getByRole("button", { name: /^Account menu for / }).click();
  await expect(page.getByRole("menu")).toBeVisible();
}

/** Signs out through the account menu and waits for the sign-in page. */
export async function signOut(page: Page): Promise<void> {
  await openAccountMenu(page);
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/signin/);
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeVisible();
}

/**
 * The non-admin persona of the volume audit (quick 261120-6i0, D-05). Created by `volume-seed.ts`
 * only, so it is exported but NOT part of `PERSONAS`: no other spec depends on volume folders.
 * The 200-character display name has no spaces and the email is 254 characters long.
 */
export const VOLUME_WANDERER: Persona = {
  username: "vol-wanderer",
  password: "Vol-Wanderer-2026-pass!",
  displayName: `Wanderer${"w".repeat(192)}`,
  folderPath: "vol-org-1",
  role: "team_reader",
};
export const VOLUME_WANDERER_EMAIL = `${"w".repeat(64)}@${"wanderer".padEnd(60, "w")}.${"volume".padEnd(60, "v")}.${"audit".padEnd(62, "a")}.test`;
