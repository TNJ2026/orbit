"""Parsing helpers for the text protocol emitted by Agent CLI runners."""

from __future__ import annotations

import json
import re
from typing import Any

from .workflow_data import parse_workflow_result


STEP_SUMMARY_MAX = 2000

_VERDICT_RE = re.compile(
    r"WORKFLOW_OUTCOME\s*[:=]\s*(done|rework|blocked)", re.IGNORECASE
)
_PORT_RE = re.compile(r"WORKFLOW_PORT\s*[:=]\s*([a-z][a-z0-9_-]*)", re.IGNORECASE)
_RESULT_SUMMARY_RE = re.compile(r"(?im)^RESULT_SUMMARY\s*:\s*(.+?)\s*$")
_ARTIFACTS_RE = re.compile(r"(?im)^ARTIFACTS\s*:\s*(\[.*\])\s*$")


def tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[-limit:]


def parse_runner_verdict(text: str) -> str | None:
    """Return the last self-reported workflow outcome, if present."""
    matches = _VERDICT_RE.findall(text or "")
    return matches[-1].lower() if matches else None


def parse_runner_port(text: str) -> str | None:
    """Return the last business output port selected by a runner, if present."""
    structured, _ = parse_workflow_result(text)
    if structured and structured.get("port"):
        return str(structured["port"])
    matches = _PORT_RE.findall(text or "")
    return matches[-1].lower() if matches else None


def parse_step_output_metadata(text: str) -> tuple[str, list[str]]:
    """Extract RESULT_SUMMARY and ARTIFACTS with a legacy text fallback."""
    text = (text or "").strip()
    summaries = _RESULT_SUMMARY_RE.findall(text)
    summary = summaries[-1].strip()[:STEP_SUMMARY_MAX] if summaries else ""
    artifacts: list[str] = []
    artifact_matches = _ARTIFACTS_RE.findall(text)
    if artifact_matches:
        try:
            raw = json.loads(artifact_matches[-1])
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = []
        if isinstance(raw, list):
            for item in raw:
                value = str(item).strip() if isinstance(item, str) else ""
                if value and value not in artifacts:
                    artifacts.append(value[:1000])
                if len(artifacts) >= 100:
                    break
    if not summary:
        legacy_lines = [
            line
            for line in text.splitlines()
            if not re.match(
                r"(?i)^\s*(WORKFLOW_OUTCOME|WORKFLOW_PORT|WORKFLOW_RESULT|TOKENS_USED|RESULT_SUMMARY|ARTIFACTS)\s*[:=]",
                line,
            )
        ]
        summary = tail("\n".join(legacy_lines), STEP_SUMMARY_MAX)
    return summary, artifacts


def normalized_step_result(text: str, port: str = "") -> tuple[dict[str, Any], str]:
    """Normalize structured JSON or legacy text into one handler result shape."""
    structured, error = parse_workflow_result(text)
    legacy_summary, legacy_artifacts = parse_step_output_metadata(text)
    if structured is None:
        return {
            "port": port,
            "output": {},
            "summary": legacy_summary,
            "artifacts": legacy_artifacts,
        }, error
    if not structured.get("port"):
        structured["port"] = port
    if not structured.get("summary"):
        structured["summary"] = legacy_summary
    if not structured.get("artifacts"):
        structured["artifacts"] = legacy_artifacts
    return structured, error


def structured_upstream(result: str) -> str:
    """Build compact downstream context from a completed step's output."""
    normalized, _ = normalized_step_result(result)
    summary = str(normalized.get("summary") or "")
    artifacts = normalized.get("artifacts") or []
    output = normalized.get("output") or {}
    lines: list[str] = []
    if summary:
        lines.append(summary)
    if artifacts:
        lines.append("ARTIFACTS:")
        lines.extend(
            f"- {artifact.get('uri', '') if isinstance(artifact, dict) else artifact}"
            for artifact in artifacts
        )
        lines.append("（完整细节请直接读取上述产物文件/引用，不要依赖摘要复述。）")
    if output:
        lines.append("STRUCTURED_OUTPUT:")
        lines.append(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


# CLI-native token formats are preferred over the self-reported fallback.
_NATIVE_TOKEN_PATTERNS = (
    re.compile(r"tokens used\s*[\r\n]+\s*([\d,]+)", re.IGNORECASE),
)
_SELF_REPORT_TOKEN_RE = re.compile(
    r"TOKENS_USED\s*[:=]\s*([\d,]+)", re.IGNORECASE
)
_DECOMPOSE_TOKEN_RE = re.compile(
    r'"tokens_used"\s*:\s*"?([\d,]+)"?', re.IGNORECASE
)


def parse_run_tokens(stdout: str, stderr: str) -> int | None:
    """Read native or self-reported token usage from one CLI run."""
    text = f"{stderr or ''}\n{stdout or ''}"
    for pattern in _NATIVE_TOKEN_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            return int(matches[-1].replace(",", ""))
    matches = _SELF_REPORT_TOKEN_RE.findall(text)
    if matches:
        return int(matches[-1].replace(",", ""))
    matches = _DECOMPOSE_TOKEN_RE.findall(text)
    if matches:
        return int(matches[-1].replace(",", ""))
    return None


_USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _sum_usage_tokens(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    values = [
        usage[field]
        for field in _USAGE_TOKEN_FIELDS
        if isinstance(usage.get(field), int)
    ]
    return sum(values) if values else None


def parse_claude_json_output(stdout: str) -> tuple[str, int | None] | None:
    """Decode Claude's stream-json or single-object JSON output."""
    raw = stdout or ""
    candidates = [line for line in raw.splitlines() if line.lstrip().startswith("{")]
    if not candidates and raw.lstrip().startswith("{"):
        candidates = [raw]
    events: list[dict[str, Any]] = []
    for line in candidates:
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    if not events or not any(event.get("type") for event in events):
        return None
    text: str | None = None
    tokens: int | None = None
    for event in events:
        if event.get("type") == "result":
            if isinstance(event.get("result"), str):
                text = event["result"]
            tokens = _sum_usage_tokens(event.get("usage"))
    if text is None:
        parts: list[str] = []
        for event in events:
            if event.get("type") != "assistant":
                continue
            for block in ((event.get("message") or {}).get("content") or []):
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
        text = "\n".join(parts).strip()
    if tokens is None:
        total, seen = 0, False
        for event in events:
            if event.get("type") != "assistant":
                continue
            count = _sum_usage_tokens((event.get("message") or {}).get("usage"))
            if count is not None:
                total, seen = total + count, True
        tokens = total if seen else None
    return text, tokens


def parse_gemini_json_output(stdout: str) -> tuple[str, int | None] | None:
    """Decode Gemini's JSON response and sum per-model token totals."""
    raw = (stdout or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return None
    if (
        not isinstance(obj, dict)
        or "response" not in obj
        or not isinstance(obj.get("stats"), dict)
    ):
        return None
    text = str(obj.get("response") or "")
    models = obj["stats"].get("models")
    total, seen = 0, False
    if isinstance(models, dict):
        for model in models.values():
            count = ((model or {}).get("tokens") or {}).get("total")
            if isinstance(count, int):
                total, seen = total + count, True
    return text, (total if seen else None)


_AGENT_OUTPUT_PARSERS = (parse_claude_json_output, parse_gemini_json_output)


def normalize_agent_output(stdout: str, stderr: str) -> tuple[str, int | None]:
    """Normalize structured and plain-text Agent CLI output."""
    for parser in _AGENT_OUTPUT_PARSERS:
        parsed = parser(stdout)
        if parsed is not None:
            text, tokens = parsed
            if tokens is None:
                tokens = parse_run_tokens(text or "", stderr)
            return (text if text else stdout), tokens
    return stdout, parse_run_tokens(stdout, stderr)
