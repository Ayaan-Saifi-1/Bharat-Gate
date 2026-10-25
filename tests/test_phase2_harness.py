import ast
import json
from pathlib import Path
import httpx
import pytest

from bharat_gate.core.gateway.cache import GatewayCache
from bharat_gate.core.gateway.client import BharatCodeClient
from bharat_gate.core.gateway.queue import RequestQueue
from bharat_gate.core.gateway.telemetry import TelemetryLedger
from bharat_gate.core.graph.blast_radius import (
    build_blast_radius_report,
    extract_python_symbols,
)
from bharat_gate.core.graph.diff_parser import parse_unified_diff
from bharat_gate.core.graph.secret_scanner import scan_and_redact_secrets
from bharat_gate.core.graph.tools import RepositorySandboxTools
from bharat_gate.core.harness.orchestrator import BharatGateOrchestrator
from bharat_gate.core.harness.patch_builder import synthesize_regression_pytest
from bharat_gate.core.schemas import (
    AuditFinding,
    AuditVerdict,
    BlastRadiusReport,
    FunctionCallItem,
)


def test_diff_parser_tracks_line_numbers_and_hashes() -> None:
    """Verifies parse_unified_diff accurately computes old_lineno, new_lineno, added_count, deleted_count, and deterministic diff_hash."""
    diff_text = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 x = 1
-y = 2
+y = 20
+z = 30
 w = 4
"""
    parsed = parse_unified_diff(diff_text)
    assert len(parsed.files) == 1
    f = parsed.files[0]
    assert f.old_path == "app.py"
    assert f.new_path == "app.py"
    assert f.change_type == "modified"
    assert f.added_count == 2
    assert f.deleted_count == 1
    assert parsed.total_added == 2
    assert parsed.total_deleted == 1
    assert len(parsed.diff_hash) == 64

    # Verify line numbers in hunk
    hunk = f.hunks[0]
    add_lines = [l for l in hunk.lines if l.line_type == "add"]
    del_lines = [l for l in hunk.lines if l.line_type == "delete"]
    ctx_lines = [l for l in hunk.lines if l.line_type == "context"]

    assert len(add_lines) == 2
    assert add_lines[0].new_lineno == 2
    assert add_lines[1].new_lineno == 3

    assert len(del_lines) == 1
    assert del_lines[0].old_lineno == 2

    assert len(ctx_lines) == 2
    assert ctx_lines[0].old_lineno == 1 and ctx_lines[0].new_lineno == 1
    assert ctx_lines[1].old_lineno == 3 and ctx_lines[1].new_lineno == 4


def test_secret_scanner_redacts_api_keys_and_high_entropy() -> None:
    """Verifies scan_and_redact_secrets redacts BHARATCODE_API_KEY, AKIA..., and sk-... while leaving normal code untouched."""
    raw_diff = """diff --git a/credentials.py b/credentials.py
--- a/credentials.py
+++ b/credentials.py
@@ -1,3 +1,7 @@
 import os
+BHARATCODE_API_KEY = "bc_live_9876543210abcdef12345"
+AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
+OPENAI_KEY = "sk-live-1234567890abcdef12345678"
+UNTOUCHED_VAR = "normal_config_value"
 def test():
     pass
"""
    scan = scan_and_redact_secrets(raw_diff)
    assert scan.has_secrets is True
    assert "<REDACTED:BHARATCODE_KEY>" in scan.sanitized_diff_text
    assert "<REDACTED:AWS_ACCESS_KEY>" in scan.sanitized_diff_text
    assert "<REDACTED:API_SECRET_KEY>" in scan.sanitized_diff_text
    assert "UNTOUCHED_VAR = \"normal_config_value\"" in scan.sanitized_diff_text
    assert len(scan.findings) == 3


def test_ast_extract_python_symbols() -> None:
    """Verifies extract_python_symbols finds functions, async functions, classes, and methods with accurate start/end lines and signatures."""
    source = """
def calculate_tax(income: float, rate: float = 0.18) -> float:
    return income * rate

async def fetch_profile(user_id: int):
    return {"id": user_id}

class AccountManager:
    def __init__(self, owner: str):
        self.owner = owner

    def close_account(self) -> bool:
        return True
"""
    symbols = extract_python_symbols(source, "accounts.py")
    assert len(symbols) == 5
    sym_by_name = {s.name: s for s in symbols}

    assert sym_by_name["calculate_tax"].kind == "function"
    assert "def calculate_tax" in sym_by_name["calculate_tax"].signature

    assert sym_by_name["fetch_profile"].kind == "async_function"
    assert "async def fetch_profile" in sym_by_name["fetch_profile"].signature

    assert sym_by_name["AccountManager"].kind == "class"
    assert "class AccountManager" in sym_by_name["AccountManager"].signature

    assert sym_by_name["close_account"].kind == "method"
    assert sym_by_name["close_account"].start_line > sym_by_name["AccountManager"].start_line


def test_ast_blast_radius_finds_cross_file_callers(tmp_path: Path) -> None:
    """Creates a temporary repo in tmp_path (billing.py defining charge_card(amount_paise) and webhook.py calling charge_card(100)), runs build_blast_radius_report on a diff modifying charge_card, and asserts webhook.py is detected in impacted_callers."""
    billing_file = tmp_path / "billing.py"
    billing_file.write_text(
        "def charge_card(amount_paise: int) -> bool:\n    return True\n",
        encoding="utf-8",
    )
    webhook_file = tmp_path / "webhook.py"
    webhook_file.write_text(
        "from billing import charge_card\n\ndef process_event():\n    return charge_card(100)\n",
        encoding="utf-8",
    )

    diff_text = """diff --git a/billing.py b/billing.py
--- a/billing.py
+++ b/billing.py
@@ -1,2 +1,2 @@
-def charge_card(amount_paise: int) -> bool:
+def charge_card(amount_rupees: float) -> bool:
     return True
"""
    parsed = parse_unified_diff(diff_text)
    report = build_blast_radius_report(parsed, tmp_path)

    assert len(report.modified_symbols) == 1
    assert report.modified_symbols[0].name == "charge_card"
    caller_files = [c.caller_file for c in report.impacted_callers]
    assert "webhook.py" in caller_files
    assert "webhook.py" in report.impacted_files


def test_sandbox_tools_block_path_traversal_and_dotenv(tmp_path: Path) -> None:
    """Asserts _resolve_safe_path('../../etc/passwd') and _resolve_safe_path('.env') are blocked and execute_tool_call returns an error JSON with trace.is_error is True."""
    tools = RepositorySandboxTools(tmp_path)

    with pytest.raises(ValueError):
        tools._resolve_safe_path("../../etc/passwd")

    with pytest.raises(ValueError):
        tools._resolve_safe_path(".env")

    with pytest.raises(ValueError):
        tools._resolve_safe_path(".env.local")

    out_item, trace = tools.execute_tool_call(
        FunctionCallItem(
            call_id="call_block",
            name="read_file_lines",
            arguments='{"file_path": "../../etc/passwd", "start_line": 1, "end_line": 10}',
        )
    )
    assert trace.is_error is True
    err_json = json.loads(out_item.output)
    assert "error" in err_json
    assert "blocked" in err_json["error"].lower() or "traversal" in err_json["error"].lower()


def test_sandbox_tool_read_file_lines_and_symbols(tmp_path: Path) -> None:
    """Verifies read_file_lines and get_file_symbols_and_imports return accurate numbered lines and AST signatures."""
    test_file = tmp_path / "service.py"
    test_file.write_text(
        "import os\nfrom typing import Any\ndef run_service():\n    return 'running'\n",
        encoding="utf-8",
    )

    tools = RepositorySandboxTools(tmp_path)

    res_lines = tools.read_file_lines("service.py", 1, 3)
    assert res_lines["start_line"] == 1
    assert res_lines["end_line"] == 3
    assert "1: import os" in res_lines["lines"][0]
    assert "2: from typing import Any" in res_lines["lines"][1]
    assert "3: def run_service():" in res_lines["lines"][2]

    res_symbols = tools.get_file_symbols_and_imports("service.py")
    assert "import os" in res_symbols["imports"]
    assert any("from typing import Any" in i for i in res_symbols["imports"])
    assert len(res_symbols["symbols"]) == 1
    assert res_symbols["symbols"][0]["name"] == "run_service"


def test_sandbox_tool_validate_unified_patch_checks_ast_syntax(tmp_path: Path) -> None:
    """Verifies valid Python replacement passes and broken syntax (def foo(:) returns syntax_valid=False."""
    target_file = tmp_path / "logic.py"
    target_file.write_text("def validate():\n    return 1\n", encoding="utf-8")

    tools = RepositorySandboxTools(tmp_path)

    # Valid patch
    res_ok = tools.validate_unified_patch("logic.py", "return 1", "return 42")
    assert res_ok["is_applicable"] is True
    assert res_ok["syntax_valid"] is True

    # Broken syntax patch
    res_bad = tools.validate_unified_patch("logic.py", "return 1", "def broken(:")
    assert res_bad["is_applicable"] is True
    assert res_bad["syntax_valid"] is False
    assert "SyntaxError" in res_bad["reason"]


def test_orchestrator_zero_token_fast_pass_on_docs_only(tmp_path: Path) -> None:
    """Verifies a diff touching only README.md (with no secrets) returns verdict='PASS' with 0 HTTP calls."""
    def error_transport(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No network call should be made on documentation fast pass!")

    client = BharatCodeClient(
        api_key="mock-key",
        cache_db_path=tmp_path / "gateway.sqlite3",
        transport=httpx.MockTransport(error_transport),
    )
    orch = BharatGateOrchestrator(client=client, repo_root=tmp_path)

    diff = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 # Documentation
+Added new installation guide.
"""
    result = orch.run_audit(diff)
    assert result.verdict.verdict == "PASS"
    assert result.verdict.risk_score == 0
    assert result.total_tokens == 0
    assert len(result.request_ids) == 0
    client.close()


def test_orchestrator_stateless_multi_turn_tool_call_replay(tmp_path: Path) -> None:
    """Uses httpx.MockTransport where Turn 1 returns a function_call (call_id='call_99', name='find_symbol_callers') and Turn 2 inspects the request payload sent by BharatGateOrchestrator, asserting body['store'] is False, FunctionCallItem(call_id='call_99'), and FunctionCallOutputItem(call_id='call_99') are both present in body['input'], before returning the final AuditVerdict JSON."""
    (tmp_path / "core.py").write_text("def ping():\n    return 'pong'\n", encoding="utf-8")

    turn = 0
    def mock_server(request: httpx.Request) -> httpx.Response:
        nonlocal turn
        turn += 1
        body = json.loads(request.content.decode("utf-8"))
        assert body["store"] is False

        if turn == 1:
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_99",
                            "name": "find_symbol_callers",
                            "arguments": '{"symbol_name": "ping"}',
                        }
                    ],
                    "usage": {"input_tokens": 80, "output_tokens": 40, "total_tokens": 120},
                },
                headers={"x-request-id": "req-hop-1"},
            )
        else:
            inputs = body["input"]
            assert any(i.get("type") == "function_call" and i.get("call_id") == "call_99" for i in inputs)
            assert any(i.get("type") == "function_call_output" and i.get("call_id") == "call_99" for i in inputs)

            verdict_payload = {
                "verdict": "PASS",
                "risk_score": 5,
                "summary": "Verified with callers successfully",
                "findings": [],
            }
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [{"type": "message", "content": json.dumps(verdict_payload)}],
                    "usage": {"input_tokens": 120, "output_tokens": 50, "total_tokens": 170},
                },
                headers={"x-request-id": "req-hop-2"},
            )

    client = BharatCodeClient(
        api_key="mock-key",
        cache_db_path=tmp_path / "gateway.sqlite3",
        transport=httpx.MockTransport(mock_server),
    )
    orch = BharatGateOrchestrator(client=client, repo_root=tmp_path)

    diff = """diff --git a/core.py b/core.py
--- a/core.py
+++ b/core.py
@@ -1,2 +1,2 @@
-def ping():
+def ping() -> str:
     return 'pong'
"""
    result = orch.run_audit(diff)
    assert result.verdict.verdict == "PASS"
    assert len(result.tool_traces) == 1
    assert result.tool_traces[0].call_id == "call_99"
    assert result.total_tokens == 290
    client.close()


def test_orchestrator_self_repairs_malformed_json(tmp_path: Path) -> None:
    """Mocks Turn 1 returning invalid JSON ({'verdict': 'INVALID_ENUM', 'risk_score': 999}) and Turn 2 returning valid AuditVerdict JSON, asserting the repair call uses reasoning={'effort': 'none'} and resolves cleanly."""
    (tmp_path / "file.py").write_text("x = 1\n", encoding="utf-8")
    turn = 0

    def mock_server(request: httpx.Request) -> httpx.Response:
        nonlocal turn
        turn += 1
        body = json.loads(request.content.decode("utf-8"))

        if turn == 1:
            # Turn 1: Invalid verdict JSON
            bad_json = '{"verdict": "INVALID_ENUM", "risk_score": 999}'
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [{"type": "message", "content": bad_json}],
                    "usage": {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70},
                },
                headers={"x-request-id": "req-bad-json"},
            )
        else:
            # Turn 2: Self-repair call
            assert body.get("reasoning") == {"effort": "none"}
            valid_payload = {
                "verdict": "WARN",
                "risk_score": 40,
                "summary": "Repaired JSON output",
                "findings": [],
            }
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [{"type": "message", "content": json.dumps(valid_payload)}],
                    "usage": {"input_tokens": 60, "output_tokens": 30, "total_tokens": 90},
                },
                headers={"x-request-id": "req-repaired"},
            )

    client = BharatCodeClient(
        api_key="mock-key",
        cache_db_path=tmp_path / "gateway.sqlite3",
        transport=httpx.MockTransport(mock_server),
    )
    orch = BharatGateOrchestrator(client=client, repo_root=tmp_path)

    diff = """diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,1 +1,1 @@
-x = 1
+x = 2
"""
    result = orch.run_audit(diff)
    assert result.verdict.verdict == "WARN"
    assert result.verdict.risk_score == 40
    assert result.verdict.summary == "Repaired JSON output"
    client.close()


def test_orchestrator_recovers_from_truncated_tool_call(tmp_path: Path) -> None:
    """Mocks Turn 1 returning status='incomplete' with a function_call, asserting the orchestrator catches TruncatedToolCallError and retries with disable_reasoning=True."""
    (tmp_path / "file.py").write_text("x = 1\n", encoding="utf-8")
    turn = 0

    def mock_server(request: httpx.Request) -> httpx.Response:
        nonlocal turn
        turn += 1
        body = json.loads(request.content.decode("utf-8"))

        if turn == 1:
            # Turn 1: Truncated response containing function_call
            return httpx.Response(
                200,
                json={
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_trunc",
                            "name": "search_repo_text",
                            "arguments": '{"query": "x',
                        }
                    ],
                    "usage": {"input_tokens": 50, "output_tokens": 4096, "total_tokens": 4146},
                },
                headers={"x-request-id": "req-trunc-tool"},
            )
        else:
            # Turn 2: Fallback retry with disable_reasoning=True
            assert body.get("reasoning") == {"effort": "none"}
            assert body.get("max_output_tokens") == 8192
            valid_payload = {
                "verdict": "PASS",
                "risk_score": 0,
                "summary": "Recovered from truncation",
                "findings": [],
            }
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [{"type": "message", "content": json.dumps(valid_payload)}],
                    "usage": {"input_tokens": 60, "output_tokens": 30, "total_tokens": 90},
                },
                headers={"x-request-id": "req-trunc-recovered"},
            )

    client = BharatCodeClient(
        api_key="mock-key",
        cache_db_path=tmp_path / "gateway.sqlite3",
        transport=httpx.MockTransport(mock_server),
    )
    orch = BharatGateOrchestrator(client=client, repo_root=tmp_path)

    diff = """diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,1 +1,1 @@
-x = 1
+x = 2
"""
    result = orch.run_audit(diff)
    assert result.verdict.verdict == "PASS"
    assert result.verdict.summary == "Recovered from truncation"
    client.close()


def test_orchestrator_injects_critical_secret_finding_and_blocks(tmp_path: Path) -> None:
    """Verifies that even if the LLM returns verdict='PASS', a leaked secret caught by secret_scanner forces verdict='BLOCK' and prepends a CRITICAL SECURITY finding."""
    (tmp_path / "file.py").write_text("x = 1\n", encoding="utf-8")

    def mock_server(request: httpx.Request) -> httpx.Response:
        verdict_payload = {
            "verdict": "PASS",
            "risk_score": 0,
            "summary": "LLM says pass",
            "findings": [],
        }
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"type": "message", "content": json.dumps(verdict_payload)}],
                "usage": {"input_tokens": 50, "output_tokens": 30, "total_tokens": 80},
            },
            headers={"x-request-id": "req-secret-leak"},
        )

    client = BharatCodeClient(
        api_key="mock-key",
        cache_db_path=tmp_path / "gateway.sqlite3",
        transport=httpx.MockTransport(mock_server),
    )
    orch = BharatGateOrchestrator(client=client, repo_root=tmp_path)

    diff = """diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,1 +1,2 @@
 x = 1
+BHARATCODE_API_KEY = "bc_live_9876543210abcdef12345"
"""
    result = orch.run_audit(diff)
    assert result.verdict.verdict == "BLOCK"
    assert result.verdict.risk_score >= 85
    assert len(result.verdict.findings) == 1
    assert result.verdict.findings[0].id == "SEC-1"
    assert result.verdict.findings[0].severity == "CRITICAL"
    assert result.verdict.findings[0].category == "SECURITY"
    client.close()


def test_synthesize_regression_pytest_produces_valid_python_ast() -> None:
    """Asserts ast.parse(result.synthesized_test_code) succeeds with zero syntax errors."""
    verdict = AuditVerdict(
        verdict="BLOCK",
        risk_score=95,
        summary="Security and regression findings",
        findings=[
            AuditFinding(
                id="SEC-1",
                severity="CRITICAL",
                category="SECURITY",
                file_path="config.py",
                start_line=5,
                end_line=5,
                title="API Key Leaked",
                explanation="Hardcoded secret in diff",
            ),
            AuditFinding(
                id="BG-102",
                severity="HIGH",
                category="BREAKING_CALLER",
                file_path="billing.py",
                start_line=12,
                end_line=15,
                title="Signature change breaks callers",
                explanation="Added mandatory parameter",
            ),
        ],
    )
    blast = BlastRadiusReport(
        modified_symbols=[],
        impacted_callers=[],
        impacted_files=["config.py", "billing.py"],
        risk_level="CRITICAL",
    )
    code = synthesize_regression_pytest(verdict, blast)
    parsed_ast = ast.parse(code)
    assert parsed_ast is not None
    assert "test_finding_sec_1_0" in code
    assert "test_finding_bg_102_1" in code


def test_full_phase1_and_phase2_suite_compatibility(tmp_path: Path) -> None:
    """Verifies TelemetryLedger and GatewayCache work seamlessly across multi-hop orchestrator runs."""
    db_path = tmp_path / "gateway.sqlite3"
    ledger = TelemetryLedger(db_path=db_path)
    cache = GatewayCache(db_path=db_path)

    queue = RequestQueue(max_retries=2, sleep_fn=lambda _: None)
    client = BharatCodeClient(
        api_key="mock-key",
        cache_db_path=db_path,
        queue=queue,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [{"type": "message", "content": '{"verdict": "PASS", "risk_score": 0, "summary": "ok", "findings": []}'}],
                    "usage": {"input_tokens": 40, "output_tokens": 20, "total_tokens": 60},
                },
                headers={"x-request-id": "req-compat"},
            )
        ),
    )
    orch = BharatGateOrchestrator(client=client, repo_root=tmp_path)

    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,1 +1,1 @@
-x = 1
+x = 99
"""
    res = orch.run_audit(diff)
    assert res.verdict.verdict == "PASS"

    summary = ledger.get_summary(cache.get_stats())
    assert summary.total_requests >= 1
    assert "req-compat" in summary.recent_request_ids

    client.close()
    ledger.close()
    cache.close()
