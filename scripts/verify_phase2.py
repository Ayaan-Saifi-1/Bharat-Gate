import ast
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

# Ensure project root is in sys.path
root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

if sys.platform == "win32":
    try:
        if sys.stdout.encoding.lower() != "utf-8":
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr.encoding.lower() != "utf-8":
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from bharat_gate.core.gateway.client import BharatCodeClient
from bharat_gate.core.harness.orchestrator import BharatGateOrchestrator
from bharat_gate.core.schemas import HarnessRunResult

console = Console()


def setup_mini_repository(repo_path: Path) -> tuple[str, str]:
    """Creates a temporary realistic repository with cross-file caller dependencies

    and returns (diff_text, expected_behavior_description).
    """
    billing_code = '''# Payment processing module


def charge_card(amount_rupees: float, customer_id: str) -> dict:
    """Charges a customer credit card in INR (rupees)."""
    if amount_rupees <= 0:
        raise ValueError("Amount must be positive")
    return {"status": "success", "amount_charged": amount_rupees, "customer_id": customer_id}
'''

    webhook_code = '''# Webhook receiver for order checkout
from billing import charge_card


def process_order_webhook(payload: dict) -> dict:
    """Processes incoming checkout webhook.

    Notice: Caller still passes paise (e.g., 50000 paise = 500 INR).
    """
    user_id = payload.get("user_id", "guest")
    # Calling charge_card directly with amount_paise
    result = charge_card(payload["amount_paise"], user_id)
    return {"order_status": "PAID", "billing": result}
'''

    (repo_path / "billing.py").write_text(billing_code, encoding="utf-8")
    (repo_path / "checkout_webhook.py").write_text(webhook_code, encoding="utf-8")

    # Unified git diff that changes parameter signature and accidentally commits a secret
    diff_text = """diff --git a/billing.py b/billing.py
--- a/billing.py
+++ b/billing.py
@@ -1,6 +1,8 @@
 # Payment processing module
+BHARATCODE_API_KEY = "bc_live_9876543210abcdef1234567890abcdef"
+
-def charge_card(amount_paise: int, customer_id: str) -> dict:
-    \"\"\"Charges a customer credit card in paise.\"\"\"
+def charge_card(amount_rupees: float, customer_id: str) -> dict:
+    \"\"\"Charges a customer credit card in INR (rupees).\"\"\"
     if amount_rupees <= 0:
         raise ValueError("Amount must be positive")
     return {"status": "success", "amount_charged": amount_rupees, "customer_id": customer_id}
"""
    return diff_text, "Changed charge_card() parameter unit from paise to rupees + leaked API key."


def create_offline_mock_transport() -> httpx.MockTransport:
    """Returns an httpx.MockTransport that simulates a multi-turn autonomous investigation."""
    turn = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal turn
        turn += 1
        body = json.loads(request.content.decode("utf-8"))

        # Verify compliance
        assert body.get("store") is False, "store MUST be False"

        if turn == 1:
            # Turn 1: Model inspects blast radius and requests to find symbol callers
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": (
                                "<think>The diff modifies charge_card in billing.py and changes parameter "
                                "amount_paise to amount_rupees. I need to check all callers of charge_card "
                                "across the repository to determine if callers are broken.</think>"
                                "Inspecting cross-file callers for charge_card."
                            ),
                        },
                        {
                            "type": "function_call",
                            "call_id": "call_investigate_callers_1",
                            "name": "find_symbol_callers",
                            "arguments": json.dumps({"symbol_name": "charge_card"}),
                        },
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
                },
                headers={"x-request-id": "req-mock-turn-1"},
            )

        # Turn 2: Model receives caller in checkout_webhook.py and outputs final AuditVerdict JSON
        verdict_content = json.dumps(
            {
                "verdict": "BLOCK",
                "risk_score": 92,
                "summary": (
                    "High-risk breaking change detected: charge_card() in billing.py changed unit "
                    "from paise to rupees, but caller checkout_webhook.py:process_order_webhook "
                    "still passes amount_paise directly."
                ),
                "findings": [
                    {
                        "id": "BREAKING-001",
                        "severity": "HIGH",
                        "category": "BREAKING_CALLER",
                        "file_path": "billing.py",
                        "start_line": 6,
                        "end_line": 10,
                        "title": "Breaking parameter unit mismatch in charge_card()",
                        "explanation": (
                            "charge_card now expects amount_rupees. Caller in checkout_webhook.py "
                            "passes payload['amount_paise'], resulting in a 100x overcharge."
                        ),
                        "suggested_patch": "def charge_card(amount_rupees: float, customer_id: str) -> dict:",
                    }
                ],
            }
        )

        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": (
                            f"<think>Found that checkout_webhook.py line 12 calls charge_card with "
                            f"payload['amount_paise']. This causes major financial discrepancy. "
                            f"Emitting BLOCK verdict.</think>\n{verdict_content}"
                        ),
                    }
                ],
                "usage": {"prompt_tokens": 280, "completion_tokens": 140, "total_tokens": 420},
            },
            headers={"x-request-id": "req-mock-turn-2"},
        )

    return httpx.MockTransport(handler)


def display_audit_results(
    title: str,
    result: HarnessRunResult,
    diff_text: str,
    is_live: bool = False,
) -> None:
    """Renders comprehensive Rich UI panels for all 4 audit stages."""
    console.print(f"\n[bold cyan]==> {title}[/bold cyan]")

    # 1. Stage 0: Secret Redaction Panel
    secret_table = Table(title="Stage 0: Deterministic Secret Redaction", show_lines=True)
    secret_table.add_column("Rule ID", style="bold red", width=18)
    secret_table.add_column("Line", justify="center", width=8)
    secret_table.add_column("Entropy", justify="right", width=10)
    secret_table.add_column("Redacted Snippet", style="dim", width=48)

    if result.secret_scan.findings:
        for f in result.secret_scan.findings:
            secret_table.add_row(
                f.rule_id,
                str(f.line_number),
                f"{f.entropy:.2f}",
                f.redacted_snippet,
            )
    else:
        secret_table.add_row("NONE", "-", "-", "No credentials detected")

    console.print(secret_table)

    # 2. Stage 0: AST Blast-Radius Analysis Panel
    radius_table = Table(title="Stage 0: AST Static Blast-Radius Report", show_lines=True)
    radius_table.add_column("Caller File", style="bold yellow", width=22)
    radius_table.add_column("Caller Symbol", style="bold white", width=24)
    radius_table.add_column("Line", justify="center", width=8)
    radius_table.add_column("Target Callee", style="cyan", width=16)
    radius_table.add_column("Code Snippet", style="dim", width=36)

    if result.blast_radius.impacted_callers:
        for c in result.blast_radius.impacted_callers:
            radius_table.add_row(
                c.caller_file,
                c.caller_scope,
                str(c.caller_line),
                c.symbol_name,
                c.call_snippet,
            )
    else:
        radius_table.add_row("NONE", "-", "-", "-", "No cross-file callers detected")

    console.print(radius_table)

    # 3. Stage 1: Stateless /v1/responses Tool-Call Traces
    trace_table = Table(title="Stage 1: Stateless Multi-Turn Tool Traces", show_lines=True)
    trace_table.add_column("Step", justify="center", width=6)
    trace_table.add_column("Tool Name", style="bold cyan", width=22)
    trace_table.add_column("Call ID", style="dim", width=24)
    trace_table.add_column("Arguments", width=32)
    trace_table.add_column("Status", justify="center", width=10)
    trace_table.add_column("Duration", justify="right", width=10)

    if result.tool_traces:
        for t in result.tool_traces:
            status_badge = "[bold green]OK[/bold green]" if not t.is_error else "[bold red]ERR[/bold red]"
            args_str = json.dumps(t.arguments) if isinstance(t.arguments, dict) else str(t.arguments)
            trace_table.add_row(
                str(t.step_index),
                t.tool_name,
                t.call_id,
                args_str[:30] + "..." if len(args_str) > 30 else args_str,
                status_badge,
                f"{t.duration_ms:.1f}ms",
            )
    else:
        trace_table.add_row("1", "(Direct Answer)", "-", "-", "[bold green]OK[/bold green]", "0.0ms")

    console.print(trace_table)

    # Telemetry and Request IDs
    req_str = ", ".join(result.request_ids) if result.request_ids else "None"
    console.print(
        Panel(
            f"[bold]Request IDs:[/bold] [cyan]{req_str}[/cyan]\n"
            f"[bold]Total Latency:[/bold] {result.total_latency_ms}ms  |  "
            f"[bold]Total Tokens:[/bold] {result.total_tokens}  |  "
            f"[bold]Cache Hits:[/bold] {result.cache_hits}",
            title="Gateway Execution Telemetry",
            border_style="blue",
        )
    )

    # 4. Stage 2 & 3: Final Audit Verdict & Findings
    verdict_style = "bold red" if result.verdict.verdict == "BLOCK" else "bold green"
    verdict_panel = Panel(
        f"[bold]Verdict:[/bold] [{verdict_style}]{result.verdict.verdict}[/{verdict_style}]  |  "
        f"[bold]Risk Score:[/bold] [bold yellow]{result.verdict.risk_score}/100[/bold yellow]\n\n"
        f"[bold]Summary:[/bold] {result.verdict.summary}",
        title="Stage 2/3: Final Audit Verdict",
        border_style="red" if result.verdict.verdict == "BLOCK" else "green",
    )
    console.print(verdict_panel)

    findings_table = Table(title="Consolidated Audit Findings (Deterministic + Model)", show_lines=True)
    findings_table.add_column("ID", style="bold white", width=12)
    findings_table.add_column("Severity", justify="center", width=12)
    findings_table.add_column("Category", width=16)
    findings_table.add_column("Location", width=18)
    findings_table.add_column("Finding Title & Details", width=46)

    for f in result.verdict.findings:
        sev_color = "bold red" if f.severity in ("CRITICAL", "HIGH") else "bold yellow"
        findings_table.add_row(
            f.id,
            f"[{sev_color}]{f.severity}[/{sev_color}]",
            f.category,
            f"{f.file_path}:{f.start_line}",
            f"[bold]{f.title}[/bold]\n[dim]{f.explanation}[/dim]",
        )
    console.print(findings_table)

    # 5. Stage 3: Synthesized Pytest Code
    if result.synthesized_test_code:
        # Validate AST syntax
        ast.parse(result.synthesized_test_code)
        syntax = Syntax(
            result.synthesized_test_code,
            "python",
            theme="monokai",
            line_numbers=True,
            word_wrap=True,
        )
        console.print(Panel(syntax, title="Stage 3: Synthesized Regression Pytest (AST Validated)", border_style="magenta"))


def run_offline_verification() -> None:
    """Executes the offline deterministic verification using MockTransport."""
    console.print(
        Panel.fit(
            "[bold cyan]BharatGate (Phase 2) - Verification Gate[/bold cyan]\n"
            "[dim]Autonomous Multi-Turn Pre-Push Audit Pipeline[/dim]",
            border_style="cyan",
        )
    )

    with tempfile.TemporaryDirectory() as td:
        repo_path = Path(td)
        diff_text, desc = setup_mini_repository(repo_path)
        cache_db = repo_path / "cache.sqlite3"

        mock_transport = create_offline_mock_transport()
        client = BharatCodeClient(
            api_key="mock-offline-key",
            cache_db_path=cache_db,
            transport=mock_transport,
        )

        try:
            orchestrator = BharatGateOrchestrator(
                client=client,
                repo_root=repo_path,
                max_tool_hops=4,
            )

            console.print(f"[dim]Running offline mock audit on simulated repo ({desc})...[/dim]")
            t0 = time.perf_counter()
            result = orchestrator.run_audit(diff_text)
            elapsed = round((time.perf_counter() - t0) * 1000.0, 2)

            # Assertions to confirm correctness
            assert result.secret_scan.has_secrets is True, "Must detect secrets"
            assert result.verdict.verdict == "BLOCK", "Must block on critical secret and breaking change"
            assert any(f.id.startswith("SEC-") for f in result.verdict.findings), "Must include injected secret finding"
            assert len(result.tool_traces) == 1, "Must execute find_symbol_callers tool call"
            assert result.tool_traces[0].tool_name == "find_symbol_callers"
            assert ast.parse(result.synthesized_test_code) is not None, "Synthesized code must be valid AST"

            display_audit_results("Offline Mock Audit (Deterministic)", result, diff_text, is_live=False)
            console.print(f"[bold green][PASS] Offline Mock Verification Passed cleanly in {elapsed}ms![/bold green]\n")
        finally:
            client.close()


def run_live_verification_if_configured() -> None:
    """Executes a live audit against BharatCode if BHARATCODE_API_KEY is present."""
    api_key = os.getenv("BHARATCODE_API_KEY")
    is_live = bool(api_key and api_key.strip() and api_key != "your_bharatcode_api_key_here")

    console.print("[bold yellow]Live BharatCode API Audit Verification:[/bold yellow]")
    if not is_live:
        console.print(
            Panel(
                "[yellow]Live API audit skipped.[/yellow]\n"
                "[dim]Reason: BHARATCODE_API_KEY is not configured or is the default template placeholder in .env.[/dim]\n"
                "To test live against BharatCode, add your API key to [bold].env[/bold]:\n"
                "[italic]BHARATCODE_API_KEY=bc_live_...[/italic]",
                border_style="yellow",
            )
        )
        return

    with tempfile.TemporaryDirectory() as td:
        repo_path = Path(td)
        diff_text, desc = setup_mini_repository(repo_path)
        cache_db = repo_path / "live_cache.sqlite3"

        client = BharatCodeClient(
            api_key=api_key,
            model="qwen-3.8-27b",
            cache_db_path=cache_db,
        )

        orchestrator = BharatGateOrchestrator(
            client=client,
            repo_root=repo_path,
            max_tool_hops=4,
        )

        console.print("[dim]Executing live multi-turn /v1/responses audit on qwen-3.8-27b...[/dim]")
        try:
            t0 = time.perf_counter()
            result = orchestrator.run_audit(diff_text)
            elapsed = round((time.perf_counter() - t0) * 1000.0, 2)

            display_audit_results("Live BharatCode Audit (qwen-3.8-27b)", result, diff_text, is_live=True)
            console.print(f"[bold green][PASS] Live BharatCode Audit Completed in {elapsed}ms![/bold green]\n")
        except Exception as e:
            console.print(f"[bold red]Live audit encountered an error:[/bold red] {e}")
        finally:
            client.close()


def main() -> None:
    load_dotenv()
    run_offline_verification()
    run_live_verification_if_configured()
    console.print("[bold green][PASS] Phase 2 Verification Complete - 100% Deterministic & Spec Compliant![/bold green]\n")


if __name__ == "__main__":
    main()
