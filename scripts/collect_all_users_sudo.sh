#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/collect_all_users_sudo.sh [options] [-- collect_llm_traces.py args...]

Collect all readable users under /home with sudo, while keeping final outputs
owned by the launching user.

Options:
  -o, --output PATH       Output JSONL path.
                          Default: trace/llm_round_trace.all_users.jsonl
  --home-root PATH        Home root to scan. Default: /home
  --sanitize              Also write PATH with .public.jsonl suffix.
  --quiet-progress        Suppress collector progress messages.
  --no-summary            Do not print overview_summary output.
  --no-sudo               Run without sudo.
  -h, --help              Show this help.

Environment:
  PYTHON                  Python interpreter to run. Defaults to .venv/bin/python
                          when available, then python3.
EOF
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"

output="$repo_root/trace/llm_round_trace.all_users.jsonl"
home_root="/home"
sanitize=0
summary=1
use_sudo=1
quiet_progress=0
extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output)
      output="$2"
      shift 2
      ;;
    --home-root)
      home_root="$2"
      shift 2
      ;;
    --sanitize)
      sanitize=1
      shift
      ;;
    --quiet-progress)
      quiet_progress=1
      shift
      ;;
    --no-summary)
      summary=0
      shift
      ;;
    --no-sudo)
      use_sudo=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      extra_args+=("$@")
      break
      ;;
    *)
      extra_args+=("$1")
      shift
      ;;
  esac
done

case "$output" in
  /*) ;;
  *) output="$repo_root/$output" ;;
esac

python_bin="${PYTHON:-}"
if [[ -z "$python_bin" ]]; then
  if [[ -x "$repo_root/.venv/bin/python" ]]; then
    python_bin="$repo_root/.venv/bin/python"
  else
    python_bin="$(command -v python3)"
  fi
fi

mkdir -p -- "$(dirname -- "$output")"
tmp_output="$(mktemp "${TMPDIR:-/tmp}/coding-trace-all-users.XXXXXX.jsonl")"
tmp_report="$(mktemp "${TMPDIR:-/tmp}/coding-trace-all-users-report.XXXXXX.json")"

cleanup() {
  [[ -e "${tmp_output:-}" ]] && rm -f -- "$tmp_output"
  [[ -e "${tmp_report:-}" ]] && rm -f -- "$tmp_report"
}
trap cleanup EXIT

collect_cmd=(
  "$python_bin"
  "$script_dir/collect_llm_traces.py"
  --all-user
  --home-root "$home_root"
  --extract-rounds "$tmp_output"
  --fresh-extract
  --json
)

if [[ "$quiet_progress" -eq 1 ]]; then
  collect_cmd+=(--quiet-host-progress)
fi

collect_cmd+=("${extra_args[@]}")

if [[ "$use_sudo" -eq 1 && "$(id -u)" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required for all-user collection; rerun with --no-sudo to skip it." >&2
    exit 1
  fi
  sudo env PYTHONDONTWRITEBYTECODE=1 "${collect_cmd[@]}" > "$tmp_report"
  sudo chown "$(id -u):$(id -g)" "$tmp_output" "$tmp_report"
else
  PYTHONDONTWRITEBYTECODE=1 "${collect_cmd[@]}" > "$tmp_report"
fi

report_output="${output%.jsonl}.collection_report.json"
mv -- "$tmp_output" "$output"
mv -- "$tmp_report" "$report_output"
trap - EXIT

owner_uid="${SUDO_UID:-$(id -u)}"
owner_gid="${SUDO_GID:-$(id -g)}"
if [[ "$(id -u)" -eq 0 ]]; then
  chown "$owner_uid:$owner_gid" "$output" "$report_output"
fi

echo "wrote trace: $output"
echo "wrote collection report: $report_output"

if [[ "$sanitize" -eq 1 ]]; then
  public_output="${output%.jsonl}.public.jsonl"
  "$python_bin" "$script_dir/sanitize_round_trace.py" "$output" -o "$public_output"
  echo "wrote sanitized trace: $public_output"
fi

if [[ "$summary" -eq 1 ]]; then
  "$python_bin" "$repo_root/artifacts/trace_facts/overview_summary/analyze.py" -i "$output"
fi
