/* Standalone Pyodide QA worker for web/ai_infra/tester.html.
 *
 * This mirrors the app worker's QA path without requiring Vite bundling:
 * - load Pyodide
 * - mount /py payload files
 * - materialize an uploaded JSONL trace to /work/trace.duckdb
 * - execute run_python(code) tool calls locally
 */

const PYODIDE_VERSION = 'v0.27.2';
const PYODIDE_BASE = `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`;
const MOUNT = '/repo';
const WORK_DIR = '/work';
const TRACE_PATH = `${WORK_DIR}/input.jsonl`;
const TRACE_DB_PATH = `${WORK_DIR}/trace.duckdb`;
const QA_OUT = '/out';

let pyodidePromise = null;

function post(msg, transfer) {
  if (transfer) self.postMessage(msg, transfer);
  else self.postMessage(msg);
}

function mkdirp(FS, dir) {
  let cur = '';
  for (const part of dir.split('/')) {
    if (!part) continue;
    cur += `/${part}`;
    try {
      FS.mkdir(cur);
    } catch {
      /* already exists */
    }
  }
}

async function mountPayload(pyodide, manifest) {
  const FS = pyodide.FS;
  mkdirp(FS, MOUNT);
  for (const rel of manifest.files) {
    const res = await fetch(`/py/${rel}`);
    if (!res.ok) throw new Error(`payload fetch failed (${res.status}): ${rel}`);
    const bytes = new Uint8Array(await res.arrayBuffer());
    const dest = `${MOUNT}/${rel}`;
    mkdirp(FS, dest.slice(0, dest.lastIndexOf('/')));
    FS.writeFile(dest, bytes);
  }
}

async function boot() {
  if (pyodidePromise) return pyodidePromise;
  pyodidePromise = (async () => {
    post({ type: 'boot-progress', message: 'Loading Python runtime...' });
    importScripts(`${PYODIDE_BASE}pyodide.js`);
    const pyodide = await loadPyodide({ indexURL: PYODIDE_BASE });

    const manifest = await (await fetch('/py/manifest.json')).json();
    post({ type: 'boot-progress', message: `Loading packages: ${manifest.packages.join(', ')}` });
    await pyodide.loadPackage(manifest.packages);

    post({ type: 'boot-progress', message: 'Mounting analysis toolkit...' });
    await mountPayload(pyodide, manifest);

    pyodide.runPython(`
import os, sys
os.environ['MPLBACKEND'] = 'Agg'
os.environ.setdefault('MPLCONFIGDIR', '/tmp/mpl')
os.makedirs('/tmp/mpl', exist_ok=True)
if ${JSON.stringify(MOUNT)} not in sys.path:
    sys.path.insert(0, ${JSON.stringify(MOUNT)})
`);
    post({ type: 'booted' });
    return pyodide;
  })();
  return pyodidePromise;
}

async function gunzip(data) {
  const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream('gzip'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function loadTrace(requestId, bytes, gzip, filename) {
  const pyodide = await boot();
  const FS = pyodide.FS;
  let data = new Uint8Array(bytes);
  if (gzip) data = await gunzip(data);
  mkdirp(FS, WORK_DIR);
  FS.writeFile(TRACE_PATH, data);

  pyodide.globals.set('__qa_mount', MOUNT);
  pyodide.globals.set('__qa_trace_path', TRACE_PATH);
  pyodide.globals.set('__qa_db_path', TRACE_DB_PATH);
  try {
    await pyodide.runPythonAsync(`
import os, sys
from pathlib import Path

utils = str(Path(__qa_mount) / "artifacts" / "utils")
if utils not in sys.path:
    sys.path.insert(0, utils)

import trace_db

db_path = Path(__qa_db_path)
if db_path.exists():
    db_path.unlink()
trace_db.materialize(__qa_trace_path, db_path)
`);
  } finally {
    try {
      pyodide.globals.delete('__qa_mount');
      pyodide.globals.delete('__qa_trace_path');
      pyodide.globals.delete('__qa_db_path');
    } catch {
      /* ignore */
    }
  }

  post({ type: 'trace-ready', requestId, tracePath: TRACE_PATH, dbPath: TRACE_DB_PATH, filename });
}

async function runPython(requestId, code) {
  const pyodide = await boot();
  pyodide.globals.set('__qa_code', code);
  pyodide.globals.set('__qa_out', QA_OUT);
  const raw = await pyodide.runPythonAsync(`
import base64
import contextlib
import io
import json
import mimetypes
import os
from pathlib import Path
import shutil
import traceback

out_dir = Path(__qa_out)
try:
    shutil.rmtree(out_dir)
except FileNotFoundError:
    pass
out_dir.mkdir(parents=True, exist_ok=True)

stdout = io.StringIO()
stderr = io.StringIO()
error = None
namespace = {"__name__": "__main__"}

try:
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exec(__qa_code, namespace, namespace)
except Exception as exc:
    error = {
        "name": type(exc).__name__,
        "value": str(exc),
        "traceback": traceback.format_exc(),
    }

def data_url(mime, raw):
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")

artifacts = []
for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
    size = path.stat().st_size
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    item = {
        "path": str(path),
        "size": size,
        "type": "file",
        "mime": mime,
        "is_image": mime.startswith("image/"),
        "display": False,
    }
    try:
        if mime.startswith("image/") and size <= 2_000_000:
            item["data_url"] = data_url(mime, path.read_bytes())
            item["display"] = True
            item["source"] = "artifact"
        elif (mime.startswith("text/") or mime in {"application/json", "text/csv"}) and size <= 2_000_000:
            item["text_preview"] = path.read_text(errors="replace")[:10000]
    except Exception as exc:
        item["inline_error"] = type(exc).__name__ + ": " + str(exc)
    artifacts.append(item)

display_images = [
    {
        "path": item.get("path"),
        "mime": item.get("mime"),
        "size": item.get("size"),
        "data_url": item.get("data_url"),
        "source": item.get("source", "artifact"),
        "display": True,
    }
    for item in artifacts
    if item.get("display") and item.get("data_url")
]

json.dumps({
    "stdout": stdout.getvalue().splitlines(True),
    "stderr": stderr.getvalue().splitlines(True),
    "error": error,
    "results": [],
    "artifacts": artifacts,
    "display_images": display_images,
})
`);
  try {
    pyodide.globals.delete('__qa_code');
    pyodide.globals.delete('__qa_out');
  } catch {
    /* ignore */
  }
  post({ type: 'tool-result', requestId, result: JSON.parse(raw) });
}

self.onmessage = async (event) => {
  const msg = event.data;
  try {
    if (msg.type === 'boot') {
      await boot();
    } else if (msg.type === 'load-trace') {
      await loadTrace(msg.requestId, msg.bytes, msg.gzip, msg.filename);
    } else if (msg.type === 'run-python') {
      await runPython(msg.requestId, msg.code);
    }
  } catch (err) {
    post({
      type: 'error',
      requestId: msg.requestId || '',
      message: err instanceof Error ? err.message : String(err),
    });
  }
};
