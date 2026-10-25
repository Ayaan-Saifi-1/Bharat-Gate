from pathlib import Path
from typing import Any

from bharat_gate.core.exceptions import (
    SchemaRepairExhaustedError,
    TruncatedToolCallError,
)
from bharat_gate.core.gateway.client import BharatCodeClient
from bharat_gate.core.gateway.parser import validate_json_with_schema
from bharat_gate.core.graph.blast_radius import build_blast_radius_report
from bharat_gate.core.graph.diff_parser import parse_unified_diff
from bharat_gate.core.graph.secret_scanner import scan_and_redact_secrets
from bharat_gate.core.graph.tools import RepositorySandboxTools
from bharat_gate.core.harness.patch_builder import synthesize_regression_pytest
from bharat_gate.core.harness.prompts import (
    INVESTIGATION_SYSTEM_PROMPT,
    JSON_REPAIR_SYSTEM_PROMPT,
    TRIAGE_SYSTEM_PROMPT,
)
from bharat_gate.core.schemas import (
    AuditFinding,
    AuditVerdict,
    HarnessRunResult,
    MessageInputItem,
    ResponsesInputItem,
    ToolExecutionTrace,
    TriageDecision,
)

DOC_EXTENSIONS = {".md", ".txt", ".rst", ".markdown"}


class BharatGateOrchestrator:
    """Orchestrates autonomous, multi-turn pre-push verification of pull requests using BharatCode models."""

    def __init__(
        self,
        client: BharatCodeClient,
        repo_root: str | Path,
        max_tool_hops: int = 4,
        max_repair_attempts: int = 2,
    ) -> None:
        self.client = client
        self.repo_root = Path(repo_root).resolve()
        self.tools = RepositorySandboxTools(self.repo_root)
        self.max_tool_hops = max_tool_hops
        self.max_repair_attempts = max_repair_attempts

    def run_audit(
        self,
        diff_text: str,
        run_triage_with_llm: bool = False,
    ) -> HarnessRunResult:
        """Runs the 4-stage audit pipeline on the provided git diff."""
        # Stage 0: Deterministic Local Pre-Pass (0 Tokens)
        secret_scan = scan_and_redact_secrets(diff_text)
        parsed_diff = parse_unified_diff(secret_scan.sanitized_diff_text)
        blast_radius = build_blast_radius_report(
            parsed_diff,
            self.repo_root,
            has_secrets=secret_scan.has_secrets,
        )

        # Empty diff check
        if len(parsed_diff.files) == 0 and not secret_scan.has_secrets:
            verdict = AuditVerdict(
                verdict="PASS",
                risk_score=0,
                summary="Empty diff; no code modifications or secrets detected.",
                findings=[],
            )
            triage = TriageDecision(
                needs_deep_audit=False,
                risk_level="LOW",
                rationale="Empty diff.",
                focus_files=[],
            )
            return HarnessRunResult(
                verdict=verdict,
                triage=triage,
                blast_radius=blast_radius,
                secret_scan=secret_scan,
                tool_traces=[],
                reasoning_traces=[],
                request_ids=[],
                total_latency_ms=0.0,
                total_tokens=0,
                cache_hits=0,
                synthesized_test_code="",
            )

        # Documentation-only fast pass
        touched_files = [f.new_path for f in parsed_diff.files]
        all_docs = bool(parsed_diff.files) and all(
            Path(f.new_path).suffix.lower() in DOC_EXTENSIONS for f in parsed_diff.files
        )
        if all_docs and not secret_scan.has_secrets:
            triage = TriageDecision(
                needs_deep_audit=False,
                risk_level="LOW",
                rationale="Documentation-only modifications detected without secrets.",
                focus_files=touched_files,
            )
            verdict = AuditVerdict(
                verdict="PASS",
                risk_score=0,
                summary="Documentation-only change passed deterministic triage with 0 tokens.",
                findings=[],
            )
            return HarnessRunResult(
                verdict=verdict,
                triage=triage,
                blast_radius=blast_radius,
                secret_scan=secret_scan,
                tool_traces=[],
                reasoning_traces=[],
                request_ids=[],
                total_latency_ms=0.0,
                total_tokens=0,
                cache_hits=0,
                synthesized_test_code="",
            )

        triage = TriageDecision(
            needs_deep_audit=True,
            risk_level=blast_radius.risk_level,
            rationale="Code modifications detected requiring investigation.",
            focus_files=touched_files,
        )

        # Stage 1: Stateless /v1/responses Tool-Calling Investigation Loop
        initial_prompt = (
            f"Audit the following sanitized git diff:\n\n"
            f"```diff\n{secret_scan.sanitized_diff_text}\n```\n\n"
            f"Blast-Radius Static Analysis Summary:\n"
            f"```json\n{blast_radius.model_dump_json(indent=2)}\n```\n\n"
            f"Use available tools to inspect modified functions, check callers, and verify patches. "
            f"When ready, output the final AuditVerdict JSON."
        )

        conversation: list[ResponsesInputItem | dict[str, Any]] = [
            MessageInputItem(role="user", content=initial_prompt)
        ]

        tool_traces: list[ToolExecutionTrace] = []
        reasoning_traces: list[str] = []
        request_ids: list[str] = []
        total_latency_ms = 0.0
        total_tokens = 0
        cache_hits = 0
        final_text = ""

        for hop in range(1, self.max_tool_hops + 1):
            try:
                res = self.client.create_responses(
                    input_items=conversation,
                    instructions=INVESTIGATION_SYSTEM_PROMPT,
                    tools=self.tools.get_tool_definitions(),
                    disable_reasoning=False,
                    max_output_tokens=4096,
                )
            except TruncatedToolCallError:
                # Automatic Truncation Recovery
                res = self.client.create_responses(
                    input_items=conversation,
                    instructions=INVESTIGATION_SYSTEM_PROMPT,
                    tools=self.tools.get_tool_definitions(),
                    disable_reasoning=True,
                    max_output_tokens=8192,
                )

            if res.request_id:
                request_ids.append(res.request_id)
            if res.reasoning_trace:
                reasoning_traces.append(res.reasoning_trace)
            total_latency_ms += res.latency_ms
            total_tokens += res.usage.total_tokens
            if res.cache_hit:
                cache_hits += 1

            if len(res.function_calls) == 0:
                final_text = res.clean_text
                break

            for fc in res.function_calls:
                conversation.append(fc)
                output_item, trace = self.tools.execute_tool_call(
                    fc, step_index=len(tool_traces) + 1
                )
                conversation.append(output_item)
                tool_traces.append(trace)

        # If tool calls continued through the last hop, request final verdict
        if not final_text:
            final_res = self.client.create_responses(
                input_items=conversation,
                instructions=INVESTIGATION_SYSTEM_PROMPT
                + "\nDo not call any more tools. Output the final AuditVerdict JSON now.",
                tools=None,
                disable_reasoning=True,
                max_output_tokens=4096,
            )
            if final_res.request_id:
                request_ids.append(final_res.request_id)
            if final_res.reasoning_trace:
                reasoning_traces.append(final_res.reasoning_trace)
            total_latency_ms += final_res.latency_ms
            total_tokens += final_res.usage.total_tokens
            if final_res.cache_hit:
                cache_hits += 1
            final_text = final_res.clean_text

        # Stage 2: Pydantic Validation & Self-Repair Loop
        val = validate_json_with_schema(final_text, AuditVerdict)
        repair_attempts = 0

        while not val.is_valid and repair_attempts < self.max_repair_attempts:
            repair_attempts += 1
            repair_prompt = (
                f"Your previous JSON output failed Pydantic schema validation with errors:\n"
                f"{val.error_summary}\n\n"
                f"Raw output was:\n{final_text}\n\n"
                f"Return ONLY a corrected valid JSON object matching AuditVerdict."
            )
            repair_res = self.client.create_responses(
                input_items=repair_prompt,
                instructions=JSON_REPAIR_SYSTEM_PROMPT,
                tools=None,
                disable_reasoning=True,
                max_output_tokens=4096,
                use_cache=False,
            )
            if repair_res.request_id:
                request_ids.append(repair_res.request_id)
            total_latency_ms += repair_res.latency_ms
            total_tokens += repair_res.usage.total_tokens
            final_text = repair_res.clean_text
            val = validate_json_with_schema(final_text, AuditVerdict)

        if not val.is_valid:
            raise SchemaRepairExhaustedError(val.error_summary or "Failed to repair JSON")

        verdict: AuditVerdict = val.validated_data

        # Stage 3: Merge Deterministic Secret Findings & Synthesize Regression Test
        secret_findings: list[AuditFinding] = []
        for idx, f in enumerate(secret_scan.findings):
            secret_findings.append(
                AuditFinding(
                    id=f"SEC-{idx+1}",
                    severity="CRITICAL",
                    category="SECURITY",
                    file_path=f.file_path,
                    start_line=f.line_number,
                    end_line=f.line_number,
                    title=f"Exposed Secret Detected ({f.rule_id})",
                    explanation=(
                        f"Detected credential matching rule '{f.rule_id}' with Shannon entropy {f.entropy}. "
                        f"Redacted snippet: {f.redacted_snippet}"
                    ),
                    suggested_patch="Remove hardcoded secret and load from environment variables.",
                )
            )

        if secret_findings:
            verdict.findings = secret_findings + verdict.findings

        # If any finding is CRITICAL or HIGH, force BLOCK verdict
        has_high_or_crit = any(
            f.severity in ("CRITICAL", "HIGH") for f in verdict.findings
        )
        if has_high_or_crit:
            verdict.verdict = "BLOCK"
            verdict.risk_score = max(verdict.risk_score, 85)

        synthesized_test = synthesize_regression_pytest(verdict, blast_radius)

        return HarnessRunResult(
            verdict=verdict,
            triage=triage,
            blast_radius=blast_radius,
            secret_scan=secret_scan,
            tool_traces=tool_traces,
            reasoning_traces=reasoning_traces,
            request_ids=request_ids,
            total_latency_ms=round(total_latency_ms, 2),
            total_tokens=total_tokens,
            cache_hits=cache_hits,
            synthesized_test_code=synthesized_test,
        )
