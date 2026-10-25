import ast
import os
from pathlib import Path

from bharat_gate.core.graph.diff_parser import get_modified_line_ranges
from bharat_gate.core.schemas import (
    BlastRadiusReport,
    CallerReference,
    ParsedDiff,
    SymbolDefinition,
)

IGNORED_DIRS = {
    ".git",
    "venv",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".bharat_gate_cache",
    "dist",
    "build",
}


def extract_python_symbols(source_code: str, file_path: str) -> list[SymbolDefinition]:
    """Extracts top-level functions, async functions, classes, and class methods using Python's ast."""
    try:
        tree = ast.parse(source_code, filename=file_path)
    except SyntaxError:
        return []

    symbols: list[SymbolDefinition] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            args_str = ast.unparse(node.args)
            ret_str = f" -> {ast.unparse(node.returns)}" if getattr(node, "returns", None) is not None else ""
            prefix = "async def " if kind == "async_function" else "def "
            sig = f"{prefix}{node.name}({args_str}){ret_str}"
            start_line = node.lineno
            end_line = getattr(node, "end_lineno", node.lineno)
            symbols.append(
                SymbolDefinition(
                    name=node.name,
                    kind=kind,
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    signature=sig,
                )
            )
        elif isinstance(node, ast.ClassDef):
            bases_str = f"({', '.join(ast.unparse(b) for b in node.bases)})" if node.bases else ""
            class_sig = f"class {node.name}{bases_str}"
            start_line = node.lineno
            end_line = getattr(node, "end_lineno", node.lineno)
            symbols.append(
                SymbolDefinition(
                    name=node.name,
                    kind="class",
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    signature=class_sig,
                )
            )

            # Class methods
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def " if isinstance(item, ast.AsyncFunctionDef) else "def "
                    args_str = ast.unparse(item.args)
                    ret_str = f" -> {ast.unparse(item.returns)}" if getattr(item, "returns", None) is not None else ""
                    sig = f"{prefix}{item.name}({args_str}){ret_str}"
                    item_start = item.lineno
                    item_end = getattr(item, "end_lineno", item.lineno)
                    symbols.append(
                        SymbolDefinition(
                            name=item.name,
                            kind="method",
                            file_path=file_path,
                            start_line=item_start,
                            end_line=item_end,
                            signature=sig,
                        )
                    )

    return symbols


def find_modified_symbols(parsed_diff: ParsedDiff, repo_root: Path) -> list[SymbolDefinition]:
    """Finds symbols whose line spans overlap with modified line ranges in the diff."""
    modified_symbols: list[SymbolDefinition] = []
    seen: set[tuple[str, str]] = set()

    for diff_file in parsed_diff.files:
        if diff_file.change_type in ("modified", "added"):
            full_path = (repo_root / diff_file.new_path).resolve()
            if full_path.exists() and full_path.suffix == ".py":
                try:
                    source = full_path.read_text(encoding="utf-8", errors="replace")
                    symbols = extract_python_symbols(source, diff_file.new_path)
                    ranges = get_modified_line_ranges(diff_file)
                    for sym in symbols:
                        for r_start, r_end in ranges:
                            if not (r_end < sym.start_line or r_start > sym.end_line):
                                key = (sym.file_path, sym.name)
                                if key not in seen:
                                    seen.add(key)
                                    modified_symbols.append(sym)
                                break
                except Exception:
                    continue

    return modified_symbols


class ScopeCallerVisitor(ast.NodeVisitor):
    """AST visitor that tracks scope and identifies callers and import references of a target symbol."""

    def __init__(self, symbol_name: str, rel_path: str, source_lines: list[str]) -> None:
        self.symbol_name = symbol_name
        self.rel_path = rel_path
        self.source_lines = source_lines
        self.scope_stack: list[str] = []
        self.callers: list[CallerReference] = []

    @property
    def current_scope(self) -> str:
        return ".".join(self.scope_stack) if self.scope_stack else "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        is_target = False
        if isinstance(node.func, ast.Name) and node.func.id == self.symbol_name:
            is_target = True
        elif isinstance(node.func, ast.Attribute) and node.func.attr == self.symbol_name:
            is_target = True

        if is_target:
            lineno = node.lineno
            snippet = (
                self.source_lines[lineno - 1].strip()
                if 0 <= lineno - 1 < len(self.source_lines)
                else ""
            )
            self.callers.append(
                CallerReference(
                    symbol_name=self.symbol_name,
                    caller_file=self.rel_path,
                    caller_line=lineno,
                    caller_scope=self.current_scope,
                    call_snippet=snippet,
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == self.symbol_name:
                lineno = node.lineno
                snippet = (
                    self.source_lines[lineno - 1].strip()
                    if 0 <= lineno - 1 < len(self.source_lines)
                    else ""
                )
                self.callers.append(
                    CallerReference(
                        symbol_name=self.symbol_name,
                        caller_file=self.rel_path,
                        caller_line=lineno,
                        caller_scope="import",
                        call_snippet=snippet,
                    )
                )
        self.generic_visit(node)


def find_symbol_callers_in_repo(
    symbol_name: str,
    repo_root: Path,
    max_results: int = 25,
) -> list[CallerReference]:
    """Finds all call sites and import references of symbol_name across Python files in repo_root."""
    callers: list[CallerReference] = []
    repo_root = Path(repo_root).resolve()

    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for file in files:
            if file.endswith(".py"):
                file_path = Path(root) / file
                try:
                    rel_path = file_path.relative_to(repo_root).as_posix()
                except ValueError:
                    rel_path = file_path.name
                try:
                    source = file_path.read_text(encoding="utf-8", errors="replace")
                    tree = ast.parse(source, filename=rel_path)
                    source_lines = source.splitlines()
                    visitor = ScopeCallerVisitor(symbol_name, rel_path, source_lines)
                    visitor.visit(tree)
                    callers.extend(visitor.callers)
                    if len(callers) >= max_results:
                        return callers[:max_results]
                except Exception:
                    continue

    return callers[:max_results]


def build_blast_radius_report(
    parsed_diff: ParsedDiff,
    repo_root: Path,
    has_secrets: bool = False,
) -> BlastRadiusReport:
    """Computes modified AST symbols, locates cross-file callers, and determines risk level."""
    repo_root = Path(repo_root).resolve()
    modified_symbols = find_modified_symbols(parsed_diff, repo_root)

    impacted_callers: list[CallerReference] = []
    seen_callers: set[tuple[str, int, str]] = set()
    for sym in modified_symbols:
        callers = find_symbol_callers_in_repo(sym.name, repo_root)
        for c in callers:
            key = (c.caller_file, c.caller_line, c.symbol_name)
            if key not in seen_callers:
                seen_callers.add(key)
                impacted_callers.append(c)

    impacted_files_set: set[str] = set()
    for f in parsed_diff.files:
        if f.new_path != "/dev/null":
            impacted_files_set.add(f.new_path)
        if f.old_path != "/dev/null":
            impacted_files_set.add(f.old_path)
    for c in impacted_callers:
        impacted_files_set.add(c.caller_file)

    impacted_files = sorted(list(impacted_files_set))
    total_diff_lines = parsed_diff.total_added + parsed_diff.total_deleted

    if has_secrets:
        risk_level = "CRITICAL"
    elif len(impacted_callers) >= 3 or total_diff_lines > 150:
        risk_level = "HIGH"
    elif len(modified_symbols) >= 1 or len(parsed_diff.files) >= 2:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    return BlastRadiusReport(
        modified_symbols=modified_symbols,
        impacted_callers=impacted_callers,
        impacted_files=impacted_files,
        risk_level=risk_level,
    )
