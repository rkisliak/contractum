# Release docs pass

Every release includes a docs pass over the components the release changed. Do it on the
release-preparation branch, before the release workflow runs, so the tagged commit ships true
docs. A release candidate counts: its docs must be true for the candidate too.

## 1. Find what changed

List the commits and files since the last stable tag:

```bash
last=$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' --exclude '*-rc*')
git log --oneline "$last"..HEAD
git diff --name-only "$last"..HEAD | sort
```

Map each changed area to its pages with the table in section 3 of `SKILL.md`.

## 2. Release state and versions

Check every page that tells readers how to install or upgrade:
`README.md`, `clients/python/README.md`, `docs/user/sdk-cli.md`, `docs/user/getting-started.md`,
`docs/admin/deploy-kubernetes.md`, `docs/admin/upgrade-backup.md`,
`deploy/helm/contractum/README.md`.

- [ ] No page says there is no release, or tells readers to wait for one.
- [ ] Install commands point at a real release asset or a `vX.Y.Z` placeholder that the page
      explains. A version in an example is one that exists.
- [ ] Upgrade notes cover every breaking change in this release (a changed setting, a removed
      option, a migration step).
- [ ] No "upcoming", "planned", "coming", "in a future release" wording about anything this
      release shipped. Delete it, or state the shipped behaviour.
- [ ] Anything removed in this release is gone from every page, table and example.

## 3. Sweep the changed components

For each page from step 1:

1. Run the stale-statement sweep (section 6 of `SKILL.md`) on it.
2. Read each section that describes a changed behaviour and check it against the code.
3. Edit only the sections that are wrong. Do not restyle the rest of the page.

Then run `/gsd-docs-update --verify-only` and fix every inaccuracy it reports in those pages.

## 4. Gate and checks

- [ ] `python3 scripts/docs_gate.py --all` passes (stale claims, internal ids, Vale errors
      on every doc file).
- [ ] The checks in section 13 of `SKILL.md` pass.
- [ ] The release-pass commit carries no `Docs-Impact: none` trailer: it is a docs change.

## 5. After the tag

The stale-claim check starts failing the moment a `v*` tag exists and a page still says there is
no release. If it fails on `main` after a release, fix the page in the next commit; do not
remove the phrase from the list.
