#!/usr/bin/env bash
# Install the TraceLab raw-session snapshot service on this host (run as root).
#
#   sudo ./install.sh --output /data/tracelab-raw --owner alice
#   sudo ./install.sh --uninstall          # archives under --output are kept
#
# Installs
#   /usr/local/lib/tracelab-raw/tracelab_raw_snapshot.py   root-owned copy of the script
#   /etc/tracelab-raw.json                                 config (output, owner, ...)
#   /etc/systemd/system/tracelab-raw-snapshot.service      the snapshot run (root, idle I/O)
#   /etc/systemd/system/tracelab-raw-snapshot.timer        1st and 15th of each month, 03:00
#   /etc/systemd/system/tracelab-raw-snapshot.path         manual trigger, see below
#
# Manual trigger, no sudo needed:  touch <output>/<host>/request    (incremental)
#                                  echo full > <output>/<host>/request
# The service deletes the file when it starts. Progress: <output>/<host>/snapshot.log
#
# --stage DIR writes everything under DIR instead of / and skips systemctl (for testing).
set -euo pipefail

NAME=tracelab-raw-snapshot
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="" OWNER="" STAGE="" UNINSTALL=0 EXTRA_CONFIG="{}"

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "install.sh: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output) OUTPUT="$2"; shift 2 ;;
        --owner) OWNER="$2"; shift 2 ;;
        --config-json) EXTRA_CONFIG="$2"; shift 2 ;;  # extra config keys, e.g. '{"scan_workers": 8}'
        --stage) STAGE="$2"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

LIB="$STAGE/usr/local/lib/tracelab-raw"
CONFIG="$STAGE/etc/tracelab-raw.json"
UNITS="$STAGE/etc/systemd/system"
run_systemctl() { if [[ -z "$STAGE" ]]; then systemctl "$@"; else echo "(stage) systemctl $*"; fi; }

if [[ -z "$STAGE" && $EUID -ne 0 ]]; then
    die "run as root (or use --stage DIR to test)"
fi

if [[ $UNINSTALL -eq 1 ]]; then
    run_systemctl disable --now "$NAME.timer" "$NAME.path" 2>/dev/null || true
    run_systemctl stop "$NAME.service" 2>/dev/null || true
    rm -f "$UNITS/$NAME".{service,timer,path} "$CONFIG"
    rm -rf "$LIB"
    run_systemctl daemon-reload
    echo "uninstalled; archives were left in place"
    exit 0
fi

[[ -n "$OUTPUT" && -n "$OWNER" ]] || die "--output and --owner are required"
[[ "$OUTPUT" == /* ]] || die "--output must be an absolute path"
id "$OWNER" >/dev/null 2>&1 || die "no such user: $OWNER"
for tool in zstd find; do command -v "$tool" >/dev/null || die "$tool not found"; done

# The service runs as root, so its interpreter must not be user-writable (e.g. a conda or
# ~/.local python): anyone who can change it could run code as root.
PYTHON="$(readlink -f "$(command -v python3)")"
[[ -x "$PYTHON" ]] || die "python3 not found"
[[ "$(stat -c %u "$PYTHON")" == 0 ]] || die "$PYTHON is not owned by root"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "$PYTHON is older than 3.9"

# Same host name as the script uses for <output>/<host>.
HOST="$("$PYTHON" -c 'import socket; print(socket.gethostname().split(".")[0])')"
HOST_DIR="$OUTPUT/$HOST"
if [[ -z "$STAGE" ]]; then
    case "$(stat -f -c %T "$(dirname "$OUTPUT")" 2>/dev/null || echo missing)" in
        nfs*|cifs|smb*|fuse*) die "$OUTPUT is on a network filesystem; use local disk" ;;
    esac
fi

install -d -m 755 "$LIB" "$UNITS" "$(dirname "$CONFIG")"
install -m 644 "$HERE/tracelab_raw_snapshot.py" "$LIB/tracelab_raw_snapshot.py"

"$PYTHON" - "$CONFIG" "$OUTPUT" "$OWNER" "$EXTRA_CONFIG" <<'EOF'
import json, os, sys
path, output, owner, extra = sys.argv[1:]
cfg = {"output": output, "owner": owner, **json.loads(extra)}
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(cfg, fh, indent=1, sort_keys=True)
    fh.write("\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
EOF

# <output> and <output>/<host> exist up front so the path unit can watch for `request`,
# and snapshot.log belongs to the owner so they can read it without sudo.
install -d -m 750 -o "$OWNER" -g "$(id -gn "$OWNER")" "$STAGE$OUTPUT" "$STAGE$HOST_DIR"
touch "$STAGE$HOST_DIR/snapshot.log"
chown "$OWNER:$(id -gn "$OWNER")" "$STAGE$HOST_DIR/snapshot.log"
chmod 640 "$STAGE$HOST_DIR/snapshot.log"

cat > "$UNITS/$NAME.service" <<EOF
[Unit]
Description=TraceLab raw session snapshot (Claude Code / Codex transcripts)
Documentation=file:/usr/local/lib/tracelab-raw/tracelab_raw_snapshot.py
After=local-fs.target

[Service]
Type=oneshot
# The script appends to snapshot.log itself: StandardOutput=append: is opened by systemd
# (init_t), which SELinux denies on unlabeled filesystems such as ZFS. Output also goes
# to the journal.
ExecStart=$PYTHON /usr/local/lib/tracelab-raw/tracelab_raw_snapshot.py --config /etc/tracelab-raw.json --request-file $HOST_DIR/request --log-file $HOST_DIR/snapshot.log
TimeoutStartSec=12h
Nice=19
IOSchedulingClass=idle
CPUSchedulingPolicy=batch
# Read everything, write only the archive directory.
ProtectSystem=strict
ReadWritePaths=$OUTPUT
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE CAP_CHOWN CAP_FOWNER
EOF

cat > "$UNITS/$NAME.timer" <<EOF
[Unit]
Description=TraceLab raw session snapshot, twice a month

[Timer]
OnCalendar=*-*-01,15 03:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > "$UNITS/$NAME.path" <<EOF
[Unit]
Description=TraceLab raw session snapshot, manual trigger ($HOST_DIR/request)

[Path]
PathExists=$HOST_DIR/request
Unit=$NAME.service

[Install]
WantedBy=paths.target
EOF

chmod 644 "$UNITS/$NAME".{service,timer,path}
run_systemctl daemon-reload
run_systemctl enable --now "$NAME.timer" "$NAME.path"

cat <<EOF
installed on $HOST
  archives:  $HOST_DIR/
  log:       $HOST_DIR/snapshot.log
  next run:  systemctl list-timers $NAME.timer
  run now:   touch $HOST_DIR/request          (or: echo full > $HOST_DIR/request)
EOF
