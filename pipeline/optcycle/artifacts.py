import hashlib
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
        return self.root / "decision"


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
