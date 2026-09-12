import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .state import ExperimentState


NEXT_COMMANDS = {
    ExperimentState.CREATED: "baseline",
    ExperimentState.BASELINE_READY: "candidate",
    ExperimentState.CANDIDATE_READY: "analyze",
    ExperimentState.READY_FOR_ARENA: "arena",
    ExperimentState.ARENA_RUNNING: "sync-arena",
    ExperimentState.ARENA_PASSED: "decide",
    ExperimentState.ARENA_FAILED: "decide",
}


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"event line {line_number} is not an object")
        events.append(value)
    return events


def next_command(manifest: Mapping[str, Any]) -> str | None:
    state = ExperimentState(manifest["state"])
    command = NEXT_COMMANDS.get(state)
    if command is None:
        return None
    return f"python3 pipeline/optimize.py {command} {manifest['experiment_id']}"


def render_experiment_readme(
    manifest: Mapping[str, Any], events: Iterable[Mapping[str, Any]]
) -> str:
    source = manifest["source"]
    schedule = manifest["schedule"]
    attempts = manifest["arena"]["attempts"]
    decision = manifest["decision"]
    lines = [
        f"# {manifest['name']}",
        "",
        f"- Experiment: {manifest['experiment_id']}",
        f"- Kernel: {manifest['kernel']['name']}",
        f"- State: {manifest['state']}",
        f"- Hypothesis: {manifest['hypothesis']}",
        "",
        "## Source",
        "",
        f"- Base commit: {manifest['git']['base_commit']}",
        f"- Baseline fingerprint: {source.get('baseline_fingerprint') or '-'}",
        f"- Candidate fingerprint: {source.get('candidate_fingerprint') or '-'}",
        "- Change: source/candidate.patch",
        "",
        "## Static result",
        "",
        f"- Baseline schedule: {_artifact_path(schedule.get('baseline'))}",
        f"- Candidate schedule: {_artifact_path(schedule.get('candidate'))}",
        f"- Review: {schedule.get('analysis_path') or '-'}",
        "",
        "## RNGD result",
        "",
    ]
    if attempts:
        latest = attempts[-1]
        lines.extend(
            [
                f"- Attempt: {latest.get('attempt', '-')}",
                f"- Arena Job: {latest.get('job_id', '-')}",
                f"- Accuracy: {'PASS' if latest.get('accuracy_passed') is True else 'FAIL'}",
            ]
        )
        for name, result in latest.get("kernels", {}).items():
            lines.append(f"- {name} cycle: {result.get('cycles', '-')}")
    else:
        lines.append("- No Arena attempt")
    lines.extend(["", "## Decision", ""])
    if decision:
        lines.append(f"{decision['result']}: {decision['reason']}")
    else:
        lines.append("Pending")
    command = next_command(manifest)
    if command:
        lines.extend(["", "## Next command", "", f"```bash\n{command}\n```"])
    lines.extend(["", "## Timeline", ""])
    timeline = list(events)
    if timeline:
        for event in timeline:
            lines.append(f"- {event['at']} {event['event']} ({event['result']})")
    else:
        lines.append("- No events")
    return "\n".join(lines) + "\n"


def _artifact_path(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("path") or "-")
    return "-"
