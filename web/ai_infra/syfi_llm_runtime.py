#!/usr/bin/env python3
"""Reusable LLM -> tool runtime for SYFI LLM features.

This module is the maintained runtime for text QA, analysis, and plot-producing LLM calls:

1. Create an E2B sandbox from the prebuilt SYFI template.
2. Send conversation history plus a `run_python(code)` tool schema to an OpenAI-compatible
   chat-completions endpoint, usually OpenRouter or local vLLM.
3. Execute every model-requested `run_python` call inside the E2B sandbox.
4. Feed tool results back to the model until it returns a final answer.

There is no canned tool result and no per-session DB upload. The template must already contain
`/data/syfi_coding_trace.duckdb`. The public functions are useful both from the CLI and from a
small HTTP sidecar used by the browser tester.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import mimetypes
import os
import shutil
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any

def service_config() -> dict[str, Any]:
    """The centralized config (config/services.json): ports + default LLM backend.

    Shared single source of truth read by the AI sidecar and the Astro dev proxy. Env vars still take
    precedence over these values; a missing/malformed file degrades to ``{}`` rather than raising.
    """
    try:
        path = Path(__file__).resolve().parents[2] / "config" / "services.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_LLM_CONFIG = service_config().get("llm", {})


def _active_llm_config() -> dict[str, Any]:
    """Settings for the selected LLM provider (``llm[llm.provider]`` in config/services.json).

    ``provider`` chooses ``vllm`` (local deployment) vs ``openrouter``; ``SYFI_LLM_PROVIDER`` overrides
    it. Falls back to a flat ``{base_url, model}`` block for backward compatibility. The provider's
    ``base_url`` is what decides key handling downstream (an openrouter.ai URL requires
    ``OPENROUTER_API_KEY``; otherwise the key is optional, e.g. for vLLM).
    """
    provider = os.environ.get("SYFI_LLM_PROVIDER") or _LLM_CONFIG.get("provider")
    if isinstance(provider, str):
        block = _LLM_CONFIG.get(provider)
        if isinstance(block, dict):
            return block
    return {k: v for k, v in _LLM_CONFIG.items() if k in ("base_url", "model")}


_ACTIVE_LLM = _active_llm_config()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_LLM_BASE_URL = (
    os.environ.get("SYFI_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or _ACTIVE_LLM.get("base_url")
)
DEFAULT_LLM_CHAT_COMPLETIONS_URL = os.environ.get("SYFI_LLM_CHAT_COMPLETIONS_URL") or os.environ.get(
    "OPENAI_CHAT_COMPLETIONS_URL"
)
DEFAULT_TEMPLATE = os.environ.get("E2B_SYFI_TEMPLATE", "syfi-qa-code-interpreter:latest")
DEFAULT_MODEL = (
    os.environ.get("SYFI_LLM_MODEL")
    or os.environ.get("OPENAI_MODEL")
    or os.environ.get("OPENROUTER_MODEL")
    or os.environ.get("OPENROUTE_MODEL")
    or _ACTIVE_LLM.get("model")
    or "openai/gpt-4o-mini"
)
REMOTE_DB = "/data/syfi_coding_trace.duckdb"
REMOTE_OUT = "/out"
DEFAULT_PROMPT_FILE = Path(__file__).with_name("syfi_qa_system_prompt.md")
DEFAULT_MAX_TOKENS = int(os.environ.get("SYFI_LLM_MAX_TOKENS", 8192))
DEFAULT_MAX_ARTIFACT_INLINE_BYTES = int(os.environ.get("SYFI_MAX_ARTIFACT_INLINE_BYTES", 2_000_000))
DEFAULT_OPENROUTER_MAX_RETRIES = int(os.environ.get("OPENROUTER_MAX_RETRIES", 3))
DEFAULT_MAX_GENERATION_RETRIES = int(os.environ.get("SYFI_LLM_MAX_GENERATION_RETRIES", 3))
MAX_TOKEN_FINISH_REASONS = {"length", "max_tokens"}
DEFAULT_TEMPERATURE = float(os.environ.get("SYFI_LLM_TEMPERATURE", 1.0))
DEFAULT_TOP_P = float(os.environ.get("SYFI_LLM_TOP_P", 0.95))
DEFAULT_TOP_K = int(os.environ.get("SYFI_LLM_TOP_K", 20))
DEFAULT_MIN_P = float(os.environ.get("SYFI_LLM_MIN_P", 0.0))
DEFAULT_PRESENCE_PENALTY = float(os.environ.get("SYFI_LLM_PRESENCE_PENALTY", 1.5))
DEFAULT_REPETITION_PENALTY = float(os.environ.get("SYFI_LLM_REPETITION_PENALTY", 1.0))
DEFAULT_SYFI_TRACE_CONTEXT = (
    "You are analyzing the public SYFI coding-trace dataset. Treat results as public SYFI "
    "dataset results."
)
DEFAULT_USER_TRACE_CONTEXT = (
    "You are analyzing the user's uploaded coding trace. Treat all counts and plots as specific "
    "to that uploaded trace, not the public SYFI dataset."
)
VALID_BROWSER_ROLES = {"user", "assistant"}


def env_value(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def require_env(*names: str) -> str:
    value = env_value(*names)
    if not value:
        raise RuntimeError(f"{' or '.join(names)} is not set")
    return value


def _json_env_dict(*names: str) -> dict[str, Any]:
    for name in names:
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{name} must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{name} must be a JSON object")
        return parsed
    return {}


def default_chat_completions_url() -> str:
    if DEFAULT_LLM_CHAT_COMPLETIONS_URL:
        return DEFAULT_LLM_CHAT_COMPLETIONS_URL
    if DEFAULT_LLM_BASE_URL:
        base = DEFAULT_LLM_BASE_URL.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"
    return OPENROUTER_URL


def _is_openrouter_url(url: str) -> bool:
    return url.startswith("https://openrouter.ai/") or url.startswith("http://openrouter.ai/")


def default_llm_api_key(chat_url: str) -> str | None:
    if _is_openrouter_url(chat_url):
        return require_env("OPENROUTER_API_KEY", "OPENROUTE_KEY")
    return env_value("SYFI_LLM_API_KEY", "OPENAI_API_KEY")


def default_llm_extra_body() -> dict[str, Any]:
    extra = _json_env_dict("SYFI_LLM_EXTRA_BODY", "OPENAI_EXTRA_BODY")
    chat_template_kwargs = _json_env_dict("SYFI_LLM_CHAT_TEMPLATE_KWARGS")
    if chat_template_kwargs and "chat_template_kwargs" not in extra:
        extra["chat_template_kwargs"] = chat_template_kwargs
    return extra


def default_sampling_params() -> dict[str, Any]:
    return {
        "temperature": DEFAULT_TEMPERATURE,
        "top_p": DEFAULT_TOP_P,
        "top_k": DEFAULT_TOP_K,
        "min_p": DEFAULT_MIN_P,
        "presence_penalty": DEFAULT_PRESENCE_PENALTY,
        "repetition_penalty": DEFAULT_REPETITION_PENALTY,
    }


# ---- backend selection + failover ---------------------------------------------------------
#
# A single turn is sent to one resolved backend. With ``llm.fallback`` configured, the primary
# provider's ``/models`` endpoint is probed first; if it is unreachable (connection error / 5xx)
# the turn transparently routes to the fallback provider's own backend (e.g. self-hosted vLLM ->
# OpenRouter). Probe results are cached briefly so back-to-back turns don't re-probe.

PROBE_TIMEOUT_SECONDS = float(os.environ.get("SYFI_LLM_PROBE_TIMEOUT", 2))
PROBE_TTL_SECONDS = float(os.environ.get("SYFI_LLM_PROBE_TTL", 30))
# Cache a "down" verdict longer than an "up" one: re-probing an unreachable primary costs the full
# timeout, and the fallback is a fine stand-in — so we tolerate a slower flip back to the primary in
# exchange for paying that cold-probe tax far less often (once per this window, process-wide).
PROBE_TTL_DOWN_SECONDS = float(os.environ.get("SYFI_LLM_PROBE_TTL_DOWN", 90))
_probe_cache: dict[str, tuple[float, bool]] = {}
_probe_lock = threading.Lock()


@dataclass(frozen=True)
class LLMTarget:
    """A fully-resolved LLM backend: where to send chat completions and how to auth."""

    provider: str
    chat_url: str
    model: str
    api_key: str | None
    extra_body: dict[str, Any]


def _provider_block(name: str | None) -> dict[str, Any]:
    block = _LLM_CONFIG.get(name) if name else None
    return block if isinstance(block, dict) else {}


def _chat_url_from_base(base_url: str | None) -> str:
    if not base_url:
        return OPENROUTER_URL
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


def _models_url(chat_url: str) -> str:
    if chat_url.endswith("/chat/completions"):
        return f"{chat_url[: -len('/chat/completions')]}/models"
    return f"{chat_url.rstrip('/')}/models"


def _extra_body_for(block: dict[str, Any]) -> dict[str, Any]:
    """Env extra-body merged with the provider's configured OpenRouter routing.

    A ``provider_routing`` block in the config (e.g. ``{"order": ["atlas-cloud/fp8"],
    "allow_fallbacks": true}``) is applied as the request ``provider`` field unless an explicit env
    extra-body already pins ``provider``.
    """
    extra = default_llm_extra_body()
    routing = block.get("provider_routing")
    if isinstance(routing, dict) and "provider" not in extra:
        extra = {**extra, "provider": routing}
    return extra


def resolve_target(provider: str | None, *, apply_global_env: bool) -> LLMTarget:
    """Build an :class:`LLMTarget` from the ``llm[provider]`` config block.

    ``apply_global_env`` lets the *primary* provider keep honoring the historical flat overrides
    (``SYFI_LLM_BASE_URL`` / ``SYFI_LLM_MODEL`` / ``SYFI_LLM_CHAT_COMPLETIONS_URL``). The fallback
    provider is resolved purely from its own config block so those overrides — which target the
    primary — can't bleed across into the wrong backend.
    """
    block = _provider_block(provider)
    base_url = block.get("base_url")
    model = block.get("model")
    if apply_global_env:
        base_url = env_value("SYFI_LLM_BASE_URL", "OPENAI_BASE_URL") or base_url
        model = (
            env_value("SYFI_LLM_MODEL", "OPENAI_MODEL", "OPENROUTER_MODEL", "OPENROUTE_MODEL") or model
        )
        chat_url = env_value("SYFI_LLM_CHAT_COMPLETIONS_URL", "OPENAI_CHAT_COMPLETIONS_URL") or _chat_url_from_base(
            base_url
        )
    else:
        chat_url = _chat_url_from_base(base_url)
    return LLMTarget(
        provider=provider or "llm",
        chat_url=chat_url,
        model=model or "openai/gpt-4o-mini",
        api_key=default_llm_api_key(chat_url),
        extra_body=_extra_body_for(block),
    )


def probe_target(target: LLMTarget, *, timeout: float = PROBE_TIMEOUT_SECONDS, logger=None) -> bool:
    """Is this backend reachable? GET ``/models``; a 5xx or connection error counts as down.

    Any HTTP response below 500 (incl. 401/404) means the server is up, so we never fail over on an
    auth quirk or a missing path — only on an unreachable host or a server-side outage.
    """
    url = _models_url(target.chat_url)
    request = urllib.request.Request(url, method="GET")
    if target.api_key:
        request.add_header("Authorization", f"Bearer {target.api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ok = int(getattr(response, "status", 200) or 200) < 500
            detail = f"http {getattr(response, 'status', '?')}"
    except urllib.error.HTTPError as exc:
        ok = exc.code < 500
        detail = f"http {exc.code}"
    except Exception as exc:  # noqa: BLE001 — any connection/timeout error means unreachable
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    _event(logger, "llm_probe", {"provider": target.provider, "url": url, "ok": ok, "detail": detail[:200]})
    return ok


def _probe_cached(target: LLMTarget, *, logger=None) -> bool:
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(target.provider)
        if cached is not None:
            ttl = PROBE_TTL_SECONDS if cached[1] else PROBE_TTL_DOWN_SECONDS
            if now - cached[0] < ttl:
                return cached[1]
    ok = probe_target(target, logger=logger)
    with _probe_lock:
        _probe_cache[target.provider] = (time.monotonic(), ok)
    return ok


def _legacy_target(provider: str | None) -> LLMTarget:
    """Single-provider resolution (no fallback) — preserves the exact pre-failover behavior."""
    chat_url = default_chat_completions_url()
    return LLMTarget(
        provider=provider or "llm",
        chat_url=chat_url,
        model=DEFAULT_MODEL,
        api_key=default_llm_api_key(chat_url),
        extra_body=_extra_body_for(_provider_block(provider)),
    )


def select_llm_target(*, logger=None) -> LLMTarget:
    """Pick the active backend, failing over to ``llm.fallback`` when the primary is unreachable.

    Failover is config-driven (``llm.fallback``) and skipped when ``SYFI_LLM_PROVIDER`` is set — an
    explicit provider pin disables fallback. The primary is probed (result cached for
    ``PROBE_TTL_SECONDS``); when it's down we resolve and return the fallback provider's own backend.
    """
    explicit = os.environ.get("SYFI_LLM_PROVIDER")
    provider = explicit or _LLM_CONFIG.get("provider")
    fallback = None if explicit else _LLM_CONFIG.get("fallback")
    if not fallback or fallback == provider:
        return _legacy_target(provider)
    primary = resolve_target(provider, apply_global_env=True)
    if _probe_cached(primary, logger=logger):
        return primary
    backup = resolve_target(fallback, apply_global_env=False)
    _event(
        logger,
        "llm_failover",
        {"from": primary.provider, "to": backup.provider, "model": backup.model, "chat_url": backup.chat_url},
    )
    return backup


def print_json(label: str, value: Any) -> None:
    print(f"{label}: {json.dumps(value, sort_keys=True)}", flush=True)


def create_sandbox(
    *,
    template: str,
    sandbox_timeout: int,
    allow_internet: bool,
):
    from e2b_code_interpreter import Sandbox

    return Sandbox.create(
        template=template,
        timeout=sandbox_timeout,
        allow_internet_access=allow_internet,
        metadata={
            "app": "coding-trace",
            "purpose": "syfi-llm-runtime",
            "repo": "coding_trace_refactor",
            "script": "web/ai_infra/syfi_llm_runtime.py",
        },
    )


def _file_type_value(value: Any) -> str:
    return getattr(value, "value", str(value))


def _guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def _data_url(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def reset_output_dir(sandbox: Any) -> None:
    try:
        sandbox.files.remove(REMOTE_OUT, request_timeout=30)
    except Exception:
        pass
    sandbox.files.make_dir(REMOTE_OUT)


def _artifact_payload(sandbox: Any, path: str, size: int, *, max_inline_bytes: int) -> dict[str, Any]:
    mime = _guess_mime(path)
    payload: dict[str, Any] = {
        "path": path,
        "size": size,
        "mime": mime,
        "is_image": mime.startswith("image/"),
        "display": False,
    }
    if size > max_inline_bytes:
        payload["inline_error"] = f"artifact exceeds inline limit ({max_inline_bytes} bytes)"
        return payload

    if mime.startswith("image/"):
        raw = bytes(sandbox.files.read(path, format="bytes", request_timeout=30))
        payload["data_url"] = _data_url(mime, raw)
        payload["display"] = True
    elif mime.startswith("text/") or mime in {"application/json", "text/csv"}:
        text = sandbox.files.read(path, format="text", request_timeout=30)
        payload["text_preview"] = text[:10_000]
    return payload


def compact_tool_result_for_model(result: dict[str, Any]) -> dict[str, Any]:
    """Remove browser-only payloads before feeding tool output back to the LLM."""
    compact = {
        "stdout": result.get("stdout", []),
        "stderr": result.get("stderr", []),
        "error": result.get("error"),
        "results": [
            {
                "text": item.get("text"),
                "json": item.get("json"),
                "png_bytes_approx": item.get("png_bytes_approx"),
                "svg_chars": item.get("svg_chars"),
            }
            for item in result.get("results", [])
        ],
    }
    if "artifact_error" in result:
        compact["artifact_error"] = result["artifact_error"]
    compact["artifacts"] = [
        {
            "path": item.get("path"),
            "size": item.get("size"),
            "type": item.get("type"),
            "mime": item.get("mime"),
            "is_image": item.get("is_image"),
            "display": item.get("display"),
            "inline_error": item.get("inline_error"),
            "text_preview": item.get("text_preview"),
        }
        for item in result.get("artifacts", [])
    ]
    compact["display_images"] = [
        {
            "path": item.get("path"),
            "mime": item.get("mime"),
            "size": item.get("size"),
            "source": item.get("source"),
            "display": item.get("display"),
        }
        for item in result.get("display_images", [])
    ]
    return compact


def summarize_execution(
    execution: Any,
    sandbox: Any,
    *,
    max_artifact_inline_bytes: int = DEFAULT_MAX_ARTIFACT_INLINE_BYTES,
) -> dict[str, Any]:
    stdout = [getattr(msg, "line", str(msg)) for msg in getattr(execution.logs, "stdout", [])]
    stderr = [getattr(msg, "line", str(msg)) for msg in getattr(execution.logs, "stderr", [])]
    results = []
    inline_images = []
    for item in execution.results:
        png = getattr(item, "png", None)
        png_bytes_approx = round(len(png) * 3 / 4) if png else None
        image = None
        if png and (png_bytes_approx or 0) <= max_artifact_inline_bytes:
            image = {
                "mime": "image/png",
                "data_url": f"data:image/png;base64,{png}",
                "display": True,
                "source": "inline_result",
            }
            inline_images.append(image)
        results.append(
            {
                "text": getattr(item, "text", None),
                "json": getattr(item, "json", None),
                "png_bytes_approx": png_bytes_approx,
                "image": image,
                "svg_chars": len(item.svg) if getattr(item, "svg", None) else None,
            }
        )
    error = execution.error
    summary = {
        "stdout": stdout,
        "stderr": stderr,
        "error": None
        if error is None
        else {
            "name": error.name,
            "value": error.value,
            "traceback": error.traceback,
        },
        "results": results,
    }
    try:
        artifacts = sandbox.files.list(REMOTE_OUT)
        summary["artifacts"] = []
        for artifact in artifacts:
            item = {
                "path": artifact.path,
                "size": artifact.size,
                "type": _file_type_value(artifact.type),
            }
            if _file_type_value(artifact.type) == "file":
                try:
                    item.update(
                        _artifact_payload(
                            sandbox,
                            artifact.path,
                            artifact.size,
                            max_inline_bytes=max_artifact_inline_bytes,
                        )
                    )
                except Exception as exc:
                    item["inline_error"] = f"{type(exc).__name__}: {exc}"
            summary["artifacts"].append(item)
    except Exception as exc:
        summary["artifact_error"] = f"{type(exc).__name__}: {exc}"
    artifact_images = [
        {
            "path": item.get("path"),
            "mime": item.get("mime"),
            "size": item.get("size"),
            "data_url": item.get("data_url"),
            "source": "artifact",
            "display": True,
        }
        for item in summary.get("artifacts", [])
        if item.get("display") and item.get("data_url")
    ]
    summary["display_images"] = artifact_images or inline_images
    return summary


def run_python_tool(
    sandbox: Any,
    code: str,
    *,
    timeout: float,
    max_artifact_inline_bytes: int = DEFAULT_MAX_ARTIFACT_INLINE_BYTES,
) -> dict[str, Any]:
    execution = sandbox.run_code(code, timeout=timeout, request_timeout=timeout + 60)
    return summarize_execution(
        execution,
        sandbox,
        max_artifact_inline_bytes=max_artifact_inline_bytes,
    )


def _provider_slug(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


def _extract_failed_provider_slugs(value: Any, *, depth: int = 0) -> set[str]:
    if depth > 6:
        return set()
    providers: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"provider_name", "providerName", "provider_slug", "providerSlug"} and isinstance(item, str):
                providers.add(_provider_slug(item))
            providers.update(_extract_failed_provider_slugs(item, depth=depth + 1))
    elif isinstance(value, list):
        for item in value:
            providers.update(_extract_failed_provider_slugs(item, depth=depth + 1))
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                providers.update(_extract_failed_provider_slugs(json.loads(text), depth=depth + 1))
            except json.JSONDecodeError:
                pass
    return providers


def _parse_error_json(body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _is_retryable_openrouter_error(status: int | None, body: str) -> bool:
    if status in {401, 403}:
        return False
    if status is None or status in {408, 409, 425, 429} or status >= 500:
        return True
    if status == 400:
        retry_markers = (
            "Provider returned error",
            "Backend request failed",
            "invoke model error",
            "forward bad request",
            "upstream",
            "provider",
        )
        return any(marker in body for marker in retry_markers)
    return False


def openrouter_chat(
    *,
    api_key: str | None,
    chat_url: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    max_retries: int = DEFAULT_OPENROUTER_MAX_RETRIES,
    extra_body: dict[str, Any] | None = None,
    usage_sink: list[dict[str, Any]] | None = None,
    logger=None,
) -> dict[str, Any]:
    ignored_providers: set[str] = set()
    last_error = ""
    max_retries = max(0, max_retries)
    is_openrouter = _is_openrouter_url(chat_url)
    provider_name = "OpenRouter" if is_openrouter else "LLM"

    for attempt in range(max_retries + 1):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        payload.update(default_sampling_params())
        if tools is not None:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = False
        if extra_body:
            payload.update(extra_body)
        if is_openrouter:
            # Preserve any configured provider routing (e.g. order=["atlas-cloud/fp8"]) and merge in
            # providers that failed earlier attempts so OpenRouter routes around them.
            provider_pref = dict(payload.get("provider") or {})
            if ignored_providers:
                merged = set(provider_pref.get("ignore") or []) | ignored_providers
                provider_pref["ignore"] = sorted(merged)
                provider_pref.setdefault("allow_fallbacks", True)
            if provider_pref:
                payload["provider"] = provider_pref
            else:
                payload.pop("provider", None)

        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if is_openrouter:
            headers["HTTP-Referer"] = "https://github.com/uw-syfi/TraceLab"
            headers["X-Title"] = "SyFI Trace Atlas QA"

        request = urllib.request.Request(
            chat_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                if attempt:
                    _event(
                        logger,
                        "openrouter_retry",
                        {
                            "status": "recovered",
                            "attempt": attempt + 1,
                            "ignored_providers": sorted(ignored_providers),
                        },
                    )
                data = json.loads(response.read().decode("utf-8"))
                # Capture this call's token usage for the turn-level accounting (response-time only —
                # `usage` exists in the reply, never in the request). Retries append another entry.
                if usage_sink is not None and isinstance(data.get("usage"), dict):
                    usage_sink.append(data["usage"])
                return data
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = f"{provider_name} HTTP {exc.code}: {body}"
            parsed = _parse_error_json(body)
            if is_openrouter:
                ignored_providers.update(_extract_failed_provider_slugs(parsed if parsed is not None else body))
            retryable = _is_retryable_openrouter_error(exc.code, body)
            _event(
                logger,
                "openrouter_retry",
                {
                    "status": "retrying" if retryable and attempt < max_retries else "giving_up",
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "http_status": exc.code,
                    "retryable": retryable,
                    "ignored_providers": sorted(ignored_providers),
                    "error_preview": body[:700],
                },
            )
            if not retryable or attempt >= max_retries:
                raise RuntimeError(last_error) from exc
        except urllib.error.URLError as exc:
            last_error = f"{provider_name} request failed: {exc}"
            _event(
                logger,
                "openrouter_retry",
                {
                    "status": "retrying" if attempt < max_retries else "giving_up",
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "retryable": True,
                    "ignored_providers": sorted(ignored_providers),
                    "error_preview": str(exc)[:700],
                },
            )
            if attempt >= max_retries:
                raise RuntimeError(last_error) from exc

        time.sleep(min(0.4 * (2**attempt), 3.0))

    raise RuntimeError(last_error or f"{provider_name} request failed")


def _finish_reason(choice: dict[str, Any]) -> str:
    reason = choice.get("finish_reason")
    if reason is None:
        reason = choice.get("stop_reason")
    return str(reason or "").lower()


def _hit_max_tokens(choice: dict[str, Any]) -> bool:
    return _finish_reason(choice) in MAX_TOKEN_FINISH_REASONS


def _max_token_retry_instruction(*, retry: int, max_tokens: int) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "The previous generation reached the output token limit before stopping. "
            f"Retry this same turn and finish within {max_tokens} output tokens. "
            "Keep thinking concise enough to complete. If a tool is useful, call it now with only "
            "the necessary code; otherwise give the final answer directly. "
            f"This is retry {retry}."
        ),
    }


def chat_with_generation_retries(
    *,
    api_key: str | None,
    chat_url: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    max_retries: int,
    max_generation_retries: int,
    extra_body: dict[str, Any] | None = None,
    usage_sink: list[dict[str, Any]] | None = None,
    logger=None,
) -> dict[str, Any]:
    """Retry successful responses that ended only because the model hit max_tokens."""
    max_generation_retries = max(0, max_generation_retries)
    last_response: dict[str, Any] | None = None

    for retry in range(max_generation_retries + 1):
        request_messages = messages
        if retry:
            request_messages = [
                *messages,
                _max_token_retry_instruction(retry=retry, max_tokens=max_tokens),
            ]

        response = openrouter_chat(
            api_key=api_key,
            chat_url=chat_url,
            model=model,
            messages=request_messages,
            tools=tools,
            max_tokens=max_tokens,
            max_retries=max_retries,
            extra_body=extra_body,
            usage_sink=usage_sink,
            logger=logger,
        )
        last_response = response
        choice = response["choices"][0]
        if not _hit_max_tokens(choice):
            if retry:
                _event(
                    logger,
                    "generation_retry",
                    {
                        "status": "recovered",
                        "retry": retry,
                        "max_retries": max_generation_retries,
                        "finish_reason": choice.get("finish_reason"),
                    },
                )
            return response

        msg = choice.get("message") or {}
        retry_number = min(retry + 1, max_generation_retries)
        _event(
            logger,
            "generation_retry",
            {
                "status": "retrying" if retry < max_generation_retries else "giving_up",
                "retry": retry_number,
                "max_retries": max_generation_retries,
                "finish_reason": choice.get("finish_reason"),
                "max_tokens": max_tokens,
                "content_preview": (msg.get("content") or "")[:700],
            },
        )
        if retry >= max_generation_retries:
            return response

    if last_response is None:
        raise RuntimeError("LLM request failed before receiving a response")
    return last_response



def tool_schema(*, db_path: str = REMOTE_DB, out_dir: str = REMOTE_OUT) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "run_python",
                "description": (
                    "Run Python code against the trace analysis environment. "
                    f"The DuckDB database is read-only at {db_path}. "
                    f"Write generated plots or CSVs under {out_dir}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Python code to execute in the sandbox.",
                        }
                    },
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def system_prompt(
    prompt_file: Path = DEFAULT_PROMPT_FILE,
    *,
    db_path: str = REMOTE_DB,
    out_dir: str = REMOTE_OUT,
    trace_context: str = DEFAULT_SYFI_TRACE_CONTEXT,
) -> str:
    prompt = prompt_file.read_text(encoding="utf-8")
    return (
        prompt.replace("{{REMOTE_DB}}", db_path)
        .replace("{{REMOTE_OUT}}", out_dir)
        .replace("{{TRACE_CONTEXT}}", trace_context.strip())
        .strip()
    )


def normalize_browser_messages(messages: list[dict[str, Any]], *, max_messages: int = 12) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for msg in messages[-max_messages:]:
        role = msg.get("role")
        content = msg.get("content")
        if role in VALID_BROWSER_ROLES and isinstance(content, str) and content.strip():
            normalized.append({"role": role, "content": content.strip()})
    return normalized


def _event(logger, label: str, value: Any) -> None:
    if logger is not None:
        logger(label, value)


def parse_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    fn = call.get("function", {})
    name = fn.get("name") or "run_python"
    try:
        args_obj = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError as exc:
        args_obj = {"code": "", "parse_error": str(exc)}
    code = args_obj.get("code") or ""
    return name, args_obj, code


def split_visible_thinking(content: str) -> tuple[str, str]:
    """Return (thinking, visible_text) for Qwen-style content containing </think>."""
    if "</think>" not in content:
        return "", content
    thinking, visible = content.split("</think>", 1)
    if "<think>" in thinking:
        thinking = thinking.split("<think>", 1)[1]
    return thinking.strip(), visible.lstrip()


def display_text_from_message(msg: dict[str, Any]) -> tuple[str, str]:
    content = msg.get("content") or ""
    if not isinstance(content, str):
        return "", ""
    return split_visible_thinking(content)


def assistant_message_for_history(msg: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields needed for OpenAI/OpenRouter tool-result continuation."""
    compact = {
        "role": "assistant",
        "content": msg.get("content") or "",
    }
    if msg.get("tool_calls"):
        compact["tool_calls"] = msg["tool_calls"]
    return compact


def display_images_from_tool_events(tool_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        image
        for event in tool_events
        for image in event.get("result", {}).get("display_images", [])
    ]


def _aggregate_usage(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum token usage across every LLM call made during a turn (tool-loop turns + retries).

    Both vLLM (OpenAI-compatible) and OpenRouter report prompt/completion/total tokens; OpenRouter
    may also include a per-call ``cost`` (USD). Missing fields default to 0. ``calls`` is the number
    of LLM round-trips, so a turn that looped or retried is distinguishable from a one-shot answer.
    """
    agg: dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": len(calls),
    }
    cost, has_cost = 0.0, False
    for usage in calls:
        agg["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        agg["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        agg["total_tokens"] += int(usage.get("total_tokens") or 0)
        if usage.get("cost") is not None:
            try:
                cost += float(usage["cost"])
                has_cost = True
            except (TypeError, ValueError):
                pass
    if has_cost:
        agg["cost"] = cost
    return agg


def _final_result(
    *,
    final: dict[str, str],
    turns: int,
    forced: bool,
    tool_events: list[dict[str, Any]],
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "final",
        "assistant_message": final,
        "content": final["content"],
        "turns": turns,
        "forced": forced,
        "tool_events": tool_events,
        "display_images": display_images_from_tool_events(tool_events),
        "usage": usage,
    }


class ToolExecutor:
    """Execution boundary for one ``run_python`` tool call.

    The loop is identical for every source; only the executor instance forks. Subclasses
    run the model's code wherever the data lives — an E2B sandbox (public DB), a connected
    browser over a socket (uploaded trace), or a local DuckDB file (standalone testing) —
    and return the shared tool-result shape ``{stdout, stderr, error, results, artifacts,
    display_images}``.
    """

    mode = "server"

    def __init__(self, *, db_path: str, out_dir: str) -> None:
        self.db_path = db_path
        self.out_dir = out_dir

    def tools(self) -> list[dict[str, Any]]:
        return tool_schema(db_path=self.db_path, out_dir=self.out_dir)

    def execute_tool_call(
        self,
        call: dict[str, Any],
        *,
        print_code: bool,
        tool_timeout: float,
        max_artifact_inline_bytes: int,
        logger,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class E2BPythonExecutor(ToolExecutor):
    mode = "e2b"

    def __init__(
        self,
        *,
        template: str,
        sandbox_timeout: int,
        allow_internet: bool,
        sandbox_future: Any | None = None,
        kill_sandbox: bool = True,
        db_path: str = REMOTE_DB,
        out_dir: str = REMOTE_OUT,
    ) -> None:
        super().__init__(db_path=db_path, out_dir=out_dir)
        self.template = template
        self.sandbox_timeout = sandbox_timeout
        self.allow_internet = allow_internet
        self.sandbox_future = sandbox_future
        self.kill_sandbox = kill_sandbox
        self.sandbox = None
        self.sandbox_prepared = False

    def ensure_sandbox(self, logger):
        if self.sandbox is None:
            if self.sandbox_future is not None:
                status = (
                    "using_sandbox"
                    if getattr(self.sandbox_future, "done", lambda: False)()
                    else "waiting_for_sandbox"
                )
                _event(logger, "e2b", {"status": status, "template": self.template})
                self.sandbox = self.sandbox_future.result()
            else:
                _event(logger, "e2b", {"status": "creating_sandbox", "template": self.template})
                self.sandbox = create_sandbox(
                    template=self.template,
                    sandbox_timeout=self.sandbox_timeout,
                    allow_internet=self.allow_internet,
                )
            _event(
                logger,
                "e2b",
                {
                    "status": "sandbox_ready",
                    "template": self.template,
                    "sandbox_id": getattr(self.sandbox, "sandbox_id", None),
                },
            )
        if not self.sandbox_prepared:
            reset_output_dir(self.sandbox)
            self.sandbox_prepared = True
        return self.sandbox

    def execute_tool_call(
        self,
        call: dict[str, Any],
        *,
        print_code: bool,
        tool_timeout: float,
        max_artifact_inline_bytes: int,
        logger,
    ) -> dict[str, Any]:
        name, args_obj, code = parse_tool_call(call)
        if name != "run_python":
            result = {"error": f"unsupported tool: {name}"}
        elif args_obj.get("parse_error"):
            result = {"error": f"invalid tool arguments: {args_obj['parse_error']}"}
        else:
            if print_code:
                _event(logger, "tool_code", {"tool_call_id": call.get("id"), "code": code})
            sandbox = self.ensure_sandbox(logger)
            result = run_python_tool(
                sandbox,
                code,
                timeout=tool_timeout,
                max_artifact_inline_bytes=max_artifact_inline_bytes,
            )
        return {
            "tool_call_id": call.get("id"),
            "name": name,
            "code": code,
            "result": result,
        }

    def close(self) -> None:
        if self.sandbox is not None and self.kill_sandbox:
            self.sandbox.kill()


def local_python_exec(
    code: str,
    *,
    out_dir: str,
    max_artifact_inline_bytes: int = DEFAULT_MAX_ARTIFACT_INLINE_BYTES,
) -> dict[str, Any]:
    """Run code in-process and summarize stdout/stderr/artifacts.

    Mirrors the Pyodide QA worker: reset ``out_dir``, redirect stdout/stderr, ``exec`` in a
    fresh namespace, then scan ``out_dir`` for generated artifacts. Returns the same shape the
    E2B and browser paths produce. Code runs in this process, so this is for trusted local
    data (standalone testing), not untrusted input.
    """
    out_path = Path(out_dir)
    try:
        shutil.rmtree(out_path)
    except FileNotFoundError:
        pass
    out_path.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("MPLBACKEND", "Agg")
    stdout = io.StringIO()
    stderr = io.StringIO()
    error = None
    namespace: dict[str, Any] = {"__name__": "__main__"}
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(code, namespace, namespace)  # noqa: S102 — trusted local test harness
    except Exception as exc:  # noqa: BLE001 — surface as a tool error, like the sandbox does
        error = {
            "name": type(exc).__name__,
            "value": str(exc),
            "traceback": traceback.format_exc(),
        }

    artifacts: list[dict[str, Any]] = []
    for path in sorted(p for p in out_path.rglob("*") if p.is_file()):
        size = path.stat().st_size
        mime = _guess_mime(str(path))
        item: dict[str, Any] = {
            "path": str(path),
            "size": size,
            "type": "file",
            "mime": mime,
            "is_image": mime.startswith("image/"),
            "display": False,
        }
        try:
            if size > max_artifact_inline_bytes:
                item["inline_error"] = f"artifact exceeds inline limit ({max_artifact_inline_bytes} bytes)"
            elif mime.startswith("image/"):
                item["data_url"] = _data_url(mime, path.read_bytes())
                item["display"] = True
                item["source"] = "artifact"
            elif mime.startswith("text/") or mime in {"application/json", "text/csv"}:
                item["text_preview"] = path.read_text(errors="replace")[:10_000]
        except Exception as exc:  # noqa: BLE001
            item["inline_error"] = f"{type(exc).__name__}: {exc}"
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
    return {
        "stdout": stdout.getvalue().splitlines(True),
        "stderr": stderr.getvalue().splitlines(True),
        "error": error,
        "results": [],
        "artifacts": artifacts,
        "display_images": display_images,
    }


class LocalDuckDBExecutor(ToolExecutor):
    """Run model code in-process against a local DuckDB file (no E2B, no socket).

    This is the standalone seam for the user-trace loop: it mirrors what the Pyodide QA worker
    does in the browser, so ``run_chat_turn(messages, executor=LocalDuckDBExecutor(db))`` — wired
    to the ``--db`` CLI flag — exercises the whole user path as a plain CLI call. The model's code
    reads the DB from the literal ``db_path`` baked into the system prompt.
    """

    mode = "local_duckdb"

    def __init__(self, *, db_path: str, out_dir: str | None = None) -> None:
        self._owns_out_dir = out_dir is None
        resolved_out = out_dir or tempfile.mkdtemp(prefix="syfi-local-out-")
        super().__init__(db_path=str(db_path), out_dir=resolved_out)

    def execute_tool_call(
        self,
        call: dict[str, Any],
        *,
        print_code: bool,
        tool_timeout: float,
        max_artifact_inline_bytes: int,
        logger,
    ) -> dict[str, Any]:
        name, args_obj, code = parse_tool_call(call)
        if name != "run_python":
            result: dict[str, Any] = {"error": f"unsupported tool: {name}"}
        elif args_obj.get("parse_error"):
            result = {"error": f"invalid tool arguments: {args_obj['parse_error']}"}
        else:
            if print_code:
                _event(logger, "tool_code", {"tool_call_id": call.get("id"), "code": code})
            result = local_python_exec(
                code,
                out_dir=self.out_dir,
                max_artifact_inline_bytes=max_artifact_inline_bytes,
            )
        return {
            "tool_call_id": call.get("id"),
            "name": name,
            "code": code,
            "result": result,
        }

    def close(self) -> None:
        if self._owns_out_dir:
            shutil.rmtree(self.out_dir, ignore_errors=True)


class ClientBridgeExecutor(ToolExecutor):
    """Delegate ``run_python`` to a connected client (browser Pyodide) over a transport.

    The server loop calls ``execute_tool_call`` synchronously; this executor sends a
    ``tool_request`` frame through the thread-safe ``send`` callback and blocks on a Future until
    the client returns the result. The transport (WebSocket handler) resolves that Future via
    :meth:`resolve` from its receive task. Only the generated code and the aggregated result cross
    the boundary — never raw trace rows.
    """

    mode = "client_bridge"

    def __init__(
        self,
        *,
        send,
        db_path: str = "/work/trace.duckdb",
        out_dir: str = "/out",
    ) -> None:
        super().__init__(db_path=db_path, out_dir=out_dir)
        self._send = send
        self._lock = threading.Lock()
        self._pending: dict[str, Future] = {}

    def execute_tool_call(
        self,
        call: dict[str, Any],
        *,
        print_code: bool,
        tool_timeout: float,
        max_artifact_inline_bytes: int,
        logger,
    ) -> dict[str, Any]:
        name, args_obj, code = parse_tool_call(call)
        tool_call_id = call.get("id") or ""
        if name != "run_python":
            result: dict[str, Any] = {"error": f"unsupported tool: {name}"}
        elif args_obj.get("parse_error"):
            result = {"error": f"invalid tool arguments: {args_obj['parse_error']}"}
        else:
            if print_code:
                _event(logger, "tool_code", {"tool_call_id": tool_call_id, "code": code})
            result = self._request_client(tool_call_id, code, tool_timeout=tool_timeout)
        return {
            "tool_call_id": tool_call_id,
            "name": name,
            "code": code,
            "result": result,
        }

    def _request_client(self, tool_call_id: str, code: str, *, tool_timeout: float) -> dict[str, Any]:
        future: Future = Future()
        key = tool_call_id or f"call-{id(future):x}"
        with self._lock:
            self._pending[key] = future
        try:
            self._send({"type": "tool_request", "tool_call_id": key, "code": code})
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._pending.pop(key, None)
            return {"error": f"failed to dispatch tool to client: {exc}"}
        try:
            return future.result(timeout=tool_timeout + 60)
        except FuturesTimeoutError:
            with self._lock:
                self._pending.pop(key, None)
            return {"error": f"client tool execution timed out after {tool_timeout}s"}

    def resolve(self, tool_call_id: str, result: dict[str, Any] | None) -> None:
        """Resolve a pending ``tool_request`` with the client's result (called from the receive task)."""
        with self._lock:
            future = self._pending.pop(tool_call_id, None)
        if future is not None and not future.done():
            future.set_result(result or {})

    def fail_all(self, message: str) -> None:
        """Resolve every pending request with an error (e.g. on client disconnect)."""
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_result({"error": message})


def _append_tool_result_messages(
    *,
    openrouter_messages: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    logger,
) -> None:
    for item in tool_results:
        result = item.get("result") or {}
        event = {
            "tool_call_id": item.get("tool_call_id"),
            "name": item.get("name") or "run_python",
            "code": item.get("code") or "",
            "result": result,
        }
        tool_events.append(event)
        _event(
            logger,
            "tool_result",
            {
                "tool_call_id": event["tool_call_id"],
                "summary": compact_tool_result_for_model(result),
            },
        )
        openrouter_messages.append(
            {
                "role": "tool",
                "tool_call_id": event["tool_call_id"],
                "name": event["name"],
                "content": json.dumps(compact_tool_result_for_model(result)),
            }
        )


def _run_tool_loop(
    *,
    llm_api_key: str | None,
    llm_chat_url: str,
    llm_extra_body: dict[str, Any],
    model: str,
    openrouter_messages: list[dict[str, Any]],
    executor: ToolExecutor,
    start_turn: int,
    max_tool_turns: int,
    max_tokens: int,
    max_generation_retries: int,
    tool_timeout: float,
    print_code: bool,
    max_artifact_inline_bytes: int,
    openrouter_max_retries: int,
    tool_events: list[dict[str, Any]],
    logger,
) -> dict[str, Any]:
    tools = executor.tools()
    # Every LLM call in this turn appends its `usage` here; summed into the final result for logging.
    usage_calls: list[dict[str, Any]] = []

    for turn in range(start_turn, max_tool_turns + 1):
        response = chat_with_generation_retries(
            api_key=llm_api_key,
            chat_url=llm_chat_url,
            model=model,
            messages=openrouter_messages,
            tools=tools,
            max_tokens=max_tokens,
            max_retries=openrouter_max_retries,
            max_generation_retries=max_generation_retries,
            extra_body=llm_extra_body,
            usage_sink=usage_calls,
            logger=logger,
        )
        choice = response["choices"][0]
        msg = choice["message"]
        tool_calls = msg.get("tool_calls") or []
        thinking_text, visible_content = display_text_from_message(msg)
        reasoning_available = bool(msg.get("reasoning") or msg.get("reasoning_details"))
        _event(
            logger,
            "model_turn",
            {
                "turn": turn,
                "finish_reason": choice.get("finish_reason"),
                "content": visible_content,
                "thinking": thinking_text,
                "reasoning_redacted": reasoning_available,
                "usage": response.get("usage"),  # this round's (final) model-call token usage
                "tool_calls": [
                    {
                        "id": call.get("id"),
                        "name": call.get("function", {}).get("name"),
                        "args_preview": call.get("function", {}).get("arguments", "")[:700],
                    }
                    for call in tool_calls
                ],
            },
        )

        if not tool_calls:
            final_content = visible_content or (msg.get("content") or "")
            result = _final_result(
                final={"role": "assistant", "content": final_content},
                turns=turn,
                forced=False,
                tool_events=tool_events,
                usage=_aggregate_usage(usage_calls),
            )
            _event(logger, "final", {"content": result["content"], "turns": turn})
            return result

        openrouter_messages.append(assistant_message_for_history(msg))
        for call in tool_calls:
            event = executor.execute_tool_call(
                call,
                print_code=print_code,
                tool_timeout=tool_timeout,
                max_artifact_inline_bytes=max_artifact_inline_bytes,
                logger=logger,
            )
            _append_tool_result_messages(
                openrouter_messages=openrouter_messages,
                tool_events=tool_events,
                tool_results=[event],
                logger=logger,
            )

    openrouter_messages.append(
        {
            "role": "user",
            "content": "Use the tool results already provided to answer now. Do not call another tool.",
        }
    )
    response = chat_with_generation_retries(
        api_key=llm_api_key,
        chat_url=llm_chat_url,
        model=model,
        messages=openrouter_messages,
        tools=None,
        max_tokens=max_tokens,
        max_retries=openrouter_max_retries,
        max_generation_retries=max_generation_retries,
        extra_body=llm_extra_body,
        usage_sink=usage_calls,
        logger=logger,
    )
    final_msg = response["choices"][0]["message"]
    thinking_text, visible_content = display_text_from_message(final_msg)
    final_content = visible_content or (final_msg.get("content") or "")
    result = _final_result(
        final={"role": "assistant", "content": final_content},
        turns=max_tool_turns,
        forced=True,
        tool_events=tool_events,
        usage=_aggregate_usage(usage_calls),
    )
    _event(logger, "final", {"content": result["content"], "turns": max_tool_turns, "forced": True})
    return result


def run_chat_turn(
    *,
    messages: list[dict[str, Any]],
    model: str | None = None,
    template: str = DEFAULT_TEMPLATE,
    prompt_file: Path = DEFAULT_PROMPT_FILE,
    max_tool_turns: int = 4,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_generation_retries: int = DEFAULT_MAX_GENERATION_RETRIES,
    tool_timeout: float = 120,
    sandbox_timeout: int = 600,
    allow_internet: bool = False,
    print_code: bool = False,
    max_history_messages: int = 12,
    max_artifact_inline_bytes: int = DEFAULT_MAX_ARTIFACT_INLINE_BYTES,
    openrouter_max_retries: int = DEFAULT_OPENROUTER_MAX_RETRIES,
    trace_context: str = DEFAULT_SYFI_TRACE_CONTEXT,
    sandbox_future: Any | None = None,
    kill_sandbox: bool = True,
    executor: ToolExecutor | None = None,
    logger=None,
) -> dict[str, Any]:
    """Run one model -> tool -> answer turn.

    ``executor`` selects where ``run_python`` runs. When ``None`` (the default, public path), an
    ``E2BPythonExecutor`` is built from ``template`` and owned/closed here — byte-identical to the
    original CLI behavior. When injected (e.g. a pooled E2B executor, a ``ClientBridgeExecutor`` for
    the browser, or a ``LocalDuckDBExecutor`` for standalone tests), the caller owns its lifecycle.
    """
    target = select_llm_target(logger=logger)
    llm_chat_url = target.chat_url
    llm_api_key = target.api_key
    llm_extra_body = target.extra_body
    # An explicit caller-supplied model overrides the resolved backend's default; otherwise the
    # selected target's model wins (vLLM and OpenRouter use different model ids).
    model = model or target.model
    _event(logger, "llm_target", {"provider": target.provider, "model": model, "chat_url": llm_chat_url})

    owns_executor = executor is None
    if executor is None:
        e2b_key = require_env("E2B_API_KEY", "E2B_KEY")
        os.environ.setdefault("E2B_API_KEY", e2b_key)
        executor = E2BPythonExecutor(
            template=template,
            sandbox_timeout=sandbox_timeout,
            allow_internet=allow_internet,
            sandbox_future=sandbox_future,
            kill_sandbox=kill_sandbox,
        )
    openrouter_messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": system_prompt(
                prompt_file,
                db_path=executor.db_path,
                out_dir=executor.out_dir,
                trace_context=trace_context,
            ),
        },
        *normalize_browser_messages(messages, max_messages=max_history_messages),
    ]
    try:
        result = _run_tool_loop(
            llm_api_key=llm_api_key,
            llm_chat_url=llm_chat_url,
            llm_extra_body=llm_extra_body,
            model=model,
            openrouter_messages=openrouter_messages,
            executor=executor,
            start_turn=1,
            max_tool_turns=max_tool_turns,
            max_tokens=max_tokens,
            max_generation_retries=max_generation_retries,
            tool_timeout=tool_timeout,
            print_code=print_code,
            max_artifact_inline_bytes=max_artifact_inline_bytes,
            openrouter_max_retries=openrouter_max_retries,
            tool_events=[],
            logger=logger,
        )
        # Tag which backend actually served this turn (vllm GPU-time vs openrouter $) so usage logs
        # can be costed correctly. Resolved per turn by select_llm_target() above.
        result["provider"] = target.provider
        result["model"] = model
        return result
    finally:
        if owns_executor:
            executor.close()


def run_tool_loop(args: argparse.Namespace) -> dict[str, Any]:
    # --db swaps the public E2B path for a local DuckDB executor: the whole user-trace loop runs
    # in-process (mirroring the browser's Pyodide), so it's testable without E2B, a server, or a
    # browser. Without --db the public E2B path is unchanged.
    executor: ToolExecutor | None = None
    trace_context = DEFAULT_SYFI_TRACE_CONTEXT
    if getattr(args, "db", None):
        executor = LocalDuckDBExecutor(db_path=str(args.db), out_dir=args.out_dir)
        trace_context = DEFAULT_USER_TRACE_CONTEXT
    try:
        return run_chat_turn(
            messages=[{"role": "user", "content": args.question}],
            model=args.model,
            template=args.template,
            prompt_file=args.prompt_file,
            max_tool_turns=args.max_tool_turns,
            max_tokens=args.max_tokens,
            max_generation_retries=args.max_generation_retries,
            tool_timeout=args.tool_timeout,
            sandbox_timeout=args.sandbox_timeout,
            allow_internet=args.allow_internet,
            print_code=args.print_code,
            max_artifact_inline_bytes=args.max_artifact_inline_bytes,
            openrouter_max_retries=args.openrouter_max_retries,
            trace_context=trace_context,
            executor=executor,
            logger=print_json,
        )
    finally:
        if executor is not None:
            executor.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument(
        "--model",
        default=None,
        help="Override the resolved backend's model id (default: chosen by the active/failover provider).",
    )
    parser.add_argument("--prompt-file", type=Path, default=DEFAULT_PROMPT_FILE)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Run the user-trace loop locally against this DuckDB file (no E2B/server/browser).",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Artifact output dir for --db runs (default: a temp dir, removed on exit).",
    )
    parser.add_argument(
        "--question",
        default="Count SYFI rounds by provider. Return the provider names and exact round counts.",
    )
    parser.add_argument("--max-tool-turns", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--max-generation-retries", type=int, default=DEFAULT_MAX_GENERATION_RETRIES)
    parser.add_argument("--tool-timeout", type=float, default=120)
    parser.add_argument("--sandbox-timeout", type=int, default=600)
    parser.add_argument("--max-artifact-inline-bytes", type=int, default=DEFAULT_MAX_ARTIFACT_INLINE_BYTES)
    parser.add_argument("--openrouter-max-retries", type=int, default=DEFAULT_OPENROUTER_MAX_RETRIES)
    parser.add_argument("--allow-internet", action="store_true")
    parser.add_argument("--print-code", action="store_true")
    return parser.parse_args()


def main() -> int:
    run_tool_loop(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
