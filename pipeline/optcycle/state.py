from enum import Enum
from typing import Collection, Mapping, Any

from .kernels import KERNELS


class StateError(RuntimeError):
    """Raised when a command is invalid for the current experiment state."""


class ExperimentState(str, Enum):
    CREATED = "CREATED"
    BASELINE_READY = "BASELINE_READY"
    CANDIDATE_READY = "CANDIDATE_READY"
    READY_FOR_ARENA = "READY_FOR_ARENA"
    ARENA_RUNNING = "ARENA_RUNNING"
    ARENA_PASSED = "ARENA_PASSED"
    ARENA_FAILED = "ARENA_FAILED"
    KEPT = "KEPT"
    REJECTED = "REJECTED"


def require_state(
    current: ExperimentState,
    allowed: Collection[ExperimentState],
    command: str,
) -> None:
    if current in allowed:
        return
    required = ", ".join(sorted(state.value for state in allowed))
    raise StateError(f"{command} requires {required}; current state is {current.value}")


def can_keep(manifest: Mapping[str, Any]) -> bool:
    for attempt in manifest.get("arena", {}).get("attempts", []):
        if attempt.get("accuracy_passed") is not True:
            continue
        kernels = attempt.get("kernels", {})
        if all(
            isinstance(kernels.get(name, {}).get("cycles"), int)
            and kernels[name]["cycles"] >= 0
            for name in KERNELS
        ):
            return True
    return False
