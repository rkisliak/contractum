// The contract page frame: breadcrumbs (Contracts, the folder chain, the contract), the name as
// h1 with the Latest semver badge, the ODCS id, and the tab navigation over the child routes
// (Overview, Document, Drafts, Versions, Diff, Rule sets, Subscribers). A 404 is NotFound.
import type { ReactNode } from "react";
import type { ContractSummary } from "@/api/types";
import { Badge } from "@/components/Badge";
import type { Crumb } from "@/components/Breadcrumb";
import { NotFound } from "@/components/NotFound";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { TabNav } from "@/components/Tabs";
import { useCapabilities } from "@/features/access/useCapabilities";
import { useFolderList } from "@/features/folders/api";
import { ancestorIds } from "@/features/folders/tree";
import { isNotFound, problemOf } from "@/lib/unexpected";
import { useContract } from "./api";

export function contractName(
  contract: Pick<ContractSummary, "name" | "odcs_id">,
) {
  return contract.name ?? contract.odcs_id;
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
  const allows = (permission: string) => caps.isPending || caps.can(permission);
  const canRead = allows("contract:read");
  const tabs = [
    { to: base, label: "Overview", exact: true },
    ...(canRead || data.latest_version
      ? [{ to: `${base}/document`, label: "Document" }]
      : []),
    ...(canRead ? [{ to: `${base}/drafts`, label: "Drafts" }] : []),
    { to: `${base}/versions`, label: "Versions" },
    { to: `${base}/diff`, label: "Diff" },
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
      {children}
    </>
  );
}
