from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

BharatCodeModelType = Literal["qwen-3.8-27b", "deepseek-v4.1-flash"]
EndpointType = Literal["/responses", "/chat/completions"]


class MessageInputItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["message"] = "message"
    role: Literal["system", "user", "assistant"]
    content: str


class FunctionCallItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["function_call"] = "function_call"
    call_id: str
    name: str
    arguments: str  # Raw JSON string of arguments from model


class FunctionCallOutputItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    output: str  # JSON string returned by local tool execution


ResponsesInputItem = MessageInputItem | FunctionCallItem | FunctionCallOutputItem


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["function"] = "function"
    name: str
    description: str
    parameters: dict[str, Any]


class ParsedTextResult(BaseModel):
    clean_text: str
    reasoning_trace: str | None = None
    has_unclosed_think_tag: bool = False


class TruncationCheckResult(BaseModel):
    is_truncated: bool
    reason: str | None = None


class SchemaValidationResult(BaseModel):
    is_valid: bool
    validated_data: Any | None = None
    raw_json_extracted: str | None = None
    error_summary: str | None = None


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class GatewayResponse(BaseModel):
    request_id: str | None = None
    model: str
    endpoint: EndpointType
    clean_text: str
    reasoning_trace: str | None = None
    function_calls: list[FunctionCallItem] = Field(default_factory=list)
    raw_output_items: list[dict[str, Any]] = Field(default_factory=list)
    is_truncated: bool = False
    truncation_reason: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    retry_count: int = 0
    cache_hit: bool = False


class TelemetryRecord(BaseModel):
    request_id: str | None = None
    endpoint: EndpointType
    model: str
    status: Literal["completed", "incomplete", "cached", "error"]
    http_status: int
    latency_ms: float
    retry_count: int
    cache_hit: bool
    input_tokens: int = 0
    output_tokens: int = 0
    error_message: str | None = None
    timestamp: str


class TelemetrySummary(BaseModel):
    total_requests: int
    cache_hits: int
    cache_misses: int
    cache_hit_rate_pct: float
    total_retries: int
    avg_latency_ms: float
    total_input_tokens: int
    total_output_tokens: int
    estimated_tokens_saved: int
    recent_request_ids: list[str]


class HealthCheckResult(BaseModel):
    ok: bool
    http_status: int
    request_id: str | None = None
    latency_ms: float
    model: str
    reply_text: str = ""
    error: str | None = None


class AuditFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    category: Literal["SECURITY", "BUG", "BREAKING_CALLER", "CONCURRENCY", "PERFORMANCE"]
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    title: str
    explanation: str
    suggested_patch: str = ""


class AuditVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verdict: Literal["PASS", "WARN", "BLOCK"]
    risk_score: int = Field(ge=0, le=100)
    summary: str
    findings: list[AuditFinding] = Field(default_factory=list)


class DiffLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_type: Literal["context", "add", "delete"]
    old_lineno: int | None = None
    new_lineno: int | None = None
    content: str


class DiffHunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str = ""
    lines: list[DiffLine] = Field(default_factory=list)
    hunk_hash: str = ""


class DiffFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_path: str
    new_path: str
    change_type: Literal["added", "modified", "deleted", "renamed"] = "modified"
    hunks: list[DiffHunk] = Field(default_factory=list)
    added_count: int = 0
    deleted_count: int = 0


class ParsedDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[DiffFile] = Field(default_factory=list)
    total_added: int = 0
    total_deleted: int = 0
    diff_hash: str = ""


class RedactedSecretMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    file_path: str
    line_number: int
    redacted_snippet: str
    entropy: float = 0.0


class SecretScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sanitized_diff_text: str
    findings: list[RedactedSecretMatch] = Field(default_factory=list)
    has_secrets: bool = False


class SymbolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: Literal["function", "async_function", "class", "method"]
    file_path: str
    start_line: int
    end_line: int
    signature: str


class CallerReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol_name: str
    caller_file: str
    caller_line: int
    caller_scope: str
    call_snippet: str


class BlastRadiusReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modified_symbols: list[SymbolDefinition] = Field(default_factory=list)
    impacted_callers: list[CallerReference] = Field(default_factory=list)
    impacted_files: list[str] = Field(default_factory=list)
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"


class TriageDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    needs_deep_audit: bool = True
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "MEDIUM"
    rationale: str = ""
    focus_files: list[str] = Field(default_factory=list)


class ToolExecutionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_index: int
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    output_preview: str
    duration_ms: float
    is_error: bool = False


class PatchVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_applicable: bool
    syntax_valid: bool
    file_path: str
    reason: str = "OK"
    unified_diff: str = ""


class HarnessRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: AuditVerdict
    triage: TriageDecision
    blast_radius: BlastRadiusReport
    secret_scan: SecretScanResult
    tool_traces: list[ToolExecutionTrace] = Field(default_factory=list)
    reasoning_traces: list[str] = Field(default_factory=list)
    request_ids: list[str] = Field(default_factory=list)
    total_latency_ms: float = 0.0
    total_tokens: int = 0
    cache_hits: int = 0
    synthesized_test_code: str = ""

