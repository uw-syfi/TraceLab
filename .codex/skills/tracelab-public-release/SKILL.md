---
name: tracelab-public-release
description: Publish TraceLab public snapshots from the private/internal repo to the public uw-syfi/TraceLab GitHub repo. Use when adding or checking the public remote, exporting a single-commit public tree without internal history, pushing to public, or creating GitHub releases with syfi_coding_trace.jsonl.gz and syfi_coding_trace.duckdb assets.
---

# TraceLab Public Release

## Purpose

Use this skill to publish curated TraceLab releases from the internal working repository to
the public repository:

```text
https://github.com/uw-syfi/TraceLab.git
```

The public repo must receive release snapshots, not the internal commit history.

## Guardrails

1. Treat `origin` as the internal/source-of-truth remote unless the user says otherwise.
2. Add/use a separate `public` remote for `https://github.com/uw-syfi/TraceLab.git`.
3. Do not normal-merge internal branches into public. Publish a single-commit export snapshot.
4. Do not commit trace data to Git. Release data files belong only on GitHub Releases.
5. Keep ignored/local files such as `trace/*.jsonl*`, `trace/*.duckdb`, `trace.tar.gz`, generated artifact outputs, and server runtime data out of the public Git tree.
6. Preserve the web product title `SyFI Trace Atlas` unless the user explicitly asks to rename it.

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

## Public Export Workflow

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

5. Initialize the public export as a fresh Git repo:

```bash
cd "$EXPORT"
git init -b main
git add .
git commit -m "TraceLab public snapshot"
git remote add origin https://github.com/uw-syfi/TraceLab.git
git push -u origin main
```

6. Create a release after the public commit is pushed:

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

After publishing:

```bash
git ls-remote public HEAD refs/heads/main
gh release view vYYYY-MM-DD-syfi-trace --repo uw-syfi/TraceLab
```

Check that release download URLs in `README.md` point to `uw-syfi/TraceLab`, and that
the public commit has no internal remote URLs except intentional historical references.
