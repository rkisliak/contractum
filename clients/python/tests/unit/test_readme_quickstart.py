"""The documentation cannot drift from the CLI: every documented command, flag and name is real.

The README quickstart and ``docs/user/sdk-cli.md`` show many ``contractum ...`` command lines.
This test reads the fenced code blocks of both files, finds every command line and walks the real
click tree: the command (or sub-command) must exist and every ``--flag`` must belong to it. It also
checks the documented ``CONTRACTUM_*`` variables and pytest options, scans the text for
secret-shaped values, and requires the exit codes and report flags to be explained.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import click
import pytest

from contractum.cli import cli

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PACKAGE_ROOT / "src" / "contractum"
REPO_ROOT = PACKAGE_ROOT.parents[1]
README = PACKAGE_ROOT / "README.md"
GUIDE = REPO_ROOT / "docs" / "user" / "sdk-cli.md"
DOCS = [README, GUIDE]

FENCE = re.compile(r"^```(?P<lang>[A-Za-z0-9_+-]*)\s*$")
# `contractum` (or an image `.../contractum:tag`) at a word start, then a word: a command or a flag.
INVOCATION = re.compile(
    r"""(?:^|(?<=[\s"'/]))contractum(?::[\w.-]+)?[ \t]+(?P<rest>(?:[a-z][a-z-]*|-).*)$"""
)
GLOBAL_VALUE_FLAGS = {"--profile", "--url", "--ca-bundle"}
PYTHON_IMPORT = ("from ", "import ")
SEGMENT_END = re.compile(r"^[();<>|&]+$")

# Same value shapes as tests/repo/test_no_default_secrets.py (placeholders do not match).
DCP_TOKEN_VALUE = re.compile(r"dcp_[0-9a-f]{16}_[A-Za-z0-9_-]+")
WHSEC_VALUE = re.compile(r"whsec_[A-Za-z0-9+/=]{20,}")
SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>[\w.-]*(?:password|secret|token))
    (?:["']?:\s*|["']?\s*=\s*)
    (?P<value>"[^"\s][^"]*"|'[^'\s][^']*')
    """
)
SHELL_VARIABLE = re.compile(r"""^["']\$(?:[A-Za-z_]\w*|\{[A-Za-z_]\w*\})["']$""")
GITHUB_EXPRESSION = re.compile(r"""^["']\$\{\{[^}]+\}\}["']$""")


@dataclass(frozen=True)
class Documented:
    """One command line found in a document."""

    source: str
    line: int
    tokens: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: contractum {' '.join(self.tokens)}"


def _read(path: Path) -> str:
    if not path.is_file():
        pytest.skip(f"{path.name} is not part of this checkout")
    return path.read_text(encoding="utf-8")


def _blocks(text: str) -> Iterator[tuple[str, int, list[str]]]:
    """Yield (language, first line number, lines) of every fenced block."""
    lang: str | None = None
    start = 0
    body: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = FENCE.match(line.strip())
        if match and lang is None:
            lang, start, body = match.group("lang").lower(), number + 1, []
        elif match and lang is not None:
            yield lang, start, body
            lang = None
        elif lang is not None:
            body.append(line)


def _logical_lines(lines: list[str], first: int) -> Iterator[tuple[int, str]]:
    """Join shell continuations (a trailing backslash) into one logical line."""
    pending, number = "", first
    for offset, raw in enumerate(lines):
        if not pending:
            number = first + offset
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        yield number, pending + stripped
        pending = ""
    if pending:
        yield number, pending


def _split(rest: str, quote: str | None) -> list[str]:
    if quote:  # the command sits inside a quoted string: it ends at the closing quote
        rest = rest.split(quote, 1)[0]
    try:
        lexer = shlex.shlex(rest, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        tokens = rest.rstrip("\"')").split()
    segment: list[str] = []
    for token in tokens:
        if SEGMENT_END.match(token):
            break
        segment.append(token)
    return segment


def documented_commands(path: Path) -> list[Documented]:
    """Every ``contractum ...`` command line inside the fenced blocks of ``path``."""
    found: list[Documented] = []
    for _lang, first, lines in _blocks(_read(path)):
        for number, line in _logical_lines(lines, first):
            if "docker build" in line or line.lstrip().startswith(PYTHON_IMPORT):
                continue
            match = INVOCATION.search(line)
            if match is None:
                continue
            before = line[: match.start()]
            quote = before[-1] if before and before[-1] in "\"'" else None
            tokens = _split(match.group("rest"), quote)
            if tokens:
                found.append(Documented(path.name, number, tuple(tokens)))
    return found


def _option_names(command: click.Command) -> set[str]:
    names = {"--help"}
    for param in command.params:
        names.update(param.opts)
        names.update(param.secondary_opts)
    return names


def problems_with(entry: Documented) -> list[str]:
    """Why a documented command line does not match the real CLI (empty when it does)."""
    ctx = click.Context(cli)
    tokens = list(entry.tokens)
    global_options = _option_names(cli)
    command: click.Command | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-") and token != "-":
            name = token.split("=", 1)[0]
            if name not in global_options:
                return [f"{name} is not a global option; global options go before the command"]
            index += 2 if name in GLOBAL_VALUE_FLAGS and "=" not in token else 1
            continue
        command = cli.get_command(ctx, token)
        if command is None:
            return [f"{token!r} is not a contractum command"]
        index += 1
        break
    if command is None:
        return []  # only global options, for example `contractum --version`
    # sub-commands such as `config set`
    while isinstance(command, click.Group) and index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            break
        child = command.get_command(ctx, token)
        if child is None:
            return [f"{token!r} is not a sub-command of {command.name}"]
        command, index = child, index + 1
    allowed = _option_names(command)
    errors = []
    for token in tokens[index:]:
        if token.startswith("-") and token != "-" and not re.fullmatch(r"-[0-9.]+", token):
            name = token.split("=", 1)[0]
            if name not in allowed:
                errors.append(f"{name} is not an option of `{command.name}`")
    return errors


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_every_documented_command_and_flag_exists(path: Path) -> None:
    entries = documented_commands(path)
    assert entries, f"no contractum command lines found in {path.name}"
    failures = [f"{entry}: {why}" for entry in entries for why in problems_with(entry)]
    assert not failures, "\n".join(failures)


def test_the_guide_documents_every_command_of_the_cli() -> None:
    text = _read(GUIDE)
    mentioned = {entry.tokens[0] for entry in documented_commands(GUIDE) if entry.tokens}
    commands = set(cli.list_commands(click.Context(cli)))
    assert commands - mentioned == set(), "undocumented commands"
    assert "contractum test" in text


def test_the_checker_rejects_unknown_commands_and_flags() -> None:
    """The drift check can fail: a renamed flag or command is reported."""
    good = Documented("x.md", 1, ("test", "d.parquet", "--contract", "a", "--junit", "r.xml"))
    renamed = Documented("x.md", 1, ("test", "d.parquet", "--contract", "a", "--junit-file", "r"))
    unknown = Documented("x.md", 1, ("verify", "d.parquet"))
    wrong_scope = Documented("x.md", 1, ("show", "a", "--strict"))
    late_global = Documented("x.md", 1, ("test", "d", "--quiet"))
    sub = Documented("x.md", 1, ("config", "set", "url", "https://x"))
    bad_sub = Documented("x.md", 1, ("config", "put", "url", "x"))
    assert problems_with(good) == []
    assert problems_with(sub) == []
    assert "--junit-file" in problems_with(renamed)[0]
    assert "'verify'" in problems_with(unknown)[0]
    assert "--strict" in problems_with(wrong_scope)[0]
    assert "--quiet" in problems_with(late_global)[0]
    assert "'put'" in problems_with(bad_sub)[0]


def test_the_extractor_reads_continuations_pipes_images_and_quoted_strings() -> None:
    text = (
        "```bash\n"
        "CONTRACTUM_URL=https://x \\\n"
        "  uvx contractum test d.parquet --contract a --json - | jq .summary\n"
        'docker run --rm -v "$PWD:/work:ro" ghcr.io/o/contractum:latest test d.csv --contract a\n'
        "uv tool install contractum\n"
        "```\n"
        "```python\n"
        'bash_command="contractum test d.csv --contract a --frozen",\n'
        "from contractum import Client\n"
        "```\n"
    )
    lines: list[tuple[str, ...]] = []
    for _lang, first, body in _blocks(text):
        for _number, line in _logical_lines(body, first):
            match = None if line.lstrip().startswith(PYTHON_IMPORT) else INVOCATION.search(line)
            if match:
                before = line[: match.start()]
                quote = before[-1] if before and before[-1] in "\"'" else None
                lines.append(tuple(_split(match.group("rest"), quote)))
    assert lines == [
        ("test", "d.parquet", "--contract", "a", "--json", "-"),
        ("test", "d.csv", "--contract", "a"),
        ("test", "d.csv", "--contract", "a", "--frozen"),
    ]


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_documented_environment_variables_exist_in_the_code(path: Path) -> None:
    source = "\n".join(
        file.read_text(encoding="utf-8")
        for file in SRC_ROOT.rglob("*.py")
        if "_generated" not in file.parts
    )
    in_code = set(re.findall(r"CONTRACTUM_[A-Z0-9_]+", source))
    documented = set(re.findall(r"CONTRACTUM_[A-Z0-9_]+", _read(path)))
    unknown = {name for name in documented if name not in in_code}
    assert not unknown, f"{sorted(unknown)} are documented but not read by the code"


def test_documented_pytest_options_are_registered_by_the_plugin() -> None:
    registered = set(
        re.findall(r'"(--contractum-[a-z-]+)"', (SRC_ROOT / "pytest_plugin.py").read_text("utf-8"))
    )
    for path in DOCS:
        documented = set(re.findall(r"(?<![\w-])--contractum-[a-z-]+", _read(path)))
        assert documented <= registered, sorted(documented - registered)
    assert {"--contractum-frozen", "--contractum-offline", "--contractum-lock"} <= registered


def test_documented_library_names_exist() -> None:
    import contractum
    from contractum.client import Client
    from contractum.contracts import ContractVersion

    text = _read(GUIDE)
    for name in ("Client", "ContractVersion", "run_test", "assert_conforms", "TestOptions"):
        assert hasattr(contractum, name), name
        assert name in text
    for method in ("from_env", "get", "list_contracts", "versions", "pull"):
        assert callable(getattr(Client, method)), method
    for method in ("test", "to_arrow_schema"):
        assert callable(getattr(ContractVersion, method)), method


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_documents_carry_no_secret_shaped_value(path: Path) -> None:
    text = _read(path)
    assert not DCP_TOKEN_VALUE.search(text)
    assert not WHSEC_VALUE.search(text)
    for line in text.splitlines():
        match = SECRET_ASSIGNMENT.search(line)
        if match is None:
            continue
        value = match.group("value")
        assert SHELL_VARIABLE.match(value) or GITHUB_EXPRESSION.match(value), line


def test_the_secret_scan_can_fail() -> None:
    value = "dcp_" + "0123456789abcdef" + "_" + "x" * 20
    assert DCP_TOKEN_VALUE.search(f"CONTRACTUM_TOKEN={value}")
    assert not DCP_TOKEN_VALUE.search("dcp_<public_id>_<secret>")
    assert SECRET_ASSIGNMENT.search('client_secret = "abc123"')
    assert SHELL_VARIABLE.match('"$PAT"')


def test_the_guide_explains_exit_codes_reports_and_the_release_state() -> None:
    text = _read(GUIDE)
    for code in ("| 0 |", "| 1 |", "| 2 |"):
        assert code in text, f"exit code row {code}"
    for flag in ("--json", "--junit", "--markdown"):
        assert flag in text, flag
    assert "No platform release has been published yet" in text
    for sign_in in (
        "--token -",
        "account menu (your initials) > Access tokens",
        "az account get-access-token",
    ):
        assert sign_in in text, sign_in
    for removed in ("--entra", "--device-code", "--password-stdin", "CONTRACTUM_ENTRA_"):
        assert removed not in text, removed
    readme = _read(README)
    assert "docs/user/sdk-cli.md" in readme
    for journey in ("--frozen", "assert_conforms", "Client.from_env", "--junit"):
        assert journey in readme, journey
