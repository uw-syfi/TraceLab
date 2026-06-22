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

5. Stage the export in a clean clone of the public repo on a snapshot branch:

```bash
PUBLIC_WORK=/tmp/tracelab-public-pr
BRANCH=public-snapshot-YYYY-MM-DD
rm -rf "$PUBLIC_WORK"
git clone https://github.com/uw-syfi/TraceLab.git "$PUBLIC_WORK"
cd "$PUBLIC_WORK"
git switch -c "$BRANCH" origin/main
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a "$EXPORT"/. "$PUBLIC_WORK"/
git status --short
git add .
git commit -m "TraceLab public snapshot YYYY-MM-DD"
git push -u origin "$BRANCH"
```

6. Open a draft PR instead of pushing to `main`:

```bash
gh pr create \
  --repo uw-syfi/TraceLab \
  --base main \
  --head "$BRANCH" \
  --draft \
  --title "TraceLab public snapshot YYYY-MM-DD" \
  --body "Draft public snapshot PR. This branch contains the curated public tree only; release data files will be uploaded as GitHub Release assets after merge."
```

7. Create a release only after the PR is reviewed and merged:

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
git ls-remote public refs/heads/main refs/heads/public-snapshot-YYYY-MM-DD
```

After the PR is merged and a release is created:

```bash
gh release view vYYYY-MM-DD-syfi-trace --repo uw-syfi/TraceLab
```

Check that release download URLs in `README.md` point to `uw-syfi/TraceLab`, and that
the public commit has no internal remote URLs except intentional historical references.
