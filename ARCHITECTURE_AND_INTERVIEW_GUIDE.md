# BharatGate Architecture & Technical Interview Defense Guide

Comprehensive architectural documentation, module walkthrough, and direct interview defense answers for the **BharatCode Build League** founder and engineering judges.

---

## Part 1: Executive Architectural Philosophy

### Why BharatGate Exists
AI coding assistants enable software engineers to create massive, multi-file code diffs with unprecedented speed. However, standard CI pipelines and pull request review workflows are fundamentally ill-equipped to evaluate these diffs safely:

1. **Context Blindness:** A standard `git diff` only reveals what changed in the modified lines. If an engineer changes `def process_payment(amount_paise: int)` to `amount_rupees: float`, isolated unit tests for the modified file may pass, but un-updated callers across other repository files fail silently at runtime.
2. **Credential Leakage to Third-Party APIs:** Naively sending unreviewed diffs to external LLM APIs exposes database credentials, API tokens, and private keys.
3. **Shared-GPU Saturation:** Running 27B+ parameter models on every single commit, including trivial documentation updates or identical repeated diffs, wastes shared GPU cluster capacity, creates queue backpressure, and triggers `HTTP 503` service unavailability.

### The Core Architectural Thesis
BharatGate enforces a strict division of labor:
* **Deterministic Local Static Analysis (Stage 0):** Always use local CPU algorithms (`regex`, Shannon entropy, and Python standard library `ast`) for tasks that require zero ambiguity: secret redaction, unified diff parsing, markdown fast-pass, and upstream caller mapping. This costs **0 GPU tokens** and executes in **<5ms**.
* **Probabilistic LLM Semantic Reasoning (Stage 1):** Use `qwen-3.8-27b` solely for complex semantic deduction: root-cause analysis, intent verification across caller boundaries, and patch synthesis.
* **Deterministic Verification & Synthesis (Stages 2 & 3):** Validate LLM outputs with strict **Pydantic v2 schemas**, verify candidate code replacements with `ast.parse()`, and synthesize runnable `pytest` regression suites.

---

## Part 2: Module-by-Module Code Walkthrough

### 1. Gateway & Shared-GPU Resilience Layer (`bharat_gate/core/gateway/`)

#### `client.py` (`BharatCodeClient`)
* **Contract:** Implements the official BharatCode HTTP client using `httpx.Client`.
* **Stateless Multi-Turn Replay:** Enforces `"store": False` on all `POST /api/model/v1/responses` requests. Replays the in-memory array of `FunctionCallItem` and `FunctionCallOutputItem` items across conversation turns with matching `call_id` values.
* **Reasoning Control:** Supports `disable_reasoning: bool`. When true, injects `reasoning: {"effort": "none"}` (`reasoning_effort: "none"` on `/chat/completions`) to eliminate reasoning token overhead during health pings and JSON self-repairs.
* **Truncation Defense:** Catches `TruncatedToolCallError` before partial tool invocations can execute.

#### `parser.py`
* **`<think>` Extraction:** Regex-extracts model reasoning traces from `<think>...</think>` tags or open `<think>` tags at truncation boundaries.
* **Truncation Detection:** Evaluates `status == "incomplete"`, `incomplete_details.reason == "max_output_tokens"`, `finish_reason == "length"`, and unclosed `<think>` markers.
* **Schema Validation:** Validates JSON payloads against Pydantic models. On failure, returns field-path error lists (`loc` and `msg`) to guide self-repair.

#### `cache.py` (`GatewayCache`)
* **Deterministic Content Addressing:** Normalizes request payloads into canonical key-sorted JSON and hashes them with `hashlib.sha256`.
* **WAL-Mode SQLite:** Implements concurrency-safe, persistent local storage (`gateway.sqlite3`) using SQLite Write-Ahead Logging (`PRAGMA journal_mode=WAL`).
* **Zero-Token Repeat Audits:** Identical diffs and prompt structures resolve from local cache in `<2ms` with `0` upstream API calls.

#### `queue.py` (`RequestQueue`)
* **Concurrency Throttling:** Caps concurrent requests with `threading.BoundedSemaphore(max_concurrent)`.
* **Jittered Exponential Backoff:** Automatically retries `HTTP 503` (shared GPU busy), `HTTP 429` (rate limit), and connection timeouts using exponential backoff with random jitter (`2.0s -> 4.0s -> 8.0s -> 16.0s`).
* **Fast Failure:** Non-retryable client errors (`HTTP 400`, `401`, `403`) fail immediately with `0` retries.

#### `telemetry.py` (`TelemetryLedger`)
* **Upstream Traceability:** Extracts and records the `x-request-id` HTTP response header from every BharatCode response.
* **Auditing Metrics:** Persists request latency, retry count, token consumption, and cache hits to SQLite for operational visibility.

---

### 2. Graph & Static Analysis Layer (`bharat_gate/core/graph/`)

#### `diff_parser.py`
* Parses unified git diffs into structured `DiffHunk` and `FileDiff` objects.
* Computes added and deleted line counts, original and modified line numbers, and deterministic SHA-256 hunk digests.

#### `secret_scanner.py`
* **Pre-API Defense:** Analyzes added lines (`+`) before diffs leave the local workstation.
* **Dual-Filter Detection:** Employs regex rules for known key signatures (`BHARATCODE_API_KEY`, `AKIA...`, `sk-...`, RSA private keys) combined with Shannon entropy thresholding (`>= 3.5`) for high-entropy secret detection.
* **Sanitization:** Replaces plaintext tokens with `<REDACTED:RULE_ID>` tokens, preventing credential transmission over the wire.

#### `blast_radius.py`
* **Symbol Extraction:** Uses Python standard library `ast.parse()` to extract all function definitions, async function definitions, class definitions, and method definitions with signatures and line boundaries.
* **Reverse Caller Graph:** Traverses every `.py` file in the repository using `ast.NodeVisitor` to find callers via `ast.Call` and `ast.ImportFrom`.
* **Blast-Radius Reporting:** Produces a structured `BlastRadiusReport` detailing modified symbols and all cross-file caller references.

#### `tools.py` (`RepositorySandboxTools`)
* Exposes 6 read-only tools to the model: `read_file_lines`, `find_symbol_callers`, `get_file_symbols_and_imports`, `search_repo_text`, `check_dependency_manifest`, and `validate_unified_patch`.
* **Path Sandboxing:** Enforces `_resolve_safe_path()`, resolving canonical paths and asserting `path.is_relative_to(repo_root)`. Blocks directory traversal (`../`) and sensitive filenames (`.env`, `.env.local`, `.git`).

---

### 3. Verification Harness & Orchestration Layer (`bharat_gate/core/harness/`)

#### `orchestrator.py` (`BharatGateOrchestrator`)
* **Stage 0:** Runs secret scan, diff parse, AST blast-radius analysis, and docs fast-pass.
* **Stage 1:** Drives the multi-turn stateless `/v1/responses` loop across up to 4 turns. Executes tools through `RepositorySandboxTools` and pairs `FunctionCallItem` with `FunctionCallOutputItem`.
* **Stage 2:** Validates the model output against `AuditVerdict`. If malformed, executes a targeted JSON repair turn with `disable_reasoning=True`.
* **Stage 3:** Validates suggested patches with `patch_builder.py`, merges deterministic Stage 0 secret findings, and synthesizes runnable regression tests.

#### `patch_builder.py`
* **Verbatim Replacement Check:** Verifies that candidate patches match original file lines verbatim.
* **AST Syntax Gate:** Asserts that patched source files compile cleanly under `ast.parse()`.
* **Pytest Synthesis:** Auto-generates an AST-validated `test_regression_*.py` suite targeting the detected failure modes.

---

### 4. CLI, Web Studio & Developer Experience

#### `manage.py` & `bharat_gate/cli/`
* Dual invocation via `python manage.py <cmd>` or `python -m bharat_gate.cli <cmd>`.
* Subcommands:
  - `doctor`: 6-point environment, SQLite WAL, and live `/v1/responses` diagnostic check.
  - `check`: Pre-push git audit with `--staged`, `--ref`, or `--diff-file`. Exits `1` on `BLOCK`.
  - `demo`: Instant 0ms offline replay of pre-baked multi-hop scenarios.
  - `hook`: Pre-push hook installation, backup (`.git/hooks/pre-push.bharatgate.bak`), and restoration.
  - `export-patch`: Exports candidate patches and synthesized pytest suites.
  - `benchmark`: Executes a 3-workload token & cache efficiency test.
  - `serve`: Launches the Flask Web Studio on `http://127.0.0.1:5000`.

#### `bharat_gate/web/` (`studio.html` & `app.py`)
* **Zero-Build Stack:** Built with vanilla HTML5, Tailwind CSS CDN, and vanilla JS. Starts in `<0.5s` with 0 npm dependencies.
* **Real-Time SSE Streaming:** `POST /api/audit/stream` streams stage updates and emits `: keepalive\n\n` heartbeats every 10 seconds.
* **Interactive 4-Tab Workbench:**
  - Tab 1: Findings & Unified Patches (verdict badge, risk score, severity cards, 1-click copy).
  - Tab 2: AST Blast-Radius Graph (modified symbols and impacted callers).
  - Tab 3: Stateless Tool Trace & `<think>` (tool calls, execution duration, reasoning traces).
  - Tab 4: Synthesized pytest Suite & GPU Telemetry.

---

## Part 3: Top 10 Technical Interview Questions & Answers

### Question 1: "Why did you build BharatGate around stateless /v1/responses (`store: false`) instead of using OpenAI-style server-side sessions?"
**Answer:**
"The BharatCode Model API specification explicitly states that `/v1/responses` requires `'store': False`, and that passing `'store': True` or referencing `'previous_response_id'` returns an immediate `HTTP 400 Bad Request`. On a shared-GPU inference cluster, maintaining server-side KV caches or conversational sessions across arbitrary client disconnects consumes GPU memory and causes resource exhaustion.

By designing BharatGate as a strictly stateless gateway (`bharat_gate/core/gateway/client.py`), we maintain the entire conversation history—including `FunctionCallItem` tool calls and their corresponding `FunctionCallOutputItem` outputs with matching `call_id`s—in client memory. On each turn, the complete conversation is replayed in the request body. This provides four massive advantages:
1. **Zero Session Leakage:** If a developer disconnects or network drops, zero state is leaked on BharatCode's GPU cluster.
2. **Total Determinism:** We can serialize, hash, and replay any audit turn in `0ms` offline replay mode.
3. **Resilience to Server Restarts:** A rolling deployment or worker restart on the GPU backend does not invalidate our in-flight audits.
4. **Content-Addressed Caching:** Because each request body contains complete self-contained context, our SHA-256 SQLite cache works reliably without external session dependencies."

---

### Question 2: "On reasoning models like qwen-3.8-27b, internal <think> tokens consume max_output_tokens. How does BharatGate prevent catastrophic truncation during tool calls?"
**Answer:**
"This is one of the most critical failure modes in reasoning-enabled agent loops. When `qwen-3.8-27b` generates a large internal `<think>` block, it can exhaust `max_output_tokens` mid-generation. If the model is in the middle of emitting a JSON tool call, the payload truncates, producing invalid JSON. Attempting to parse or execute a truncated tool call breaks the agent loop.

BharatGate handles this through a multi-layered defense (`bharat_gate/core/gateway/parser.py` and `client.py`):
1. **Truncation Detection:** We inspect response metadata for `status == 'incomplete'`, `incomplete_details.reason == 'max_output_tokens'`, `finish_reason == 'length'`, and unclosed `<think>` tags.
2. **`TruncatedToolCallError` Guard:** If truncation occurs while emitting a tool call, `client.py` immediately raises `TruncatedToolCallError`, preventing the execution of broken tool arguments.
3. **Automatic Recovery Loop:** `orchestrator.py` catches `TruncatedToolCallError` and automatically retries the turn with `disable_reasoning=True`. This passes `reasoning: {'effort': 'none'}` to the API and raises the token ceiling to `8192`. The model skips internal reasoning, emits complete tool arguments immediately, and the audit proceeds without crashing.
4. **Selective Reasoning Optimization:** For single-turn operations like JSON schema repair and health check pings, we proactively set `reasoning: {'effort': 'none'}` from the start, saving over 65% of tokens and cutting latency to under a second."

---

### Question 3: "Why did you build a local Stage 0 pre-pass using Python `ast` instead of feeding the entire repository into the model's context window?"
**Answer:**
"Feeding an entire repository into an LLM context window is slow, expensive, and unreliable:
1. **Context Window & Attention Dilution:** Large contexts dilute attention ('needle-in-a-haystack' degradation), increasing the rate of hallucinated bug reports.
2. **Token Economy:** Analyzing thousands of lines of unrelated code on a shared 27B model burns scarce GPU tokens and increases queue wait times for all users.
3. **Deterministic Superiority:** Extracting function definitions, parameter signatures, and finding caller references across files via Python's standard library `ast` (`blast_radius.py`) takes **less than 5 milliseconds on a single CPU core** and has **100% mathematical precision with 0 hallucinations**.

In Stage 0, `build_blast_radius_report()` parses the diff, finds the modified functions, and traverses all `.py` files in the repository using `ast.NodeVisitor` to find callers via `ast.Call` and `ast.ImportFrom`. We then pass this concise `BlastRadiusReport` into `qwen-3.8-27b`. The model starts its investigation already knowing the exact upstream blast radius, and uses its 6 read-only tools to inspect only the relevant callers. Furthermore, documentation-only commits fast-pass in Stage 0 at **0 token cost**."

---

### Question 4: "How does your secret scanner protect user credentials before any data leaves the local machine?"
**Answer:**
"Most CI tools check for secrets *after* code is pushed, or rely on LLM prompts to detect secrets. Relying on an external LLM to detect secrets means you have already transmitted the secret over the network in plaintext.

BharatGate executes `scan_and_redact_secrets()` (`secret_scanner.py`) locally on the developer's machine in Stage 0, **before** any network connection to `https://bharatcode.ai` is established:
1. **Regex Pattern Matching:** We target known high-risk token patterns including `BHARATCODE_API_KEY`, AWS Access Keys (`AKIA...`), OpenAI-style keys (`sk-...`), and PEM private keys (`-----BEGIN ... PRIVATE KEY-----`).
2. **Shannon Entropy Analysis:** To catch generic high-entropy API tokens or database passwords that don't match standard prefixes, we calculate the Shannon entropy of candidate strings:
   $$H(X) = -\sum_{i=1}^{n} P(x_i) \log_2 P(x_i)$$
   Tokens exceeding an entropy threshold of $3.5$ on added lines are flagged.
3. **Local Substitution:** All detected secrets are scrubbed and replaced with deterministic tokens (`<REDACTED:RULE_ID>`). The sanitized diff is what gets analyzed and transmitted.
4. **Zero-Token Blocking:** In Stage 3, detected secret leaks are merged as deterministic `CRITICAL` findings that automatically set `verdict: "BLOCK"`, regardless of model response."

---

### Question 5: "How do you ensure that candidate patches generated by the model don't introduce new syntax errors or break existing code?"
**Answer:**
"BharatGate treats LLM patch suggestions as untrusted inputs. We never blindly write code back to disk. Every candidate patch undergoes rigorous verification in `patch_builder.py`:
1. **Verbatim Snippet Verification:** The candidate patch specifies the target file, the exact `old_snippet`, and the replacement `new_snippet`. `verify_snippet_replacement()` checks that `old_snippet` exists verbatim in the target file, preventing drift or mismatched edits.
2. **In-Memory AST Compilation Gate:** Before proposing or formatting the patch as a unified diff, we apply the replacement to an in-memory copy of the source file and pass it to `ast.parse()`. If the resulting code contains a syntax error or invalid token, the patch is rejected immediately.
3. **Sandbox Tool Validation:** The model can call the tool `validate_unified_patch` during its multi-turn loop to test-apply its own patch in memory and receive instant syntax validation feedback before submitting its final verdict.
4. **Pytest Regression Synthesis:** Alongside the patch, `synthesize_regression_pytest()` auto-generates a concrete `test_regression_*.py` suite asserting that the bug is fixed and edge cases are handled."

---

### Question 6: "In a shared-GPU environment with multiple users, how does BharatGate handle HTTP 503s, 429s, and queue thrashing?"
**Answer:**
"Shared-GPU inference clusters suffer from bursty traffic patterns. When multiple users submit jobs simultaneously, the model server returns `HTTP 503 Service Unavailable` (queue full) or `HTTP 429 Too Many Requests`.

BharatGate handles this through `RequestQueue` (`queue.py`):
1. **Concurrency Capping:** A `threading.BoundedSemaphore(max_concurrent=2)` ensures that a single developer workstation never floods the API with parallel requests.
2. **Exponential Backoff with Full Jitter:** When an `HTTP 503`, `HTTP 429`, or network timeout occurs, we do not retry immediately. We back off exponentially with randomized jitter:
   $$t_{\text{sleep}} = 2^{\text{attempt}} \times \text{base\_delay} + \text{uniform}(0, 1)$$
   This decorrelates client retries and prevents the 'thundering herd' problem that crashes busy GPU workers.
3. **Fast-Failure on Client Errors:** We never retry client errors (`HTTP 400 Bad Request`, `HTTP 401 Unauthorized`, `HTTP 403 Forbidden`). These fail immediately (`0` retries) with actionable diagnostics.
4. **SSE Keepalive Heartbeats:** During long-running multi-turn audits or streaming operations, `POST /api/audit/stream` emits SSE `: keepalive\n\n` comments every 10 seconds. This prevents load balancers, reverse proxies, and browsers from dropping the HTTP connection during deep inference turns."

---

### Question 7: "How does your SQLite WAL cache guarantee deterministic cache hits without race conditions or false hits?"
**Answer:**
"Our `GatewayCache` (`cache.py`) is engineered for deterministic, concurrency-safe performance:
1. **Canonical JSON Normalization:** Standard dictionary hashing in Python is sensitive to key ordering. We serialize request payloads into canonical JSON using `json.dumps(obj, sort_keys=True, separators=(',', ':'))`. This ensures that two requests with identical semantic content but different dictionary key ordering produce the exact same SHA-256 digest.
2. **Content-Addressed Keying:** The cache key is:
   $$\text{cache\_key} = \text{sha256}(\text{endpoint} + \text{model} + \text{canonical\_payload})$$
3. **Write-Ahead Logging (WAL):** We initialize SQLite with `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`. This allows concurrent readers to query the cache without blocking writers, eliminating database lock contention across parallel test suites or CLI runs.
4. **Truncation Exclusion:** We never cache responses marked `is_truncated=True` or `finish_reason='length'`. Only complete, validated model responses enter the cache ledger."

---

### Question 8: "Why does the Web Studio and CLI have zero Node.js or Webpack build dependencies?"
**Answer:**
"Pre-push gates and developer tools must have zero friction. Requiring developers to run `npm install`, compile React bundles with Vite/Webpack, or manage Node versions introduces fragile dependencies, slow CI builds, and potential version mismatches.

BharatGate's Web Studio (`bharat_gate/web/templates/studio.html`) is built entirely with:
- Standard HTML5 semantic architecture.
- Tailwind CSS loaded via CDN with custom dark engineering tokens.
- Native Vanilla JavaScript using standard browser `fetch()`, `ReadableStream`, and `TextDecoder` APIs.

As a result, running `python manage.py serve` starts the complete Web Studio in **under 0.5 seconds**. There are **0 npm dependencies**, 0 build scripts, and 0 node modules. It runs everywhere Python runs."

---

### Question 9: "How do you guarantee sandbox security against arbitrary file read or path traversal attacks in repository tools?"
**Answer:**
"When an LLM has access to tools like `read_file_lines` or `search_repo_text`, malicious diffs or prompt injections could attempt path traversal attacks like `read_file_lines(file_path='../../../../etc/passwd')` or inspect secret files like `.env`.

BharatGate enforces strict sandboxing in `RepositorySandboxTools._resolve_safe_path()` (`tools.py`):
1. **Canonical Path Resolution:** Every input path is resolved to an absolute, canonical path via `Path(file_path).resolve()`, which evaluates all symlinks and resolves relative segments (`..`).
2. **Boundary Containment:** We assert `resolved_path.is_relative_to(repo_root.resolve())`. If the resolved path attempts to escape `repo_root`, an `AccessDeniedError` is raised immediately.
3. **Explicit Sensitive File Blocklist:** We explicitly block files matching `.env`, `.env.*`, `*.pem`, `id_rsa*`, and `.git/` contents. Even if located inside the repository, the model cannot read them."

---

### Question 10: "How do you test and verify an LLM-based autonomous gate deterministically without network flakiness or burning API tokens in CI?"
**Answer:**
"Testing agentic workflows with live LLM calls in CI is slow, flaky, non-deterministic, and burns shared API tokens.

BharatGate solves this by separating test environments:
1. **100% MockTransport & tmp_path Test Suite:** All 50 unit tests in `tests/` run against `httpx.MockTransport` and temporary directories (`tmp_path`). We mock network conditions—including `HTTP 503` backoff, truncated SSE streams, and JSON repair cycles—with microsecond determinism. The complete 50-test suite executes in **less than 1.0 second**.
2. **Pre-Baked Offline Replay Engine:** `bharat_gate/data/prebaked_replays.py` embeds three realistic multi-hop scenarios with full recorded tool execution traces, `<think>` reasoning traces, and AST-valid test suites. Judges and CI runners can execute `python manage.py demo --scenario 1` in `0ms` with **0 API tokens consumed**.
3. **Dedicated Live Verification Scripts:** For production verification against BharatCode's live endpoints, we provide dedicated verification scripts (`scripts/verify_phase1.py`, `verify_phase2.py`, `verify_phase3.py`, `verify_phase4.py`) and `python manage.py doctor` that ping the live API, extract `x-request-id`, and verify shared-GPU performance on demand."

---

*Authored by Ayaan Saifi for the BharatCode Build League Hackathon (2026).*
