// The table columns the global Contracts list and a folder's Contracts tab share (quick
// 261121-0sh): name (opens the contract), ODCS id, ODCS status, latest version and updated time.
// Each page adds its own Folder, Domain or Tags columns between them.
import { Link } from "@tanstack/react-router";
import type { ContractSummary } from "@/api/types";
import { Badge } from "@/components/Badge";
import { orNotSet, type Column } from "@/components/DataTable";
import { Identifier } from "@/components/Identifier";
import { formatDateTime } from "@/features/folders/FolderOverview";
import styles from "./Contracts.module.css";

type ContractColumn = Column<ContractSummary>;

export const contractNameColumn: ContractColumn = {
  id: "name",
  header: "Name",
  rowHeader: true,
  sortValue: (row) => row.name ?? row.odcs_id,
  cell: (row) => (
    <Link
      to="/contracts/$contractId"
      params={{ contractId: row.id }}
      className={styles.rowLink}
    >
      {row.name ?? row.odcs_id}
    </Link>
  ),
};

export const contractOdcsIdColumn: ContractColumn = {
  id: "odcs",
  header: "ODCS id",
  cell: (row) => <Identifier value={row.odcs_id} max="12rem" />,
};

export const contractStatusColumn: ContractColumn = {
  id: "status",
  header: "ODCS status",
  cell: (row) =>
    row.status ? <Badge intent="neutral">{row.status}</Badge> : orNotSet(null),
};

export const contractLatestColumn: ContractColumn = {
  id: "latest",
  header: "Latest version",
  cell: (row) =>
    row.latest_version ? (
      <span className={styles.ident}>{row.latest_version.semver}</span>
    ) : (
      "Not published"
    ),
};

export const contractUpdatedColumn: ContractColumn = {
  id: "updated",
  header: "Updated",
  sortValue: (row) => row.updated_at,
  cell: (row) => formatDateTime(row.updated_at),
};
