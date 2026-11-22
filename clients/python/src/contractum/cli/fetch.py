"""``contractum list``, ``versions`` and ``show``: read what you may read, byte for byte."""

from __future__ import annotations

import json
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from contractum.cli import output
from contractum.cli.context import client_from_ctx, get_options, lifecycle_options
from contractum.cli.output import stdout_console
from contractum.cli.source import client_with_fallback, lock_server
from contractum.errors import UsageError

if TYPE_CHECKING:
    from contractum.contracts import RenderFormat
    from contractum.models import ContractSummary, StoredValidationReport, VersionInfo
    from contractum.pull import PullResult

MAX_FINDING_LINES = 50


def _is_tty() -> bool:
    return sys.stdout.isatty()


def _emit_table(title: str, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """A rich table on a terminal; otherwise aligned plain text that never wraps."""
    if _is_tty():
        from rich.table import Table

        table = Table(title=title, header_style="bold")
        for header in headers:
            table.add_column(header, overflow="fold")
        for row in rows:
            table.add_row(*row)
        stdout_console(get_options(click.get_current_context())).print(table)
        return
    widths = [max(len(cells[i]) for cells in (headers, *rows)) for i in range(len(headers))]
    for cells in (headers, *rows):
        line = "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True))
        click.echo(line.rstrip())


def _dump(items: Sequence[Any]) -> None:
    click.echo(json.dumps([item.model_dump(mode="json") for item in items], indent=2))


def _list_rows(items: Sequence[ContractSummary]) -> list[list[str]]:
    return [
        [
            item.odcs_id,
            item.name or "-",
            item.latest_version.semver if item.latest_version else "-",
            item.status or "-",
            item.domain or "-",
            str(item.id),
            str(item.folder_id),
        ]
        for item in items
    ]


def _version_rows(items: Sequence[VersionInfo]) -> list[list[str]]:
    return [
        [
            item.semver,
            item.status,
            "yes" if item.deprecated else "no",
            item.sunset_date.isoformat() if item.sunset_date else "-",
            item.published_at.date().isoformat() if item.published_at else "-",
        ]
        for item in items
    ]


@click.command("list")
@click.argument("query", required=False)
@click.option("--folder", metavar="PATH", help="Only contracts under this folder path.")
@click.option("--domain", metavar="DOMAIN", help="Only contracts of this domain.")
@click.option("--status", metavar="STATUS", help="Only contracts with this ODCS status.")
@click.option("--tag", "tags", multiple=True, metavar="TAG", help="Require a tag (repeatable).")
@click.option(
    "--limit", type=click.IntRange(min=1), default=50, show_default=True, help="Most contracts."
)
@click.option("--all", "all_pages", is_flag=True, default=False, help="Follow every page.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Print JSON.")
@click.pass_context
def list_cmd(
    ctx: click.Context,
    query: str | None,
    folder: str | None,
    domain: str | None,
    status: str | None,
    tags: tuple[str, ...],
    limit: int,
    all_pages: bool,
    as_json: bool,
) -> None:
    """List the contracts you can read, in the server's order."""
    with client_from_ctx(ctx) as client:
        items = client.search(
            query,
            folder=folder,
            domain=domain,
            status=status,
            tags=list(tags) or None,
            limit=limit,
            all=all_pages,
        )
    if not items:
        output.info("no contracts found", get_options(ctx))
    if as_json:
        _dump(items)
    elif items:
        headers = ["ODCS ID", "NAME", "LATEST", "STATUS", "DOMAIN", "CONTRACT ID", "FOLDER ID"]
        _emit_table("Contracts", headers, _list_rows(items))


@click.command("versions")
@click.argument("ref")
@click.option("--json", "as_json", is_flag=True, default=False, help="Print JSON.")
@click.pass_context
def versions(ctx: click.Context, ref: str, as_json: bool) -> None:
    """List the versions of a contract: latest, preview or older, deprecation and sunset."""
    with client_from_ctx(ctx) as client:
        items = client.versions(ref)
    if not items:
        output.info("no versions found", get_options(ctx))
    if as_json:
        _dump(items)
    elif items:
        _emit_table(
            f"Versions of {ref}",
            ["VERSION", "STATE", "DEPRECATED", "SUNSET", "PUBLISHED"],
            _version_rows(items),
        )


def _metadata_lines(odcs_id: str | None, contract_id: str, info: VersionInfo) -> list[str]:
    return [
        f"contract_id: {contract_id}",
        f"odcs_id: {odcs_id or '-'}",
        f"semver: {info.semver}",
        f"status: {info.status}",
        f"content_sha256: {info.content_sha256}",
        f"etag: {info.etag}",
        f"media_type: {info.media_type}",
        f"deprecated: {str(info.deprecated).lower()}",
        f"sunset_date: {info.sunset_date.isoformat() if info.sunset_date else '-'}",
        f"published_at: {info.published_at.isoformat() if info.published_at else '-'}",
    ]


def _findings(body: dict[str, Any]) -> list[dict[str, Any]]:
    raw = body.get("findings")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _section(body: dict[str, Any], name: str) -> dict[str, Any]:
    value = body.get(name)
    return value if isinstance(value, dict) else {}


def _verdict(summary: dict[str, Any]) -> str:
    publishable = summary.get("publishable")
    return "unknown" if not isinstance(publishable, bool) else "yes" if publishable else "no"


def _structural_line(structural: dict[str, Any]) -> str:
    valid = structural.get("valid")
    if valid is True:
        return "structural: valid"
    count = structural.get("error_count")
    return f"structural: invalid ({count} errors)" if valid is False else "structural: unknown"


def _severity_line(findings: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for finding in findings:
        severity = str(finding.get("severity", "unknown"))
        counts[severity] = counts.get(severity, 0) + 1
    return ", ".join(f"{name} {number}" for name, number in sorted(counts.items())) or "none"


def _finding_line(finding: dict[str, Any]) -> str:
    rule_set = finding.get("rule_set")
    owner = rule_set.get("name") if isinstance(rule_set, dict) else None
    flag = " blocking" if finding.get("blocking") is True else ""
    return (
        f"  {finding.get('rule_key', '?')} [{finding.get('severity', '?')}{flag}]"
        f" ({owner or finding.get('source', 'native')})"
    )


def _report_lines(report: StoredValidationReport) -> list[str]:
    body = report.report
    summary = _section(body, "summary")
    findings = _findings(body)
    blocking = summary.get("blocking_count")
    if not isinstance(blocking, int):
        blocking = sum(1 for item in findings if item.get("blocking") is True)
    lines = [
        f"validation report of version {report.semver} (run {report.run_id}, "
        f"validated {report.validated_at.isoformat()})",
        f"publishable: {_verdict(summary)}",
        f"findings: {len(findings)} ({blocking} blocking); by severity: {_severity_line(findings)}",
        _structural_line(_section(body, "structural")),
    ]
    if findings:
        lines.append("failing rules:")
        lines.extend(_finding_line(item) for item in findings[:MAX_FINDING_LINES])
    if len(findings) > MAX_FINDING_LINES:
        lines.append(f"  +{len(findings) - MAX_FINDING_LINES} more")
    return lines


def _show_info(
    ctx: click.Context, ref: str, version: str | None, *, metadata: bool, report: bool
) -> None:
    from contractum.contracts import deprecation_message

    with client_from_ctx(ctx) as client:
        resolved, info = client.locate(ref, version=version)
        notice = deprecation_message(
            f"{resolved.odcs_id or resolved.contract_id}@{info.semver}",
            deprecated=info.deprecated,
            sunset_date=info.sunset_date,
        )
        if notice:
            output.warn(notice)
        if metadata:
            click.echo(
                "\n".join(_metadata_lines(resolved.odcs_id, str(resolved.contract_id), info))
            )
        if report:
            click.echo(
                "\n".join(
                    _report_lines(client.validation_report(resolved.contract_id, info.semver))
                )
            )


def _show_document(
    ctx: click.Context, ref: str, version: str | None, fmt: RenderFormat, target: Path | None
) -> None:
    with client_from_ctx(ctx) as client:
        contract = client.get(ref, version=version)
    data = contract.content if fmt == "original" else contract.render(fmt).encode("utf-8")
    if target is None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    from contractum._atomic import write_atomic

    write_atomic(target, data)
    output.info(f"wrote {len(data)} bytes to {target}", get_options(ctx))


@click.command("show")
@click.argument("ref")
@click.option("--version", metavar="VERSION", help="latest, preview or an exact SemVer.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["original", "yaml", "json"]),
    default="original",
    show_default=True,
    help="original prints the stored bytes; yaml and json convert locally.",
)
@click.option("--metadata", is_flag=True, default=False, help="Print the version metadata.")
@click.option("--report", is_flag=True, default=False, help="Print the stored publish report.")
@click.option(
    "-o",
    "--output",
    "target",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the document to FILE.",
)
@click.pass_context
def show(
    ctx: click.Context,
    ref: str,
    version: str | None,
    fmt: RenderFormat,
    metadata: bool,
    report: bool,
    target: Path | None,
) -> None:
    """Print a contract: REF is [FOLDER-PATH:]ODCS-ID-OR-UUID[@latest|@preview|@X.Y.Z]."""
    info_only = metadata or report
    if info_only and target is not None:
        raise UsageError(
            "-o writes the document; it cannot be combined with --metadata or --report"
        )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        try:
            if info_only:
                _show_info(ctx, ref, version, metadata=metadata, report=report)
            else:
                _show_document(ctx, ref, version, fmt, target)
        finally:
            for item in caught:
                output.warn(str(item.message))


def _pull_summary(result: PullResult) -> dict[str, Any]:
    return {
        "lock": str(result.lock_path),
        "server": result.lock.server,
        "contracts": [
            {
                "ref": item.entry.ref,
                "requested": item.entry.requested,
                "odcs_id": item.entry.odcs_id,
                "contract_id": str(item.entry.contract_id),
                "semver": item.entry.semver,
                "content_sha256": item.entry.content_sha256,
                "file": str(item.path),
                "source": item.contract.source,
                "deprecated": item.contract.deprecated,
                "lifecycle": (
                    {"status": item.lifecycle.status, "message": item.lifecycle.message}
                    if item.lifecycle is not None
                    else None
                ),
            }
            for item in result.contracts
        ],
    }


@click.command("pull")
@click.argument("refs", nargs=-1)
@click.option(
    "--dir",
    "directory",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("contracts"),
    show_default=True,
    help="Directory for the pulled contract files.",
)
@click.option(
    "--lock",
    "lock_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("contractum.lock"),
    show_default=True,
    help="The lock file that pins versions and hashes.",
)
@click.option(
    "--frozen",
    is_flag=True,
    default=False,
    envvar="CONTRACTUM_FROZEN",
    help="Use only the pinned versions: never resolve latest or preview; fail on any drift.",
)
@click.option(
    "--offline",
    is_flag=True,
    default=False,
    help="Use only the lock, the pulled files and the cache; no URL and no network needed.",
)
@click.option(
    "--gitattributes/--no-gitattributes",
    "gitattributes",
    default=True,
    show_default=True,
    envvar="CONTRACTUM_GITATTRIBUTES",
    help="Add -text lines to .gitattributes so Git keeps the pulled bytes exact.",
)
@lifecycle_options
@click.option("--json", "as_json", is_flag=True, default=False, help="Print a JSON summary.")
@click.pass_context
def pull(
    ctx: click.Context,
    refs: tuple[str, ...],
    directory: Path,
    lock_path: Path,
    frozen: bool,
    offline: bool,
    gitattributes: bool,
    fail_on_deprecated: bool,
    sunset_within_days: int | None,
    as_json: bool,
) -> int:
    """Pin contracts: write their files and contractum.lock.

    REF is [FOLDER-PATH:]ODCS-ID-OR-UUID[@latest|@preview|@X.Y.Z]. Without REF the lock's own
    references are resolved again (or, with --frozen or --offline, read from the lock). A
    deprecated version warns; --fail-on-deprecated and --sunset-within DAYS turn that into exit
    code 1, after the files and the lock are written.
    """
    from contractum.contracts import LifecyclePolicy
    from contractum.pull import pull as run_pull

    policy = LifecyclePolicy(fail_on_deprecated, sunset_within_days)

    def run() -> PullResult:
        if offline:
            return run_pull(
                None,
                refs,
                directory=directory,
                lock_path=lock_path,
                frozen=frozen,
                offline=True,
                policy=policy,
            )
        fallback = lock_server(lock_path) if frozen else None
        with client_with_fallback(ctx, fallback) as client:
            return run_pull(
                client,
                refs,
                directory=directory,
                lock_path=lock_path,
                frozen=frozen,
                policy=policy,
                gitattributes=gitattributes,
            )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        try:
            result = run()
        finally:
            for warning in caught:
                output.warn(str(warning.message))
    options = get_options(ctx)
    for item in result.contracts:
        output.info(f"pinned {item.entry.label} -> {item.path}", options)
    if as_json:
        click.echo(json.dumps(_pull_summary(result), indent=2))
    rejected = [(p.entry.label, p.lifecycle) for p in result.contracts if p.lifecycle]
    failed = [(label, outcome) for label, outcome in rejected if outcome.status == "failed"]
    for label, outcome in failed:
        output.error(f"{label}: {outcome.message}")
    return 1 if failed else 0
