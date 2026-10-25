from datetime import datetime, timezone
import os
from pathlib import Path
import time
from typing import Any, Callable
import httpx
from pydantic import BaseModel

from bharat_gate.core.exceptions import (
    BharatCodeAPIError,
    BharatCodeConfigError,
    BharatGateError,
    TruncatedToolCallError,
)
from bharat_gate.core.gateway.cache import GatewayCache, compute_cache_key
from bharat_gate.core.gateway.parser import (
    check_response_truncation,
    extract_think_and_clean_text,
    parse_responses_output_items,
)
from bharat_gate.core.gateway.queue import RequestQueue
from bharat_gate.core.gateway.telemetry import TelemetryLedger
from bharat_gate.core.schemas import (
    BharatCodeModelType,
    GatewayResponse,
    HealthCheckResult,
    ResponsesInputItem,
    TelemetryRecord,
    TokenUsage,
    ToolDefinition,
)


class BharatCodeClient:
    """Stateless client for BharatCode /v1/responses and /v1/chat/completions APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: BharatCodeModelType = "qwen-3.8-27b",
        cache_db_path: str | Path | None = None,
        queue: RequestQueue | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        resolved_key = api_key or os.getenv("BHARATCODE_API_KEY")
        if not resolved_key:
            raise BharatCodeConfigError("BHARATCODE_API_KEY is not set.")
        self.api_key = resolved_key

        self.base_url = (base_url or os.getenv("BHARATCODE_BASE_URL") or "https://bharatcode.ai/api/model/v1").rstrip("/")
        self.default_model: BharatCodeModelType = default_model or os.getenv("BHARATCODE_MODEL") or "qwen-3.8-27b"

        if cache_db_path is not None:
            db_path = Path(cache_db_path)
            if db_path.is_dir() or not db_path.suffix:
                db_path = db_path / "gateway.sqlite3"
        else:
            cache_dir = os.getenv("BHARATCODE_CACHE_DIR", ".bharat_gate_cache")
            db_path = Path(cache_dir) / "gateway.sqlite3"

        self.cache = GatewayCache(db_path=db_path)
        self.telemetry = TelemetryLedger(db_path=db_path)
        self.queue = queue or RequestQueue()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        self._http = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers=headers,
        )

    def create_responses(
        self,
        input_items: str | list[ResponsesInputItem | dict[str, Any]],
        instructions: str | None = None,
        tools: list[ToolDefinition | dict[str, Any]] | None = None,
        model: BharatCodeModelType | None = None,
        disable_reasoning: bool = False,
        max_output_tokens: int = 4096,
        use_cache: bool = True,
        on_retry: Callable[[int, float, int | None, str | None], None] | None = None,
    ) -> GatewayResponse:
        """Executes a request against the BharatCode /v1/responses endpoint."""
        target_model = model or self.default_model

        if isinstance(input_items, str):
            serialized_input: str | list[dict[str, Any]] = input_items
        else:
            serialized_input = [
                item.model_dump() if isinstance(item, BaseModel) else item for item in input_items
            ]

        payload: dict[str, Any] = {
            "model": target_model,
            "input": serialized_input,
            "store": False,
            "max_output_tokens": max_output_tokens,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = [
                t.model_dump() if isinstance(t, BaseModel) else t for t in tools
            ]
            payload["tool_choice"] = "auto"
        if disable_reasoning:
            payload["reasoning"] = {"effort": "none"}

        cache_key = compute_cache_key({"endpoint": "/responses", **payload})

        if use_cache:
            cached_res = self.cache.get(cache_key)
            if cached_res is not None:
                self.telemetry.record(
                    TelemetryRecord(
                        request_id=cached_res.request_id,
                        endpoint="/responses",
                        model=target_model,
                        status="cached",
                        http_status=200,
                        latency_ms=0.0,
                        retry_count=0,
                        cache_hit=True,
                        input_tokens=cached_res.usage.input_tokens,
                        output_tokens=cached_res.usage.output_tokens,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                )
                return cached_res

        url = f"{self.base_url}/responses"
        start_time = time.perf_counter()

        def _op() -> httpx.Response:
            return self._http.post(url, json=payload)

        http_res, retry_count = self.queue.execute_with_retry(_op, on_retry=on_retry)
        latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        request_id = http_res.headers.get("x-request-id")

        data = http_res.json()
        output_items = data.get("output") or []
        raw_text, function_calls = parse_responses_output_items(output_items)
        parsed_text = extract_think_and_clean_text(raw_text)
        trunc = check_response_truncation(data, "/responses", parsed_text.has_unclosed_think_tag)

        usage_dict = data.get("usage") or {}
        in_tok = usage_dict.get("input_tokens", 0)
        out_tok = usage_dict.get("output_tokens", 0)
        total_tok = usage_dict.get("total_tokens", in_tok + out_tok)
        usage = TokenUsage(input_tokens=in_tok, output_tokens=out_tok, total_tokens=total_tok)

        status_label = "incomplete" if trunc.is_truncated else "completed"
        self.telemetry.record(
            TelemetryRecord(
                request_id=request_id,
                endpoint="/responses",
                model=target_model,
                status=status_label,
                http_status=http_res.status_code,
                latency_ms=latency_ms,
                retry_count=retry_count,
                cache_hit=False,
                input_tokens=in_tok,
                output_tokens=out_tok,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        if trunc.is_truncated and len(function_calls) > 0:
            raise TruncatedToolCallError(
                f"Model output truncated ({trunc.reason}) while emitting function_call; refusing to execute incomplete tool call.",
                request_id=request_id,
                reason=trunc.reason,
                partial_text=parsed_text.clean_text,
            )

        gateway_res = GatewayResponse(
            request_id=request_id,
            model=target_model,
            endpoint="/responses",
            clean_text=parsed_text.clean_text,
            reasoning_trace=parsed_text.reasoning_trace,
            function_calls=function_calls,
            raw_output_items=output_items,
            is_truncated=trunc.is_truncated,
            truncation_reason=trunc.reason,
            usage=usage,
            latency_ms=latency_ms,
            retry_count=retry_count,
            cache_hit=False,
        )

        if use_cache and not trunc.is_truncated:
            self.cache.set(cache_key, gateway_res)

        return gateway_res

    def create_chat_completion(
        self,
        messages: list[dict[str, str]],
        model: BharatCodeModelType | None = None,
        disable_reasoning: bool = False,
        max_tokens: int = 2048,
        use_cache: bool = True,
    ) -> GatewayResponse:
        """Executes a request against the standard OpenAI-compatible /v1/chat/completions endpoint."""
        target_model = model or self.default_model

        payload: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if disable_reasoning:
            payload["reasoning_effort"] = "none"

        cache_key = compute_cache_key({"endpoint": "/chat/completions", **payload})

        if use_cache:
            cached_res = self.cache.get(cache_key)
            if cached_res is not None:
                self.telemetry.record(
                    TelemetryRecord(
                        request_id=cached_res.request_id,
                        endpoint="/chat/completions",
                        model=target_model,
                        status="cached",
                        http_status=200,
                        latency_ms=0.0,
                        retry_count=0,
                        cache_hit=True,
                        input_tokens=cached_res.usage.input_tokens,
                        output_tokens=cached_res.usage.output_tokens,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                )
                return cached_res

        url = f"{self.base_url}/chat/completions"
        start_time = time.perf_counter()

        def _op() -> httpx.Response:
            return self._http.post(url, json=payload)

        http_res, retry_count = self.queue.execute_with_retry(_op)
        latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        request_id = http_res.headers.get("x-request-id")

        data = http_res.json()
        choices = data.get("choices") or []
        raw_text = ""
        if choices and isinstance(choices, list):
            msg = choices[0].get("message") or {}
            raw_text = msg.get("content") or ""

        parsed_text = extract_think_and_clean_text(raw_text)
        trunc = check_response_truncation(data, "/chat/completions", parsed_text.has_unclosed_think_tag)

        usage_dict = data.get("usage") or {}
        in_tok = usage_dict.get("prompt_tokens", 0)
        out_tok = usage_dict.get("completion_tokens", 0)
        total_tok = usage_dict.get("total_tokens", in_tok + out_tok)
        usage = TokenUsage(input_tokens=in_tok, output_tokens=out_tok, total_tokens=total_tok)

        status_label = "incomplete" if trunc.is_truncated else "completed"
        self.telemetry.record(
            TelemetryRecord(
                request_id=request_id,
                endpoint="/chat/completions",
                model=target_model,
                status=status_label,
                http_status=http_res.status_code,
                latency_ms=latency_ms,
                retry_count=retry_count,
                cache_hit=False,
                input_tokens=in_tok,
                output_tokens=out_tok,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        gateway_res = GatewayResponse(
            request_id=request_id,
            model=target_model,
            endpoint="/chat/completions",
            clean_text=parsed_text.clean_text,
            reasoning_trace=parsed_text.reasoning_trace,
            function_calls=[],
            raw_output_items=choices,
            is_truncated=trunc.is_truncated,
            truncation_reason=trunc.reason,
            usage=usage,
            latency_ms=latency_ms,
            retry_count=retry_count,
            cache_hit=False,
        )

        if use_cache and not trunc.is_truncated:
            self.cache.set(cache_key, gateway_res)

        return gateway_res

    def ping_health_check(self) -> HealthCheckResult:
        """Pings the /v1/responses endpoint with a minimal query to verify connectivity and authentication."""
        start_time = time.perf_counter()
        try:
            res = self.create_responses(
                input_items="Reply with the exact single word OK.",
                disable_reasoning=True,
                max_output_tokens=32,
                use_cache=False,
            )
            return HealthCheckResult(
                ok=True,
                http_status=200,
                request_id=res.request_id,
                latency_ms=res.latency_ms,
                model=res.model,
                reply_text=res.clean_text,
                error=None,
            )
        except BharatCodeAPIError as e:
            latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return HealthCheckResult(
                ok=False,
                http_status=e.status_code or 500,
                request_id=e.request_id,
                latency_ms=latency_ms,
                model=self.default_model,
                reply_text="",
                error=str(e),
            )
        except BharatGateError as e:
            latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return HealthCheckResult(
                ok=False,
                http_status=500,
                request_id=None,
                latency_ms=latency_ms,
                model=self.default_model,
                reply_text="",
                error=str(e),
            )
        except Exception as e:
            latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return HealthCheckResult(
                ok=False,
                http_status=500,
                request_id=None,
                latency_ms=latency_ms,
                model=self.default_model,
                reply_text="",
                error=str(e),
            )

    def close(self) -> None:
        """Closes the HTTP client and SQLite databases."""
        self._http.close()
        self.cache.close()
        self.telemetry.close()

    def __enter__(self) -> "BharatCodeClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
