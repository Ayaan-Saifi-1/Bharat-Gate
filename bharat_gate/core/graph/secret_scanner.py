from collections import Counter
import math
import re

from bharat_gate.core.schemas import RedactedSecretMatch, SecretScanResult

SECRET_PATTERNS = {
    "BHARATCODE_KEY": r"(?:BHARATCODE_API_KEY|bharatcode_api_key)\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{16,})['\"]?",
    "AWS_ACCESS_KEY": r"\b(AKIA[0-9A-Z]{16})\b",
    "API_SECRET_KEY": r"\b((?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{16,}|sk-[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{25,})\b",
    "PRIVATE_KEY_HEADER": r"(-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)",
    "GENERIC_HIGH_ENTROPY_SECRET": r"(?i)(?:api_key|secret|password|passwd|access_token|auth_token|client_secret)\s*[:=]\s*['\"]([^'\"]{16,})['\"]",
}

PLACEHOLDER_WORDS = {"your_", "example", "placeholder", "xxxx", "todo"}


def calculate_shannon_entropy(token: str) -> float:
    """Calculates standard base-2 Shannon entropy for a given token string."""
    if not token:
        return 0.0
    length = len(token)
    counts = Counter(token)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def scan_and_redact_secrets(diff_text: str) -> SecretScanResult:
    """Scans added lines in a unified diff, redacting matching secrets with deterministic tags."""
    if not diff_text:
        return SecretScanResult(sanitized_diff_text="", findings=[], has_secrets=False)

    lines = diff_text.splitlines()
    sanitized_lines: list[str] = []
    findings: list[RedactedSecretMatch] = []

    current_file = "unknown"
    current_new_lineno = 1

    hunk_header_re = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")

    for line in lines:
        if line.startswith("diff --git"):
            m_git = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
            if m_git:
                current_file = m_git.group(2)
            sanitized_lines.append(line)
            continue

        if line.startswith("+++ "):
            raw_path = line[4:].strip().split("\t")[0].strip()
            if raw_path != "/dev/null":
                current_file = (
                    raw_path[2:]
                    if raw_path.startswith("b/") or raw_path.startswith("a/")
                    else raw_path
                )
            sanitized_lines.append(line)
            continue

        if line.startswith("--- "):
            sanitized_lines.append(line)
            continue

        hunk_match = hunk_header_re.match(line)
        if hunk_match:
            current_new_lineno = int(hunk_match.group(1))
            sanitized_lines.append(line)
            continue

        if line.startswith("+") and not line.startswith("+++"):
            target_lineno = current_new_lineno
            current_new_lineno += 1

            modified_line = line
            for rule_id, pattern in SECRET_PATTERNS.items():
                match = re.search(pattern, modified_line)
                if match:
                    secret_token = match.group(1)
                    if "<REDACTED:" in secret_token:
                        continue
                    if rule_id == "GENERIC_HIGH_ENTROPY_SECRET":
                        if any(p in secret_token.lower() for p in PLACEHOLDER_WORDS):
                            continue
                        if calculate_shannon_entropy(secret_token) < 3.5:
                            continue

                    entropy = calculate_shannon_entropy(secret_token)
                    redacted_replacement = f"<REDACTED:{rule_id}>"
                    modified_line = (
                        modified_line[: match.start(1)]
                        + redacted_replacement
                        + modified_line[match.end(1) :]
                    )
                    findings.append(
                        RedactedSecretMatch(
                            rule_id=rule_id,
                            file_path=current_file,
                            line_number=target_lineno,
                            redacted_snippet=modified_line.strip(),
                            entropy=round(entropy, 2),
                        )
                    )
                    break

            sanitized_lines.append(modified_line)
            continue

        if line.startswith("-") and not line.startswith("---"):
            sanitized_lines.append(line)
            continue

        if line.startswith(" ") or line == "":
            current_new_lineno += 1
            sanitized_lines.append(line)
            continue

        sanitized_lines.append(line)

    sanitized_diff_text = "\n".join(sanitized_lines)
    if diff_text.endswith("\n") and not sanitized_diff_text.endswith("\n"):
        sanitized_diff_text += "\n"

    return SecretScanResult(
        sanitized_diff_text=sanitized_diff_text,
        findings=findings,
        has_secrets=len(findings) > 0,
    )
