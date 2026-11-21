// The search params of every file route are untrusted input (a pasted or bookmarked URL): each
// route keeps only the keys and types it knows, drops empty values, and trims or caps text. These
// call each route's own validateSearch, so the URL contract is pinned without rendering a screen.
import { describe, expect, it } from "vitest";
import { Route as CardRoute } from "./_app/cards/$cardId";
import { Route as CardsRoute } from "./_app/cards/index";
import { Route as DiffRoute } from "./_app/contracts/$contractId/diff";
import { Route as VersionsRoute } from "./_app/contracts/$contractId/versions/index";
import { Route as ContractsRoute } from "./_app/contracts/index";
import { Route as NewContractRoute } from "./_app/contracts/new";
import { Route as FolderCardsRoute } from "./_app/folders/$folderId/cards";
import { Route as FolderContractsRoute } from "./_app/folders/$folderId/contracts";
import { Route as FoldersRoute } from "./_app/folders/index";
import { Route as DriftRoute } from "./_app/governance/drift";
import { Route as DryRunRoute } from "./_app/governance/dry-run";
import { Route as RuleSetRoute } from "./_app/rule-sets/$ruleSetId";
import { Route as RuleSetsRoute } from "./_app/rule-sets/index";
import { Route as TemplatesRoute } from "./_app/templates/index";
import { Route as CallbackRoute } from "./auth.callback";
import { Route as RotateRoute } from "./signin_.rotate";

type Validator = (search: Record<string, unknown>) => unknown;

/** The route's validateSearch as a plain function (every route here declares one). */
function validatorOf(route: { options: { validateSearch?: unknown } }) {
  const validate = route.options.validateSearch;
  if (typeof validate !== "function") throw new Error("no validateSearch");
  return validate as Validator;
}

const LONG = "x".repeat(300);

describe.each<
  [
    string,
    { options: { validateSearch?: unknown } },
    [Record<string, unknown>, unknown][],
  ]
>([
  [
    "/contracts",
    ContractsRoute,
    [
      [
        {
          q: "  orders ",
          status: "active",
          domain: "sales",
          deleted: "c-1",
          deletedName: "Orders",
          deletedFolder: "f-1",
        },
        {
          q: "orders",
          status: "active",
          domain: "sales",
          deleted: "c-1",
          deletedName: "Orders",
          deletedFolder: "f-1",
        },
      ],
      [{ q: "   ", status: 3, domain: null, deleted: "" }, {}],
      [{ status: LONG }, { status: "x".repeat(40) }],
    ],
  ],
  [
    "/contracts/new",
    NewContractRoute,
    [
      [
        { folder: "f-1", template: "t-1" },
        { folder: "f-1", template: "t-1" },
      ],
      [{ folder: "", template: 7 }, {}],
      [{ folder: LONG }, { folder: "x".repeat(64) }],
    ],
  ],
  [
    "/contracts/$contractId/diff",
    DiffRoute,
    [
      [
        { from: "1.0.0", to: "1.1.0" },
        { from: "1.0.0", to: "1.1.0" },
      ],
      [{ from: "", to: 2 }, {}],
      [{ to: LONG }, { to: "x".repeat(200) }],
    ],
  ],
  [
    "/contracts/$contractId/versions",
    VersionsRoute,
    [
      [
        { published: "1.2.0", status: "published", warnings: 3 },
        { published: "1.2.0", status: "published", warnings: 3 },
      ],
      [{ published: 1, status: null, warnings: "3" }, {}],
    ],
  ],
  [
    "/cards",
    CardsRoute,
    [
      [
        {
          folder: "f-1",
          subtree: false,
          q: "  team ",
          archived: true,
          active: true,
        },
        {
          folder: "f-1",
          subtree: false,
          q: "team",
          archived: true,
          active: true,
        },
      ],
      [{ folder: "", subtree: true, q: "  ", archived: "true", active: 1 }, {}],
    ],
  ],
  [
    "/cards/$cardId",
    CardRoute,
    [
      [{ tab: "members" }, { tab: "members" }],
      [{ tab: "overview" }, {}],
      [{ tab: "nope" }, {}],
    ],
  ],
  [
    "/folders",
    FoldersRoute,
    [
      [{ archived: true }, { archived: true }],
      [{ archived: "true" }, { archived: true }],
      [{ archived: "yes" }, {}],
    ],
  ],
  [
    "/folders/$folderId/cards",
    FolderCardsRoute,
    [
      [
        { q: " pay ", archived: true, active: true },
        { q: "pay", archived: true, active: true },
      ],
      [{ q: " ", archived: false, active: "true" }, {}],
    ],
  ],
  [
    "/folders/$folderId/contracts",
    FolderContractsRoute,
    [
      [
        { q: " ord ", subtree: false },
        { q: "ord", subtree: false },
      ],
      [{ q: "", subtree: "true" }, {}],
    ],
  ],
  [
    "/governance/drift",
    DriftRoute,
    [
      [{ folder_id: "f-1" }, { folder_id: "f-1" }],
      [{ folder_id: "" }, {}],
    ],
  ],
  [
    "/governance/dry-run",
    DryRunRoute,
    [
      [
        { rule_set_id: "rs-1", version: "draft", folder_id: "f-1" },
        { rule_set_id: "rs-1", version: "draft", folder_id: "f-1" },
      ],
      [{ version: "3" }, { version: 3 }],
      [{ rule_set_id: "", version: "0", folder_id: "" }, {}],
      [{ version: "1.5" }, {}],
    ],
  ],
  [
    "/rule-sets",
    RuleSetsRoute,
    [
      [{ folder_id: "f-1" }, { folder_id: "f-1" }],
      [{ folder_id: 5 }, {}],
    ],
  ],
  [
    "/rule-sets/$ruleSetId",
    RuleSetRoute,
    [
      [
        { via: "f-1", tab: "rollout" },
        { via: "f-1", tab: "rollout" },
      ],
      [{ via: "", tab: "nope" }, {}],
    ],
  ],
  [
    "/templates",
    TemplatesRoute,
    [
      [{ folder_id: "f-1" }, { folder_id: "f-1" }],
      [{ folder_id: "" }, {}],
    ],
  ],
  [
    "/auth/callback",
    CallbackRoute,
    [
      [
        { start: "1", redirect: "/audit" },
        { start: "1", redirect: "/audit" },
      ],
      [{ start: 1 }, { start: "1" }],
      [{ start: "yes", redirect: 4 }, {}],
      [{ redirect: "/".repeat(3000) }, { redirect: "/".repeat(2048) }],
    ],
  ],
  [
    "/signin/rotate",
    RotateRoute,
    [
      [{ redirect: "/cards" }, { redirect: "/cards" }],
      [{ redirect: ["/cards"] }, {}],
    ],
  ],
])("the %s search params", (_path, route, cases) => {
  it.each(cases)("maps %j to %j", (input, expected) => {
    expect(validatorOf(route)(input)).toEqual(expected);
  });
});
