---
name: tracelab-public-release
description: Publish TraceLab public snapshots from the private/internal repo to the public uw-syfi/TraceLab GitHub repo through draft pull requests. Use when adding or checking the public remote, exporting a public tree without internal history, drafting a public snapshot PR, or creating GitHub releases with syfi_coding_trace.jsonl.gz and syfi_coding_trace.duckdb assets after the PR is merged.
---

# TraceLab Public Release

## Purpose

Use this skill to draft curated TraceLab public snapshot pull requests from the internal repository to
the public repository:

```text
https://github.com/uw-syfi/TraceLab.git
```

The public repo must receive release snapshots, not the internal commit history. Do not push
snapshots directly to `main`; open a draft PR for review.

## Guardrails

1. Treat `origin` as the internal/source-of-truth remote unless the user says otherwise.
2. Add/use a separate `public` remote for `https://github.com/uw-syfi/TraceLab.git`.
3. Do not normal-merge internal branches into public. Draft a PR containing a public export snapshot.
4. Do not commit trace data to Git. Release data files belong only on GitHub Releases.
5. Keep ignored/local files such as `trace/*.jsonl*`, `trace/*.duckdb`, `trace.tar.gz`, generated artifact outputs, and server runtime data out of the public Git tree.
6. Preserve the web product title `SyFI Trace Atlas` unless the user explicitly asks to rename it.
7. Do not push directly to the public `main` branch. Push only a release branch and create a draft PR.

## Standard Release Assets

Upload both assets when creating a public data release:

```text
trace/syfi_coding_trace.jsonl.gz
trace/syfi_coding_trace.duckdb
```

Before upload, verify:

```bash
gzip -t trace/syfi_coding_trace.jsonl.gz
sha256sum trace/syfi_coding_trace.jsonl.gz trace/syfi_coding_trace.duckdb
```

## Public PR Export Workflow

1. Inspect the worktree and remotes:

```bash
git status --short --branch
git remote -v
```

2. Add the public remote if missing:

```bash
git remote add public https://github.com/uw-syfi/TraceLab.git
```

3. Build an export directory instead of switching the dirty internal worktree:

```bash
EXPORT=/tmp/tracelab-public-export
rm -rf "$EXPORT"
mkdir -p "$EXPORT"
git archive HEAD | tar -x -C "$EXPORT"
```

4. Overlay intentional uncommitted source/doc changes. Prefer explicit paths from
`git status --short`; do not copy ignored trace data or generated outputs.

5. Stage the export in a clean clone of the public repo on a temporary review branch:

```bash
PUBLIC_WORK=/tmp/tracelab-public-pr
REVIEW_BRANCH=review-public-export
rm -rf "$PUBLIC_WORK"
git clone https://github.com/uw-syfi/TraceLab.git "$PUBLIC_WORK"
cd "$PUBLIC_WORK"
git switch -c "$REVIEW_BRANCH" origin/main
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a "$EXPORT"/. "$PUBLIC_WORK"/
```

6. Review the entire public diff before choosing the branch name, commit message, PR title, or
PR body. This is mandatory. Do not summarize only the agent's own recent edits.

Use at least:

```bash
git status --short
git diff --name-status origin/main...HEAD
git diff --stat origin/main...HEAD
```

Then inspect changes by area. Read enough changed files to understand the actual release scope:

- top-level docs and config (`README.md`, `artifacts/README.md`, `config/services.json`, `.gitignore`)
- new/changed artifact READMEs and scripts, grouped by category
- renamed or moved tools, especially `replay/` and web/AI infrastructure
- web UI changes under `web/app/src` and helper scripts under `web/tools`
- deleted files, renames, and generated-file removals

Run public-safety checks before pushing:

```bash
find trace -maxdepth 2 -type f 2>/dev/null | sort
find . -maxdepth 4 \( -name '*.duckdb' -o -name '*.jsonl.gz' -o -name '*.jsonl' \)
rg -n '/m-coriander|coding_trace_refactor|serendipity-zk|coding-trace-collect|API_KEY=' . -g '!web/app/dist/**'
git diff --check
```

Expected benign matches such as documentation mentioning environment variable names are okay, but
internal absolute paths, private remotes, secrets, trace data, or generated runtime data must be
removed or explicitly justified before continuing. If the diff is large, still read representative
files from every changed category and note unreviewed risk in the PR body.

7. Choose a descriptive branch/topic after reviewing all changes. Base it on the actual public diff,
not on the fact that this is a snapshot. Do not put dates in the PR title or branch name. Prefer
names like:

```text
refresh-artifacts-replay-ui
refresh-web-analytics-docs
release-trace-assets
```

Avoid generic names such as `public-snapshot` unless the diff is truly only a mechanical snapshot
with no coherent product or artifact theme.

Rename the temporary review branch to the descriptive topic before committing:

```bash
BRANCH=refresh-meaningful-topic
git branch -m "$BRANCH"
git status --short
git add .
git commit -m "Refresh artifact analyses, replay tooling, and detail UI"
git push -u origin "$BRANCH"
```

8. Open a draft PR instead of pushing to `main`. The PR title and body must name the substantive
changes. Include a short overview, concrete bullets grouped by area, and public-release safety
checks. Do not use boilerplate-only titles such as "TraceLab public snapshot".

```bash
gh pr create \
  --repo uw-syfi/TraceLab \
  --base main \
  --head "$BRANCH" \
  --draft \
  --title "Refresh artifact analyses, replay tooling, and detail UI" \
  --body-file /tmp/tracelab-public-pr-body.md
```

Suggested PR body structure:

```markdown
## What this changes
One paragraph describing the actual release theme.

## Main updates
- Artifact analyses: ...
- Replay / AI tooling: ...
- Web UI: ...
- Docs / release workflow: ...

## Public-release safety
- No trace release data files are committed to Git.
- `trace/` contains only expected public documentation.
- Internal absolute paths, private remotes, and secrets were scanned.
- Release assets will be uploaded only after review and merge.
```

9. Create a release only after the PR is reviewed and merged:

```bash
gh release create vYYYY-MM-DD-syfi-trace \
  ../coding_trace_refactor/trace/syfi_coding_trace.jsonl.gz \
  ../coding_trace_refactor/trace/syfi_coding_trace.duckdb \
  --repo uw-syfi/TraceLab \
  --target main \
  --title "TraceLab public trace snapshot YYYY-MM-DD" \
  --notes "Public sanitized trace release. Assets include the compressed JSONL rows and a DuckDB database."
```

If the release exists, use `gh release upload --clobber`.

## Verification

After drafting the PR:

```bash
gh pr view --repo uw-syfi/TraceLab --web
git ls-remote public refs/heads/main refs/heads/refresh-meaningful-topic
```

After the PR is merged and a release is created:

```bash
gh release view vYYYY-MM-DD-syfi-trace --repo uw-syfi/TraceLab
```

Check that release download URLs in `README.md` point to `uw-syfi/TraceLab`, and that
the public commit has no internal remote URLs except intentional historical references.
