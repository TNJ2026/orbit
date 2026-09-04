"""Built-in child adapters for the machine Background Agent Worker."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping


RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "result_json": {
            "type": "string",
            "description": "A JSON-encoded object containing the workflow step result.",
        },
    },
    "required": ["result_json"],
    "additionalProperties": False,
}


def _request(source=None) -> Mapping[str, Any]:
    try:
        value = json.load(sys.stdin if source is None else source)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"delegation request is not valid JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError("delegation request must be an object")
    return value


def _prompt(request: Mapping[str, Any]) -> str:
    task = request.get("input")
    return (
        "Execute the following Orbit workflow Agent step in the current working "
        "directory. Respect the repository instructions and the sandbox. Return "
        "a JSON object matching the supplied schema. Put the complete business "
        "result object, JSON-encoded, in result_json. Do not wrap the response "
        "in Markdown.\n\n"
        "Orbit delegation request:\n"
        + json.dumps(task, ensure_ascii=False, sort_keys=True)
    )


def run_codex(request: Mapping[str, Any], *, executable: str | None = None) -> Mapping[str, Any]:
    codex = executable or shutil.which("codex")
    if not codex:
        raise RuntimeError("Codex CLI is not installed or is not on PATH")
    config = request.get("config") or {}
    if not isinstance(config, Mapping):
        raise ValueError("delegation config must be an object")
    effects = str(config.get("effects", "read"))
    sandbox = "workspace-write" if effects == "write" else "read-only"
    timeout = int(config.get("max_wall_seconds", 1800))
    with tempfile.TemporaryDirectory(prefix="orbit-codex-agent-") as temporary:
        root = Path(temporary)
        schema = root / "result-schema.json"
        answer = root / "answer.json"
        schema.write_text(json.dumps(RESULT_SCHEMA), encoding="utf-8")
        command = [
            codex, "exec", "--skip-git-repo-check", "--ephemeral",
            "--sandbox", sandbox, "--color", "never",
            "--output-schema", str(schema),
            "--output-last-message", str(answer), "-",
        ]
        completed = subprocess.run(
            command, input=_prompt(request), text=True, capture_output=True,
            timeout=timeout, check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                detail[-4000:] or f"Codex exited with status {completed.returncode}"
            )
        try:
            envelope = json.loads(answer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Codex did not return a valid JSON object: {exc}") from exc
        if not isinstance(envelope, Mapping) or not isinstance(
            envelope.get("result_json"), str
        ):
            raise RuntimeError("Codex result is missing result_json")
        try:
            result = json.loads(envelope["result_json"])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Codex result_json is invalid: {exc}") from exc
        if not isinstance(result, Mapping):
            raise RuntimeError("Codex result is not an object")
        return dict(result)


def main() -> None:
    backend = sys.argv[1] if len(sys.argv) > 1 else "codex"
    try:
        request = _request()
        if backend != "codex":
            raise ValueError(f"unknown background Agent backend: {backend}")
        result = run_codex(request)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
