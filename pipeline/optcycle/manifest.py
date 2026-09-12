import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .kernels import KernelSpec
from .state import ExperimentState


class ManifestError(ValueError):
    """Raised when a manifest does not satisfy the supported schema."""


REQUIRED_TOP_LEVEL_KEYS = {
    "schema_version",
    "experiment_id",
    "kernel",
    "name",
    "hypothesis",
    "state",
    "created_at",
    "updated_at",
    "git",
    "source",
    "schedule",
    "arena",
    "decision",
}


def new_manifest(
    *,
    experiment_id: str,
    kernel: KernelSpec,
    name: str,
    hypothesis: str,
    now: str,
    base_commit: str,
    initial_status: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "kernel": {"name": kernel.name, "rust_path": kernel.rust_path},
        "name": name,
        "hypothesis": hypothesis,
        "state": ExperimentState.CREATED.value,
        "created_at": now,
        "updated_at": now,
        "git": {
            "base_commit": base_commit,
            "initial_status": list(initial_status),
        },
        "source": {
            "baseline_fingerprint": None,
            "candidate_fingerprint": None,
        },
        "schedule": {
            "baseline": None,
            "candidate": None,
            "analysis_path": None,
            "approved_note": None,
        },
        "arena": {"attempts": []},
        "decision": None,
    }


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ManifestError("unsupported schema_version; expected 1")
    missing = sorted(REQUIRED_TOP_LEVEL_KEYS - manifest.keys())
    if missing:
        raise ManifestError(f"missing required manifest field: {', '.join(missing)}")
    try:
        ExperimentState(manifest["state"])
    except (KeyError, ValueError) as error:
        raise ManifestError(f"invalid state: {manifest.get('state')!r}") from error


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot load manifest {path}: {error}") from error
    if not isinstance(manifest, dict):
        raise ManifestError("manifest root must be an object")
    validate_manifest(manifest)
    return manifest


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    validate_manifest(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ManifestError(f"cannot write manifest {path}: {error}") from error
