#!/usr/bin/env bash
set -euo pipefail

# Serve Qwen/Qwen3.6-35B-A3B-FP8 through vLLM's OpenAI-compatible API.
#
# This intentionally uses the caller's default Hugging Face cache:
# - If HF_HOME is set, mount that directory.
# - Otherwise, mount ~/.cache/huggingface.
# The vLLM compile cache is mounted under the same host cache tree by default.
#
# Useful overrides:
#   PORT=60995
#   VLLM_GPUS=device=1
#   TENSOR_PARALLEL_SIZE=1
#   MAX_MODEL_LEN=32768
#   TOOL_CALL_PARSER=qwen3_xml
#   VLLM_API_KEY=...
#   VLLM_CACHE_HOST=/path/to/vllm-cache
#   ENABLE_PROMPT_TOKENS_DETAILS=1
#   ENABLE_PREFIX_CACHING=1
#   DETACH=1

# Defaults for the served-model name + port come from the centralized config/services.json so they
# stay in sync with what the runtime/Astro app expect; env vars still override.
_CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/config/services.json"
_cfg() { python3 -c "import json;print(json.load(open('${_CONFIG}'))${1})" 2>/dev/null || true; }

MODEL_ID="${MODEL_ID:-Qwen/Qwen3.6-35B-A3B-FP8}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-$(_cfg "['llm']['vllm']['model']")}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-qwen3.6-35b-a3b-fp8}"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-syfi-qwen36-35b-a3b-fp8-vllm}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-$(_cfg "['ports']['vllm']")}"
PORT="${PORT:-60995}"
VLLM_GPUS="${VLLM_GPUS:-device=1}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_xml}"
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-1}"
VLLM_API_KEY="${VLLM_API_KEY:-${SYFI_VLLM_API_KEY:-}}"
ENABLE_PROMPT_TOKENS_DETAILS="${ENABLE_PROMPT_TOKENS_DETAILS:-0}"
ENABLE_PREFIX_CACHING="${ENABLE_PREFIX_CACHING:-}"
DETACH="${DETACH:-0}"

HF_CACHE_HOST="${HF_HOME:-$HOME/.cache/huggingface}"
VLLM_CACHE_HOST="${VLLM_CACHE_HOST:-${HF_CACHE_HOST}/vllm-cache}"
mkdir -p "${HF_CACHE_HOST}"
mkdir -p "${VLLM_CACHE_HOST}"

args=(
  docker run --rm
  --name "${CONTAINER_NAME}"
  --runtime nvidia
  --gpus "${VLLM_GPUS}"
  -p "${HOST}:${PORT}:8000"
  --ipc=host
  -v "${HF_CACHE_HOST}:/root/.cache/huggingface"
  -v "${VLLM_CACHE_HOST}:/root/.cache/vllm"
  -e "HF_HOME=/root/.cache/huggingface"
)

if [[ "${DETACH}" == "1" || "${DETACH}" == "true" ]]; then
  args+=(-d)
fi

if [[ -n "${HF_TOKEN:-}" ]]; then
  args+=(-e "HF_TOKEN=${HF_TOKEN}")
fi

args+=(
  "${VLLM_IMAGE}"
  --model "${MODEL_ID}"
  --served-model-name "${SERVED_MODEL_NAME}"
  --host 0.0.0.0
  --port 8000
  --dtype auto
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}"
  --max-model-len "${MAX_MODEL_LEN}"
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
  --enable-auto-tool-choice
  --tool-call-parser "${TOOL_CALL_PARSER}"
)

if [[ "${ENABLE_PROMPT_TOKENS_DETAILS}" == "1" || "${ENABLE_PROMPT_TOKENS_DETAILS}" == "true" ]]; then
  args+=(--enable-prompt-tokens-details)
fi

if [[ "${ENABLE_PREFIX_CACHING}" == "1" || "${ENABLE_PREFIX_CACHING}" == "true" ]]; then
  args+=(--enable-prefix-caching)
elif [[ "${ENABLE_PREFIX_CACHING}" == "0" || "${ENABLE_PREFIX_CACHING}" == "false" ]]; then
  args+=(--no-enable-prefix-caching)
fi

if [[ "${TRUST_REMOTE_CODE}" == "1" || "${TRUST_REMOTE_CODE}" == "true" ]]; then
  args+=(--trust-remote-code)
fi

if [[ -n "${VLLM_API_KEY}" ]]; then
  args+=(--api-key "${VLLM_API_KEY}")
fi

args+=("$@")

printf 'HF cache: %s\n' "${HF_CACHE_HOST}"
printf 'vLLM cache: %s\n' "${VLLM_CACHE_HOST}"
printf 'Serving model: %s as %s\n' "${MODEL_ID}" "${SERVED_MODEL_NAME}"
printf 'Endpoint: http://%s:%s/v1\n' "${HOST}" "${PORT}"
exec "${args[@]}"
