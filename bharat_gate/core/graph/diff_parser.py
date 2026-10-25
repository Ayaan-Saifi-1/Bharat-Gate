import hashlib
import re

from bharat_gate.core.schemas import DiffFile, DiffHunk, DiffLine, ParsedDiff


def parse_unified_diff(diff_text: str) -> ParsedDiff:
    """Parses standard or bare unified diffs into structured Pydantic models with line numbers and hashes."""
    if not diff_text or not diff_text.strip():
        empty_hash = hashlib.sha256(b"").hexdigest()
        return ParsedDiff(files=[], total_added=0, total_deleted=0, diff_hash=empty_hash)

    lines = diff_text.splitlines()
    files: list[DiffFile] = []

    current_old_path: str | None = None
    current_new_path: str | None = None
    current_hunks: list[DiffHunk] = []
    current_hunk: DiffHunk | None = None
    curr_old = 0
    curr_new = 0
    file_added_count = 0
    file_deleted_count = 0

    def _finalize_hunk(hunk: DiffHunk) -> None:
        normalized = [
            f"+{l.content}" if l.line_type == "add" else f"-{l.content}"
            for l in hunk.lines
            if l.line_type in ("add", "delete")
        ]
        hunk.hunk_hash = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()

    def finalize_file() -> None:
        nonlocal current_old_path, current_new_path, current_hunks, current_hunk
        nonlocal file_added_count, file_deleted_count
        if current_hunk is not None:
            _finalize_hunk(current_hunk)
            current_hunks.append(current_hunk)
            current_hunk = None

        if current_old_path is not None and current_new_path is not None:
            if current_old_path == "/dev/null":
                change_type = "added"
            elif current_new_path == "/dev/null":
                change_type = "deleted"
            elif current_old_path != current_new_path:
                change_type = "renamed"
            else:
                change_type = "modified"

            files.append(
                DiffFile(
                    old_path=current_old_path,
                    new_path=current_new_path,
                    change_type=change_type,
                    hunks=current_hunks,
                    added_count=file_added_count,
                    deleted_count=file_deleted_count,
                )
            )

        current_old_path = None
        current_new_path = None
        current_hunks = []
        current_hunk = None
        file_added_count = 0
        file_deleted_count = 0

    hunk_header_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$")

    for line in lines:
        if line.startswith("diff --git"):
            finalize_file()
            m_git = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
            if m_git:
                current_old_path = m_git.group(1)
                current_new_path = m_git.group(2)
            continue

        if line.startswith("--- "):
            if current_old_path is not None and current_new_path is not None and current_hunks:
                finalize_file()
            raw_path = line[4:].strip().split("\t")[0].strip()
            if raw_path == "/dev/null":
                current_old_path = "/dev/null"
            else:
                current_old_path = (
                    raw_path[2:]
                    if raw_path.startswith("a/") or raw_path.startswith("b/")
                    else raw_path
                )
            continue

        if line.startswith("+++ "):
            raw_path = line[4:].strip().split("\t")[0].strip()
            if raw_path == "/dev/null":
                current_new_path = "/dev/null"
            else:
                current_new_path = (
                    raw_path[2:]
                    if raw_path.startswith("b/") or raw_path.startswith("a/")
                    else raw_path
                )
            continue

        hunk_match = hunk_header_re.match(line)
        if hunk_match:
            if current_hunk is not None:
                _finalize_hunk(current_hunk)
                current_hunks.append(current_hunk)
            old_start = int(hunk_match.group(1))
            old_lines = int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
            new_start = int(hunk_match.group(3))
            new_lines = int(hunk_match.group(4)) if hunk_match.group(4) is not None else 1
            header = hunk_match.group(5).strip()
            curr_old = old_start
            curr_new = new_start
            current_hunk = DiffHunk(
                old_start=old_start,
                old_lines=old_lines,
                new_start=new_start,
                new_lines=new_lines,
                header=header,
                lines=[],
                hunk_hash="",
            )
            continue

        if current_hunk is not None:
            if line.startswith(r"\ No newline at end of file"):
                continue
            if line.startswith("+") and not line.startswith("+++"):
                current_hunk.lines.append(
                    DiffLine(
                        line_type="add",
                        old_lineno=None,
                        new_lineno=curr_new,
                        content=line[1:],
                    )
                )
                curr_new += 1
                file_added_count += 1
            elif line.startswith("-") and not line.startswith("---"):
                current_hunk.lines.append(
                    DiffLine(
                        line_type="delete",
                        old_lineno=curr_old,
                        new_lineno=None,
                        content=line[1:],
                    )
                )
                curr_old += 1
                file_deleted_count += 1
            elif line.startswith(" "):
                current_hunk.lines.append(
                    DiffLine(
                        line_type="context",
                        old_lineno=curr_old,
                        new_lineno=curr_new,
                        content=line[1:],
                    )
                )
                curr_old += 1
                curr_new += 1
            elif line == "":
                current_hunk.lines.append(
                    DiffLine(
                        line_type="context",
                        old_lineno=curr_old,
                        new_lineno=curr_new,
                        content="",
                    )
                )
                curr_old += 1
                curr_new += 1

    finalize_file()

    total_added = sum(f.added_count for f in files)
    total_deleted = sum(f.deleted_count for f in files)

    diff_hash_parts = []
    for f in files:
        diff_hash_parts.append(f"{f.old_path}->{f.new_path}:{f.change_type}")
        for h in f.hunks:
            diff_hash_parts.append(h.hunk_hash)
    diff_hash = hashlib.sha256("\n".join(diff_hash_parts).encode("utf-8")).hexdigest()

    return ParsedDiff(
        files=files,
        total_added=total_added,
        total_deleted=total_deleted,
        diff_hash=diff_hash,
    )


def get_modified_line_ranges(diff_file: DiffFile) -> list[tuple[int, int]]:
    """Returns (min_line, max_line) tuples representing modified line ranges for each hunk."""
    ranges: list[tuple[int, int]] = []
    for hunk in diff_file.hunks:
        added_lines = [
            line.new_lineno
            for line in hunk.lines
            if line.line_type == "add" and line.new_lineno is not None
        ]
        if added_lines:
            ranges.append((min(added_lines), max(added_lines)))
        else:
            end_line = max(hunk.new_start, hunk.new_start + hunk.new_lines)
            ranges.append((hunk.new_start, end_line))
    return ranges
