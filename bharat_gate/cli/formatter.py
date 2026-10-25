"""Rich terminal output formatters for doctor diagnostics, audit verdicts, and benchmarks."""

import io
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from bharat_gate.cli.doctor import DoctorReport
from bharat_gate.core.schemas import HarnessRunResult

if sys.platform == "win32":
    try:
        if sys.stdout.encoding.lower() != "utf-8":
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr.encoding.lower() != "utf-8":
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def render_doctor_report(
    report: DoctorReport,
    console: Console | None = None,
) -> str:
    """Renders the 6-point doctor diagnostic report as a Rich table and returns text."""
    target_console = console or Console()
    rec_console = Console(record=True, file=io.StringIO(), width=105)

    table = Table(title="BharatGate System Diagnostics (--doctor)", show_lines=True)
    table.add_column("Diagnostic Check", style="bold white", width=32)
    table.add_column("Status", justify="center", width=10)
    table.add_column("Details", style="dim", width=38)
    table.add_column("Fix Hint", style="yellow", width=25)

    for c in report.checks:
        status_badge = "[bold green][PASS][/bold green]" if c.passed else "[bold red][FAIL][/bold red]"
        table.add_row(c.name, status_badge, c.detail, c.fix_hint or "-")

    if report.all_passed:
        footer = Panel(
            "[bold green]All system diagnostics passed! Ready for autonomous pre-push audits.[/bold green]",
            border_style="green",
        )
    elif report.local_passed:
        footer = Panel(
            "[bold yellow]Local environment ready. BharatCode API handshake pending or skipped via --no-llm.[/bold yellow]",
            border_style="yellow",
        )
    else:
        footer = Panel(
            "[bold red]Diagnostics detected environment issues. Please resolve the failing checks above.[/bold red]",
            border_style="red",
        )

    # Print to target console
    target_console.print(table)
    target_console.print(footer)

    # Capture recorded text for return
    rec_console.print(table)
    rec_console.print(footer)
    return rec_console.export_text()


def render_audit_result(
    result: HarnessRunResult,
    console: Console | None = None,
    show_thinking: bool = False,
    show_test: bool = False,
) -> str:
    """Renders the complete 4-stage audit result with findings and telemetry."""
    target_console = console or Console()
    rec_console = Console(record=True, file=io.StringIO(), width=105)

    verdict_str = result.verdict.verdict
    if verdict_str == "BLOCK":
        v_color = "bold red"
        border = "red"
    elif verdict_str == "WARN":
        v_color = "bold yellow"
        border = "yellow"
    else:
        v_color = "bold green"
        border = "green"

    # Header Panel
    header_panel = Panel(
        f"[bold]Audit Verdict:[/bold] [{v_color}][{verdict_str}][/{v_color}]  |  "
        f"[bold]Risk Score:[/bold] [bold yellow]{result.verdict.risk_score}/100[/bold yellow]\n\n"
        f"[bold]Summary:[/bold] {result.verdict.summary}",
        title="BharatGate Pre-Push Verification Verdict",
        border_style=border,
    )
    target_console.print(header_panel)
    rec_console.print(header_panel)

    # Stage 0 Static Analysis Metrics Table
    meta_table = Table(title="Stage 0: Deterministic AST & Secret Pre-Pass", show_lines=True)
    meta_table.add_column("Triage Risk", justify="center", style="bold cyan", width=16)
    meta_table.add_column("Modified Symbols", justify="center", width=18)
    meta_table.add_column("Impacted Callers", justify="center", width=18)
    meta_table.add_column("Secrets Redacted", justify="center", style="bold red", width=18)
    meta_table.add_column("Focus Files", style="dim", width=25)

    focus_str = ", ".join(result.triage.focus_files) if result.triage.focus_files else "None"
    meta_table.add_row(
        result.triage.risk_level,
        str(len(result.blast_radius.modified_symbols)),
        str(len(result.blast_radius.impacted_callers)),
        str(len(result.secret_scan.findings)),
        focus_str[:25],
    )
    target_console.print(meta_table)
    rec_console.print(meta_table)

    # Stage 1 Stateless Tool Traces Table
    if result.tool_traces:
        tool_table = Table(title="Stage 1: Stateless /v1/responses Tool-Call Traces", show_lines=True)
        tool_table.add_column("Step", justify="center", width=6)
        tool_table.add_column("Call ID", style="dim", width=22)
        tool_table.add_column("Tool Name", style="bold cyan", width=22)
        tool_table.add_column("Duration", justify="right", width=12)
        tool_table.add_column("Status", justify="center", width=10)
        tool_table.add_column("Output Preview", width=28)

        for t in result.tool_traces:
            status = "[bold green]OK[/bold green]" if not t.is_error else "[bold red]ERROR[/bold red]"
            preview = t.output_preview.replace("\n", " ")[:26] + "..." if len(t.output_preview) > 26 else t.output_preview
            tool_table.add_row(
                str(t.step_index),
                t.call_id,
                t.tool_name,
                f"{t.duration_ms:.1f}ms",
                status,
                preview,
            )
        target_console.print(tool_table)
        rec_console.print(tool_table)

    # Reasoning Traces
    if show_thinking and result.reasoning_traces:
        for idx, trace in enumerate(result.reasoning_traces, 1):
            trace_panel = Panel(
                f"[dim]{trace}[/dim]",
                title=f"Model <think> Reasoning Trace (Turn {idx})",
                border_style="cyan",
            )
            target_console.print(trace_panel)
            rec_console.print(trace_panel)

    # Findings
    if result.verdict.findings:
        findings_table = Table(title="Audit Findings (Deterministic & Autonomous)", show_lines=True)
        findings_table.add_column("ID", style="bold white", width=12)
        findings_table.add_column("Severity", justify="center", width=12)
        findings_table.add_column("Category", width=18)
        findings_table.add_column("Location", width=20)
        findings_table.add_column("Title & Explanation", width=36)

        for f in result.verdict.findings:
            sev_color = "bold red" if f.severity in ("CRITICAL", "HIGH") else "bold yellow"
            loc = f"{f.file_path}:{f.start_line}-{f.end_line}"
            body = f"[bold]{f.title}[/bold]\n[dim]{f.explanation}[/dim]"
            findings_table.add_row(
                f.id,
                f"[{sev_color}]{f.severity}[/{sev_color}]",
                f.category,
                loc,
                body,
            )
        target_console.print(findings_table)
        rec_console.print(findings_table)

        # Print suggested patches if available
        for f in result.verdict.findings:
            if f.suggested_patch.strip():
                patch_syntax = Syntax(f.suggested_patch, "diff", theme="monokai", word_wrap=True)
                patch_panel = Panel(
                    patch_syntax,
                    title=f"Suggested Fix Diff for [{f.id}] ({f.file_path})",
                    border_style="cyan",
                )
                target_console.print(patch_panel)
                rec_console.print(patch_panel)

    # Synthesized Regression Pytest
    if show_test and result.synthesized_test_code.strip():
        test_syntax = Syntax(
            result.synthesized_test_code,
            "python",
            theme="monokai",
            line_numbers=True,
            word_wrap=True,
        )
        test_panel = Panel(
            test_syntax,
            title="Stage 3: Synthesized Regression Pytest (AST Validated)",
            border_style="magenta",
        )
        target_console.print(test_panel)
        rec_console.print(test_panel)

    # Telemetry Footer
    reqs_str = ", ".join(result.request_ids) if result.request_ids else "None"
    telemetry_panel = Panel(
        f"[bold]Request IDs:[/bold] [cyan]{reqs_str}[/cyan]\n"
        f"[bold]Total Latency:[/bold] {result.total_latency_ms:.1f}ms  |  "
        f"[bold]Total Tokens:[/bold] {result.total_tokens}  |  "
        f"[bold]Cache Hits:[/bold] {result.cache_hits}",
        title="GPU Telemetry & Gateway Ledger",
        border_style="blue",
    )
    target_console.print(telemetry_panel)
    rec_console.print(telemetry_panel)

    return rec_console.export_text()


def render_benchmark_report(
    benchmark_data: dict[str, Any],
    console: Console | None = None,
) -> str:
    """Renders the benchmark efficiency table showing token savings."""
    target_console = console or Console()
    rec_console = Console(record=True, file=io.StringIO(), width=105)

    table = Table(title="BharatGate Pre-Push Verification Token Efficiency Benchmark", show_lines=True)
    table.add_column("Workload / Pipeline Phase", style="bold white", width=34)
    table.add_column("Type", style="cyan", width=24)
    table.add_column("GPU Tokens", justify="right", style="bold yellow", width=14)
    table.add_column("Latency", justify="right", width=12)
    table.add_column("Cache Status", justify="center", width=16)

    for w in benchmark_data.get("workloads", []):
        tokens_str = f"{w.get('tokens', 0):,}"
        latency_str = f"{w.get('latency_ms', 0.0):.1f}ms"
        cache_status = w.get("cache_status", "Active")
        table.add_row(
            w.get("name", ""),
            w.get("type", ""),
            tokens_str,
            latency_str,
            cache_status,
        )

    saved_pct = benchmark_data.get("tokens_saved_pct", 0.0)
    baseline = benchmark_data.get("total_tokens_baseline", 0)
    actual = benchmark_data.get("total_tokens_actual", 0)

    summary_panel = Panel(
        f"[bold]Baseline Token Cost (Without BharatGate):[/bold] {baseline:,} tokens\n"
        f"[bold]Actual Token Cost (With BharatGate):[/bold] [bold green]{actual:,} tokens[/bold green]\n\n"
        f"[bold]Total GPU Tokens Saved:[/bold] [bold green]{saved_pct:.1f}%[/bold green] "
        f"[dim](Enabled by Stage 0 Fast-Pass + SQLite SHA-256 Response Cache)[/dim]",
        title="Verification Efficiency Summary",
        border_style="green",
    )

    target_console.print(table)
    target_console.print(summary_panel)

    rec_console.print(table)
    rec_console.print(summary_panel)
    return rec_console.export_text()
