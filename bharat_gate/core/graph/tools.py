import ast
import json
import os
from pathlib import Path
import time
from typing import Any

from bharat_gate.core.graph.blast_radius import (
    IGNORED_DIRS,
    extract_python_symbols,
    find_symbol_callers_in_repo,
)
from bharat_gate.core.schemas import (
    FunctionCallItem,
    FunctionCallOutputItem,
    ToolDefinition,
    ToolExecutionTrace,
)

BLOCKED_FILENAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}


class RepositorySandboxTools:
    """Safe, sandboxed repository interrogation tools for the BharatCode autonomous audit loop."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def _resolve_safe_path(self, rel_path: str) -> Path:
        """Resolves rel_path against repo_root, strictly blocking path traversal and sensitive files."""
        if not rel_path or "\0" in rel_path:
            raise ValueError(f"Invalid path: {rel_path}")

        path_obj = Path(rel_path)
        if path_obj.name in BLOCKED_FILENAMES or (
            path_obj.name.startswith(".env") and path_obj.name != ".env.example"
        ):
            raise ValueError(f"Access to blocked file is forbidden: {path_obj.name}")

        candidate = (self.repo_root / rel_path).resolve()
        try:
            if not candidate.is_relative_to(self.repo_root):
                raise ValueError(f"Path traversal blocked: {rel_path}")
        except AttributeError:
            if not str(candidate).startswith(str(self.repo_root)):
                raise ValueError(f"Path traversal blocked: {rel_path}")

        if candidate.name in BLOCKED_FILENAMES or (
            candidate.name.startswith(".env") and candidate.name != ".env.example"
        ):
            raise ValueError(f"Access to blocked file is forbidden: {candidate.name}")

        return candidate

    def get_tool_definitions(self) -> list[ToolDefinition]:
        """Returns the 6 exact ToolDefinition schemas for BharatCode /v1/responses."""
        return [
            ToolDefinition(
                type="function",
                name="read_file_lines",
                description="Reads lines from a file in the repository with line numbers (up to 120 lines).",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative path to file in repository",
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "Starting line number (1-based)",
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "Ending line number (inclusive)",
                        },
                    },
                    "required": ["file_path", "start_line", "end_line"],
                },
            ),
            ToolDefinition(
                type="function",
                name="find_symbol_callers",
                description="Searches repository AST for call sites and import references of a given symbol name.",
                parameters={
                    "type": "object",
                    "properties": {
                        "symbol_name": {
                            "type": "string",
                            "description": "Function or class symbol name to search",
                        }
                    },
                    "required": ["symbol_name"],
                },
            ),
            ToolDefinition(
                type="function",
                name="get_file_symbols_and_imports",
                description="Extracts all import statements and AST symbol definitions (functions, classes, methods) in a file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative path to Python file in repository",
                        }
                    },
                    "required": ["file_path"],
                },
            ),
            ToolDefinition(
                type="function",
                name="search_repo_text",
                description="Performs case-insensitive substring search across code files in the repository (max 20 matches).",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Text substring query to search for",
                        }
                    },
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                type="function",
                name="check_dependency_manifest",
                description="Checks dependency manifests (requirements.txt, pyproject.toml, package.json) for a package.",
                parameters={
                    "type": "object",
                    "properties": {
                        "package_name": {
                            "type": "string",
                            "description": "Name of package or dependency to search for",
                        }
                    },
                    "required": ["package_name"],
                },
            ),
            ToolDefinition(
                type="function",
                name="validate_unified_patch",
                description="Validates a proposed code replacement, verifying snippet presence and Python AST syntax.",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative path to target file",
                        },
                        "old_snippet": {
                            "type": "string",
                            "description": "Existing code snippet to replace",
                        },
                        "new_snippet": {
                            "type": "string",
                            "description": "New replacement code snippet",
                        },
                    },
                    "required": ["file_path", "old_snippet", "new_snippet"],
                },
            ),
        ]

    def read_file_lines(self, file_path: str, start_line: int, end_line: int) -> dict[str, Any]:
        """Reads up to 120 lines from file_path with 1-based line numbers."""
        safe_path = self._resolve_safe_path(file_path)
        if not safe_path.exists() or not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        content = safe_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()

        start_line = max(1, start_line)
        end_line = min(len(lines), end_line)
        if end_line < start_line:
            end_line = start_line

        if end_line - start_line + 1 > 120:
            end_line = start_line + 119

        numbered = [f"{i}: {lines[i-1]}" for i in range(start_line, end_line + 1)]
        return {
            "file_path": file_path,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": len(lines),
            "lines": numbered,
        }

    def find_symbol_callers(self, symbol_name: str) -> dict[str, Any]:
        """Locates call sites and imports of symbol_name across Python files in repo_root."""
        callers = find_symbol_callers_in_repo(symbol_name, self.repo_root)
        return {
            "symbol_name": symbol_name,
            "callers": [c.model_dump() for c in callers],
            "total_callers": len(callers),
        }

    def get_file_symbols_and_imports(self, file_path: str) -> dict[str, Any]:
        """Extracts imports and AST symbol definitions in file_path."""
        safe_path = self._resolve_safe_path(file_path)
        if not safe_path.exists() or not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        source = safe_path.read_text(encoding="utf-8", errors="replace")
        symbols = extract_python_symbols(source, file_path)

        imports: list[str] = []
        try:
            tree = ast.parse(source, filename=file_path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(f"import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    names = ", ".join(a.name for a in node.names)
                    imports.append(f"from {mod} import {names}")
        except Exception:
            pass

        return {
            "file_path": file_path,
            "imports": imports,
            "symbols": [s.model_dump() for s in symbols],
        }

    def search_repo_text(self, query: str) -> dict[str, Any]:
        """Performs case-insensitive substring search across repository files (max 20 matches)."""
        if not query:
            return {"query": query, "matches": [], "total_matches": 0}

        query_lower = query.lower()
        matches: list[dict[str, Any]] = []

        for root, dirs, files in os.walk(self.repo_root):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
            for file in files:
                ext = Path(file).suffix.lower()
                if ext in (".py", ".ts", ".js", ".json", ".txt", ".md", ".html", ".css", ".ini"):
                    full_path = Path(root) / file
                    try:
                        rel_path = full_path.relative_to(self.repo_root).as_posix()
                        content = full_path.read_text(encoding="utf-8", errors="replace")
                        for idx, line in enumerate(content.splitlines()):
                            if query_lower in line.lower():
                                matches.append(
                                    {
                                        "file_path": rel_path,
                                        "line_number": idx + 1,
                                        "line_content": line.strip(),
                                    }
                                )
                                if len(matches) >= 20:
                                    return {
                                        "query": query,
                                        "matches": matches,
                                        "total_matches": len(matches),
                                    }
                    except Exception:
                        continue

        return {"query": query, "matches": matches, "total_matches": len(matches)}

    def check_dependency_manifest(self, package_name: str) -> dict[str, Any]:
        """Checks dependency manifests for the given package."""
        manifest_names = ["requirements.txt", "pyproject.toml", "package.json", "setup.py", "Pipfile"]
        findings: list[dict[str, Any]] = []
        target = package_name.lower().strip()

        for name in manifest_names:
            manifest_file = (self.repo_root / name).resolve()
            if manifest_file.exists() and manifest_file.is_file():
                try:
                    content = manifest_file.read_text(encoding="utf-8", errors="replace")
                    lines = content.splitlines()
                    matched_lines = [
                        f"Line {idx+1}: {line.strip()}"
                        for idx, line in enumerate(lines)
                        if target in line.lower()
                    ]
                    if matched_lines:
                        findings.append(
                            {
                                "file": name,
                                "matched_lines": matched_lines,
                            }
                        )
                except Exception:
                    continue

        return {
            "package_name": package_name,
            "found": len(findings) > 0,
            "manifests": findings,
        }

    def validate_unified_patch(self, file_path: str, old_snippet: str, new_snippet: str) -> dict[str, Any]:
        """Validates that old_snippet is present in file_path and that replacing it produces valid Python syntax."""
        safe_path = self._resolve_safe_path(file_path)
        if not safe_path.exists() or not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        content = safe_path.read_text(encoding="utf-8", errors="replace")
        if old_snippet.strip() not in content:
            return {
                "file_path": file_path,
                "is_applicable": False,
                "syntax_valid": False,
                "reason": "old_snippet not found in file content",
            }

        patched = content.replace(old_snippet, new_snippet, 1)
        syntax_valid = True
        reason = "OK"

        if file_path.endswith(".py"):
            try:
                ast.parse(patched)
            except SyntaxError as se:
                syntax_valid = False
                reason = f"SyntaxError in patched code: {se}"

        return {
            "file_path": file_path,
            "is_applicable": True,
            "syntax_valid": syntax_valid,
            "reason": reason,
        }

    def execute_tool_call(
        self,
        call: FunctionCallItem,
        step_index: int = 1,
    ) -> tuple[FunctionCallOutputItem, ToolExecutionTrace]:
        """Dispatches a FunctionCallItem to the matching method and returns the output item and execution trace."""
        t0 = time.perf_counter()
        is_error = False

        try:
            if isinstance(call.arguments, str):
                args = json.loads(call.arguments) if call.arguments.strip() else {}
            else:
                args = call.arguments or {}
            if not isinstance(args, dict):
                args = {}
        except Exception:
            args = {}

        try:
            if call.name == "read_file_lines":
                result = self.read_file_lines(
                    file_path=str(args.get("file_path", "")),
                    start_line=int(args.get("start_line", 1)),
                    end_line=int(args.get("end_line", 1)),
                )
            elif call.name == "find_symbol_callers":
                result = self.find_symbol_callers(symbol_name=str(args.get("symbol_name", "")))
            elif call.name == "get_file_symbols_and_imports":
                result = self.get_file_symbols_and_imports(file_path=str(args.get("file_path", "")))
            elif call.name == "search_repo_text":
                result = self.search_repo_text(query=str(args.get("query", "")))
            elif call.name == "check_dependency_manifest":
                result = self.check_dependency_manifest(package_name=str(args.get("package_name", "")))
            elif call.name == "validate_unified_patch":
                result = self.validate_unified_patch(
                    file_path=str(args.get("file_path", "")),
                    old_snippet=str(args.get("old_snippet", "")),
                    new_snippet=str(args.get("new_snippet", "")),
                )
            else:
                is_error = True
                result = {"error": f"Unknown tool: {call.name}"}
        except Exception as exc:
            is_error = True
            result = {"error": str(exc)}

        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        output_json = json.dumps(result, ensure_ascii=False)

        output_item = FunctionCallOutputItem(call_id=call.call_id, output=output_json)
        trace = ToolExecutionTrace(
            step_index=step_index,
            call_id=call.call_id,
            tool_name=call.name,
            arguments=args,
            output_preview=output_json[:300],
            duration_ms=duration_ms,
            is_error=is_error,
        )
        return output_item, trace
