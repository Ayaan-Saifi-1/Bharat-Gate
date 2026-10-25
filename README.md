# BharatGate (`bharat-gate`)

**Autonomous Pre-Push Code Verification Harness, Python `ast` Blast-Radius Analyzer & Shared-GPU Inference Gateway for the [BharatCode Model API](https://bharatcode.ai/build)**

Built for the **[BharatCode Build League](https://bharatcode.ai/hackathon)** · **Track:** Coding & Developer Tools · **Primary Model:** `qwen-3.8-27b` (`deepseek-v4.1-flash` compatible)

## Overview

AI coding assistants let developers generate large multi-file commits rapidly, but traditional diff-only code review tools fail in three ways:
1. **Blind to Upstream Callers:** Reviewing only the raw `git diff` misses callers in other files that break when a function's unit, signature, or return contract changes.
2. **Unredacted Credential Leaks:** Sending raw `git diff` payloads directly to an external LLM API exposes hardcoded keys or tokens before they are caught.
3. **Shared-GPU Token Waste:** Re-analyzing unchanged hunks or documentation-only commits burns shared GPU capacity and triggers `HTTP 503` queue saturation.

**BharatGate (`bharat-gate`)** sits between `git commit` and `git push` to solve all three problems. Before making a single model call, BharatGate runs a **0-token local deterministic pre-pass** that redacts secrets via regex + Shannon entropy, parses unified diffs into content-addressed SHA-256 hunks, and uses Python's standard library `ast` module to map modified symbols and their upstream callers across the repository. It then orchestrates **`qwen-3.8-27b`** over BharatCode's stateless `POST /api/model/v1/responses` (`store: false`) endpoint with **6 path-sandboxed read-only repository tools**, validating every finding and unified patch with **Pydantic v2** and `ast.parse()` before synthesizing a runnable `pytest` regression suite.


## 10-Second Quickstart for Judges & Reviewers

You can verify the entire CLI, AST analyzer, SQLite WAL cache, and multi-hop `/v1/responses` tool traces in **under 10 seconds**—both **offline (0ms replay mode, no API key needed)** and **live against BharatCode's `qwen-3.8-27b`**.

### 1. Clone & Install

```bash
git clone https://github.com/<your-username>/bharat-gate.git
cd bharat-gate
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment (Optional for Offline Replays, Required for Live API)

```bash
cp .env.example .env
```

Edit `.env` and add your BharatCode key from [bharatcode.ai/build](https://bharatcode.ai/build):

```ini
BHARATCODE_API_KEY=your_bharatcode_api_key_here
BHARATCODE_BASE_URL=https://bharatcode.ai/api/model/v1
BHARATCODE_MODEL=qwen-3.8-27b
BHARATCODE_CACHE_DIR=.bharat_gate_cache
BHARATCODE_MAX_CONCURRENT=2
```

### 3. Run the 4 Instant Judge Verification Commands

```bash
# 1. Run the 6-point environment, SQLite WAL, and BharatCode health diagnostic
python manage.py doctor --no-llm          # Offline local check (0 tokens)
python manage.py doctor                   # Live /v1/responses ping (prints x-request-id & latency)

# 2. Inspect a complete multi-turn /v1/responses tool-call audit in 0ms (Offline Replay Mode)
python manage.py demo --scenario 1 --show-thinking --show-test

# 3. Run the 3-workload Shared-GPU Token & SHA-256 Cache Benchmark
python manage.py benchmark

# 4. Run all 38 deterministic pytest unit tests (executes in <0.4s via httpx.MockTransport)
pytest
```


## Architecture & 4-Stage Verification Pipeline

BharatGate separates **deterministic static analysis** from **probabilistic LLM reasoning** so shared GPU compute is used only where surgical reasoning is required:

| Stage | Module | Execution Mode | What Happens |
| :--- | :--- | :--- | :--- |
| **Stage 0: Local Pre-Pass & AST Graph** | `diff_parser.py`, `secret_scanner.py`, `blast_radius.py` | **Local CPU (`<5ms`, `0` Tokens)** | Scans added lines (`+`) for secrets using regex + Shannon entropy (`>= 3.5`), replaces raw secrets with `<REDACTED:RULE_ID>` prior to API transmission, parses unified diff hunks, and walks repository `.py` files with `ast.parse()` to find modified functions and their cross-file callers. Docs-only commits (`README.md`, `.txt`) fast-pass immediately with `0` GPU tokens. |
| **Stage 1: Stateless `/v1/responses` Tool Loop** | `orchestrator.py`, `tools.py`, `client.py` | **BharatCode `qwen-3.8-27b` (`store: False`)** | Sends the sanitized diff and `BlastRadiusReport` to `POST /v1/responses`. Across up to 4 hops, `qwen-3.8-27b` invokes read-only local tools (`find_symbol_callers`, `read_file_lines`, `validate_unified_patch`, etc.). BharatGate replays `FunctionCallItem` and `FunctionCallOutputItem` objects with matching `call_id` values. |
| **Stage 2: Truncation Guard & Pydantic Self-Repair** | `parser.py`, `orchestrator.py`, `schemas.py` | **Pydantic v2 + Fast Repair (`effort: "none"`)** | Strips `<think>...</think>` blocks into a separate trace stream, checks `status == "incomplete"` (`max_output_tokens`), and validates the final verdict against `AuditVerdict`. If JSON validation fails, feeds the exact Pydantic field-path errors back with `reasoning: {"effort": "none"}` for sub-second self-repair. |
| **Stage 3: AST Patch Verification & Test Synthesis** | `patch_builder.py` | **Local AST Verifier** | Verifies that proposed code replacements apply cleanly and pass `ast.parse()` without syntax errors, merges deterministic `CRITICAL` secret findings, and generates a runnable `pytest` regression suite (`synthesized_test_code`). |


## Strict BharatCode API Engineering (`https://bharatcode.ai/build`)

BharatGate is engineered specifically around the documented operational characteristics of the [BharatCode Model API](https://bharatcode.ai/build):

| BharatCode API Specification | BharatGate Implementation (`bharat_gate/core/gateway/`) |
| :--- | :--- |
| **Stateless `/v1/responses` (`store: False`)** | `BharatCodeClient.create_responses()` hardcodes `"store": False` on every request (`store: True` and `previous_response_id` return `HTTP 400` on BharatCode) and maintains the multi-turn `input` array in memory with strict `call_id` pairing. |
| **Reasoning Tokens Inside `max_output_tokens`** | On `qwen-3.8-27b`, internal `<think>` tokens count against `max_output_tokens`. `parser.py` inspects `status == "incomplete"`, `incomplete_details.reason == "max_output_tokens"`, `finish_reason == "length"`, and unclosed `<think>` tags. If a response is truncated while emitting a `function_call`, `client.py` raises `TruncatedToolCallError` (preventing broken tool execution) and `orchestrator.py` automatically retries with `disable_reasoning=True` (`reasoning: {"effort": "none"}`) and an `8192` token ceiling. |
| **Selective Reasoning Control** | Uses full `<think>` reasoning during multi-file root-cause investigation, and switches to `reasoning: {"effort": "none"}` (`reasoning_effort: "none"` on `/chat/completions`) for health checks and JSON schema self-repair calls—cutting latency and GPU token usage by >65%. |
| **Shared-GPU `HTTP 503` Queue & Jitter Backoff** | `RequestQueue` (`queue.py`) limits concurrent upstream requests via `threading.BoundedSemaphore(2)` and automatically retries `HTTP 503` (shared GPU busy), `HTTP 429`, and network timeouts using exponential backoff with random jitter (`2s -> 4s -> 8s -> 16s`), while failing fast (`0` retries) on `HTTP 400`, `401`, and `403`. |
| **Content-Addressed SQLite WAL Cache** | `GatewayCache` (`cache.py`) normalizes request payloads into canonical key-sorted JSON, hashes them with `hashlib.sha256`, and stores non-truncated responses in a thread-safe WAL-mode SQLite database (`.bharat_gate_cache/gateway.sqlite3`). Repeat audits of unchanged diffs resolve in `<2ms` with `0` GPU tokens consumed. |
| **`x-request-id` Telemetry Audit Trail** | `TelemetryLedger` (`telemetry.py`) captures the `x-request-id` HTTP response header on every API call alongside latency, retry count, cache status, and token usage. |


## The 6 Path-Sandboxed Repository Tools (`bharat_gate/core/graph/tools.py`)

Instead of stuffing an entire codebase into a single prompt, `qwen-3.8-27b` inspects the repository surgically using 6 read-only tools. Every tool path is validated through `RepositorySandboxTools._resolve_safe_path()`, which resolves symlinks, blocks directory traversal (`../`) outside `repo_root`, and blocks access to sensitive files (`.env`, `.env.local`, `id_rsa`).

| Tool Name | Parameters | Purpose in Multi-Turn Investigation |
| :--- | :--- | :--- |
| `read_file_lines` | `file_path`, `start_line`, `end_line` | Reads up to 120 line-numbered source lines around a target function or caller. |
| `find_symbol_callers` | `symbol_name` | Walks `.py` files across the repository using `ast.NodeVisitor` (`ast.Call` & `ast.ImportFrom`) to return exact caller files, line numbers, scopes, and snippets. |
| `get_file_symbols_and_imports` | `file_path` | Parses a file's imports and top-level function, async function, class, and method signatures via `ast.parse()`. |
| `search_repo_text` | `query` | Performs a bounded, case-insensitive search across source files (max 20 matches) to locate config keys, constants, or error strings. |
| `check_dependency_manifest` | `package_name` | Inspects `requirements.txt`, `pyproject.toml`, and `package.json` to verify whether a library is installed and check its version constraints. |
| `validate_unified_patch` | `file_path`, `old_snippet`, `new_snippet` | Dry-runs a candidate code fix in memory to verify `old_snippet` matches verbatim and confirms the resulting file passes `ast.parse()` with zero syntax errors. |


## CLI Reference (`manage.py` & `python -m bharat_gate.cli`)

BharatGate supports dual invocation via either `python manage.py <command>` or `python -m bharat_gate.cli <command>`:

| Command | Example Usage | Description & Exit Code |
| :--- | :--- | :--- |
| **`doctor`** | `python manage.py doctor [--no-llm] [--json]` | Runs 6 diagnostic checks: Python >= 3.11, core packages, Git workspace, SQLite WAL cache/telemetry DB, `BHARATCODE_API_KEY` config, and live `/v1/responses` handshake (`x-request-id`). Exits `0` when healthy. |
| **`check`** | `python manage.py check [--staged] [--ref main] [--diff-file my.diff] [--offline-mock] [--show-thinking] [--show-test] [--json]` | Audits staged git changes, working tree changes, or a `.diff` file. Saves the full report to `.bharat_gate_cache/last_audit.json`. Exits **`1`** if `verdict == "BLOCK"` or **`0`** on `"PASS"` / `"WARN"`. |
| **`demo`** | `python manage.py demo --scenario 1 [--live] [--show-thinking] [--show-test]` | Renders 1 of the 3 embedded multi-hop scenarios in `0ms` offline replay mode, or pass `--live` to execute the scenario live on `qwen-3.8-27b` inside a temporary sandbox repo. |
| **`hook`** | `python manage.py hook <install \| uninstall \| status>` | Safely installs or removes a native `.git/hooks/pre-push` gate. Automatically backs up any existing user hook to `.git/hooks/pre-push.bharatgate.bak` and restores it on uninstall. |
| **`export-patch`** | `python manage.py export-patch [--finding-id BG-001] [--output fix.patch] [--export-test test_reg.py]` | Exports the unified diff patch and/or AST-verified `pytest` regression suite from the most recent audit run. |
| **`benchmark`** | `python manage.py benchmark [--json]` | Executes a 3-pass workload (Docs Fast-Pass, Cold Multi-Hop Audit, Warm SQLite SHA-256 Cache Hit) demonstrating `66.7%` GPU token savings. |


## Pre-Baked Multi-Hop Engineering Scenarios (`bharat_gate/data/prebaked_replays.py`)

For instant review even when shared GPU queues are busy, BharatGate includes three realistic scenarios that can be replayed offline in `0ms` (`python manage.py demo --scenario <1|2|3>`) or executed live against `qwen-3.8-27b` (`--live`):

| Scenario ID / Alias | Title | Bug Class & Blast Radius Demonstrated | Tools Invoked by `qwen-3.8-27b` |
| :--- | :--- | :--- | :--- |
| **`1` / `sse-gpu-leak`** | **Shared-GPU Inference Proxy: Aborted SSE Stream Leak & Missing 503 Backoff** | Removing `try/finally: gpu_slot.release()` in `proxy/stream_worker.py` leaks GPU concurrency slots when clients disconnect mid-stream, while retrying `HTTP 400` instead of `HTTP 503`. | `find_symbol_callers("stream_model_response")`, `read_file_lines("proxy/routes.py", 1, 60)` |
| **`2` / `webhook-paise-bug`** | **Silent 100x Mischarge: Breaking Currency Contract Across Callers** | `payments/processor.py` changes `capture_payment(order_id, amount_paise: int)` to `amount_rupees: float`, silently breaking un-updated callers in `webhooks/razorpay_handler.py` and `billing/invoice_service.py`. | `find_symbol_callers("capture_payment")`, `read_file_lines("webhooks/razorpay_handler.py", 1, 50)` |
| **`3` / `auth-sqli-secret`** | **Pre-API Secret Redaction + SQL Injection in Multi-Tenant Auth Middleware** | Hardcodes a live `BHARATCODE_API_KEY` token (caught and redacted locally in Stage 0 before API transmission) alongside an unparameterized SQL query in `auth/middleware.py`. | `read_file_lines("auth/middleware.py", 1, 60)`, `validate_unified_patch` |


## Repository Structure

```text
bharat-gate/
├── manage.py                               # Root CLI entry point (`python manage.py <cmd>`)
├── requirements.txt                        # Dependencies (pydantic, httpx, flask, rich, pytest, python-dotenv)
├── pytest.ini                              # Configures pytest discovery and pythonpath
├── .env.example                            # Documented environment variable template
├── bharat_gate/
│   ├── __init__.py
│   ├── core/
│   │   ├── exceptions.py                   # Custom exception hierarchy (BharatCodeAPIError, TruncatedToolCallError, etc.)
│   │   ├── schemas.py                      # 26 strict Pydantic v2 data contracts
│   │   ├── gateway/
│   │   │   ├── parser.py                   # <think> trace extractor, truncation guard & JSON schema validator
│   │   │   ├── cache.py                    # Canonical SHA-256 hasher & WAL-mode SQLite response cache
│   │   │   ├── queue.py                    # BoundedSemaphore concurrency limiter & HTTP 503/429 jitter backoff
│   │   │   ├── telemetry.py                # SQLite x-request-id & token savings audit ledger
│   │   │   └── client.py                   # Stateless BharatCodeClient (/v1/responses & /v1/chat/completions)
│   │   ├── graph/
│   │   │   ├── diff_parser.py              # Unified git diff parser & hunk SHA-256 hasher
│   │   │   ├── secret_scanner.py           # Pre-API regex + Shannon entropy secret redactor
│   │   │   ├── blast_radius.py             # Python `ast` symbol extractor & cross-repo caller graph
│   │   │   └── tools.py                    # 6 path-sandboxed read-only repository tools
│   │   └── harness/
│   │       ├── prompts.py                  # System prompts for Triage, Investigation & JSON Self-Repair
│   │       ├── patch_builder.py            # AST-validated unified patch builder & pytest regression synthesizer
│   │       └── orchestrator.py             # 4-stage stateless /v1/responses orchestrator
│   ├── data/
│   │   └── prebaked_replays.py             # 3 complete multi-hop scenarios for 0ms Judge Replay Mode
│   └── cli/
│       ├── __main__.py                     # Enables `python -m bharat_gate.cli`
│       ├── git_ops.py                      # Git subprocess wrapper, pre-push hook manager & audit persistence
│       ├── doctor.py                       # 6-point diagnostic engine & live /v1/responses ping
│       ├── formatter.py                    # Rich terminal tables, diff panels & telemetry renderer
│       └── main.py                         # CLI command router
├── tests/
│   ├── test_phase1_gateway.py              # 12 unit tests for gateway, parser, cache, 503 backoff & truncation guard
│   ├── test_phase2_harness.py              # 14 unit tests for diff parser, secrets, AST graph, sandbox tools & loop
│   └── test_phase3_cli.py                  # 12 unit tests for CLI commands, doctor, git hooks, replays & benchmark
└── scripts/
    ├── verify_phase1.py                    # Phase 1 gateway & live handshake verification script
    ├── verify_phase2.py                    # Phase 2 multi-turn tool-calling audit verification script
    └── verify_phase3.py                    # Phase 3 end-to-end CLI workflow verification script
```


## Verification & Automated Test Suite (`38/38 Passing`)

All 38 unit tests use `httpx.MockTransport`, temporary Git workspaces (`tmp_path`), and injectable clocks so the entire test suite runs deterministically in **under 0.5 seconds** with zero network flakiness:

| Test Suite | Test Count | Key Behaviors Verified |
| :--- | :--- | :--- |
| `tests/test_phase1_gateway.py` | **12 Tests** | Closed/unclosed `<think>` stripping, `/responses` (`status="incomplete"`) and `/chat/completions` (`finish_reason="length"`) truncation detection, `TruncatedToolCallError` blocking on incomplete tool calls, `store: False` enforcement, `HTTP 503` retry recovery (`retry_count == 2`), `HTTP 400/401` immediate fail-fast, key-order-independent SHA-256 SQLite caching, and `x-request-id` telemetry aggregation. |
| `tests/test_phase2_harness.py` | **14 Tests** | Unified diff line-number tracking, pre-API secret redaction (`BHARATCODE_API_KEY`, `AKIA...`, `sk-...`), Python `ast` symbol extraction, cross-file caller discovery (`blast_radius.py`), sandbox path-traversal (`../../etc/passwd`) and `.env` blocking, 0-token docs-only fast-pass, multi-turn `FunctionCallItem` + `FunctionCallOutputItem` (`call_id`) replay, Pydantic JSON self-repair with `reasoning={"effort": "none"}`, and AST-valid `pytest` test synthesis. |
| `tests/test_phase3_cli.py` | **12 Tests** | Pre-baked scenario validation, `doctor --no-llm` and mocked live handshake, `.git/hooks/pre-push` installation/backup/restoration, `demo` across all 3 scenarios, `check --diff-file` blocking (`exit code 1`) on leaked secrets, `export-patch` file generation, `benchmark` token-savings calculation, and `manage.py` subprocess execution. |


## Security & Privacy Guarantees

* **Server/CLI-Only Credentials:** `BHARATCODE_API_KEY` is read exclusively from server/CLI environment variables (`os.getenv("BHARATCODE_API_KEY")`) and is never logged, cached in plaintext, or exposed to client bundles.
* **Pre-Transmission Secret Redaction:** `scan_and_redact_secrets()` scrubs high-entropy tokens and known API key formats from diffs locally **before** any payload is sent to `https://bharatcode.ai/api/model/v1`.
* **Read-Only Path Sandboxing:** All 6 model-invocable tools enforce `Path.resolve().is_relative_to(repo_root)` and explicitly block `.env*` and private key files.
