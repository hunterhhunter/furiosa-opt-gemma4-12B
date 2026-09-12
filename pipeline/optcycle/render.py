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


def render_analysis_template(manifest: Mapping[str, Any]) -> str:
    baseline = _artifact_path(manifest["schedule"].get("baseline"))
    candidate = _artifact_path(manifest["schedule"].get("candidate"))
    return f"""# Static schedule review: {manifest['experiment_id']}

This document records a human review. Blank fields and checked boxes are reviewer
decisions; the pipeline does not infer an improvement from this template.

- Baseline schedule: `{baseline}`
- Candidate schedule: `{candidate}`
- Kernel: `{manifest['kernel']['name']}`
- Hypothesis: {manifest['hypothesis']}

## Makespan

- [ ] Baseline value:
- [ ] Candidate value:
- [ ] Interpretation:

## Context occupancy

- [ ] Idle regions and utilization:
- [ ] Context pressure:

## Overlap

- [ ] Compute and transfer overlap:
- [ ] Serialization points:

## Source hotspots

- [ ] Viewer hotspot mapped to source:
- [ ] Relevant operation or loop:

## Memory

- [ ] TDMA/PDMA behavior:
- [ ] SRAM/VRF pressure and copies:

## Hypothesis evaluation

- [ ] Evidence supporting or rejecting the hypothesis:
- [ ] Possible confounders:

## Arena recommendation

- [ ] Proceed to Arena validation
- [ ] Revise candidate in a new experiment
- Reviewer conclusion:
"""


def render_decision(manifest: Mapping[str, Any]) -> str:
    decision = manifest["decision"]
    return f"""# Experiment decision

- Experiment: {manifest['experiment_id']}
- Result: {decision['result']}
- Decided at: {decision['at']}
- Reason: {decision['reason']}

This record changes no source file or Git state.
"""


def render_reproduce(manifest: Mapping[str, Any]) -> str:
    experiment_id = manifest["experiment_id"]
    worktree = f"../reproduce-{experiment_id}"
    record = f"$PWD/pipeline/records/{experiment_id}"
    rust_path = manifest["kernel"]["rust_path"]
    return f"""# Reproduce {experiment_id}

Review both patches before applying them. These commands use a separate Git
worktree and do not change the current checkout.

```bash
git worktree add {worktree} {manifest['git']['base_commit']}

git -C {worktree} apply --check "{record}/baseline.patch"
git -C {worktree} apply "{record}/baseline.patch"
git -C {worktree} apply --check "{record}/candidate.patch"
git -C {worktree} apply "{record}/candidate.patch"

cargo furiosa-opt compile {rust_path} --exact \\
  --manifest-path {worktree}/Cargo.toml \\
  --dump-schedule {worktree}/target/reproduced.schedule.json
```

Tool versions should be captured when reproducing:

```bash
rustup show active-toolchain
cargo furiosa-opt --version
furiosa-arena --version
```

The recorded RNGD cycles and any new measurement are separate observations.
"""


def _artifact_path(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("path") or "-")
    return "-"
