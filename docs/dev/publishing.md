# Publishing a public copy

This repository is private. It holds planning notes, tool state and other content that must not
go public. `scripts/publish/export-public.sh` builds a clean public copy of the code as one fresh
commit with no history. You can then push that copy to a new public repository by hand.

The script only runs when you start it. No workflow runs it. It never pushes and never adds a
remote.

## What you need

- A committed `HEAD`. The export takes the last commit; uncommitted changes are left out, and the
  script warns when there are any.
- `git config user.name` and `git config user.email` set in this repository. They become the author
  of the export commit. The script does not write them into any file.
- [uv](https://docs.astral.sh/uv/). The script uses it to run the leak check with Python 3.13.
- Network access the first time, to download gitleaks (see [The leak check](#the-leak-check)).

## Run a dry run

Run it from the repository root. Without `--out` the script exports into a new temporary folder:

```bash
bash scripts/publish/export-public.sh
```

Or pick an empty folder outside the repository:

```bash
bash scripts/publish/export-public.sh --out "$(mktemp -d)/contractum-public"
```

The script refuses an `--out` folder that is not empty or that lies inside the repository. A dry
run is the same as a real run: nothing leaves your machine until you push it yourself. When the
export folder is not needed any more, delete it.

## What the export does

1. Takes a fresh `git clone --no-local` of this repository. Only tracked files reach the copy, so
   untracked local files cannot leak.
2. Extracts the last commit into the export folder.
3. Removes the folders and files that never ship:
   - the planning folder (`.planning/`) and the GSD tool state (`.gsd/`);
   - the development secrets folder (`.dev/`);
   - scanner, coverage and site output (`.security-reports/`, `.coverage-data/`, `htmlcov/`,
     `site/`, `coverage.xml`, `.coverage`, `ui/coverage/`);
   - under every `.claude` folder: the agent worktrees, the agent project folders, any memory
     folder or memory index file, and `settings.local.json`.

   Most of these are untracked, so they are already absent. The script removes them anyway.
4. Strips two blocks from `.claude/CLAUDE.md`: the GSD workflow block and the developer profile
   block. Each block runs from its `<!-- GSD:...-start -->` comment to its
   `<!-- GSD:...-end -->` comment. The rest of the file stays as it is. If a block marker is
   missing, the export stops.
5. Creates a new repository in the export folder and commits everything once, as
   "Initial public release" on `main`.
6. Runs the leak check. If it fails, the script stops and tells you not to publish.

The project skills under `.claude/skills/` and in each module, and the `CLAUDE.md` files, are
shipped. The dated security reports are not in the repository: they live in the private notes.

## The leak check

`scripts/publish/leak_check.py` scans every text file of the export and fails on:

| Marker | What it finds |
|---|---|
| `home-path` | An absolute macOS or Linux home folder path |
| `claude-projects` | The agent project folder under `.claude` |
| `memory-file` | The agent memory index file name |
| `planning-path` | The planning folder name used as a path |
| `owner-email` | The owner's email address outside `LICENSE` and `NOTICE` files |
| `excluded-path` | A folder or file from the list above that is still in the export |

The owner's addresses are the ones in the export's `LICENSE` plus your `git config user.email`.
Each finding prints as `path:line: marker`. The matched text is never printed.

The check also runs gitleaks 8.30.1 with the repository's `.gitleaks.toml`, the same version and
config as the `gitleaks` job in `security.yml`. The script uses a `gitleaks` on your `PATH` if
`gitleaks version` prints exactly that version. Otherwise it downloads the official release once
into `${XDG_CACHE_HOME:-~/.cache}/contractum/gitleaks/8.30.1/` and checks its SHA-256 before use.
`tests/repo/test_publish_leak_check.py` keeps the version and the Linux checksum equal to
`security.yml`.

### Fixing a finding

Fix a finding at the source, commit, and run the export again. For example, reword a comment that
points into the planning folder so it no longer needs the path.

Some files must name the planning folder to work, such as `.gitignore`, `.dockerignore` and the
lint configs. They are listed in `ALLOWLIST` in `scripts/publish/leak_check.py`. To add one, add a
`(path, "planning-path", reason)` entry with a short reason. The allowlist only takes
planning-folder mentions: a home path, a memory marker or an email is always fixed at the source.

`tests/repo/test_publish_leak_check.py` also scans every tracked file outside the planning folder,
so a new finding fails the unit tests before you ever export.

## Publish the copy

When the leak check passes, the script prints the export path, the commit count (always 1) and the
next steps. Do them by hand:

1. Review the export once more (README, licence, docs) as a reader would.
2. Create a new, empty repository. Do not add a README, licence or `.gitignore` there.
3. Add it as a remote of the export and push:

   ```bash
   git -C <export-folder> remote add origin <new-repo-url>
   git -C <export-folder> push -u origin main
   ```

To publish a later version, run the export again. Each export is a fresh single commit, so it does
not share history with the previous one.
