import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class ArtifactError(RuntimeError):
    """Raised when an experiment artifact cannot be created or located safely."""


EXPERIMENT_ID_PATTERN = re.compile(
    r"^\d{8}-\d{6}-[a-z0-9]+(?:-[a-z0-9]+)*(?:-\d{2})?$"
)


@dataclass(frozen=True)
class ExperimentPaths:
    repo: Path
    kernel: str
    experiment_id: str
    root: Path

    @classmethod
    def create(cls, repo: Path, kernel: str, experiment_id: str) -> "ExperimentPaths":
        repo = repo.resolve()
        pipeline_root = (repo / "target/pipeline").resolve()
        root = (pipeline_root / kernel / experiment_id).resolve()
        try:
            root.relative_to(pipeline_root)
        except ValueError as error:
            raise ArtifactError("experiment path escapes target/pipeline") from error
        if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
            raise ArtifactError(f"invalid experiment id: {experiment_id!r}")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", kernel):
            raise ArtifactError(f"invalid kernel name: {kernel!r}")
        return cls(repo=repo, kernel=kernel, experiment_id=experiment_id, root=root)

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def source(self) -> Path:
        return self.root / "source"

    @property
    def schedule(self) -> Path:
        return self.root / "schedule"

    @property
    def build(self) -> Path:
        return self.root / "build"

    @property
    def arena(self) -> Path:
        return self.root / "arena"

    @property
    def decision(self) -> Path:
        return self.root / "decision.md"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ArtifactError("name must contain an ASCII letter or digit")
    return slug


def make_experiment_id(name: str, timestamp: str, existing: set[str]) -> str:
    if not re.fullmatch(r"\d{8}-\d{6}", timestamp):
        raise ArtifactError(f"invalid timestamp: {timestamp!r}")
    base = f"{timestamp}-{_slugify(name)}"
    if base not in existing:
        return base
    sequence = 2
    while f"{base}-{sequence:02d}" in existing:
        sequence += 1
    return f"{base}-{sequence:02d}"


def find_experiment(repo: Path, experiment_id: str) -> ExperimentPaths:
    if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
        raise ArtifactError(f"invalid experiment id: {experiment_id!r}")
    pipeline_root = repo.resolve() / "target/pipeline"
    matches = [
        path
        for path in pipeline_root.glob(f"*/{experiment_id}")
        if path.is_dir() and (path / "manifest.json").is_file()
    ]
    if not matches:
        raise ArtifactError(f"experiment not found: {experiment_id}")
    if len(matches) > 1:
        raise ArtifactError(f"experiment id is ambiguous: {experiment_id}")
    return ExperimentPaths.create(repo, matches[0].parent.name, experiment_id)


def _submission_files(root: Path) -> Iterable[tuple[str, Path]]:
    root = root.resolve()
    candidates = [root / "src/ops.rs"]
    device = root / "src/device"
    if device.is_dir():
        candidates.extend(sorted(device.rglob("*")))
    for path in sorted(candidates, key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as error:
            raise ArtifactError(f"source path escapes root: {path}") from error
        yield relative, path


def source_fingerprint(root: Path) -> str:
    entries = list(_submission_files(root))
    if not any(relative == "src/ops.rs" for relative, _ in entries):
        raise ArtifactError(f"missing submission source: {root / 'src/ops.rs'}")
    combined = hashlib.sha256()
    for relative, path in entries:
        per_file = hashlib.sha256()
        per_file.update(relative.encode("utf-8"))
        per_file.update(b"\0")
        per_file.update(path.read_bytes())
        per_file.update(b"\0")
        combined.update(per_file.digest())
    return combined.hexdigest()


def snapshot_submission_source(repo: Path, destination: Path) -> None:
    if destination.exists():
        raise ArtifactError(f"snapshot destination already exists: {destination}")
    files = list(_submission_files(repo))
    if not any(relative == "src/ops.rs" for relative, _ in files):
        raise ArtifactError(f"missing submission source: {repo / 'src/ops.rs'}")
    for relative, source in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def snapshot_git_submission_source(repo: Path, commit: str, destination: Path) -> None:
    if destination.exists():
        raise ArtifactError(f"snapshot destination already exists: {destination}")
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit, "--", "src/ops.rs", "src/device"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        raise ArtifactError(f"cannot read base commit {commit}: {listed.stderr.strip()}")
    files = [line for line in listed.stdout.splitlines() if line]
    if "src/ops.rs" not in files:
        raise ArtifactError(f"base commit {commit} has no src/ops.rs")
    for relative in files:
        if relative != "src/ops.rs" and not relative.startswith("src/device/"):
            continue
        shown = subprocess.run(
            ["git", "show", f"{commit}:{relative}"],
            cwd=repo,
            capture_output=True,
            check=False,
        )
        if shown.returncode != 0:
            stderr = shown.stderr.decode("utf-8", errors="replace")
            raise ArtifactError(f"cannot read {relative} at {commit}: {stderr.strip()}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(shown.stdout)


def create_patch(baseline: Path, candidate: Path, patch_path: Path) -> None:
    if patch_path.exists():
        raise ArtifactError(f"patch already exists: {patch_path}")
    with tempfile.TemporaryDirectory(prefix="optcycle-patch-") as temp_name:
        temp = Path(temp_name)
        shutil.copytree(baseline / "src", temp / "left/src")
        shutil.copytree(candidate / "src", temp / "right/src")
        result = subprocess.run(
            [
                "git",
                "diff",
                "--no-index",
                "--binary",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "left/src",
                "right/src",
            ],
            cwd=temp,
            text=False,
            capture_output=True,
            check=False,
        )
        if result.returncode not in (0, 1):
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise ArtifactError(f"git diff failed: {stderr.strip()}")
        patch = result.stdout.replace(b"a/left/src/", b"a/src/")
        patch = patch.replace(b"b/right/src/", b"b/src/")
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_bytes(patch)


def commit_artifacts(moves: list[tuple[Path, Path]]) -> None:
    for source, destination in moves:
        if not source.exists():
            raise ArtifactError(f"temporary artifact is missing: {source}")
        if destination.exists():
            raise ArtifactError(f"artifact already exists: {destination}")
    completed: list[tuple[Path, Path]] = []
    try:
        for source, destination in moves:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            completed.append((source, destination))
    except OSError as error:
        rollback_errors = []
        for source, destination in reversed(completed):
            try:
                os.replace(destination, source)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        detail = f"artifact commit failed: {error}"
        if rollback_errors:
            detail += f"; rollback failed: {'; '.join(rollback_errors)}"
        raise ArtifactError(detail) from error


def build_export_manifest(local_manifest: dict) -> dict:
    schedule = local_manifest["schedule"]
    attempts = []
    for source in local_manifest["arena"]["attempts"]:
        attempts.append(
            {
                key: source.get(key)
                for key in (
                    "attempt",
                    "job_id",
                    "job_name",
                    "status",
                    "exit_code",
                    "accuracy_passed",
                    "kernels",
                )
            }
        )
    return {
        "schema_version": 1,
        "experiment_id": local_manifest["experiment_id"],
        "kernel": dict(local_manifest["kernel"]),
        "name": local_manifest["name"],
        "hypothesis": local_manifest["hypothesis"],
        "state": local_manifest["state"],
        "created_at": local_manifest["created_at"],
        "updated_at": local_manifest["updated_at"],
        "git": {"base_commit": local_manifest["git"]["base_commit"]},
        "source": {
            "baseline_fingerprint": local_manifest["source"].get("baseline_fingerprint"),
            "candidate_fingerprint": local_manifest["source"].get("candidate_fingerprint"),
        },
        "schedule": {
            "baseline_sha256": (schedule.get("baseline") or {}).get("sha256"),
            "candidate_sha256": (schedule.get("candidate") or {}).get("sha256"),
            "analysis_sha256": schedule.get("analysis_sha256"),
            "approved_note": schedule.get("approved_note"),
        },
        "arena": {"attempts": attempts},
        "decision": dict(local_manifest["decision"]),
    }


def validate_export_content(value, repo: Path, key: str = "root") -> None:
    forbidden_keys = ("token", "secret", "credential", "environment")
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            if any(term in str(child_key).lower() for term in forbidden_keys):
                raise ArtifactError(f"forbidden export key: {child_key}")
            validate_export_content(child_value, repo, str(child_key))
    elif isinstance(value, list):
        for child in value:
            validate_export_content(child, repo, key)
    elif isinstance(value, str):
        if repo.resolve().as_posix() in value:
            raise ArtifactError(f"repository absolute path found in export field {key}")
        if Path(value).is_absolute():
            raise ArtifactError(f"absolute path found in export field {key}")
