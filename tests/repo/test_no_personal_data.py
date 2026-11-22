"""No tracked file leaks a home-directory path or a commit author's email address.

The guard holds no personal data itself, only the mechanism:

* **Home-directory paths.** ``/Users/<name>/``, ``/home/<name>/`` and ``C:\\Users\\<name>\\`` are
  refused unless ``<name>`` is an obvious placeholder (:data:`PLACEHOLDER_USERS`) or a template
  token such as ``<user>``, ``$USER``, ``${USER}`` or ``%USERNAME%``. Write ``$HOME``, ``~`` or a
  placeholder instead of a real path.
* **Author emails.** Every author and committer email in the git history
  (``git log --format='%ae%n%ce'``) is refused anywhere in the tracked files, except GitHub's
  ``noreply`` addresses and bot accounts. The list is read at test time, so a new contributor is
  covered without editing this file.

Each hit is reported as ``file:line``. Binary files (a NUL byte in the first 8 KiB) are skipped.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

# Names that are clearly not a person: documentation placeholders and service accounts.
PLACEHOLDER_USERS = frozenset(
    {
        "you",
        "your-name",
        "yourname",
        "username",
        "user",
        "me",
        "example",
        "runner",  # the GitHub-hosted runner account
        "node",  # the node image's user
        "app",
        "pwuser",  # the Playwright image's user
        "shared",
        "public",
        "default",
    }
)

# Not preceded by a word character, dot or hyphen, so a URL path such as example.com/Users/list
# is not a home directory.
HOME_PATH = re.compile(
    r"(?<![\w.-])(?:/Users/|/home/|[A-Za-z]:\\\\?Users\\\\?)"
    r"(?P<name>[^/\\\s\"'`)<>]+)(?:[/\\]|$)"
)
TEMPLATE_TOKEN = re.compile(r"^(?:<[^>]*|\$\{?\w+\}?|%\w+%|\{\{.*|\*+)$")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# Fixed argv with an absolute git path (GitHub runners and macOS both ship /usr/bin/git).
def _tracked_names() -> list[str]:
    listed = subprocess.run(
        ["/usr/bin/git", "ls-files", "-z"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout
    return [name for name in listed.split("\0") if name]


def _history_emails() -> list[str]:
    return subprocess.run(
        ["/usr/bin/git", "log", "--format=%ae%n%ce"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


def _tracked_text_files() -> list[Path]:
    files: list[Path] = []
    for name in _tracked_names():
        path = REPO / name
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as handle:
            if b"\0" in handle.read(8192):
                continue
        files.append(path)
    return files


def is_placeholder_user(name: str) -> bool:
    return name.lower() in PLACEHOLDER_USERS or bool(TEMPLATE_TOKEN.match(name))


def home_path_hits(text: str) -> list[tuple[int, str]]:
    """(line number, matched path) for each real-looking home-directory path in ``text``."""
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in HOME_PATH.finditer(line):
            if not is_placeholder_user(match.group("name")):
                hits.append((number, match.group(0)))
    return hits


def is_public_email(email: str) -> bool:
    """GitHub's noreply addresses (users, bots, web-flow commits) are public by design."""
    local, _, domain = email.lower().partition("@")
    return domain in {"users.noreply.github.com", "github.com"} or local.endswith("[bot]")


def author_emails() -> set[str]:
    emails = {line.strip().lower() for line in _history_emails()}
    return {email for email in emails if email and not is_public_email(email)}


def email_hits(text: str, emails: set[str]) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in EMAIL.finditer(line):
            if match.group(0).lower() in emails:
                hits.append((number, "an author email"))
    return hits


def test_no_tracked_file_holds_a_home_directory_path() -> None:
    hits = [
        f"{path.relative_to(REPO)}:{number}: {found}"
        for path in _tracked_text_files()
        for number, found in home_path_hits(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not hits, "home-directory paths (use $HOME, ~ or a placeholder):\n" + "\n".join(hits)


def test_no_tracked_file_holds_a_commit_author_email() -> None:
    emails = author_emails()
    hits = [
        f"{path.relative_to(REPO)}:{number}: {found}"
        for path in _tracked_text_files()
        for number, found in email_hits(path.read_text(encoding="utf-8", errors="replace"), emails)
    ]
    # The email itself is never printed, only where it is.
    assert not hits, "commit author emails in tracked files:\n" + "\n".join(hits)


@pytest.mark.parametrize(
    "line",
    [
        "cd /Users/alice/src/contractum",
        "export PATH=/home/bob/.local/bin:$PATH",
        r"C:\Users\carol\AppData\Local",
        r'"C:\\Users\\dave\\repo"',
    ],
)
def test_real_home_paths_are_found(line: str) -> None:
    hits = home_path_hits(line)
    assert len(hits) == 1
    assert hits[0][0] == 1


@pytest.mark.parametrize(
    "line",
    [
        "cd /Users/you/src/contractum",
        "ls /home/<user>/projects",
        "ls /home/$USER/projects",
        "ls /home/${USER}/projects",
        r"C:\Users\%USERNAME%\AppData",
        "~/src/contractum",
        "$HOME/.config",
        "/home/runner/work/contractum",
        "/home/node/app",
        "/home/pwuser",
        "https://example.com/Users/list",  # not a home directory
    ],
)
def test_placeholders_and_non_home_paths_pass(line: str) -> None:
    assert home_path_hits(line) == []


@pytest.mark.parametrize(
    ("email", "public"),
    [
        ("12345+someone@users.noreply.github.com", True),
        ("noreply@github.com", True),
        ("49699333+dependabot[bot]@users.noreply.github.com", True),
        ("someone@example.org", False),
    ],
)
def test_public_email_classification(email: str, public: bool) -> None:
    assert is_public_email(email) is public


def test_email_hits_report_the_line_and_never_the_address() -> None:
    text = "first line\ncontact: Someone@Example.org\n"
    assert email_hits(text, {"someone@example.org"}) == [(2, "an author email")]
    assert email_hits(text, {"other@example.org"}) == []
