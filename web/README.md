# Web Dev Stack

The web app is three local dev services plus one remote inference service.

## Ports

- `60990` — Astro frontend dev server
- `60980` — Ask-the-trace AI backend (`/api/chat/ws`)
- `60981` — contributor backend (`/api/pool`, `/api/contribute*`)
- `60995` — remote vLLM (host set via `SYFI_VLLM_SSH_HOST` / `SYFI_LLM_BASE_URL`)

Ports and the default LLM backend live in [`../config/services.json`](../config/services.json).

## Start Everything

From this directory:

```bash
cd web
just start-all
```

Then open:

```text
http://127.0.0.1:60990
```

If you open the dev server from another machine, use the server's visible host for Vite HMR:

```bash
VITE_HMR_HOST=<your-host-ip> just dev
# or: VITE_HMR_HOST=<your-host-ip> just dev-all
```

Use the same host you put in the browser URL. This keeps the module reload socket on the same
reachable address as the page.

`just start-all` first ensures the remote vLLM server is running, then starts the AI
backend, contributor backend, and frontend in one terminal. It sources `~/.bashrc`, so the existing
`SYFI_LLM_API_KEY`, `SYFI_LLM_BASE_URL`, `SYFI_LLM_MODEL`, and `E2B_KEY` exports are picked up.

Stop the local stack with `Ctrl-C`.

If you already know the remote vLLM server is up, use the faster local-only command:

```bash
just dev-all
```

## Check Status

```bash
cd web
just status
```

This checks the frontend, both local backends, the frontend `/api` proxy, and authenticated access to
the remote vLLM endpoint.

## Individual Commands

```bash
just ai-serve       # AI backend only
just contrib-serve  # contributor backend only
just dev            # frontend only on 60990
just serve          # both local API backends, no frontend
just vllm-ensure    # check/start the remote vLLM engine (SYFI_VLLM_SSH_HOST)
```

For public SYFI trace questions, the AI backend also needs `E2B_API_KEY` or `E2B_KEY`. For uploaded
user-trace questions, E2B is not needed because analysis runs in the browser/Pyodide worker.
