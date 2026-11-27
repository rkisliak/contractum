// The contract page frame: breadcrumbs (Contracts, the folder chain, the contract), the name as
// h1 with the Latest semver badge, the ODCS id, and the tab navigation over the child routes
// (Overview, Document, Drafts, Versions, Diff, Rule sets, Subscribers). A 404 is NotFound.
import { useLocation } from "@tanstack/react-router";
import type { ReactNode } from "react";
import type { ContractSummary } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import type { Crumb } from "@/components/Breadcrumb";
import { NotFound } from "@/components/NotFound";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { TabNav } from "@/components/Tabs";
import { useCapabilities } from "@/features/access/useCapabilities";
import { useFolderList } from "@/features/folders/api";
import { ancestorIds, contractDisplayName } from "@/features/folders/tree";
import { isNotFound, problemOf } from "@/lib/unexpected";
import { useContract } from "./api";
import { metadataOnlyNotice } from "./copy";

/** The one display rule for a contract's name: its ODCS name, or its id when the name is empty. */
export const contractName = contractDisplayName;

/**
 * Whether a metadata-only caller may open the page at `pathname`: the Overview and Rule sets
 * answer a metadata read, every other tab needs content or published reads the API refuses.
 * `bases` are the page's root paths, the API's canonical id and the id as the URL spells it (a
 * UUID may arrive in another case); a path is compared without case or a trailing slash.
 */
export function metadataOnlyMayOpen(
  pathname: string,
  ...bases: string[]
): boolean {
  const path = pathname.replace(/\/+$/, "").toLowerCase();
  return bases.some((base) => {
    const root = base.toLowerCase();
    return path === root || path === `${root}/rule-sets`;
  });
}

/**
 * What the caller's role lets the contract page offer. `metadataOnly` is the API's own flag
 * (`content_redacted`, a Policy Manager without a contract role); `canSeeVersions` is true when
 * the Versions list, a version and the Diff would be answered (a published read or a full read,
 * shown while the capabilities load). The UI decides nothing: it only leaves out what the API
 * would refuse, so the page sends no request the caller is refused.
 */
export function contractAccess(
  contract: Pick<ContractSummary, "content_redacted">,
  caps: { isPending: boolean; can: (permission: string) => boolean },
) {
  const allows = (permission: string) => caps.isPending || caps.can(permission);
  const metadataOnly = contract.content_redacted;
  // A metadata-only caller holds no content right, also while the capabilities load.
  const canRead = !metadataOnly && allows("contract:read");
  return {
    allows,
    metadataOnly,
    canRead,
    canSeeVersions:
      !metadataOnly && (allows("contract:read-published") || canRead),
  };
}

export function useFolderCrumbs(folderId: string | undefined): Crumb[] {
  const list = useFolderList(true);
  const folders = list.data?.folders ?? [];
  if (!folderId) return [];
  const byId = new Map(folders.map((folder) => [folder.id, folder]));
  const crumbs: Crumb[] = [];
  for (const id of [...ancestorIds(folders, folderId), folderId]) {
    const folder = byId.get(id);
    if (folder && folder.kind !== "root")
      crumbs.push({
        label: folder.display_name,
        to: `/folders/${folder.id}/contracts`,
      });
  }
  return crumbs;
}

export function ContractLayout({
  contractId,
  children,
}: {
  contractId: string;
  children: ReactNode;
}) {
  const contract = useContract(contractId);
  const folderCrumbs = useFolderCrumbs(contract.data?.folder_id);
  const caps = useCapabilities(contract.data?.folder_id ?? null);
  const pathname = useLocation({ select: (location) => location.pathname });

  if (contract.isError) {
    return isNotFound(contract.error) ? (
      <NotFound back={{ label: "contracts", to: "/contracts" }} />
    ) : (
      <ProblemAlert
        problem={problemOf(contract.error)}
        onRetry={() => void contract.refetch()}
      />
    );
  }
  if (contract.data === undefined)
    return <Skeleton label="contract" rows={4} />;
  const data = contract.data;
  const name = contractName(data);
  const base = `/contracts/${data.id}`;
  // Tabs follow what each tab's first request needs (shown while the capabilities load): the
  // current document, drafts and subscribers need contract:read, attached rule sets
  // contract:read-metadata. A published-only reader (a consumer) still reads the Latest document.
  // A metadata-only caller (content_redacted) gets neither content nor version tabs.
  const { allows, metadataOnly, canRead, canSeeVersions } = contractAccess(
    data,
    caps,
  );
  const tabs = [
    { to: base, label: "Overview", exact: true },
    ...(!metadataOnly && (canRead || data.latest_version)
      ? [{ to: `${base}/document`, label: "Document" }]
      : []),
    ...(canRead ? [{ to: `${base}/drafts`, label: "Drafts" }] : []),
    ...(canSeeVersions
      ? [
          { to: `${base}/versions`, label: "Versions" },
          { to: `${base}/diff`, label: "Diff" },
        ]
      : []),
    ...(allows("contract:read-metadata")
      ? [{ to: `${base}/rule-sets`, label: "Rule sets" }]
      : []),
    ...(canRead ? [{ to: `${base}/subscribers`, label: "Subscribers" }] : []),
  ];

  return (
    <>
      <PageHeader
        title={name}
        breadcrumbs={[
          { label: "Contracts", to: "/contracts" },
          ...folderCrumbs,
          { label: name },
        ]}
        badges={
          data.latest_version ? (
            <Badge intent="success">Latest {data.latest_version.semver}</Badge>
          ) : (
            <Badge intent="neutral">Not published</Badge>
          )
        }
        description={<span>{data.odcs_id}</span>}
      />
      <TabNav label="Contract sections" items={tabs} />
      {metadataOnly &&
      !metadataOnlyMayOpen(pathname, base, `/contracts/${contractId}`) ? (
        // A direct link to a content tab: the child is not mounted, so it sends no refused request.
        <Alert intent="info">{metadataOnlyNotice}</Alert>
      ) : (
        children
      )}
    </>
  );
}
