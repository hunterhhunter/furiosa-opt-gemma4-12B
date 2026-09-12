import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .kernels import KERNELS


@dataclass
class ArenaAttempt:
    job_id: int | None = None
    status: str | None = None
    exit_code: int | None = None
    accuracy_passed: bool = False
    kernels: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArenaParseError(ValueError):
    def __init__(self, message: str, partial: ArenaAttempt):
        super().__init__(message)
        self.partial = partial


JOB_ID_PATTERN = re.compile(r"submitted job\s+([0-9]+)", re.IGNORECASE)
HEADING_PATTERN = re.compile(
    r"^==>\s+(sliding_project_qkv|sliding_attention_output|decoder_feedforward)\s*$"
)
CYCLE_PATTERN = re.compile(r"cycles=([0-9]+)")
TERMINAL_PATTERN = re.compile(
    r"==>\s+job\s+[0-9]+\s+(succeeded|failed|completed|cancelled|canceled)\s+\(exit\s+([^\)]+)\)",
    re.IGNORECASE,
)


def parse_job_id_line(line: str) -> int | None:
    match = JOB_ID_PATTERN.search(line)
    return int(match.group(1)) if match else None


def parse_arena_log(text: str) -> ArenaAttempt:
    parsed = ArenaAttempt()
    job_match = JOB_ID_PATTERN.search(text)
    if job_match:
        parsed.job_id = int(job_match.group(1))
    terminal = TERMINAL_PATTERN.search(text)
    if terminal:
        parsed.status = terminal.group(1).upper()
        try:
            parsed.exit_code = int(terminal.group(2))
        except ValueError:
            parsed.exit_code = None
    else:
        for line in text.splitlines():
            if '"status"' not in line:
                continue
            try:
                status_record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(status_record, dict):
                status = status_record.get("status")
                if isinstance(status, str):
                    parsed.status = status.upper()
                exit_code = status_record.get("exit_code")
                if isinstance(exit_code, int):
                    parsed.exit_code = exit_code

    current: str | None = None
    pass_markers: dict[str, list[bool]] = {name: [] for name in KERNELS}
    for line in text.splitlines():
        heading = HEADING_PATTERN.match(line.strip())
        if heading:
            current = heading.group(1)
            parsed.kernels.setdefault(current, {})
            continue
        if current is None:
            continue
        if "->PASS" in line:
            pass_markers[current].append(True)
        elif "->FAIL" in line:
            pass_markers[current].append(False)
        cycle = CYCLE_PATTERN.search(line)
        if cycle:
            parsed.kernels[current]["cycles"] = int(cycle.group(1))
            current = None

    for name, result in parsed.kernels.items():
        markers = pass_markers[name]
        result["passed"] = bool(markers) and all(markers)

    missing = [
        name
        for name in KERNELS
        if name not in parsed.kernels or "cycles" not in parsed.kernels[name]
    ]
    if missing:
        raise ArenaParseError(
            f"missing cycle result for: {', '.join(missing)}",
            parsed,
        )
    parsed.accuracy_passed = all(
        parsed.kernels[name].get("passed") is True for name in KERNELS
    )
    return parsed
