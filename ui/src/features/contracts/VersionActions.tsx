// The per-version actions menu (D-UI-10-08..11, D-UI-10-14, D-UI-10-16): view, compare with
// Latest, copy the fetch URL, the Latest alias and a curl example, download YAML or JSON, and the
// lifecycle writes (promote, deprecate or change the sunset date, unpublish, republish), each
// behind a confirmation that names its effect. The server decides derived statuses, so a success
// invalidates the contract's queries instead of patching rows optimistically.
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import {
  ArrowUpCircle,
  CalendarClock,
  Copy,
  Download,
  Eye,
  EyeOff,
  GitCompare,
  MoreHorizontal,
  RotateCcw,
  Terminal,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { isApiError } from "@/api/problem";
import type { ContractSummary, ContractVersion } from "@/api/types";
import { IconButton } from "@/components/Button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { TextField } from "@/components/form";
import { usePublicSettings } from "@/features/settings/api";
import { Menu, type MenuItem } from "@/components/Menu";
import {
  ACCEPT,
  absoluteUrl,
  contractKeys,
  fetchVersionDocument,
  fileNameFor,
  saveText,
  useApi,
  type DocFormat,
} from "./api";
import { reasonsOf } from "./ContractOverview";

type Action = "promote" | "deprecate" | "unpublish" | "republish";

export function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

export function curlExample(url: string): string {
  return `curl -H "Authorization: Bearer $DC_TOKEN" -H "Accept: application/yaml" ${url}`;
}

export function VersionActions({
  contract,
  version,
  latest,
  canPublish,
  onNotice,
}: {
  contract: ContractSummary;
  version: ContractVersion;
  latest: string | null;
  canPublish: boolean;
  onNotice: (message: string) => void;
}) {
  const api = useApi();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [action, setAction] = useState<Action | null>(null);
  const [sunset, setSunset] = useState(version.sunset_date ?? "");
  const [sunsetError, setSunsetError] = useState<string | undefined>(undefined);
  // A reason for unpublish and deprecate: optional, required while the Global admin setting says so.
  const reasonRequired =
    usePublicSettings().data?.require_lifecycle_reason === true;
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string | undefined>(undefined);
  const path = { contract_id: contract.id, ref: version.semver };
  const published = version.lifecycle === "published";
  const fetchUrl = absoluteUrl(version.fetch_url);
  const latestUrl = absoluteUrl(
    `/api/v1/contracts/contracts/${contract.id}/versions/latest`,
  );

  function copy(value: string, message: string) {
    navigator.clipboard.writeText(value).then(
      () => {
        onNotice(message);
      },
      () => {
        onNotice(
          "The clipboard is unavailable. Copy the value from the version page.",
        );
      },
    );
  }

  async function download(format: DocFormat) {
    try {
      const doc = await fetchVersionDocument(
        api,
        contract.id,
        version.semver,
        format,
      );
      saveText(
        doc.text,
        fileNameFor(contract.odcs_id, version.semver, format),
        ACCEPT[format],
      );
    } catch {
      onNotice(`Downloading ${version.semver} failed. Try again.`);
    }
  }

  const items: MenuItem[] = [
    {
      label: "View version",
      icon: Eye,
      onSelect: () => {
        void navigate({
          to: "/contracts/$contractId/versions/$ref",
          params: { contractId: contract.id, ref: version.semver },
        });
      },
    },
    ...(latest && latest !== version.semver
      ? [
          {
            label: "Compare with Latest",
            icon: GitCompare,
            onSelect: () => {
              void navigate({
                to: "/contracts/$contractId/diff",
                params: { contractId: contract.id },
                search: { from: "latest", to: version.semver },
              });
            },
          },
        ]
      : []),
    {
      label: "Copy fetch URL",
      icon: Copy,
      onSelect: () => {
        copy(fetchUrl, `Fetch URL for ${version.semver} copied.`);
      },
    },
    {
      label: "Copy Latest alias URL",
      icon: Copy,
      onSelect: () => {
        copy(latestUrl, "Latest alias URL copied.");
      },
    },
    {
      label: "Copy curl example",
      icon: Terminal,
      onSelect: () => {
        copy(curlExample(fetchUrl), "curl example copied.");
      },
    },
    {
      label: "Download YAML",
      icon: Download,
      onSelect: () => void download("yaml"),
    },
    {
      label: "Download JSON",
      icon: Download,
      onSelect: () => void download("json"),
    },
  ];
  if (canPublish && published && version.status !== "latest")
    items.push({
      label: "Promote to Latest",
      icon: ArrowUpCircle,
      onSelect: () => {
        setAction("promote");
      },
    });
  if (canPublish && published)
    items.push({
      label: version.deprecated ? "Change sunset date" : "Deprecate version",
      icon: CalendarClock,
      onSelect: () => {
        setSunset(version.sunset_date ?? "");
        setSunsetError(undefined);
        setAction("deprecate");
      },
    });
  if (canPublish && published)
    items.push({
      label: "Unpublish version",
      icon: EyeOff,
      danger: true,
      onSelect: () => {
        setAction("unpublish");
      },
    });
  if (canPublish && version.lifecycle === "unpublished")
    items.push({
      label: "Republish version",
      icon: RotateCcw,
      onSelect: () => {
        setAction("republish");
      },
    });

  async function run(kind: Action) {
    const reasonText = reason.trim();
    if (
      (kind === "unpublish" || kind === "deprecate") &&
      reasonRequired &&
      reasonText === ""
    ) {
      setReasonError("Enter a reason.");
      throw new Error("missing reason");
    }
    if (kind === "promote")
      await api.POST(
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/promote",
        { params: { path } },
      );
    if (kind === "unpublish")
      await api.POST(
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/unpublish",
        {
          params: { path },
          ...(reasonText === "" ? {} : { body: { reason: reasonText } }),
        },
      );
    if (kind === "republish")
      await api.POST(
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/republish",
        { params: { path } },
      );
    if (kind === "deprecate") {
      if (!sunset) {
        setSunsetError("Choose a sunset date.");
        throw new Error("missing sunset");
      }
      try {
        await api.POST(
          "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/deprecate",
          {
            params: { path },
            body: {
              sunset_date: sunset,
              ...(reasonText === "" ? {} : { reason: reasonText }),
            },
          },
        );
      } catch (error) {
        if (
          isApiError(error) &&
          error.problem.kind === "problem" &&
          error.problem.slug === "invalid-sunset-date"
        )
          setSunsetError(
            error.problem.detail ?? "Choose a date from today onwards.",
          );
        if (
          isApiError(error) &&
          error.problem.kind === "problem" &&
          error.problem.slug === "lifecycle-reason-required"
        )
          setReasonError("Enter a reason.");
        throw error;
      }
    }
    await queryClient.invalidateQueries({
      queryKey: contractKeys.all(contract.id),
    });
    await queryClient.invalidateQueries({ queryKey: contractKeys.lists });
    onNotice(
      kind === "promote"
        ? `${version.semver} is now the Latest.`
        : kind === "deprecate"
          ? `${version.semver} is deprecated; sunset ${sunset}.`
          : kind === "unpublish"
            ? `${version.semver} is unpublished.`
            : `${version.semver} is published again.`,
    );
  }

  const v = version.semver;
  const dialogs: Record<
    Action,
    { title: string; body: ReactNode; confirm: string; busy: string }
  > = {
    promote: {
      title: `Make ${v} the Latest?`,
      body: `Consumers fetching "latest" will get ${v}${latest ? ` instead of ${latest}` : ""}. Promoting a deprecated version clears its deprecation.`,
      confirm: `Promote ${v}`,
      busy: "Promoting…",
    },
    deprecate: {
      title: version.deprecated
        ? `Change the sunset date of ${v}?`
        : `Deprecate ${v}?`,
      body: (
        <>
          <p>
            It stays fetchable. Consumers receive Deprecation and Sunset
            headers.
          </p>
          <TextField
            id={`sunset-${v}`}
            autoComplete="off"
            label="Sunset date (UTC)"
            type="date"
            min={todayUtc()}
            value={sunset}
            error={sunsetError}
            onChange={(event) => {
              setSunset(event.target.value);
              setSunsetError(undefined);
            }}
          />
          <TextField
            id={`lifecycle-reason-${v}`}
            autoComplete="off"
            label="Reason"
            optional={!reasonRequired}
            maxLength={500}
            helper="Recorded in the audit trail and sent to subscriber cards."
            value={reason}
            error={reasonError}
            onChange={(event) => {
              setReason(event.target.value);
              setReasonError(undefined);
            }}
          />
        </>
      ),
      confirm: version.deprecated ? "Change sunset date" : `Deprecate ${v}`,
      busy: "Saving…",
    },
    unpublish: {
      title: `Unpublish ${v}?`,
      body: (
        <>
          <p>
            It disappears for everyone without publish rights. The number {v} is
            never reused.
          </p>
          <TextField
            id={`lifecycle-reason-${v}`}
            autoComplete="off"
            label="Reason"
            optional={!reasonRequired}
            maxLength={500}
            helper="Recorded in the audit trail and sent to subscriber cards."
            value={reason}
            error={reasonError}
            onChange={(event) => {
              setReason(event.target.value);
              setReasonError(undefined);
            }}
          />
        </>
      ),
      confirm: `Unpublish ${v}`,
      busy: "Unpublishing…",
    },
    republish: {
      title: `Republish ${v}?`,
      body: "It returns as a Preview or older version, never automatically the Latest.",
      confirm: `Republish ${v}`,
      busy: "Republishing…",
    },
  };
  const current = action ? dialogs[action] : null;

  return (
    <>
      <Menu
        trigger={
          <IconButton
            icon={MoreHorizontal}
            aria-label={`Actions for ${v}`}
            variant="ghost"
            size={32}
          />
        }
        items={items}
      />
      {current && action ? (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) {
              setAction(null);
              setReason("");
              setReasonError(undefined);
            }
          }}
          title={current.title}
          body={current.body}
          objectName={v}
          confirmLabel={current.confirm}
          busyLabel={current.busy}
          onConfirm={async () => {
            try {
              await run(action);
            } catch (error) {
              if (
                isApiError(error) &&
                error.problem.kind === "problem" &&
                error.problem.slug === "version-pinned"
              ) {
                const reasons = reasonsOf(error.problem);
                onNotice(
                  `${v} is pinned by subscriber cards${reasons.length ? `: ${reasons.join(", ")}` : ""}. It was not unpublished.`,
                );
              }
              throw error;
            }
          }}
        />
      ) : null}
    </>
  );
}
