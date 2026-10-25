"""Pre-baked multi-hop replay scenarios for BharatGate demo, offline replay, and benchmarks."""

from pydantic import BaseModel, ConfigDict

from bharat_gate.core.schemas import (
    AuditFinding,
    AuditVerdict,
    BlastRadiusReport,
    CallerReference,
    HarnessRunResult,
    RedactedSecretMatch,
    SecretScanResult,
    SymbolDefinition,
    ToolExecutionTrace,
    TriageDecision,
)


class PrebakedScenario(BaseModel):
    """Represents a prebaked PR verification scenario for 0ms demo exploration and benchmarks."""

    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    subtitle: str
    sample_files: dict[str, str]  # Relative file path -> full source code
    raw_diff: str  # Unified git diff to audit
    recorded_result: HarnessRunResult


# ============================================================================
# SCENARIO 1: Shared-GPU Inference Proxy: Aborted SSE Stream Leak
# ============================================================================

_SCENARIO_1_SAMPLE_FILES = {
    "proxy/stream_worker.py": '''"""GPU Worker Proxy Module for streaming inference."""
import time
from typing import Generator


class GPUSlotPool:
    """Simulated GPU slot allocator with concurrency limit."""

    def __init__(self, capacity: int = 4) -> None:
        self.capacity = capacity
        self.allocated = 0

    def acquire(self) -> str:
        if self.allocated >= self.capacity:
            raise RuntimeError("All GPU slots saturated (HTTP 503)")
        self.allocated += 1
        return f"gpu_slot_{self.allocated}"

    def release(self, slot_id: str) -> None:
        if self.allocated > 0:
            self.allocated -= 1


gpu_pool = GPUSlotPool(capacity=4)


def stream_model_response(prompt: str) -> Generator[str, None, None]:
    """Streams inference tokens from the GPU slot."""
    slot = gpu_pool.acquire()
    # Bug in modified code: try/finally was removed!
    for chunk in ["Bharat", "Code", "Build", "League"]:
        time.sleep(0.01)
        yield chunk
    gpu_pool.release(slot)
''',
    "proxy/routes.py": '''"""Inference route handlers."""
from proxy.stream_worker import stream_model_response


def handle_inference_request(req: dict) -> dict:
    """Handles client POST /v1/chat/completions with stream=True."""
    prompt = req.get("prompt", "")
    stream = stream_model_response(prompt)
    return {"stream": list(stream)}
''',
}

_SCENARIO_1_RAW_DIFF = """diff --git a/proxy/stream_worker.py b/proxy/stream_worker.py
--- a/proxy/stream_worker.py
+++ b/proxy/stream_worker.py
@@ -26,9 +26,6 @@
 def stream_model_response(prompt: str) -> Generator[str, None, None]:
     \"\"\"Streams inference tokens from the GPU slot.\"\"\"
     slot = gpu_pool.acquire()
-    try:
-        for chunk in ["Bharat", "Code", "Build", "League"]:
-            time.sleep(0.01)
-            yield chunk
-    finally:
-        gpu_pool.release(slot)
+    for chunk in ["Bharat", "Code", "Build", "League"]:
+        time.sleep(0.01)
+        yield chunk
+    gpu_pool.release(slot)
"""

_SCENARIO_1_TEST_CODE = '''"""Synthesized regression test suite for sse-gpu-leak."""
import pytest
from proxy.stream_worker import GPUSlotPool, stream_model_response, gpu_pool


def test_gpu_slot_released_on_generator_close():
    """Asserts that closing an active stream releases acquired GPU memory slot."""
    initial_allocated = gpu_pool.allocated
    gen = stream_model_response("Test prompt")
    _ = next(gen)
    # Simulate client disconnect mid-SSE stream
    gen.close()
    assert gpu_pool.allocated == initial_allocated, (
        f"GPU slot leaked! Allocated: {gpu_pool.allocated}, Expected: {initial_allocated}"
    )


def test_gpu_saturation_backoff():
    """Asserts that saturated GPU pool raises clear capacity error."""
    pool = GPUSlotPool(capacity=1)
    s1 = pool.acquire()
    with pytest.raises(RuntimeError, match="saturated"):
        pool.acquire()
    pool.release(s1)
'''

SCENARIO_1 = PrebakedScenario(
    id="sse-gpu-leak",
    title="Shared-GPU Inference Proxy: Aborted SSE Stream Leak & Missing HTTP 503 Backoff",
    subtitle="Autonomous discovery of leaked GPU slots on client disconnect & unhandled server overload",
    sample_files=_SCENARIO_1_SAMPLE_FILES,
    raw_diff=_SCENARIO_1_RAW_DIFF,
    recorded_result=HarnessRunResult(
        verdict=AuditVerdict(
            verdict="BLOCK",
            risk_score=92,
            summary=(
                "Critical concurrency defect in stream_model_response(): removing try/finally "
                "leaks GPU slots upon client SSE disconnect or generator abort, leading to "
                "permanent resource exhaustion. Missing HTTP 503 retry backoff under saturation."
            ),
            findings=[
                AuditFinding(
                    id="LEAK-01",
                    severity="CRITICAL",
                    category="CONCURRENCY",
                    file_path="proxy/stream_worker.py",
                    start_line=26,
                    end_line=34,
                    title="GPU Slot Leak on Premature Stream Disconnect",
                    explanation=(
                        "stream_model_response() acquires a hardware slot from GPUSlotPool but "
                        "omits try/finally cleanup. If an SSE client disconnects mid-stream or raises "
                        "GeneratorExit, gpu_pool.release(slot) is bypassed, permanently leaking GPU memory."
                    ),
                    suggested_patch="""--- a/proxy/stream_worker.py
+++ b/proxy/stream_worker.py
@@ -28,5 +28,8 @@
     slot = gpu_pool.acquire()
+    try:
         for chunk in ["Bharat", "Code", "Build", "League"]:
             time.sleep(0.01)
             yield chunk
+    finally:
+        gpu_pool.release(slot)
""",
                ),
                AuditFinding(
                    id="BACKOFF-02",
                    severity="HIGH",
                    category="BUG",
                    file_path="proxy/stream_worker.py",
                    start_line=13,
                    end_line=17,
                    title="Missing Exponential Backoff on GPU Saturation",
                    explanation=(
                        "When GPUSlotPool capacity is exceeded, RuntimeError is raised immediately "
                        "without retry-after headers or exponential jitter backoff, failing requests instead of queueing."
                    ),
                    suggested_patch="",
                ),
            ],
        ),
        triage=TriageDecision(
            needs_deep_audit=True,
            risk_level="CRITICAL",
            rationale="Concurrency modification to GPU stream allocator impacting routes.py callers.",
            focus_files=["proxy/stream_worker.py", "proxy/routes.py"],
        ),
        blast_radius=BlastRadiusReport(
            modified_symbols=[
                SymbolDefinition(
                    name="stream_model_response",
                    kind="function",
                    file_path="proxy/stream_worker.py",
                    start_line=26,
                    end_line=34,
                    signature="def stream_model_response(prompt: str) -> Generator[str, None, None]:",
                )
            ],
            impacted_callers=[
                CallerReference(
                    symbol_name="stream_model_response",
                    caller_file="proxy/routes.py",
                    caller_line=8,
                    caller_scope="handle_inference_request",
                    call_snippet="stream = stream_model_response(prompt)",
                )
            ],
            impacted_files=["proxy/stream_worker.py", "proxy/routes.py"],
            risk_level="CRITICAL",
        ),
        secret_scan=SecretScanResult(
            sanitized_diff_text=_SCENARIO_1_RAW_DIFF,
            findings=[],
            has_secrets=False,
        ),
        tool_traces=[
            ToolExecutionTrace(
                step_index=1,
                call_id="call_sse_01",
                tool_name="find_symbol_callers",
                arguments={"symbol_name": "stream_model_response"},
                output_preview="Found 1 caller: proxy/routes.py:8 (handle_inference_request)",
                duration_ms=4.2,
                is_error=False,
            ),
            ToolExecutionTrace(
                step_index=2,
                call_id="call_sse_02",
                tool_name="read_file_lines",
                arguments={"file_path": "proxy/routes.py", "start_line": 1, "end_line": 15},
                output_preview="1: from proxy.stream_worker import stream_model_response\n... 8: stream = stream_model_response(prompt)",
                duration_ms=3.1,
                is_error=False,
            ),
        ],
        reasoning_traces=[
            (
                "The diff removes try/finally in stream_model_response in proxy/stream_worker.py. "
                "Because this is a generator yielding chunks, any consumer disconnect will trigger GeneratorExit. "
                "Without the finally block, gpu_pool.release(slot) is never executed. Let me check callers with find_symbol_callers."
            ),
            (
                "proxy/routes.py consumes stream_model_response via list(stream) or direct SSE chunks. "
                "In an SSE gateway, aborts are common. This change creates a severe resource leak under load. "
                "I will emit a BLOCK verdict with risk score 92 and synthesize a regression test."
            ),
        ],
        request_ids=["req_bc_9f81a201", "req_bc_9f81a209"],
        total_latency_ms=7.3,
        total_tokens=680,
        cache_hits=0,
        synthesized_test_code=_SCENARIO_1_TEST_CODE,
    ),
)


# ============================================================================
# SCENARIO 2: Silent 100x Undercharge: Breaking Currency Unit Contract
# ============================================================================

_SCENARIO_2_SAMPLE_FILES = {
    "payments/processor.py": '''"""Payment processing engine."""


def capture_payment(order_id: str, amount_rupees: float) -> dict:
    """Captures payment from customer.

    Expects amount in INR Rupees. Multiplies by 100 internally for payment gateway paise transmission.
    """
    paise = int(amount_rupees * 100)
    return {"order_id": order_id, "amount_paise": paise, "status": "captured"}
''',
    "webhooks/razorpay_handler.py": '''"""Razorpay webhook event listener."""
from payments.processor import capture_payment


def handle_razorpay_webhook(event: dict) -> dict:
    """Processes incoming payment.captured webhook payload."""
    order_id = event["payload"]["order"]["id"]
    # Razorpay amount is ALWAYS delivered in integer paise (e.g. 50000 paise = Rs 500)
    raw_amount = event["payload"]["order"]["amount"]
    return capture_payment(order_id, raw_amount)
''',
    "billing/invoice_service.py": '''"""Automated invoice reconciliation service."""
from payments.processor import capture_payment


def settle_invoice(invoice: dict) -> dict:
    """Settles invoice balance stored in system paise."""
    balance_paise = invoice["balance_paise"]
    return capture_payment(invoice["id"], balance_paise)
''',
}

_SCENARIO_2_RAW_DIFF = """diff --git a/payments/processor.py b/payments/processor.py
--- a/payments/processor.py
+++ b/payments/processor.py
@@ -1,5 +1,6 @@
-def capture_payment(order_id: str, amount_paise: int) -> dict:
-    return {"order_id": order_id, "amount_paise": amount_paise, "status": "captured"}
+def capture_payment(order_id: str, amount_rupees: float) -> dict:
+    paise = int(amount_rupees * 100)
+    return {"order_id": order_id, "amount_paise": paise, "status": "captured"}
"""

_SCENARIO_2_TEST_CODE = '''"""Synthesized regression test suite for webhook-paise-bug."""
import pytest
from payments.processor import capture_payment
from webhooks.razorpay_handler import handle_razorpay_webhook
from billing.invoice_service import settle_invoice


def test_razorpay_webhook_unit_contract():
    """Asserts that 50000 paise (Rs 500) does not get multiplied to 5,000,000 paise."""
    event = {"payload": {"order": {"id": "ord_123", "amount": 50000}}}
    res = handle_razorpay_webhook(event)
    # If bug is present, amount_paise will be 5,000,000 paise (Rs 50,000) instead of 50,000 paise
    assert res["amount_paise"] == 50000, (
        f"Critical currency discrepancy! Expected 50000 paise, got {res['amount_paise']} paise"
    )


def test_invoice_settlement_unit_contract():
    """Asserts that settle_invoice passes paise without inflation."""
    invoice = {"id": "inv_99", "balance_paise": 10000}
    res = settle_invoice(invoice)
    assert res["amount_paise"] == 10000
'''

SCENARIO_2 = PrebakedScenario(
    id="webhook-paise-bug",
    title="Silent 100x Undercharge: Breaking Currency Unit Contract Across Webhook Callers",
    subtitle="Multi-caller AST static analysis flagging breaking rupee-to-paise contract conversion",
    sample_files=_SCENARIO_2_SAMPLE_FILES,
    raw_diff=_SCENARIO_2_RAW_DIFF,
    recorded_result=HarnessRunResult(
        verdict=AuditVerdict(
            verdict="BLOCK",
            risk_score=88,
            summary=(
                "Breaking parameter unit change in capture_payment() (paise to rupees). "
                "Callers in webhooks/razorpay_handler.py and billing/invoice_service.py still pass "
                "integer paise amounts, causing an unintended 100x financial overcharge."
            ),
            findings=[
                AuditFinding(
                    id="BREAK-01",
                    severity="HIGH",
                    category="BREAKING_CALLER",
                    file_path="payments/processor.py",
                    start_line=1,
                    end_line=5,
                    title="Currency Unit Contract Inversion Breaking Webhook Callers",
                    explanation=(
                        "capture_payment changed parameter from amount_paise to amount_rupees and added "
                        "internal '* 100' multiplication. Existing callers in webhooks/razorpay_handler.py and "
                        "billing/invoice_service.py pass raw paise amounts, resulting in a 100x overcharge."
                    ),
                    suggested_patch="""--- a/payments/processor.py
+++ b/payments/processor.py
@@ -1,4 +1,3 @@
-def capture_payment(order_id: str, amount_rupees: float) -> dict:
-    paise = int(amount_rupees * 100)
+def capture_payment(order_id: str, amount_paise: int) -> dict:
     return {"order_id": order_id, "amount_paise": amount_paise, "status": "captured"}
""",
                )
            ],
        ),
        triage=TriageDecision(
            needs_deep_audit=True,
            risk_level="HIGH",
            rationale="Modified financial transaction signature with cross-file webhook callers.",
            focus_files=["payments/processor.py", "webhooks/razorpay_handler.py", "billing/invoice_service.py"],
        ),
        blast_radius=BlastRadiusReport(
            modified_symbols=[
                SymbolDefinition(
                    name="capture_payment",
                    kind="function",
                    file_path="payments/processor.py",
                    start_line=1,
                    end_line=5,
                    signature="def capture_payment(order_id: str, amount_rupees: float) -> dict:",
                )
            ],
            impacted_callers=[
                CallerReference(
                    symbol_name="capture_payment",
                    caller_file="webhooks/razorpay_handler.py",
                    caller_line=10,
                    caller_scope="handle_razorpay_webhook",
                    call_snippet="return capture_payment(order_id, raw_amount)",
                ),
                CallerReference(
                    symbol_name="capture_payment",
                    caller_file="billing/invoice_service.py",
                    caller_line=8,
                    caller_scope="settle_invoice",
                    call_snippet="return capture_payment(invoice['id'], balance_paise)",
                ),
            ],
            impacted_files=["payments/processor.py", "webhooks/razorpay_handler.py", "billing/invoice_service.py"],
            risk_level="HIGH",
        ),
        secret_scan=SecretScanResult(
            sanitized_diff_text=_SCENARIO_2_RAW_DIFF,
            findings=[],
            has_secrets=False,
        ),
        tool_traces=[
            ToolExecutionTrace(
                step_index=1,
                call_id="call_pay_01",
                tool_name="find_symbol_callers",
                arguments={"symbol_name": "capture_payment"},
                output_preview="Found 2 callers: webhooks/razorpay_handler.py:10, billing/invoice_service.py:8",
                duration_ms=5.4,
                is_error=False,
            ),
            ToolExecutionTrace(
                step_index=2,
                call_id="call_pay_02",
                tool_name="read_file_lines",
                arguments={"file_path": "webhooks/razorpay_handler.py", "start_line": 1, "end_line": 15},
                output_preview="1: from payments.processor import capture_payment\n... 10: return capture_payment(order_id, raw_amount)",
                duration_ms=2.8,
                is_error=False,
            ),
        ],
        reasoning_traces=[
            (
                "The diff changes capture_payment signature from amount_paise to amount_rupees. "
                "Let me trace all cross-file invocations using find_symbol_callers."
            ),
            (
                "Identified callers in razorpay_handler.py and invoice_service.py. Razorpay webhooks send "
                "amounts in integer paise. Multiplying by 100 inside capture_payment inflates the charges 100x. "
                "Emitting a BLOCK verdict with BREAKING_CALLER finding."
            ),
        ],
        request_ids=["req_bc_41c0021b", "req_bc_41c00224"],
        total_latency_ms=8.2,
        total_tokens=540,
        cache_hits=0,
        synthesized_test_code=_SCENARIO_2_TEST_CODE,
    ),
)


# ============================================================================
# SCENARIO 3: Pre-API Secret Redaction + SQL Injection in Multi-Tenant Auth
# ============================================================================

_SCENARIO_3_SAMPLE_FILES = {
    "auth/middleware.py": '''"""Multi-tenant authentication middleware."""
import os
from auth.db import execute_auth_query

BHARATCODE_API_KEY = "bc_live_9876543210abcdef1234567890abcdef"


def authenticate_tenant(token: str) -> dict:
    """Authenticates tenant token against database."""
    # Vulnerable to SQL injection: f-string formatting
    query = f"SELECT * FROM tenants WHERE token = \'{token}\'"
    return execute_auth_query(query)
''',
    "auth/db.py": '''"""Database execution helper."""
import sqlite3


def execute_auth_query(query: str, params: tuple = ()) -> dict:
    """Executes authentication query safely."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    cursor.execute(query, params)
    row = cursor.fetchone()
    conn.close()
    return {"row": row}
''',
}

_SCENARIO_3_RAW_DIFF = """diff --git a/auth/middleware.py b/auth/middleware.py
--- a/auth/middleware.py
+++ b/auth/middleware.py
@@ -1,6 +1,9 @@
 import os
 from auth.db import execute_auth_query
+BHARATCODE_API_KEY = "bc_live_9876543210abcdef1234567890abcdef"
+
 def authenticate_tenant(token: str) -> dict:
-    return execute_auth_query("SELECT * FROM tenants WHERE token = ?", (token,))
+    query = f"SELECT * FROM tenants WHERE token = '{token}'"
+    return execute_auth_query(query)
"""

_SCENARIO_3_TEST_CODE = '''"""Synthesized regression test suite for auth-sqli-secret."""
import os
import pytest
from auth.middleware import authenticate_tenant


def test_sqli_payload_fails_to_bypass_tenant_isolation():
    """Asserts that SQL injection payloads do not execute as raw SQL."""
    payload = "' OR '1'='1"
    # Parameterized queries will look for token matching literal "' OR '1'='1", returning None
    res = authenticate_tenant(payload)
    assert res.get("row") is None, "SQL Injection vulnerable! Unauthorized tenant accessed."


def test_secret_loaded_from_environment():
    """Asserts that API key is loaded dynamically from env rather than hardcoded."""
    from auth import middleware
    assert not hasattr(middleware, "BHARATCODE_API_KEY") or not middleware.BHARATCODE_API_KEY.startswith("bc_live_"), (
        "Hardcoded live API key found in module namespace!"
    )
'''

SCENARIO_3 = PrebakedScenario(
    id="auth-sqli-secret",
    title="Pre-API Secret Redaction + SQL Injection in Multi-Tenant Auth Middleware",
    subtitle="Deterministic Stage 0 secret masking preventing API key leak + SQL injection detection",
    sample_files=_SCENARIO_3_SAMPLE_FILES,
    raw_diff=_SCENARIO_3_RAW_DIFF,
    recorded_result=HarnessRunResult(
        verdict=AuditVerdict(
            verdict="BLOCK",
            risk_score=98,
            summary=(
                "Critical security violations detected: hardcoded live BharatCode API key committed in diff, "
                "and authenticate_tenant() introduced raw f-string SQL injection bypassing multi-tenant isolation."
            ),
            findings=[
                AuditFinding(
                    id="SEC-1",
                    severity="CRITICAL",
                    category="SECURITY",
                    file_path="auth/middleware.py",
                    start_line=3,
                    end_line=3,
                    title="Exposed Secret Detected (BHARATCODE_KEY)",
                    explanation=(
                        "Detected credential matching rule 'BHARATCODE_KEY' with Shannon entropy 4.27. "
                        "Redacted snippet: +BHARATCODE_API_KEY = '<REDACTED:BHARATCODE_KEY>'."
                    ),
                    suggested_patch="""--- a/auth/middleware.py
+++ b/auth/middleware.py
@@ -3,1 +3,1 @@
-BHARATCODE_API_KEY = "bc_live_9876543210abcdef1234567890abcdef"
+BHARATCODE_API_KEY = os.getenv("BHARATCODE_API_KEY", "")
""",
                ),
                AuditFinding(
                    id="SQLI-01",
                    severity="CRITICAL",
                    category="SECURITY",
                    file_path="auth/middleware.py",
                    start_line=7,
                    end_line=8,
                    title="SQL Injection via Unsanitized Token Formatting",
                    explanation=(
                        "authenticate_tenant constructs raw SQL using an f-string: f\"SELECT * FROM tenants WHERE token = '{token}'\". "
                        "An attacker can pass \"' OR '1'='1\" to bypass tenant boundaries and dump arbitrary records."
                    ),
                    suggested_patch="""--- a/auth/middleware.py
+++ b/auth/middleware.py
@@ -7,2 +7,1 @@
-    query = f"SELECT * FROM tenants WHERE token = '{token}'"
-    return execute_auth_query(query)
+    return execute_auth_query("SELECT * FROM tenants WHERE token = ?", (token,))
""",
                ),
            ],
        ),
        triage=TriageDecision(
            needs_deep_audit=True,
            risk_level="CRITICAL",
            rationale="Critical credential leak and database auth query modifications detected.",
            focus_files=["auth/middleware.py", "auth/db.py"],
        ),
        blast_radius=BlastRadiusReport(
            modified_symbols=[
                SymbolDefinition(
                    name="authenticate_tenant",
                    kind="function",
                    file_path="auth/middleware.py",
                    start_line=5,
                    end_line=9,
                    signature="def authenticate_tenant(token: str) -> dict:",
                )
            ],
            impacted_callers=[],
            impacted_files=["auth/middleware.py"],
            risk_level="CRITICAL",
        ),
        secret_scan=SecretScanResult(
            sanitized_diff_text="""diff --git a/auth/middleware.py b/auth/middleware.py
--- a/auth/middleware.py
+++ b/auth/middleware.py
@@ -1,6 +1,9 @@
 import os
 from auth.db import execute_auth_query
+BHARATCODE_API_KEY = "<REDACTED:BHARATCODE_KEY>"
+
 def authenticate_tenant(token: str) -> dict:
-    return execute_auth_query("SELECT * FROM tenants WHERE token = ?", (token,))
+    query = f"SELECT * FROM tenants WHERE token = '{token}'"
+    return execute_auth_query(query)
""",
            findings=[
                RedactedSecretMatch(
                    rule_id="BHARATCODE_KEY",
                    file_path="auth/middleware.py",
                    line_number=3,
                    redacted_snippet='+BHARATCODE_API_KEY = "<REDACTED:BHARATCODE_KEY>"',
                    entropy=4.27,
                )
            ],
            has_secrets=True,
        ),
        tool_traces=[
            ToolExecutionTrace(
                step_index=1,
                call_id="call_sec_01",
                tool_name="read_file_lines",
                arguments={"file_path": "auth/db.py", "start_line": 1, "end_line": 15},
                output_preview="1: import sqlite3\n... 8: cursor.execute(query, params)",
                duration_ms=2.3,
                is_error=False,
            )
        ],
        reasoning_traces=[
            (
                "Stage 0 detected and redacted BHARATCODE_KEY on line 3. Now analyzing code changes in auth/middleware.py. "
                "Line 7 replaces parameterized SQL execution with string interpolation f'SELECT * FROM tenants WHERE token = \\'{token}\\''. "
                "This introduces critical SQL injection. Emitting BLOCK verdict."
            )
        ],
        request_ids=["req_bc_77a90102"],
        total_latency_ms=4.5,
        total_tokens=420,
        cache_hits=0,
        synthesized_test_code=_SCENARIO_3_TEST_CODE,
    ),
)


# ============================================================================
# Lookup Functions
# ============================================================================

def get_all_scenarios() -> list[PrebakedScenario]:
    """Returns the list of all 3 pre-baked engineering scenarios."""
    return [SCENARIO_1, SCENARIO_2, SCENARIO_3]


def get_scenario_by_id(scenario_id: str) -> PrebakedScenario:
    """Retrieves a scenario by its canonical ID or numerical index ('1', '2', '3').

    Raises KeyError if the scenario is not found.
    """
    cleaned = str(scenario_id).strip().lower()

    alias_map = {
        "1": "sse-gpu-leak",
        "2": "webhook-paise-bug",
        "3": "auth-sqli-secret",
    }
    target_id = alias_map.get(cleaned, cleaned)

    for s in get_all_scenarios():
        if s.id.lower() == target_id:
            return s

    available = ", ".join(f"{s.id} ({i+1})" for i, s in enumerate(get_all_scenarios()))
    raise KeyError(f"Scenario '{scenario_id}' not found. Available scenarios: {available}")
