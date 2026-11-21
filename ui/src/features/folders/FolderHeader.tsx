// The folder page frame (D-UI-09-03): breadcrumbs from the readable ancestors, the display name
// as h1 with kind and Archived badges, the primary "Create {child kind}" action and a folder
// actions menu (rename, archive, unarchive, delete), all gated on /me/capabilities at this folder,
// then the archived banner and the tab navigation from the tab registry. Delete is shown to
// `folder:delete` holders on non-ROOT folders only; it asks the deletion check, stays focusable
// but disabled (with the blockers as its reason) until the folder is empty, and lands on the
// parent folder after a successful delete. Non-holders never request the check.
import {
  Archive,
  ArchiveRestore,
  MoreHorizontal,
  Pencil,
  Plus,
  Trash2,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useRouterState } from "@tanstack/react-router";
import { useState } from "react";
import type { Folder } from "@/api/types";
import { Alert } from "@/components/Alert";
import { Badge } from "@/components/Badge";
import type { Crumb } from "@/components/Breadcrumb";
import { Button, IconButton } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Menu, type MenuItem } from "@/components/Menu";
import { PageHeader } from "@/components/PageHeader";
import { TabNav } from "@/components/Tabs";
import { useToast } from "@/components/Toast";
import { childKindOf, kindLabel } from "@/features/access/roles";
import { useCapabilities } from "@/features/access/useCapabilities";
import {
  folderKeys,
  useFolderDeletionCheck,
  useFolderList,
  useFolderWrites,
} from "./api";
import { folderCopy } from "./copy";
import { CreateFolderDialog } from "./CreateFolderDialog";
import { RenameFolderDialog } from "./RenameFolderDialog";
import { FolderSelect } from "./FolderSelect";
import { FOLDER_TABS, tabVisible } from "./tabs";
import { ancestorIds, folderName } from "./tree";
import styles from "./FolderPages.module.css";

export function FolderHeader({ folder }: { folder: Folder }) {
  const caps = useCapabilities(folder.id);
  const list = useFolderList(true);
  const { archive, unarchive, remove, settle } = useFolderWrites();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [dialog, setDialog] = useState<
    "create" | "rename" | "archive" | "unarchive" | "delete" | null
  >(null);

  const name = folderName(folder);
  const childKind = childKindOf(folder.kind);
  const isRoot = folder.kind === "root";
  // Only holders of folder:delete on a non-ROOT folder ever ask the deletion check, which fans out
  // to identity, contracts and validation.
  const canDelete = !isRoot && caps.can("folder:delete");
  const deletion = useFolderDeletionCheck(folder.id, { enabled: canDelete });

  const folders = list.data?.folders ?? [];
  const byId = new Map(folders.map((candidate) => [candidate.id, candidate]));
  // Every ancestor is a link (Home > Folders > Acme > Payments > Ledger); Root is the Folders page.
  const crumbs: Crumb[] = [{ label: "Folders", to: "/folders" }];
  for (const id of ancestorIds(folders, folder.id)) {
    const ancestor = byId.get(id);
    if (ancestor && ancestor.kind !== "root")
      crumbs.push({
        label: ancestor.display_name,
        to: `/folders/${ancestor.id}`,
      });
  }
  crumbs.push({ label: name });

  const menuItems: MenuItem[] = [];
  if (!isRoot && caps.can("folder:update") && !folder.archived)
    menuItems.push({
      label: "Rename folder",
      icon: Pencil,
      onSelect: () => {
        setDialog("rename");
      },
    });
  if (!isRoot && folder.archived && caps.can("folder:unarchive"))
    menuItems.push({
      label: "Unarchive folder",
      icon: ArchiveRestore,
      onSelect: () => {
        setDialog("unarchive");
      },
    });
  if (!isRoot && !folder.archived && caps.can("folder:archive"))
    menuItems.push({
      label: "Archive folder",
      icon: Archive,
      danger: true,
      onSelect: () => {
        setDialog("archive");
      },
    });

  if (canDelete) {
    const check = deletion.data;
    let unavailableReason: string | undefined;
    if (deletion.isError) unavailableReason = folderCopy.deleteCheckFailed;
    else if (check === undefined) unavailableReason = folderCopy.deleteChecking;
    else if (!check.deletable)
      unavailableReason = folderCopy.deleteUnavailable(check.blockers);
    menuItems.push({
      label: folderCopy.deleteLabel,
      icon: Trash2,
      danger: true,
      ...(unavailableReason === undefined
        ? {
            onSelect: () => {
              setDialog("delete");
            },
          }
        : { unavailableReason }),
    });
  }

  const canCreate =
    childKind !== null && !folder.archived && caps.can("folder:create");
  const base = `/folders/${folder.id}`;
  const tabs = FOLDER_TABS.filter((tab) => tabVisible(tab, folder, caps)).map(
    (tab) => ({
      to: tab.segment === "" ? base : `${base}/${tab.segment}`,
      label: tab.label,
      exact: tab.segment === "",
    }),
  );

  return (
    <>
      <PageHeader
        title={name}
        breadcrumbs={crumbs}
        badges={
          <>
            <Badge intent="brand">{kindLabel(folder.kind)}</Badge>
            {folder.archived ? (
              <Badge intent="warning" icon={Archive}>
                Archived
              </Badge>
            ) : null}
          </>
        }
        description={
          isRoot ? undefined : (
            <span>{folder.path === "" ? "/" : folder.path}</span>
          )
        }
        actions={
          <>
            <FolderSwitcher folder={folder} />
            {canCreate ? (
              <Button
                variant="primary"
                size="small"
                icon={Plus}
                onClick={() => {
                  setDialog("create");
                }}
              >
                {folderCopy.createLabel(childKind)}
              </Button>
            ) : null}
            {menuItems.length > 0 ? (
              <Menu
                trigger={
                  <IconButton
                    icon={MoreHorizontal}
                    size={36}
                    variant="outline-neutral"
                    aria-label={`Actions for ${name}`}
                  />
                }
                items={menuItems}
              />
            ) : null}
          </>
        }
      />
      {folder.archived ? (
        <Alert intent="warning">{folderCopy.archivedBanner}</Alert>
      ) : null}
      <TabNav label="Folder sections" items={tabs} />

      {childKind !== null ? (
        <CreateFolderDialog
          open={dialog === "create"}
          onOpenChange={(open) => {
            setDialog(open ? "create" : null);
          }}
          parentId={folder.id}
          parentName={name}
          childKind={childKind}
        />
      ) : null}
      {dialog === "rename" ? (
        <RenameFolderDialog
          open
          onOpenChange={(open) => {
            setDialog(open ? "rename" : null);
          }}
          folder={folder}
        />
      ) : null}
      <ConfirmDialog
        open={dialog === "archive"}
        onOpenChange={(open) => {
          setDialog(open ? "archive" : null);
        }}
        title={folderCopy.archiveTitle(name)}
        body={folderCopy.archiveBody}
        objectName={name}
        confirmLabel={`Archive ${name}`}
        busyLabel="Archiving…"
        onConfirm={async () => {
          await archive.mutateAsync(folder.id);
        }}
      />
      <ConfirmDialog
        open={dialog === "unarchive"}
        onOpenChange={(open) => {
          setDialog(open ? "unarchive" : null);
        }}
        title={folderCopy.unarchiveTitle(name)}
        body={folderCopy.unarchiveBody}
        dismissLabel="Go back"
        confirmLabel={`Unarchive ${name}`}
        busyLabel="Unarchiving…"
        onConfirm={async () => {
          await unarchive.mutateAsync(folder.id);
        }}
      />
      <ConfirmDialog
        open={dialog === "delete"}
        onOpenChange={(open) => {
          setDialog(open ? "delete" : null);
        }}
        title={folderCopy.deleteTitle(name)}
        body={folderCopy.deleteBody(
          folder.path === "" ? "" : folder.path,
          deletion.data?.purges ?? [],
        )}
        objectName={name}
        confirmLabel={`Delete ${name}`}
        busyLabel="Deleting…"
        onConfirm={async () => {
          try {
            await remove.mutateAsync(folder.id);
          } catch (error) {
            // The folder changed under us (409) or identity could not check: re-ask, so the menu
            // item shows the current reason, and keep the dialog open with the problem.
            await queryClient.invalidateQueries({
              queryKey: folderKeys.deletionCheck(folder.id),
            });
            throw error;
          }
          // Navigate away first, then refresh: the deleted folder's own queries must not refetch.
          const parent = byId.get(folder.parent_id ?? "");
          queryClient.removeQueries({ queryKey: folderKeys.detail(folder.id) });
          queryClient.removeQueries({
            queryKey: folderKeys.deletionCheck(folder.id),
          });
          await navigate({
            to:
              parent === undefined || parent.kind === "root"
                ? "/folders"
                : `/folders/${parent.id}`,
          });
          toast({ title: folderCopy.deleteToast(name), intent: "success" });
          await settle();
        }}
      />
    </>
  );
}

/**
 * The compact "Switch folder" picker in the folder page header: jumps to another folder and keeps
 * the current tab when the target folder has it (Overview otherwise).
 */
function FolderSwitcher({ folder }: { folder: Folder }) {
  const navigate = useNavigate();
  const list = useFolderList();
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  });
  const segment = pathname.split("/")[3] ?? "";
  return (
    <div className={styles.switcher}>
      <FolderSelect
        id="folder-switcher"
        label="Switch folder"
        labelHidden
        placeholder="Switch folder…"
        value={folder.id}
        onChange={(folderId) => {
          if (folderId === "" || folderId === folder.id) return;
          const target = list.data?.folders.find((f) => f.id === folderId);
          const tab = FOLDER_TABS.find((t) => t.segment === segment);
          const keep =
            tab !== undefined &&
            segment !== "" &&
            target !== undefined &&
            (tab.visible === undefined || tab.visible(target));
          void navigate({
            to: keep
              ? `/folders/${folderId}/${segment}`
              : `/folders/${folderId}`,
          });
        }}
      />
    </div>
  );
}
