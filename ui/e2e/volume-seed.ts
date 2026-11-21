// The volume dataset of the high-volume rendering audit (quick 261120-6i0). TEST STACK ONLY: it is
// run by the `volume-setup` Playwright project against the compose stack (https://traefik from the
// runner), never by global-setup, never against the Kubernetes release.
//
// Every kind looks before it creates, so a second run writes nothing. Values sit at the server's
// own maxima (D-02); a value the server refuses is replaced by the next candidate and listed in
// the ledger's `refused` (once per kind and reason), never by a server change.
//
// Shape (D-01): 4 organizations x 3 projects x 2 teams (40 folders), 320 local users, 160 local
// groups (5 with 120 or more members), 30 service accounts (5 with two keys), 20 Entra users, 20
// Entra groups (one with 600 members) and 15 Entra service principals registered from the fake
// directory, 65 grants on the access-heavy team, 24 folders of access for the non-admin
// `VOLUME_WANDERER`, 40 rule sets (128 versions), 210 contracts (10 with 25 versions), 20
// templates, 110 subscriber cards and 120 personal access tokens.
import type { APIRequestContext } from "@playwright/test";
import { VOLUME_WANDERER, VOLUME_WANDERER_EMAIL } from "./personas";
import { Client } from "./seed-client";

export type Folder = {
  id: string;
  parent_id: string | null;
  kind: string;
  slug: string;
  display_name: string;
  path: string;
};
type Draft = { id: string; etag: string };
export type ContractRow = { id: string; odcs_id: string; name: string };
type Named = { id: string; name: string };

/** What a seed pass did, per kind. */
export type Ledger = {
  created: Record<string, number>;
  existing: Record<string, number>;
  refused: string[];
};

export function newLedger(): Ledger {
  return { created: {}, existing: {}, refused: [] };
}

const bump = (map: Record<string, number>, kind: string, n = 1) => {
  map[kind] = (map[kind] ?? 0) + n;
};

function refuse(ledger: Ledger, kind: string, detail: string): void {
  const line = `${kind}: ${detail.slice(0, 220)}`;
  if (!ledger.refused.some((l) => l.startsWith(`${kind}:`)))
    ledger.refused.push(line);
}

/** Runs `fn` over `items` with at most `limit` in flight. */
export async function pool<T>(
  items: T[],
  limit: number,
  fn: (item: T, index: number) => Promise<void>,
): Promise<void> {
  let next = 0;
  const workers = Array.from(
    { length: Math.min(limit, items.length) },
    async () => {
      while (next < items.length) {
        const index = next++;
        await fn(items[index] as T, index);
      }
    },
  );
  await Promise.all(workers);
}

// ---------- the string catalogue (D-02) ----------

/** `stem` repeated and cut to exactly `length` characters, no spaces. */
export function longToken(stem: string, length = 200): string {
  const filler = "abcdefghijklmnopqrstuvwxyz0123456789";
  let out = stem;
  while (out.length < length) out += filler;
  return out.slice(0, length);
}

/** Sentences cut to exactly `length` characters (spaces allowed), for descriptions. */
export function longText(length: number): string {
  const sentence =
    "This record explains in detail how the volume audit exercises very long descriptions with ordinary words, a few verylongwordswithoutanyspacesatall and punctuation, so wrapping is tested. ";
  let out = "";
  while (out.length < length) out += sentence;
  return out.slice(0, length).trimEnd().padEnd(length, ".");
}

export const CATALOGUE = {
  arabic: "فريق هندسة البيانات للمدفوعات والتسويات",
  hebrew: "צוות הנדסת נתונים עם 2026 ומספרים",
  mixedBidi: "Team שלום 123 עולם Data",
  cjk: "数据合同团队和数据产品负责人",
  japanese: "データ契約チームとデータ製品",
  cyrillic: "Команда контрактов данных и витрин",
  devanagari: "डेटा अनुबंध टीम और डेटा उत्पाद",
  combining: "Café Renée Zöe Niño",
  emoji: "Contract crew \u{1F680}\u{1F4E6}\u{2705} data \u{1F9EA}",
  bidiOverride: "Spoof \u202eterces\u202c name",
  bidiIsolate: "Isolated \u2067עברית\u2069 label",
} as const;

const CATALOGUE_VALUES = Object.values(CATALOGUE);
// Written names follow the one name rule (quick 261120-lbz, D-01: letters, marks, numbers, spaces and
// - _ . , ( ) & ' / : # + @), which refuses emoji, bidi controls and zero-width characters. The seed
// therefore only writes catalogue entries made of allowed characters; the hostile entries stay in
// CATALOGUE because legacy stored names may still hold them and the UI isolation (R17) stays.
const NAME_SAFE = /^[\p{L}\p{M}\p{N} \-_.,()&'/:#+@]*$/u;
const SAFE_VALUES = CATALOGUE_VALUES.filter((v) => NAME_SAFE.test(v));
/** The n-th name-rule-safe catalogue string, with the index so every value is unique. */
export function catalogue(n: number): string {
  return `${SAFE_VALUES[n % SAFE_VALUES.length] ?? ""} ${String(n)}`;
}
/** Same as `catalogue`: kept for the call sites of fields that always refused bidi controls. */
function catalogueSafe(n: number): string {
  return catalogue(n);
}

/** A valid email at exactly `total` characters (local part 64, labels of 60 or fewer). */
export function longEmail(seed: string, total = 254): string {
  const local = longToken(`${seed}.`, 64).replace(/\.$/, "x");
  let left = total - local.length - 1;
  const labels: string[] = [];
  while (left > 63) {
    labels.push("d".repeat(60));
    left -= 61;
  }
  labels.push("e".repeat(Math.max(left, 2)));
  return `${local}@${labels.join(".")}`;
}

const pad = (n: number, width = 3) => String(n).padStart(width, "0");

// ---------- the dataset's fixed shape ----------

const ORG_COUNT = 4;
const PROJECTS_PER_ORG = 3;
const TEAMS_PER_PROJECT = 2;
const USER_COUNT = 320; // including the wanderer
const GROUP_COUNT = 160;
const BIG_GROUPS = 5;
const SA_COUNT = 30;
const CONTRACT_COUNT = 210;
const RULE_SET_COUNT = 40;
const TEMPLATE_COUNT = 20;
const CARD_COUNT = 110;
const ADMIN_PAT_COUNT = 110;
const WANDERER_PAT_COUNT = 10;

const LONG_TEAM_SLUG = `vol-long-named-team-${"abcdefghijklmnopqrstuvwxyz0123456789".slice(0, 43)}`;
export const LONG_TEAM = LONG_TEAM_SLUG;
export const RTL_TEAM = "vol-rtl-team";
export const ACCESS_TEAM = "vol-access-heavy";

type FolderSpec = {
  slug: string;
  parentSlug: string | null;
  names: string[]; // candidates, longest accepted first
  level: "org" | "project" | "team";
};

function folderSpecs(): FolderSpec[] {
  const specs: FolderSpec[] = [];
  for (let o = 1; o <= ORG_COUNT; o++) {
    const orgSlug = `vol-org-${String(o)}`;
    specs.push({
      slug: orgSlug,
      parentSlug: null,
      level: "org",
      names:
        o === 1
          ? [
              longToken("VolumeOrganizationWithAVeryLongName"),
              "Volume organization 1",
            ]
          : [`Volume organization ${String(o)} ${catalogue(o)}`],
    });
    for (let p = 1; p <= PROJECTS_PER_ORG; p++) {
      const projectSlug = `vol-o${String(o)}-p${String(p)}`;
      specs.push({
        slug: projectSlug,
        parentSlug: orgSlug,
        level: "project",
        names: [
          `Volume project ${String(o)}.${String(p)} ${catalogue(o * 10 + p)}`,
        ],
      });
      for (let t = 1; t <= TEAMS_PER_PROJECT; t++) {
        let slug = `${projectSlug}-t${String(t)}`;
        let names = [
          `Volume team ${String(o)}.${String(p)}.${String(t)} ${catalogue(o * 100 + p * 10 + t)}`,
        ];
        if (o === 1 && p === 1 && t === 1) {
          slug = LONG_TEAM_SLUG;
          names = [
            longToken("VolumeLongNamedTeamThatNeverBreaksTheLayout"),
            "Volume long team",
          ];
        }
        if (o === 1 && p === 1 && t === 2) {
          slug = RTL_TEAM;
          names = [`${CATALOGUE.arabic} ${CATALOGUE.hebrew}`, CATALOGUE.arabic];
        }
        if (o === 1 && p === 2 && t === 1) {
          slug = ACCESS_TEAM;
          names = ["Volume access heavy team"];
        }
        specs.push({ slug, parentSlug: projectSlug, names, level: "team" });
      }
    }
  }
  return specs;
}

const SPECS = folderSpecs();
/** Slugs of the 24 teams in creation order (index 0 is the long-named one, 1 the RTL one). */
export const TEAM_SLUGS = SPECS.filter((s) => s.level === "team").map(
  (s) => s.slug,
);
const orgSlugOf = (slug: string): string => {
  const m = /^vol-o(\d)-/.exec(slug);
  if (m) return `vol-org-${m[1] ?? "1"}`;
  if (slug === LONG_TEAM_SLUG || slug === RTL_TEAM || slug === ACCESS_TEAM)
    return "vol-org-1";
  return slug;
};

// ---------- API shapes ----------

type User = {
  id: string;
  username: string;
  principal_id: string;
  display_name: string;
};
type Registration = {
  id: string;
  object_id: string;
  tenant_id: string;
  principal_id?: string;
  display_name: string;
};

export type Volume = {
  folders: Record<string, Folder>;
  teams: Folder[];
  orgs: Folder[];
  longTeam: Folder;
  rtlTeam: Folder;
  accessTeam: Folder;
  /** 255-character odcs_id (the ingest bound; 1000 is refused), 200-character name. */
  longIdContract: ContractRow;
  /** 25 published versions 1.0.0 to 1.24.0, Latest 1.20.0, deprecated and unpublished ones. */
  bigContract: ContractRow;
  /** 50 tables of 40 properties. */
  wideContract: ContractRow;
  /** A long-named contract with a pending draft. */
  draftContract: ContractRow & { draftId: string };
  /** 20 contacts, 15 pins, 20 members. */
  card20: Named;
  group120: Named;
  longEmailUser: User;
  wanderer: User;
  /** The bootstrap admin (the deactivate-admin confirm). */
  adminUser: User;
  /** vol-sa-01 (two keys) and vol-sa-06 (a 100-character name). */
  serviceAccount: Named;
  serviceAccountLong: Named;
  entraUser: Registration;
  entraGroup: Registration;
  entraBigGroup: Registration;
  entraSp: Registration;
  ruleSet20: Named;
  longTemplate: Named;
};

// ---------- folders ----------

async function ensureFolders(
  api: Client,
  ledger: Ledger,
): Promise<Record<string, Folder>> {
  const listed = await api.all<Folder>("identity/folders");
  const root = listed.find((f) => f.kind === "root");
  if (!root) throw new Error("volume seed: no ROOT folder");
  const bySlug: Record<string, Folder> = {};
  for (const f of listed) if (f.slug.startsWith("vol-")) bySlug[f.slug] = f;
  for (const spec of SPECS) {
    if (bySlug[spec.slug]) {
      bump(ledger.existing, "folders");
      continue;
    }
    const parent = spec.parentSlug === null ? root : bySlug[spec.parentSlug];
    if (!parent) throw new Error(`volume seed: no parent for ${spec.slug}`);
    let created: Folder | null = null;
    for (const display_name of spec.names) {
      const res = await api.post("identity/folders", {
        json: { parent_id: parent.id, slug: spec.slug, display_name },
        accept: [422],
      });
      if (res.status === 422) {
        refuse(ledger, "folder name", JSON.stringify(res.body));
        continue;
      }
      created = res.body as Folder;
      break;
    }
    if (!created)
      throw new Error(`volume seed: no accepted name for ${spec.slug}`);
    bySlug[spec.slug] = created;
    bump(ledger.created, "folders");
  }
  return bySlug;
}

/** Posts with each candidate body until one is not a 422; the refusal is recorded once. */
async function postFirstAccepted<T>(
  api: Client,
  ledger: Ledger,
  kind: string,
  path: string,
  candidates: unknown[],
): Promise<T> {
  for (const json of candidates) {
    const res = await api.post(path, { json, accept: [422] });
    if (res.status === 422) {
      refuse(ledger, kind, JSON.stringify(res.body));
      continue;
    }
    return res.body as T;
  }
  throw new Error(`volume seed: ${kind}: every candidate was refused`);
}

// ---------- identities ----------

const userName = (n: number) => `vol-u-${pad(n)}`;
const LONG_EMAIL_USERNAME = `vol-${"longemailuser".padEnd(60, "x")}`;

async function ensureUsers(
  api: Client,
  ledger: Ledger,
): Promise<Record<string, User>> {
  const listed = await api.all<User>("identity/users");
  const byName: Record<string, User> = {};
  for (const u of listed)
    if (u.username.startsWith("vol-")) byName[u.username] = u;
  const wanted: {
    username: string;
    names: string[];
    email: string | null;
    password: string;
  }[] = [];
  wanted.push({
    username: VOLUME_WANDERER.username,
    names: [VOLUME_WANDERER.displayName, "Volume wanderer"],
    email: VOLUME_WANDERER_EMAIL,
    password: VOLUME_WANDERER.password,
  });
  wanted.push({
    username: LONG_EMAIL_USERNAME,
    names: [longToken("LongEmailUserWithAnEnormousDisplayName")],
    email: longEmail("long.email.user", 254),
    password: "Vol-LongEmail-2026-pass!",
  });
  for (let n = 1; n <= USER_COUNT - 2; n++) {
    const display =
      n % 10 === 0
        ? longToken(`VolumeUser${pad(n)}`)
        : n % 13 === 0
          ? `${CATALOGUE.mixedBidi} ${String(n)}`
          : n % 11 === 0
            ? catalogue(n)
            : `Volume user ${pad(n)} ${catalogueSafe(n)}`;
    wanted.push({
      username: userName(n),
      names: [display, `Volume user ${pad(n)}`],
      email:
        n % 4 === 0
          ? longEmail(`volume.user.${String(n)}`, 120 + (n % 130))
          : `volume.user.${pad(n)}@example.test`,
      password: `Vol-Pass-2026-${pad(n)}-Zq!`,
    });
  }
  const todo = wanted.filter((w) => {
    if (byName[w.username]) {
      bump(ledger.existing, "users");
      return false;
    }
    return true;
  });
  await pool(todo, 4, async (w) => {
    const created = await postFirstAccepted<User>(
      api,
      ledger,
      "user",
      "identity/users",
      w.names.map((display_name) => ({
        username: w.username,
        password: w.password,
        display_name,
        ...(w.email ? { email: w.email } : {}),
      })),
    );
    byName[w.username] = created;
    bump(ledger.created, "users");
  });
  return byName;
}

type Group = { id: string; name: string; owner_folder_id: string };
const groupName = (n: number) => `vol-grp-${pad(n)}`;

/** The first BIG_GROUPS groups hold 120 to 150 members; the wanderer's sits in group 1. */
function groupMembers(n: number): number[] {
  const count = n <= BIG_GROUPS ? 120 + n * 5 : n % 6;
  return Array.from({ length: count }, (_, i) => i + 1);
}

async function ensureGroups(
  api: Client,
  ledger: Ledger,
  folders: Record<string, Folder>,
  users: Record<string, User>,
): Promise<Group[]> {
  const listed = await api.all<Group>("identity/groups");
  const byName: Record<string, Group> = {};
  for (const g of listed)
    if (g.name.startsWith("vol-grp-")) byName[g.name.slice(0, 11)] = g;
  const ownerOf = (n: number) => {
    // Groups 1 and 2 belong to the first organization; the rest rotate over the four.
    const org = n <= 2 ? 1 : ((n - 1) % ORG_COUNT) + 1;
    const f = folders[`vol-org-${String(org)}`];
    if (!f) throw new Error("volume seed: organization missing");
    return f;
  };
  const out: Group[] = [];
  const nums = Array.from({ length: GROUP_COUNT }, (_, i) => i + 1);
  await pool(nums, 4, async (n) => {
    let group = byName[groupName(n)];
    if (group) bump(ledger.existing, "groups");
    else {
      const suffix =
        n === 1
          ? ` ${longToken("BigGroupWithAVeryLongName", 80)}`
          : n % 9 === 0
            ? ` ${catalogueSafe(n).slice(0, 60)}`
            : "";
      const name = `${groupName(n)}${suffix}`.slice(0, 100);
      group = await postFirstAccepted<Group>(
        api,
        ledger,
        "group",
        "identity/groups",
        [
          { name, owner_folder_id: ownerOf(n).id },
          { name: groupName(n), owner_folder_id: ownerOf(n).id },
        ],
      );
      bump(ledger.created, "groups");
    }
    out.push(group);
    const want = groupMembers(n).map((i) => userName(i));
    if (n === 1) want.push(VOLUME_WANDERER.username);
    const have = new Set(
      (
        await api.all<{ username: string | null }>(
          `identity/groups/${group.id}/members`,
        )
      ).map((m) => m.username),
    );
    for (const username of want) {
      if (have.has(username) || !users[username]) {
        if (have.has(username)) bump(ledger.existing, "group members");
        continue;
      }
      await api.post(`identity/groups/${group.id}/members`, {
        json: { username },
        allowConflict: true,
      });
      bump(ledger.created, "group members");
    }
  });
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

type ServiceAccount = {
  id: string;
  name: string;
  principal_id: string;
  owner_folder_id: string;
};

async function ensureServiceAccounts(
  api: Client,
  ledger: Ledger,
  folders: Record<string, Folder>,
): Promise<ServiceAccount[]> {
  const listed = await api.all<ServiceAccount>("identity/service-accounts");
  const byPrefix: Record<string, ServiceAccount> = {};
  for (const a of listed)
    if (a.name.startsWith("vol-sa-")) byPrefix[a.name.slice(0, 9)] = a;
  const out: ServiceAccount[] = [];
  const expires = new Date(Date.now() + 60 * 86_400_000).toISOString();
  await pool(
    Array.from({ length: SA_COUNT }, (_, i) => i + 1),
    4,
    async (n) => {
      const prefix = `vol-sa-${pad(n, 2)}`;
      let account = byPrefix[prefix];
      if (account) bump(ledger.existing, "service accounts");
      else {
        const org = folders[`vol-org-${String(((n - 1) % ORG_COUNT) + 1)}`];
        if (!org) throw new Error("volume seed: organization missing");
        const name =
          n % 6 === 0
            ? longToken(`${prefix}-`, 100)
            : n % 5 === 0
              ? `${prefix} ${catalogueSafe(n)}`.slice(0, 100)
              : prefix;
        account = await postFirstAccepted<ServiceAccount>(
          api,
          ledger,
          "service account",
          "identity/service-accounts",
          [
            { owner_folder_id: org.id, name },
            { owner_folder_id: org.id, name: prefix },
          ],
        );
        bump(ledger.created, "service accounts");
      }
      out.push(account);
      // Five accounts hold the maximum of two keys.
      const keys = await api.all<{ id: string }>(
        `identity/service-accounts/${account.id}/keys`,
      );
      const want = n <= 5 ? 2 : n <= 15 ? 1 : 0;
      for (let k = keys.length; k < want; k++) {
        await api.post(`identity/service-accounts/${account.id}/keys`, {
          json: {
            name: `${prefix}-key-${String(k + 1)}`.slice(0, 100),
            expires_at: expires,
          },
        });
        bump(ledger.created, "service account keys");
      }
      bump(
        ledger.existing,
        "service account keys",
        Math.min(keys.length, want),
      );
    },
  );
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

function needUser(
  users: Record<string, User> | User[],
  username: string,
): User {
  const found = Array.isArray(users)
    ? users.find((u) => u.username === username)
    : users[username];
  if (!found) throw new Error(`the user ${username} is missing`);
  return found;
}

function needSa(accounts: ServiceAccount[], n: number): Named {
  const found = accounts.find((a) => a.name.startsWith(`vol-sa-${pad(n, 2)}`));
  if (!found)
    throw new Error(`the volume service account ${String(n)} is missing`);
  return { id: found.id, name: found.name };
}

// ---------- Entra registrations (the generated fake directory, D-04) ----------

const guid = (prefix: string, variant: string, i: number) =>
  `${prefix}-0000-4000-${variant}-${i.toString(16).padStart(12, "0")}`;
export const ENTRA_BIG_GROUP = "ba5e0000-0000-4000-8000-000000000001";
const entraUserOid = (i: number) => guid("ba5e0002", "8000", i);
const entraGroupOid = (i: number) => guid("ba5e0001", "8000", i);
const entraSpOid = (i: number) => guid("ba5e0003", "8000", i);

async function ensureEntra(
  api: Client,
  ledger: Ledger,
  org1: Folder,
): Promise<{
  users: Registration[];
  groups: Registration[];
  sps: Registration[];
}> {
  const kinds = [
    {
      path: "identity/entra/users",
      oids: Array.from({ length: 20 }, (_, i) => entraUserOid(i + 1)),
      owner: false,
    },
    {
      path: "identity/entra/groups",
      oids: [
        ENTRA_BIG_GROUP,
        ...Array.from({ length: 19 }, (_, i) => entraGroupOid(i + 1)),
      ],
      owner: true,
    },
    {
      path: "identity/entra/service-principals",
      oids: Array.from({ length: 15 }, (_, i) => entraSpOid(i + 1)),
      owner: true,
    },
  ];
  const result: Registration[][] = [];
  for (const kind of kinds) {
    const listed = await api.all<Registration>(kind.path);
    const have = new Map(listed.map((r) => [r.object_id, r]));
    const out: Registration[] = [];
    await pool(kind.oids, 4, async (oid) => {
      const found = have.get(oid);
      if (found) {
        bump(ledger.existing, `entra ${kind.path.split("/").at(-1) ?? ""}`);
        out.push(found);
        return;
      }
      const res = await api.post(kind.path, {
        json: kind.owner
          ? { owner_folder_id: org1.id, object_id: oid }
          : { object_id: oid },
      });
      out.push(res.body as Registration);
      bump(ledger.created, `entra ${kind.path.split("/").at(-1) ?? ""}`);
    });
    result.push(out.sort((a, b) => a.object_id.localeCompare(b.object_id)));
  }
  return {
    users: result[0] ?? [],
    groups: result[1] ?? [],
    sps: result[2] ?? [],
  };
}

// ---------- grants ----------

type Grant = { id: string; principal_id: string; role: string };

async function ensureGrant(
  api: Client,
  ledger: Ledger,
  folderId: string,
  have: Set<string>,
  principal_type: string,
  principal_id: string,
  role: string,
): Promise<void> {
  if (have.has(`${principal_id}|${role}`)) {
    bump(ledger.existing, "grants");
    return;
  }
  const res = await api.post(`identity/folders/${folderId}/grants`, {
    json: { principal_type, principal_id, role },
    accept: [409, 422],
  });
  if (res.status === 409) {
    bump(ledger.existing, "grants");
    return;
  }
  if (res.status === 422) {
    refuse(ledger, `grant ${principal_type}`, JSON.stringify(res.body));
    return;
  }
  have.add(`${principal_id}|${role}`);
  bump(ledger.created, "grants");
}

async function ensureAccessGrants(
  api: Client,
  ledger: Ledger,
  accessTeam: Folder,
  users: Record<string, User>,
  groups: Group[],
  accounts: ServiceAccount[],
  entra: Awaited<ReturnType<typeof ensureEntra>>,
  org1: Folder,
): Promise<void> {
  const caps = await api.get<{ grantable_roles?: string[] }>(
    `identity/me/capabilities?folder_id=${accessTeam.id}`,
  );
  const roles = caps.grantable_roles ?? [
    "team_reader",
    "team_editor",
    "team_admin",
  ];
  const have = new Set(
    (await api.all<Grant>(`identity/folders/${accessTeam.id}/grants`)).map(
      (g) => `${g.principal_id}|${g.role}`,
    ),
  );
  type Principal = { type: string; id: string };
  const principals: Principal[] = [];
  for (let n = 2; n <= 21; n++) {
    const u = users[userName(n)];
    if (u) principals.push({ type: "user", id: u.principal_id });
  }
  for (const g of groups
    .filter((x) => x.owner_folder_id === org1.id)
    .slice(0, 15))
    principals.push({ type: "local_group", id: g.id });
  for (const g of entra.groups.slice(0, 10))
    principals.push({ type: "entra_group", id: g.object_id });
  for (const sp of entra.sps.slice(0, 5))
    principals.push({
      type: "service_principal",
      id: sp.principal_id ?? `entra:${sp.tenant_id}:${sp.object_id}`,
    });
  for (const a of accounts
    .filter((x) => x.owner_folder_id === org1.id)
    .slice(0, 10))
    principals.push({ type: "service_account", id: a.principal_id });
  for (const eu of entra.users.slice(0, 5))
    principals.push({
      type: "user",
      id: eu.principal_id ?? `entra:${eu.tenant_id}:${eu.object_id}`,
    });
  const jobs = principals.map((p, i) => ({
    p,
    role: roles[i % roles.length] ?? "team_reader",
  }));
  // Every second principal also holds one more role, so every role of the list is held by many.
  for (let i = 0; i < principals.length; i += 2) {
    const p = principals[i];
    if (p)
      jobs.push({ p, role: roles[(i + 1) % roles.length] ?? "team_reader" });
  }
  for (const job of jobs)
    await ensureGrant(
      api,
      ledger,
      accessTeam.id,
      have,
      job.p.type,
      job.p.id,
      job.role,
    );
}

/** The wanderer: team_reader on 20 teams, team_editor on 2, consumer on 2, plus a group grant. */
async function ensureWandererGrants(
  api: Client,
  ledger: Ledger,
  teams: Folder[],
  wanderer: User,
  group1: Group,
  project: Folder,
): Promise<void> {
  const roleOf = (i: number) =>
    i < 20 ? "team_reader" : i < 22 ? "team_editor" : "consumer";
  await pool(teams, 4, async (team, i) => {
    const have = new Set(
      (await api.all<Grant>(`identity/folders/${team.id}/grants`)).map(
        (g) => `${g.principal_id}|${g.role}`,
      ),
    );
    await ensureGrant(
      api,
      ledger,
      team.id,
      have,
      "user",
      wanderer.principal_id,
      roleOf(i),
    );
  });
  const have = new Set(
    (await api.all<Grant>(`identity/folders/${project.id}/grants`)).map(
      (g) => `${g.principal_id}|${g.role}`,
    ),
  );
  await ensureGrant(
    api,
    ledger,
    project.id,
    have,
    "local_group",
    group1.id,
    "project_editor",
  );
}

// ---------- rule sets ----------

type RuleSetRow = { id: string; name: string; publish_counter: number };

function ruleSetRules(n: number, version: number) {
  const long = (s: string, len: number) =>
    longText(len).replace(/^This record/, s);
  const rule = (
    key: string,
    cond: string,
    passDoc: object,
    failDoc: object,
    severity: string,
  ) => ({
    key,
    description: long(
      `Rule ${key} of set ${String(n)} version ${String(version)}.`,
      900,
    ),
    target: "$",
    condition: cond,
    severity,
    message: long(`Fix ${key}.`, 480),
    fixtures: { pass: [{ document: passDoc }], fail: [{ document: failDoc }] },
  });
  return [
    rule(
      `vol-has-name-${pad(n, 2)}`,
      "has(this.name)",
      { name: "x" },
      { domain: "y" },
      "error",
    ),
    rule(
      `vol-has-description-${pad(n, 2)}`,
      "has(this.description)",
      { description: { purpose: "x" } },
      { name: "y" },
      "warning",
    ),
    rule(
      `vol-has-domain-${pad(n, 2)}`,
      "has(this.domain)",
      { domain: "x" },
      { name: "y" },
      "info",
    ),
  ];
}

async function ensureRuleSetsFor(
  api: Client,
  ledger: Ledger,
  folders: Record<string, Folder>,
): Promise<{ twenty: Named }> {
  const twentyFound: Named[] = [];
  const jobs = SPECS.slice(0, RULE_SET_COUNT).map((spec, i) => ({ spec, i }));
  await pool(jobs, 3, async ({ spec, i }) => {
    const folder = folders[spec.slug];
    if (!folder) throw new Error("volume seed: folder missing");
    const prefix = `vol-rs-${pad(i, 2)}`;
    const target = i === 2 ? 20 : i % 4 === 0 ? 2 : 3;
    const listed = await api.all<RuleSetRow>(
      `validation/folders/${folder.id}/rule-sets`,
    );
    let row = listed.find((r) => r.name.startsWith(prefix));
    if (row) bump(ledger.existing, "rule sets");
    else {
      const name =
        i % 3 === 0
          ? longToken(`${prefix}-`, 200)
          : i % 4 === 1
            ? `${prefix} ${catalogueSafe(i)}`
            : prefix;
      const body = await postFirstAccepted<Named>(
        api,
        ledger,
        "rule set",
        `validation/folders/${folder.id}/rule-sets`,
        [
          { name, description: longText(1500) },
          { name: prefix, description: longText(1500) },
        ],
      );
      row = { id: body.id, name: body.name, publish_counter: 0 };
      bump(ledger.created, "rule sets");
    }
    if (i === 2) twentyFound.push({ id: row.id, name: row.name });
    for (let v = row.publish_counter + 1; v <= target; v++) {
      const current = await api.call("GET", `validation/rule-sets/${row.id}`);
      const saved = await api.put(`validation/rule-sets/${row.id}/draft`, {
        json: {
          description: `${longText(1400)} v${String(v)}`,
          rules: ruleSetRules(i, v),
        },
        headers: { "If-Match": current.etag },
      });
      await api.post(`validation/rule-sets/${row.id}/publish`, {
        headers: { "If-Match": saved.etag },
      });
      bump(ledger.created, "rule set versions");
    }
    bump(
      ledger.existing,
      "rule set versions",
      Math.min(row.publish_counter, target),
    );
  });
  const twenty = twentyFound[0];
  if (!twenty)
    throw new Error("volume seed: the 20-version rule set is missing");
  return { twenty };
}

// ---------- contracts ----------

function purposeOf(c: number): string {
  return c % 4 === 0
    ? longText(2000)
    : "Rows of the volume audit, one per record.";
}

function contractName(c: number): string {
  if (c % 3 === 0) return longToken(`VolumeContract${pad(c)}`);
  if (c % 7 === 0) return `${catalogueSafe(c)} contract`;
  return `Volume contract ${pad(c)}`;
}

function contractOdcsId(c: number): string {
  if (c % 11 === 0)
    return `urn:vol:long:${pad(c)}:${longToken("odcs-id-", 255 - 17)}`.slice(
      0,
      255,
    );
  return `urn:vol:c:${pad(c)}`;
}

function versionsOf(c: number): number {
  if (c < 10) return 25;
  if (c === 10) return 2; // the wide contract
  return 1 + (c % 3);
}

function contractYaml(opts: {
  odcsId: string;
  name: string;
  version: string;
  purpose: string;
  tables?: number;
  props?: number;
}): string {
  const q = (v: string) => JSON.stringify(v);
  const tables = opts.tables ?? 1;
  const props = opts.props ?? 2;
  const out: string[] = [
    "kind: DataContract",
    `id: ${q(opts.odcsId)}`,
    "apiVersion: v3.2.0",
    `name: ${q(opts.name)}`,
    `version: "${opts.version}"`,
    "status: active",
    "domain: volume",
    "description:",
    `  purpose: ${q(opts.purpose)}`,
    "schema:",
  ];
  for (let t = 0; t < tables; t++) {
    out.push(
      `  - name: table_${pad(t, 2)}`,
      "    physicalType: table",
      `    description: Volume audit table ${String(t)}.`,
      "    properties:",
    );
    for (let p = 0; p < props; p++)
      out.push(
        `      - name: column_${pad(p, 2)}`,
        `        logicalType: ${p % 3 === 0 ? "string" : p % 3 === 1 ? "number" : "date"}`,
        `        description: Column ${String(p)} of table ${String(t)}.`,
      );
  }
  return `${out.join("\n")}\n`;
}

type ContractInfo = {
  row: ContractRow;
  teamIndex: number;
  c: number;
  draftId?: string;
};

async function ensureContracts(
  api: Client,
  ledger: Ledger,
  teams: Folder[],
): Promise<ContractInfo[]> {
  const perTeam: ContractRow[][] = [];
  await pool(teams, 4, async (team, i) => {
    perTeam[i] = await api.all<ContractRow>(
      `contracts/contracts?folder_id=${team.id}`,
    );
  });
  const infos: ContractInfo[] = [];
  const jobs = Array.from({ length: CONTRACT_COUNT }, (_, c) => c).filter(
    (c) => c !== 11,
  );
  await pool(jobs, 4, async (c) => {
    const teamIndex = c % teams.length;
    const team = teams[teamIndex];
    if (!team) return;
    const odcsId = contractOdcsId(c);
    const name = contractName(c);
    const target = versionsOf(c);
    let row = perTeam[teamIndex]?.find((r) => r.odcs_id === odcsId);
    let fresh = false;
    let draft: Draft | null = null;
    const yamlOf = (version: string) =>
      c === 10
        ? contractYaml({
            odcsId,
            name,
            version,
            purpose: purposeOf(c),
            tables: 50,
            props: 40,
          })
        : contractYaml({ odcsId, name, version, purpose: purposeOf(c) });
    if (row) bump(ledger.existing, "contracts");
    else {
      const created = (
        await api.post(`contracts/folders/${team.id}/contracts`, {
          yaml: yamlOf("1.0.0"),
        })
      ).body as { contract: { id: string }; draft: Draft };
      row = { id: created.contract.id, odcs_id: odcsId, name };
      draft = created.draft;
      fresh = true;
      bump(ledger.created, "contracts");
    }
    const published = async () =>
      (
        await api.get<{ items: { semver: string; status: string }[] }>(
          `contracts/contracts/${row.id}/versions`,
        )
      ).items.map((v) => v.semver);
    let have = fresh ? [] : await published();
    if (have.length === 0 && !draft)
      throw new Error(
        `volume seed: contract ${row.id} has no published version and no first draft`,
      );
    if (draft && have.length === 0) {
      await api.post(
        `contracts/contracts/${row.id}/drafts/${draft.id}/publish`,
        {
          headers: { "If-Match": draft.etag },
        },
      );
      have = ["1.0.0"];
    }
    for (let v = have.length; v < target; v++) {
      const next = (
        await api.post(`contracts/contracts/${row.id}/drafts`, {
          json: { from_version: `1.${String(v - 1)}.0`, bump: "minor" },
        })
      ).body as { draft: Draft };
      await api.post(
        `contracts/contracts/${row.id}/drafts/${next.draft.id}/publish`,
        {
          headers: { "If-Match": next.draft.etag },
        },
      );
      bump(ledger.created, "contract versions");
      fresh = true;
    }
    let draftId: string | undefined;
    if (fresh) {
      // The lifecycle mix: Latest moved, Deprecated, Unpublished, Preview and a pending draft.
      const sunset = new Date(Date.now() + 365 * 86_400_000)
        .toISOString()
        .slice(0, 10);
      const post = (ref: string, action: string, json?: unknown) =>
        api.post(`contracts/contracts/${row.id}/versions/${ref}/${action}`, {
          ...(json === undefined ? {} : { json }),
          accept: [409, 422],
        });
      if (target >= 25) {
        await post("1.20.0", "promote");
        await post("1.3.0", "deprecate", {
          sunset_date: sunset,
          reason: longText(300),
        });
        await post("1.10.0", "deprecate", {
          sunset_date: sunset,
          reason: "Superseded.",
        });
        await post("1.7.0", "unpublish", { reason: "Published by mistake." });
      } else if (target === 3) {
        if (c % 2 === 0) await post("1.1.0", "promote");
        if (c % 4 === 2)
          await post("1.0.0", "deprecate", {
            sunset_date: sunset,
            reason: "Replaced by 1.1.0.",
          });
        if (c % 7 === 3)
          await post("1.2.0", "unpublish", { reason: "Withdrawn." });
      } else if (target === 2 && c % 2 === 0) {
        await post("1.1.0", "promote");
      }
    }
    if (c % 5 === 0) {
      const drafts = await api.get<{ items: { id: string }[] }>(
        `contracts/contracts/${row.id}/drafts`,
      );
      draftId = drafts.items[0]?.id;
      if (!draftId) {
        const d = (
          await api.post(`contracts/contracts/${row.id}/drafts`, {
            json: { from_version: `1.${String(target - 1)}.0`, bump: "minor" },
          })
        ).body as { draft: Draft };
        draftId = d.draft.id;
        bump(ledger.created, "contract drafts");
      }
    }
    infos[c] = { row, teamIndex, c, ...(draftId ? { draftId } : {}) };
  });
  return infos.filter(Boolean);
}

// ---------- templates ----------

function templateYaml(n: number, name: string): string {
  return `kind: DataContract
id: ${JSON.stringify(`urn:vol:template:${pad(n, 2)}`)}
apiVersion: v3.2.0
name: ${JSON.stringify(name)}
version: "1.0.0"
status: draft
description:
  purpose: ${JSON.stringify(longText(400))}
schema:
  - name: table_name
    physicalType: table
    properties:
      - name: id
        logicalType: string
`;
}

async function ensureTemplates(
  api: Client,
  ledger: Ledger,
  folders: Record<string, Folder>,
): Promise<Named> {
  const listed = await api.all<Named>("contracts/templates");
  let long: Named | undefined;
  const orgs = Array.from(
    { length: ORG_COUNT },
    (_, i) => folders[`vol-org-${String(i + 1)}`],
  );
  for (let n = 1; n <= TEMPLATE_COUNT; n++) {
    const prefix = `vol-tpl-${pad(n, 2)}`;
    const found = listed.find((t) => t.name.startsWith(prefix));
    if (found) {
      bump(ledger.existing, "templates");
      if (n === 1) long = found;
      continue;
    }
    const org = orgs[n % ORG_COUNT];
    if (!org) throw new Error("volume seed: organization missing");
    const candidates = [
      n === 1 ? longToken(`${prefix}-`, 200) : `${prefix} ${catalogueSafe(n)}`,
      prefix,
    ];
    let created: { template: Named } | null = null;
    for (const name of candidates) {
      const res = await api.post(
        `contracts/folders/${org.id}/templates?name=${encodeURIComponent(name)}`,
        { yaml: templateYaml(n, name), accept: [422] },
      );
      if (res.status === 422) {
        refuse(ledger, "template name", JSON.stringify(res.body));
        continue;
      }
      created = res.body as { template: Named };
      break;
    }
    if (!created) throw new Error("volume seed: template refused");
    await api.post(`contracts/templates/${created.template.id}/publish`);
    if (n === 1)
      long = { id: created.template.id, name: candidates[0] ?? prefix };
    bump(ledger.created, "templates");
  }
  if (!long) throw new Error("volume seed: the long template is missing");
  return long;
}

// ---------- subscriber cards ----------

type CardRow = Named;

function cardContacts(n: number, count: number) {
  return Array.from({ length: count }, (_, k) => {
    if (k % 3 === 2)
      return {
        kind: "url",
        name: longToken(`Link${String(k)}-`, 100),
        value: `https://example.test/${longToken(`card${String(n)}-`, 200)}`,
      };
    if (k % 3 === 1)
      return {
        kind: "text",
        name: `Channel ${String(k)}`,
        value: `#volume-${pad(n)}-${String(k)} ${catalogueSafe(k)}`,
      };
    return {
      kind: "email",
      name: longToken(`Contact${String(k)}-`, 100),
      value: longEmail(`card.${String(n)}.${String(k)}`, 254 + ((n + k) % 67)),
    };
  });
}

async function ensureCards(
  api: Client,
  ledger: Ledger,
  teams: Folder[],
  groups: Group[],
  accounts: ServiceAccount[],
  folders: Record<string, Folder>,
  contracts: ContractInfo[],
): Promise<Named> {
  const perTeam: CardRow[][] = [];
  await pool(teams, 4, async (team, i) => {
    perTeam[i] = await api.all<CardRow>(`contracts/folders/${team.id}/cards`);
  });
  // 1.0.0 stays a plain published version unless the lifecycle mix deprecates it.
  const pinnable = contracts.filter(
    (c) => !(versionsOf(c.c) === 3 && c.c % 4 === 2),
  );
  let card20: Named | undefined;
  const nums = Array.from({ length: CARD_COUNT }, (_, i) => i + 1);
  await pool(nums, 4, async (n) => {
    const teamIndex = (n * 5) % teams.length;
    const team = teams[teamIndex];
    if (!team) return;
    const org = folders[orgSlugOf(team.slug)];
    const prefix = `vol-card-${pad(n)}`;
    const heavy = n <= 10; // 20 contacts and 15 pins
    const memberHeavy = n > 10 && n <= 20; // 20 members
    let card: Named | undefined = perTeam[teamIndex]?.find((c) =>
      c.name.startsWith(prefix),
    );
    if (card) bump(ledger.existing, "cards");
    else {
      const name =
        n % 4 === 0
          ? longToken(`${prefix}-`, 200)
          : n % 5 === 0
            ? `${prefix} ${catalogueSafe(n)}`
            : prefix;
      const body = await postFirstAccepted<Named>(
        api,
        ledger,
        "card",
        `contracts/folders/${team.id}/cards`,
        [
          {
            name,
            description: longText(n % 2 === 0 ? 2000 : 300),
            contacts: cardContacts(n, heavy ? 20 : n % 3),
            email_enabled: false,
          },
          {
            name: prefix,
            description: longText(300),
            contacts: [],
            email_enabled: false,
          },
        ],
      );
      card = { id: body.id, name: body.name };
      bump(ledger.created, "cards");
    }
    if (n === 1) card20 = card;
    const detail = await api.get<
      Named & { members: unknown[]; subscriptions: { contract_id: string }[] }
    >(`contracts/cards/${card.id}`);
    // Pins: 15 for the heavy cards, otherwise 1 to 3 distinct contracts.
    const pins = heavy ? 15 : 1 + (n % 3);
    const have = new Set(detail.subscriptions.map((s) => s.contract_id));
    for (let k = 0; k < pins; k++) {
      const target = pinnable[(n * 7 + k * 13) % pinnable.length];
      if (!target || have.has(target.row.id)) continue;
      have.add(target.row.id);
      const res = await api.post(`contracts/cards/${card.id}/subscriptions`, {
        json: { contract_id: target.row.id, version: "1.0.0" },
        accept: [409, 422],
      });
      if (res.status === 422)
        refuse(ledger, "card pin", JSON.stringify(res.body));
      else if (res.status !== 409) bump(ledger.created, "card pins");
    }
    // Members: groups and accounts of the card's organization.
    const ownGroups = groups.filter((g) => g.owner_folder_id === org?.id);
    const ownAccounts = accounts.filter((a) => a.owner_folder_id === org?.id);
    // One distinct list (groups first, a few accounts at the end), so no member repeats.
    const candidates = [
      ...ownGroups.map((g) => ({ type: "local_group", id: g.id })),
      ...ownAccounts.slice(0, 5).map((a) => ({
        type: "service_account",
        id: a.principal_id,
      })),
    ];
    const wantMembers = memberHeavy || heavy ? 20 : n % 3;
    const linked = new Set(
      (detail.members as { member_id: string }[]).map((m) => m.member_id),
    );
    for (let k = 0; k < candidates.length && linked.size < wantMembers; k++) {
      const target = candidates[(n + k) % candidates.length];
      if (!target || linked.has(target.id)) continue;
      const res = await api.post(`contracts/cards/${card.id}/members`, {
        json: { member_type: target.type, member_id: target.id },
        accept: [409, 422],
      });
      if (res.status === 422)
        refuse(ledger, "card member", JSON.stringify(res.body));
      else if (res.status === 201) bump(ledger.created, "card members");
      linked.add(target.id);
    }
  });
  if (!card20) throw new Error("volume seed: card 1 missing");
  return card20;
}

// ---------- tokens ----------

function tokenName(n: number): string {
  if (n % 7 === 0) return longToken(`vol-pat-${pad(n)}-`, 100);
  if (n % 5 === 0) return `vol-pat-${pad(n)} ${catalogueSafe(n)}`.slice(0, 100);
  return `vol-pat-${pad(n)}`;
}

/**
 * The seed gives the admin 120 tokens and the wanderer 24, far above the default limit of 10 active
 * tokens per owner (quick 261120-lbz, D-03). It raises the stored `security.max_active_tokens` to
 * 1000 (with `If-Match`) before creating them; the settings spec resets the section afterwards.
 */
async function ensureTokenLimit(api: Client): Promise<void> {
  const listed = await api.get<{
    sections: {
      section: string;
      etag: string;
      values: Record<string, unknown>;
    }[];
  }>("identity/settings");
  const security = listed.sections.find((x) => x.section === "security");
  if (!security) throw new Error("volume seed: no security settings section");
  if (security.values["max_active_tokens"] === 1000) return;
  await api.put("identity/settings/security", {
    json: { ...security.values, max_active_tokens: 1000 },
    headers: { "If-Match": security.etag },
  });
}

async function ensureTokens(
  api: Client,
  ledger: Ledger,
  token: string | null,
  count: number,
  prefix: string,
): Promise<void> {
  const listed = await api.all<{ name: string; revoked_at?: string | null }>(
    "identity/pats",
  );
  const have = new Set(listed.map((t) => t.name.slice(0, prefix.length + 4)));
  const expires = new Date(Date.now() + 60 * 86_400_000).toISOString();
  const missing = Array.from({ length: count }, (_, i) => i + 1).filter(
    (n) => !have.has(`${prefix}-${pad(n)}`.slice(0, prefix.length + 4)),
  );
  bump(ledger.existing, "tokens", count - missing.length);
  await pool(missing, 4, async (n) => {
    await api.post("identity/pats", {
      json: {
        name: tokenName(n).replace("vol-pat", prefix).slice(0, 100),
        expires_at: expires,
        read_only: n % 9 === 0,
      },
      ...(token ? { token } : {}),
    });
    bump(ledger.created, "tokens");
  });
}

// ---------- the whole seed ----------

export async function ensureVolumeData(
  ctx: APIRequestContext,
  token: string,
): Promise<{ volume: Volume; ledger: Ledger }> {
  const api = new Client(ctx, token, "volume seed");
  const ledger = newLedger();
  const folders = await ensureFolders(api, ledger);
  const teams = TEAM_SLUGS.map((slug) => {
    const f = folders[slug];
    if (!f) throw new Error(`volume seed: team ${slug} missing`);
    return f;
  });
  const orgs = Array.from({ length: ORG_COUNT }, (_, i) => {
    const f = folders[`vol-org-${String(i + 1)}`];
    if (!f) throw new Error("volume seed: organization missing");
    return f;
  });
  const org1 = orgs[0] as Folder;
  const [longTeam, rtlTeam, accessTeam] = [
    LONG_TEAM,
    RTL_TEAM,
    ACCESS_TEAM,
  ].map((slug) => {
    const f = folders[slug];
    if (!f) throw new Error("volume seed: teams missing");
    return f;
  }) as [Folder, Folder, Folder];

  const users = await ensureUsers(api, ledger);
  const groups = await ensureGroups(api, ledger, folders, users);
  const accounts = await ensureServiceAccounts(api, ledger, folders);
  const entra = await ensureEntra(api, ledger, org1);
  await ensureAccessGrants(
    api,
    ledger,
    accessTeam,
    users,
    groups,
    accounts,
    entra,
    org1,
  );
  const wanderer = users[VOLUME_WANDERER.username];
  const group1 = groups.find((g) => g.name.startsWith("vol-grp-001"));
  const project3 = folders["vol-o1-p3"];
  if (!wanderer || !group1 || !project3)
    throw new Error("volume seed: wanderer pieces missing");
  await ensureWandererGrants(api, ledger, teams, wanderer, group1, project3);
  const { twenty } = await ensureRuleSetsFor(api, ledger, folders);
  const contracts = await ensureContracts(api, ledger, teams);
  const longIdContract = await ensureLongIdContract(api, ledger, longTeam);
  const longTemplate = await ensureTemplates(api, ledger, folders);
  const card20 = await ensureCards(
    api,
    ledger,
    teams,
    groups,
    accounts,
    folders,
    contracts,
  );
  await ensureTokenLimit(api);
  await ensureTokens(api, ledger, null, ADMIN_PAT_COUNT, "vol-pat");
  // The wanderer's own tokens, through its own sign-in.
  const login = await ctx.post("/api/v1/identity/auth/login", {
    data: {
      username: VOLUME_WANDERER.username,
      password: VOLUME_WANDERER.password,
    },
  });
  if (!login.ok())
    throw new Error(`volume seed: wanderer sign-in ${String(login.status())}`);
  const wandererToken = ((await login.json()) as { access_token: string })
    .access_token;
  const wApi = new Client(ctx, wandererToken, "volume seed (wanderer)");
  await ensureTokens(wApi, ledger, null, WANDERER_PAT_COUNT, "vol-wpat");

  const find = (c: number) => {
    const info = contracts.find((x) => x.c === c);
    if (!info) throw new Error(`volume seed: contract ${String(c)} missing`);
    return info;
  };
  const draftInfo = find(15);
  const group120 = group1;
  const longEmailUser = users[LONG_EMAIL_USERNAME];
  const [entraUser, entraGroup, entraSp] = [
    entra.users[0],
    entra.groups.find((g) => g.object_id !== ENTRA_BIG_GROUP),
    entra.sps[0],
  ];
  const entraBigGroup = entra.groups.find(
    (g) => g.object_id === ENTRA_BIG_GROUP,
  );
  if (!longEmailUser || !entraUser || !entraGroup || !entraSp || !entraBigGroup)
    throw new Error("volume seed: records missing");
  console.log(
    `volume seed created ${JSON.stringify(ledger.created)} existing ${JSON.stringify(ledger.existing)}`,
  );
  return {
    volume: {
      folders,
      teams,
      orgs,
      longTeam,
      rtlTeam,
      accessTeam,
      longIdContract,
      bigContract: find(0).row,
      wideContract: find(10).row,
      draftContract: { ...draftInfo.row, draftId: draftInfo.draftId ?? "" },
      card20,
      group120: { id: group120.id, name: group120.name },
      longEmailUser,
      wanderer,
      adminUser: needUser(await api.all<User>("identity/users"), "admin"),
      serviceAccount: needSa(accounts, 1),
      serviceAccountLong: needSa(accounts, 6),
      entraUser,
      entraGroup,
      entraBigGroup,
      entraSp,
      ruleSet20: twenty,
      longTemplate,
    },
    ledger,
  };
}

// ---------- the long-id contract (kept from the tracer) ----------

/** The longest `odcs_id` the ingest accepts (the document's `id`: 255 characters). */
export const ODCS_ID_MAX = 255;

async function ensureLongIdContract(
  api: Client,
  ledger: Ledger,
  team: Folder,
): Promise<ContractRow> {
  const name = longToken("VolumeContractWithAnEnormousName");
  const listed = await api.all<ContractRow>(
    `contracts/contracts?folder_id=${team.id}`,
  );
  const found = listed.find(
    (c) =>
      c.odcs_id.length >= ODCS_ID_MAX - 5 &&
      c.odcs_id.startsWith("urn:vol:long-"),
  );
  if (found) {
    bump(ledger.existing, "contracts");
    return found;
  }
  // D-02: 1000 characters is the API model's bound but the ingest refuses a document `id` above
  // 255; the refusal is recorded and the longest accepted length is used.
  for (const length of [1000, ODCS_ID_MAX]) {
    const odcsId = `urn:vol:long-${longToken("odcs-id-", length - 13)}`;
    const res = await api.post(
      `contracts/folders/${team.id}/contracts/starter`,
      {
        json: { name, odcs_id: odcsId },
        accept: [422],
      },
    );
    if (res.status === 422) {
      refuse(
        ledger,
        `contract odcs_id of ${String(length)} characters`,
        JSON.stringify(res.body),
      );
      continue;
    }
    const created = res.body as { contract: { id: string }; draft: Draft };
    await api.post(
      `contracts/contracts/${created.contract.id}/drafts/${created.draft.id}/publish`,
      {
        headers: { "If-Match": created.draft.etag },
      },
    );
    bump(ledger.created, "contracts");
    return { id: created.contract.id, odcs_id: odcsId, name };
  }
  throw new Error("volume seed: no accepted odcs_id length");
}

// ---------- lookups for the spec ----------

/** The ids the spec needs, found by listing (no state is kept between runs). */
export async function findVolume(api: Client): Promise<Volume> {
  const listed = await api.all<Folder>("identity/folders");
  const folders: Record<string, Folder> = {};
  for (const f of listed) if (f.slug.startsWith("vol-")) folders[f.slug] = f;
  const need = <T>(value: T | undefined, what: string): T => {
    if (value === undefined)
      throw new Error(
        `the volume dataset is missing ${what}: run --project=volume-setup first`,
      );
    return value;
  };
  const teams = TEAM_SLUGS.map((slug) => need(folders[slug], `team ${slug}`));
  const orgs = Array.from({ length: ORG_COUNT }, (_, i) =>
    need(folders[`vol-org-${String(i + 1)}`], "an organization"),
  );
  const longTeam = need(folders[LONG_TEAM], "the long-named team");
  const rtlTeam = need(folders[RTL_TEAM], "the right-to-left team");
  const accessTeam = need(folders[ACCESS_TEAM], "the access-heavy team");
  const inLong = await api.all<ContractRow>(
    `contracts/contracts?folder_id=${longTeam.id}`,
  );
  const longIdContract = need(
    inLong.find((c) => c.odcs_id.startsWith("urn:vol:long-")),
    "the long-id contract",
  );
  const bigContract = need(
    inLong.find((c) => c.odcs_id === contractOdcsId(0)),
    "the 25-version contract",
  );
  const teamOf = async (index: number, c: number) => {
    const team = need(teams[index], "a team");
    const rows = await api.all<ContractRow>(
      `contracts/contracts?folder_id=${team.id}`,
    );
    return need(
      rows.find((r) => r.odcs_id === contractOdcsId(c)),
      `contract ${String(c)}`,
    );
  };
  const wideContract = await teamOf(10, 10);
  const draftBase = await teamOf(15, 15);
  const drafts = await api.get<{ items: { id: string }[] }>(
    `contracts/contracts/${draftBase.id}/drafts`,
  );
  const draftContract = {
    ...draftBase,
    draftId: need(drafts.items[0]?.id, "the pending draft"),
  };
  const groups = await api.all<Group>("identity/groups");
  const group120 = need(
    groups.find((g) => g.name.startsWith("vol-grp-001")),
    "the 120-member group",
  );
  const users = await api.all<User>("identity/users");
  const longEmailUser = need(
    users.find((u) => u.username === LONG_EMAIL_USERNAME),
    "the long-email user",
  );
  const wanderer = need(
    users.find((u) => u.username === VOLUME_WANDERER.username),
    "the wanderer",
  );
  const eu = await api.all<Registration>("identity/entra/users");
  const eg = await api.all<Registration>("identity/entra/groups");
  const esp = await api.all<Registration>("identity/entra/service-principals");
  const entraUser = need(
    eu.find((r) => r.object_id === entraUserOid(10)),
    "an Entra user",
  );
  const entraGroup = need(
    eg.find((r) => r.object_id === entraGroupOid(10)),
    "an Entra group",
  );
  const entraBigGroup = need(
    eg.find((r) => r.object_id === ENTRA_BIG_GROUP),
    "the big Entra group",
  );
  const entraSp = need(
    esp.find((r) => r.object_id === entraSpOid(10)),
    "an Entra service principal",
  );
  const sets = await api.all<RuleSetRow>(
    `validation/folders/${longTeam.id}/rule-sets`,
  );
  const ruleSet20 = need(
    sets.find((r) => r.name.startsWith("vol-rs-02")),
    "the 20-version rule set",
  );
  const templates = await api.all<Named>("contracts/templates");
  const longTemplate = need(
    templates.find((t) => t.name.startsWith("vol-tpl-01")),
    "the long template",
  );
  const cards = await api.all<Named>(
    `contracts/folders/${need(teams[5], "a team").id}/cards`,
  );
  const card20 = need(
    cards.find((c) => c.name.startsWith("vol-card-001")),
    "the 20-contact card",
  );
  return {
    folders,
    teams,
    orgs,
    longTeam,
    rtlTeam,
    accessTeam,
    longIdContract,
    bigContract,
    wideContract,
    draftContract,
    card20,
    group120: { id: group120.id, name: group120.name },
    longEmailUser,
    wanderer,
    adminUser: needUser(users, "admin"),
    serviceAccount: needSa(
      await api.all<ServiceAccount>("identity/service-accounts"),
      1,
    ),
    serviceAccountLong: needSa(
      await api.all<ServiceAccount>("identity/service-accounts"),
      6,
    ),
    entraUser,
    entraGroup,
    entraBigGroup,
    entraSp,
    ruleSet20: { id: ruleSet20.id, name: ruleSet20.name },
    longTemplate,
  };
}
