"""What a node must show for its work before anything may depend on it.

A zero exit code is not evidence. An Agent CLI that was blocked, or that
decided the task was unnecessary, or that explained at length why it could
not proceed, exits zero and is recorded a success — and the node after it
starts on the assumption that a file exists, or that a change was made. §5:
"CLI 返回零退出码不等于任务完成."

So a node may declare what would have to be true for its work to count, and
that is checked after it runs, against the real project directory.

Deliberately declarative, and deliberately no command to run. "the tests
pass" is the acceptance everybody reaches for first, and it is exactly what
a workflow may never express here: this repository's founding rule is that a
workflow can *select* a reviewed command and never *describe* one. Command-
shaped acceptance belongs to the reviewed dev tools, which §3.2 keeps out of
project-access mode; until those two are reconciled, acceptance is what can
be established by looking at the files. That is a real limit, and saying so
is better than offering a `run:` field that would quietly become the hole in
the middle of the design.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CHECKS = ("files_exist", "files_non_empty", "json_valid", "files_changed")


class AcceptanceUnmet(RuntimeError):
    """A node finished, and could not show what it was asked to show."""

    def __init__(self, failures: Sequence["AcceptanceFailure"]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(item.describe() for item in self.failures))


@dataclass(frozen=True)
class AcceptanceFailure:
    check: str
    path: str
    detail: str

    def describe(self) -> str:
        return f"{self.check} {self.path}: {self.detail}"


@dataclass(frozen=True)
class AcceptanceResult:
    passed: bool
    checked: tuple[str, ...] = ()
    failures: tuple[AcceptanceFailure, ...] = ()

    def to_primitive(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checked": list(self.checked),
            "failures": [
                {"check": item.check, "path": item.path, "detail": item.detail}
                for item in self.failures
            ],
        }


def evaluate(
    config: Mapping[str, Any],
    project_root: Path | str,
    *,
    changed_paths: Iterable[str] = (),
) -> AcceptanceResult:
    """Check one node's declared acceptance against the project.

    `changed_paths` comes from the run's own change summary (§5), so
    `files_changed` asks a question about this run rather than about whatever
    state the directory happens to be in — a file that was already correct
    before the run started is not evidence that this node did anything.
    """

    root = Path(project_root)
    changed = {str(item) for item in changed_paths}
    failures: list[AcceptanceFailure] = []
    checked: list[str] = []

    for relative in config.get("files_exist", ()) or ():
        checked.append(f"files_exist:{relative}")
        if not (root / relative).exists():
            failures.append(AcceptanceFailure(
                "files_exist", relative, "does not exist",
            ))

    for relative in config.get("files_non_empty", ()) or ():
        checked.append(f"files_non_empty:{relative}")
        target = root / relative
        if not target.is_file():
            failures.append(AcceptanceFailure(
                "files_non_empty", relative, "is not a file",
            ))
        elif target.stat().st_size == 0:
            failures.append(AcceptanceFailure(
                "files_non_empty", relative, "is empty",
            ))

    for relative in config.get("json_valid", ()) or ():
        checked.append(f"json_valid:{relative}")
        target = root / relative
        if not target.is_file():
            failures.append(AcceptanceFailure(
                "json_valid", relative, "is not a file",
            ))
            continue
        try:
            json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            failures.append(AcceptanceFailure(
                "json_valid", relative, f"is not valid JSON: {exc}",
            ))

    for relative in config.get("files_changed", ()) or ():
        checked.append(f"files_changed:{relative}")
        if relative not in changed:
            failures.append(AcceptanceFailure(
                "files_changed", relative,
                "was not changed by this run",
            ))

    return AcceptanceResult(
        passed=not failures,
        checked=tuple(checked),
        failures=tuple(failures),
    )
