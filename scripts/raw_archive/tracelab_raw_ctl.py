#!/usr/bin/env python3
"""Control the raw snapshot service on every host from one machine (no sudo needed).

  tracelab_raw_ctl.py status  [hosts...]   service state, next run, latest archive, log tail
  tracelab_raw_ctl.py trigger [--full] [hosts...]   start a run now (writes <dir>/request)
  tracelab_raw_ctl.py pull    [hosts...]   rsync finished runs into the central store
  tracelab_raw_ctl.py cron                 print the crontab line for a twice-monthly pull

Hosts and their archive directories come from hosts.json next to this script (--hosts-file).
Start from the example and edit it for your site (hosts.json is git-ignored):

  cp hosts.example.json hosts.json

Host keys must be the short host names (the service writes to <output>/<short hostname>).
Each host has an "ssh" target (an ssh alias or user@host) and the "output" directory it was
installed with (install.sh --output). A host with "ssh": null is this machine. Pulled
copies land in <central>/<host>/; the pull never deletes anything, so archives survive
removal on the source host.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
UNIT = "tracelab-raw-snapshot"

# Runs on the target host (python3 >= 3.9, stdlib only). argv: host_dir
STATUS_SNIPPET = r"""
import json, subprocess, sys
from pathlib import Path
d = Path(sys.argv[1])
def sh(*cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip()
out = {"units": {u: sh("systemctl", "is-active", f"tracelab-raw-snapshot.{u}") or "?"
                 for u in ("timer", "path", "service")},
       "next": sh("systemctl", "show", "-p", "NextElapseUSecRealtime", "--value", "tracelab-raw-snapshot.timer"),
       "request_pending": (d / "request").exists()}
runs = sorted(p for p in d.glob("*-*") if p.is_dir() and not p.name.endswith(".partial"))
out["runs"] = len(runs)
out["partial"] = sorted(p.name for p in d.glob("*.partial"))
if runs:
    try:
        m = json.loads((runs[-1] / "manifest.json").read_text())
        out["latest"] = {"run": m["run"], "finished_at": m["finished_at"],
                         "stores": len(m["stores"]), "errors": len(m["errors"]),
                         "archives": {u: [a["files"], a["compressed_bytes"]] for u, a in m["archives"].items()}}
    except (OSError, ValueError, KeyError) as exc:
        out["latest"] = {"run": runs[-1].name, "manifest_error": str(exc)}
try:
    out["log_tail"] = (d / "snapshot.log").read_text(errors="replace").splitlines()[-3:]
except OSError as exc:
    out["log_tail"] = [f"<{exc}>"]
print(json.dumps(out))
"""


def load_hosts(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"{path} not found; copy {HERE / 'hosts.example.json'} to it and edit the hosts")
    return json.loads(path.read_text())


def host_dir(cfg: dict, host: str) -> str:
    return f"{cfg['hosts'][host]['output'].rstrip('/')}/{host}"


def run_on(cfg: dict, host: str, argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    target = cfg["hosts"][host].get("ssh")
    if target:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target,
                " ".join(shlex.quote(a) for a in argv)]
    return subprocess.run(argv, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def selected(cfg: dict, names: list[str]) -> list[str]:
    unknown = [n for n in names if n not in cfg["hosts"]]
    if unknown:
        sys.exit(f"unknown host(s): {', '.join(unknown)}; known: {', '.join(cfg['hosts'])}")
    return names or list(cfg["hosts"])


def cmd_status(cfg: dict, hosts: list[str]) -> int:
    rc = 0
    for host in hosts:
        p = run_on(cfg, host, ["python3", "-", host_dir(cfg, host)], stdin=STATUS_SNIPPET)
        print(f"== {host}  ({host_dir(cfg, host)})")
        if p.returncode != 0:
            print(f"   unreachable or failed: {p.stderr.strip()[-300:]}")
            rc = 1
            continue
        s = json.loads(p.stdout)
        units = " ".join(f"{u}={v}" for u, v in s["units"].items())
        print(f"   units: {units}   next: {s['next'] or '-'}"
              + ("   REQUEST PENDING" if s["request_pending"] else ""))
        if s["units"]["timer"] != "active" or s["units"]["path"] != "active":
            rc = 1
        latest = s.get("latest")
        if latest:
            sizes = ", ".join(f"{u} {n} files {b / 2**20:,.1f} MiB" for u, (n, b) in latest.get("archives", {}).items())
            print(f"   runs: {s['runs']}   latest: {latest['run']}  stores={latest.get('stores')} "
                  f"errors={latest.get('errors')}  {sizes or latest.get('manifest_error', '')}")
        else:
            print(f"   runs: {s['runs']}")
        if s["partial"]:
            print(f"   unfinished: {', '.join(s['partial'])}")
        for line in s["log_tail"]:
            print(f"   | {line}")
    return rc


def cmd_trigger(cfg: dict, hosts: list[str], full: bool) -> int:
    rc = 0
    for host in hosts:
        req = f"{host_dir(cfg, host)}/request"
        p = run_on(cfg, host, ["sh", "-c", f"echo {'full' if full else 'incr'} > \"$1\"", "sh", req])
        status = "ok" if p.returncode == 0 else f"FAILED: {p.stderr.strip()}"
        print(f"{host}: {'full' if full else 'incremental'} run requested ({req}) {status}")
        rc |= p.returncode != 0
    return rc


def cmd_pull(cfg: dict, hosts: list[str]) -> int:
    central = Path(cfg["central"])
    rc = 0
    for host in hosts:
        h = cfg["hosts"][host]
        if not h.get("ssh") and Path(host_dir(cfg, host)).resolve() == (central / host).resolve():
            print(f"{host}: archives already in {central / host}")
            continue
        dest = central / host
        dest.mkdir(parents=True, exist_ok=True, mode=0o750)
        src = f"{h['ssh']}:{host_dir(cfg, host)}/" if h.get("ssh") else f"{host_dir(cfg, host)}/"
        # Finished runs only; never delete on the receiving side.
        argv = ["rsync", "-a", "--chmod=D750,F640", "--exclude=*.partial/", "--exclude=*.tmp",
                "--exclude=request", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10", src, f"{dest}/"]
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        print(f"{host}: {'ok' if p.returncode == 0 else 'FAILED: ' + p.stderr.strip()[-300:]}")
        rc |= p.returncode != 0
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hosts-file", type=Path, default=HERE / "hosts.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "pull"):
        sub.add_parser(name).add_argument("hosts", nargs="*")
    t = sub.add_parser("trigger")
    t.add_argument("--full", action="store_true", help="Archive everything, not only changes")
    t.add_argument("hosts", nargs="*")
    sub.add_parser("cron")
    args = ap.parse_args()
    cfg = load_hosts(args.hosts_file)
    if args.cmd == "cron":
        log = Path(cfg["central"]) / "pull.log"
        print(f"30 6 2,16 * * {sys.executable} {Path(__file__).resolve()} pull >> {log} 2>&1")
        return 0
    hosts = selected(cfg, args.hosts)
    if args.cmd == "status":
        return cmd_status(cfg, hosts)
    if args.cmd == "trigger":
        return cmd_trigger(cfg, hosts, args.full)
    return cmd_pull(cfg, hosts)


if __name__ == "__main__":
    sys.exit(main())
