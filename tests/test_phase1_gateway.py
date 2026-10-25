import json
from pathlib import Path
import tempfile
import httpx
import pytest

from bharat_gate.core.exceptions import (
    BharatCodeAPIError,
    TruncatedToolCallError,
)
from bharat_gate.core.gateway.cache import GatewayCache, compute_cache_key
from bharat_gate.core.gateway.client import BharatCodeClient
from bharat_gate.core.gateway.parser import (
    check_response_truncation,
    extract_think_and_clean_text,
    validate_json_with_schema,
)
from bharat_gate.core.gateway.queue import RequestQueue
from bharat_gate.core.gateway.telemetry import TelemetryLedger
from bharat_gate.core.schemas import (
    AuditVerdict,
    GatewayResponse,
    TelemetryRecord,
    TokenUsage,
)


def test_extract_closed_think_tags() -> None:
    """Extracts single and multiple <think>...</think> blocks and returns clean text."""
    # Single closed think block
    text_single = "<think>Calculating steps...</think>Final clean result."
    res_single = extract_think_and_clean_text(text_single)
    assert res_single.clean_text == "Final clean result."
    assert res_single.reasoning_trace == "Calculating steps..."
    assert not res_single.has_unclosed_think_tag

    # Multiple closed think blocks
    text_multi = "<think>Part 1</think>Interim text.<think>Part 2</think>Conclusion."
    res_multi = extract_think_and_clean_text(text_multi)
    assert res_multi.clean_text == "Interim text.Conclusion."
    assert res_multi.reasoning_trace == "Part 1\n\n---\n\nPart 2"
    assert not res_multi.has_unclosed_think_tag


def test_extract_unclosed_think_tag_on_truncation() -> None:
    """Sets has_unclosed_think_tag=True when </think> is missing."""
    text_unclosed = "Answer starting: <think>Reasoning interrupted before closing tag"
    res = extract_think_and_clean_text(text_unclosed)
    assert res.has_unclosed_think_tag is True
    assert res.clean_text == "Answer starting:"
    assert res.reasoning_trace == "Reasoning interrupted before closing tag"


def test_truncation_detection_responses_and_chat() -> None:
    """Detects status='incomplete' (max_output_tokens) on /responses and finish_reason='length' on /chat/completions."""
    # /responses with status incomplete
    payload_resp_incomplete = {"status": "incomplete"}
    t_resp = check_response_truncation(payload_resp_incomplete, "/responses")
    assert t_resp.is_truncated is True
    assert t_resp.reason == "max_output_tokens"

    # /responses with incomplete_details reason
    payload_resp_details = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    t_resp2 = check_response_truncation(payload_resp_details, "/responses")
    assert t_resp2.is_truncated is True
    assert t_resp2.reason == "max_output_tokens"

    # /chat/completions with finish_reason length
    payload_chat = {"choices": [{"finish_reason": "length"}]}
    t_chat = check_response_truncation(payload_chat, "/chat/completions")
    assert t_chat.is_truncated is True
    assert t_chat.reason == "length"

    # Unclosed think tag flag
    t_unclosed = check_response_truncation({}, "/responses", has_unclosed_think_tag=True)
    assert t_unclosed.is_truncated is True
    assert t_unclosed.reason == "unclosed_think_tag"

    # Normal completed response
    t_ok = check_response_truncation({"status": "completed"}, "/responses")
    assert t_ok.is_truncated is False
    assert t_ok.reason is None


def test_validate_json_with_schema_success_and_fences() -> None:
    """Parses JSON inside ```json fences into AuditVerdict."""
    raw_markdown = """Here is the verified finding:
```json
{
  "verdict": "PASS",
  "risk_score": 10,
  "summary": "Everything passes checks.",
  "findings": []
}
```
Done."""
    res = validate_json_with_schema(raw_markdown, AuditVerdict)
    assert res.is_valid is True
    assert isinstance(res.validated_data, AuditVerdict)
    assert res.validated_data.verdict == "PASS"
    assert res.validated_data.risk_score == 10
    assert res.validated_data.summary == "Everything passes checks."
    assert res.error_summary is None


def test_validate_json_with_schema_error_paths() -> None:
    """Returns clear field-level validation error paths when risk_score > 100 or verdict is invalid."""
    # Invalid score > 100
    bad_score_json = '{"verdict": "PASS", "risk_score": 150, "summary": "Too high"}'
    res_score = validate_json_with_schema(bad_score_json, AuditVerdict)
    assert res_score.is_valid is False
    assert res_score.validated_data is None
    assert "risk_score" in res_score.error_summary

    # Invalid verdict literal
    bad_verdict_json = '{"verdict": "INVALID_VAL", "risk_score": 20, "summary": "Bad verdict"}'
    res_verdict = validate_json_with_schema(bad_verdict_json, AuditVerdict)
    assert res_verdict.is_valid is False
    assert res_verdict.validated_data is None
    assert "verdict" in res_verdict.error_summary


def test_cache_key_order_independence_and_sqlite_hit() -> None:
    """Verifies GatewayCache returns a cache hit regardless of dict key order and increments tokens_saved."""
    payload_a = {"model": "qwen-3.8-27b", "input": [{"role": "user", "content": "hi"}], "store": False}
    payload_b = {"store": False, "input": [{"role": "user", "content": "hi"}], "model": "qwen-3.8-27b"}
    key_a = compute_cache_key(payload_a)
    key_b = compute_cache_key(payload_b)
    assert key_a == key_b

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test_cache.sqlite3"
        cache = GatewayCache(db_path=db_path)

        dummy_response = GatewayResponse(
            request_id="req-test-cache",
            model="qwen-3.8-27b",
            endpoint="/responses",
            clean_text="Cached answer",
            usage=TokenUsage(input_tokens=25, output_tokens=35, total_tokens=60),
        )

        cache.set(key_a, dummy_response)

        # Lookup with key_b (which is identical to key_a)
        hit = cache.get(key_b)
        assert hit is not None
        assert hit.cache_hit is True
        assert hit.clean_text == "Cached answer"
        assert hit.latency_ms == 0.0

        stats = cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 0
        assert stats["tokens_saved"] == 60

        cache.close()


def test_queue_retries_503_then_succeeds() -> None:
    """Mocks two HTTP 503 responses (with x-request-id: req-503) followed by HTTP 200 (x-request-id: req-200) and verifies retry_count == 2 and request_id == 'req-200'."""
    call_count = 0

    def mock_503_then_200() -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return httpx.Response(503, text="Service Unavailable", headers={"x-request-id": "req-503"})
        return httpx.Response(200, text="OK", headers={"x-request-id": "req-200"})

    queue = RequestQueue(max_retries=4, sleep_fn=lambda _: None)
    res, attempt = queue.execute_with_retry(mock_503_then_200)

    assert attempt == 2
    assert res.status_code == 200
    assert res.headers.get("x-request-id") == "req-200"


def test_queue_fails_fast_on_400_and_401() -> None:
    """Mocks HTTP 400 and HTTP 401 and verifies BharatCodeAPIError is raised immediately with 0 retries."""
    queue = RequestQueue(max_retries=4, sleep_fn=lambda _: None)

    # HTTP 400
    calls_400 = 0
    def op_400() -> httpx.Response:
        nonlocal calls_400
        calls_400 += 1
        return httpx.Response(400, text="Bad Request", headers={"x-request-id": "req-400"})

    with pytest.raises(BharatCodeAPIError) as exc_400:
        queue.execute_with_retry(op_400)
    assert exc_400.value.status_code == 400
    assert exc_400.value.request_id == "req-400"
    assert not exc_400.value.retryable
    assert calls_400 == 1

    # HTTP 401
    calls_401 = 0
    def op_401() -> httpx.Response:
        nonlocal calls_401
        calls_401 += 1
        return httpx.Response(401, text="Unauthorized", headers={"x-request-id": "req-401"})

    with pytest.raises(BharatCodeAPIError) as exc_401:
        queue.execute_with_retry(op_401)
    assert exc_401.value.status_code == 401
    assert exc_401.value.request_id == "req-401"
    assert not exc_401.value.retryable
    assert calls_401 == 1


def test_client_responses_enforces_store_false_and_reasoning_none() -> None:
    """Inspects the raw request JSON sent to httpx.MockTransport and asserts body['store'] is False and body['reasoning'] == {'effort': 'none'}."""
    captured_body: dict = {}

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        captured_body = json.loads(request.content.decode("utf-8"))
        payload = {
            "status": "completed",
            "output": [
                {"type": "message", "content": "Clean verified response"}
            ],
            "usage": {"input_tokens": 12, "output_tokens": 18, "total_tokens": 30},
        }
        return httpx.Response(200, json=payload, headers={"x-request-id": "req-compliance"})

    transport = httpx.MockTransport(mock_transport_handler)
    with tempfile.TemporaryDirectory() as td:
        client = BharatCodeClient(
            api_key="mock-key-123",
            cache_db_path=Path(td) / "gateway.sqlite3",
            transport=transport,
        )
        res = client.create_responses(
            input_items="Compliance check",
            disable_reasoning=True,
            use_cache=False,
        )
        assert captured_body.get("store") is False
        assert captured_body.get("reasoning") == {"effort": "none"}
        assert res.clean_text == "Clean verified response"
        assert res.request_id == "req-compliance"
        client.close()


def test_client_raises_truncated_tool_call_error() -> None:
    """Mocks an /responses payload with status='incomplete' AND a function_call item, asserting TruncatedToolCallError is raised and the response is NOT cached."""
    def mock_truncated_handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_truncate_1",
                    "name": "critical_database_tool",
                    "arguments": '{"query": "DROP TABLE users;',
                }
            ],
            "usage": {"input_tokens": 15, "output_tokens": 40, "total_tokens": 55},
        }
        return httpx.Response(200, json=payload, headers={"x-request-id": "req-trunc-guard"})

    transport = httpx.MockTransport(mock_truncated_handler)
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "gateway.sqlite3"
        client = BharatCodeClient(
            api_key="mock-key-123",
            cache_db_path=db_path,
            transport=transport,
        )

        with pytest.raises(TruncatedToolCallError) as exc_info:
            client.create_responses("Execute database query", use_cache=True)

        assert exc_info.value.request_id == "req-trunc-guard"
        assert exc_info.value.reason == "max_output_tokens"

        # Assert response was NOT cached
        stats = client.cache.get_stats()
        assert stats["hits"] == 0

        client.close()


def test_client_parses_valid_function_call_items() -> None:
    """Mocks an /responses payload with status='completed' and a function_call item (call_id='call_abc'), asserting res.function_calls[0].call_id == 'call_abc'."""
    def mock_function_handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_abc",
                    "name": "search_database",
                    "arguments": '{"table": "audit_logs", "limit": 10}',
                }
            ],
            "usage": {"input_tokens": 20, "output_tokens": 25, "total_tokens": 45},
        }
        return httpx.Response(200, json=payload, headers={"x-request-id": "req-fc-parse"})

    transport = httpx.MockTransport(mock_function_handler)
    with tempfile.TemporaryDirectory() as td:
        client = BharatCodeClient(
            api_key="mock-key-123",
            cache_db_path=Path(td) / "gateway.sqlite3",
            transport=transport,
        )
        res = client.create_responses("Run search", use_cache=False)
        assert len(res.function_calls) == 1
        assert res.function_calls[0].call_id == "call_abc"
        assert res.function_calls[0].name == "search_database"
        assert res.function_calls[0].arguments == '{"table": "audit_logs", "limit": 10}'
        client.close()


def test_telemetry_ledger_summary() -> None:
    """Verifies TelemetryLedger.get_summary() accurately reports total calls, cache hit rate, retries, and recent_request_ids."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "gateway.sqlite3"
        ledger = TelemetryLedger(db_path=db_path)

        # Record 1 regular call
        ledger.record(
            TelemetryRecord(
                request_id="req-call-1",
                endpoint="/responses",
                model="qwen-3.8-27b",
                status="completed",
                http_status=200,
                latency_ms=100.0,
                retry_count=1,
                cache_hit=False,
                input_tokens=40,
                output_tokens=20,
                timestamp="2026-10-25T10:00:00Z",
            )
        )

        # Record 2 retried call
        ledger.record(
            TelemetryRecord(
                request_id="req-call-2",
                endpoint="/responses",
                model="qwen-3.8-27b",
                status="completed",
                http_status=200,
                latency_ms=200.0,
                retry_count=2,
                cache_hit=False,
                input_tokens=30,
                output_tokens=10,
                timestamp="2026-10-25T10:01:00Z",
            )
        )

        # Record 3 cached call
        ledger.record(
            TelemetryRecord(
                request_id="req-call-3",
                endpoint="/responses",
                model="qwen-3.8-27b",
                status="cached",
                http_status=200,
                latency_ms=0.0,
                retry_count=0,
                cache_hit=True,
                input_tokens=40,
                output_tokens=20,
                timestamp="2026-10-25T10:02:00Z",
            )
        )

        cache_stats = {"hits": 1, "misses": 2, "tokens_saved": 60}
        summary = ledger.get_summary(cache_stats=cache_stats)

        assert summary.total_requests == 3
        assert summary.cache_hits == 1
        assert summary.cache_misses == 2
        assert summary.cache_hit_rate_pct == 33.33
        assert summary.total_retries == 3
        assert summary.avg_latency_ms == 150.0  # Average of 100.0 and 200.0 across non-cached calls
        assert summary.total_input_tokens == 110
        assert summary.total_output_tokens == 50
        assert summary.estimated_tokens_saved == 60
        assert "req-call-3" in summary.recent_request_ids
        assert "req-call-2" in summary.recent_request_ids
        assert "req-call-1" in summary.recent_request_ids

        ledger.close()
