#!/usr/bin/env bash
set -euo pipefail

# SSH to the host running the vLLM engine and ensure it's up. The host is NOT hardcoded — set it via
# SYFI_VLLM_SSH_HOST (e.g. in ~/.bashrc). The remote serve dir defaults to ~/syfi_vllm; override with
# SYFI_VLLM_REMOTE_DIR.
host="${SYFI_VLLM_SSH_HOST:?set SYFI_VLLM_SSH_HOST to the vLLM host (e.g. export SYFI_VLLM_SSH_HOST=vllm.example.com)}"
container="${SYFI_VLLM_CONTAINER:-syfi-qwen36-35b-a3b-fp8-vllm}"
port="${SYFI_VLLM_PORT:-60995}"
remote_dir="${SYFI_VLLM_REMOTE_DIR:-\$HOME/syfi_vllm}"

ssh -o BatchMode=yes "${host}" 'bash -s' <<REMOTE
set -eo pipefail

container="${container}"
port="${port}"
remote_dir="${remote_dir}"
key="\$(awk -F= '/^export SYFI_VLLM_API_KEY=/{print \$2; exit}' "\$HOME/.bashrc" | tr -d "\"'")"
if [[ -z "\$key" ]]; then
  echo "SYFI_VLLM_API_KEY missing in ~/.bashrc" >&2
  exit 1
fi

if curl -fsS --max-time 5 -H "Authorization: Bearer \${key}" "http://127.0.0.1:\${port}/v1/models" >/dev/null; then
  echo "vLLM already running"
  exit 0
fi

echo "starting vLLM"
docker rm -f "\${container}" >/dev/null 2>&1 || true
cd "\${remote_dir}"
SYFI_VLLM_API_KEY="\$key" \\
  HOST=0.0.0.0 \\
  PORT="\${port}" \\
  VLLM_GPUS=device=1 \\
  GPU_MEMORY_UTILIZATION=0.95 \\
  DETACH=1 \\
  ./serve_qwen36_35b_a3b_fp8_vllm.sh

for _ in \$(seq 1 150); do
  if curl -fsS --max-time 5 -H "Authorization: Bearer \${key}" "http://127.0.0.1:\${port}/v1/models" >/dev/null; then
    echo "vLLM ready"
    exit 0
  fi
  sleep 2
done

docker logs --tail 100 "\${container}"
exit 1
REMOTE
