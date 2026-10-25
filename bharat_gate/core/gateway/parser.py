import json
import re
from typing import Any
from pydantic import BaseModel, ValidationError

from bharat_gate.core.schemas import (
    EndpointType,
    FunctionCallItem,
    ParsedTextResult,
    SchemaValidationResult,
    TruncationCheckResult,
)


def extract_think_and_clean_text(raw_text: str) -> ParsedTextResult:
    """Extracts <think> tags from text and returns clean output along with reasoning trace."""
    if not raw_text:
        return ParsedTextResult(clean_text="", reasoning_trace=None, has_unclosed_think_tag=False)

    reasoning_blocks = re.findall(r"<think>(.*?)</think>", raw_text, flags=re.DOTALL | re.IGNORECASE)
    leftover = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL | re.IGNORECASE)

    has_unclosed_think_tag = False
    unclosed_match = re.search(r"<think>", leftover, flags=re.IGNORECASE)
    if unclosed_match:
        has_unclosed_think_tag = True
        before = leftover[: unclosed_match.start()]
        after = leftover[unclosed_match.end() :]
        reasoning_blocks.append(after)
        clean_text = before
    else:
        clean_text = leftover

    cleaned_blocks = [b.strip() for b in reasoning_blocks if b.strip()]
    reasoning_trace = "\n\n---\n\n".join(cleaned_blocks) if cleaned_blocks else None
    return ParsedTextResult(
        clean_text=clean_text.strip(),
        reasoning_trace=reasoning_trace,
        has_unclosed_think_tag=has_unclosed_think_tag,
    )


def check_response_truncation(
    payload: dict[str, Any],
    endpoint: EndpointType,
    has_unclosed_think_tag: bool = False,
) -> TruncationCheckResult:
    """Checks whether the response was truncated by the model or provider."""
    if endpoint == "/responses":
        status = payload.get("status")
        incomplete_details = payload.get("incomplete_details") or {}
        reason = incomplete_details.get("reason")
        if status == "incomplete" or reason == "max_output_tokens":
            return TruncationCheckResult(
                is_truncated=True,
                reason=reason or "max_output_tokens",
            )
    elif endpoint == "/chat/completions":
        choices = payload.get("choices") or []
        if choices and isinstance(choices, list) and choices[0].get("finish_reason") == "length":
            return TruncationCheckResult(is_truncated=True, reason="length")

    if has_unclosed_think_tag:
        return TruncationCheckResult(is_truncated=True, reason="unclosed_think_tag")

    return TruncationCheckResult(is_truncated=False, reason=None)


def parse_responses_output_items(
    output_items: list[dict[str, Any]],
) -> tuple[str, list[FunctionCallItem]]:
    """Parses output items from BharatCode /v1/responses format into clean text and function calls."""
    text_parts: list[str] = []
    function_calls: list[FunctionCallItem] = []

    for item in output_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            content = item.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                        text_val = part.get("text", "")
                        if text_val:
                            text_parts.append(text_val)
        elif item_type == "function_call":
            call_id = str(item.get("call_id") or "")
            name = str(item.get("name") or "")
            raw_args = item.get("arguments")
            if isinstance(raw_args, dict):
                args_str = json.dumps(raw_args)
            elif raw_args is None:
                args_str = "{}"
            else:
                args_str = str(raw_args)
            function_calls.append(
                FunctionCallItem(
                    call_id=call_id,
                    name=name,
                    arguments=args_str,
                )
            )

    return ("\n".join(text_parts).strip(), function_calls)


def validate_json_with_schema(
    raw_text: str,
    schema_cls: type[BaseModel],
) -> SchemaValidationResult:
    """Validates and extracts JSON from text against a Pydantic schema class."""
    stripped_text = extract_think_and_clean_text(raw_text).clean_text

    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped_text, flags=re.IGNORECASE)
    candidate_json: str | None = None
    if fence_match:
        candidate_json = fence_match.group(1).strip()
    else:
        start_idx = stripped_text.find("{")
        end_idx = stripped_text.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
            candidate_json = stripped_text[start_idx : end_idx + 1].strip()

    if not candidate_json:
        return SchemaValidationResult(
            is_valid=False,
            validated_data=None,
            raw_json_extracted=None,
            error_summary="No JSON object found in text",
        )

    try:
        instance = schema_cls.model_validate_json(candidate_json)
        return SchemaValidationResult(
            is_valid=True,
            validated_data=instance,
            raw_json_extracted=candidate_json,
            error_summary=None,
        )
    except ValidationError as e:
        error_messages = [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in e.errors()
        ]
        return SchemaValidationResult(
            is_valid=False,
            validated_data=None,
            raw_json_extracted=candidate_json,
            error_summary="; ".join(error_messages),
        )
    except (json.JSONDecodeError, ValueError) as e:
        return SchemaValidationResult(
            is_valid=False,
            validated_data=None,
            raw_json_extracted=candidate_json,
            error_summary=str(e),
        )
