import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence, TextIO, Any

from .artifacts import (
    ArtifactError,
    ExperimentPaths,
    find_experiment,
    make_experiment_id,
    source_fingerprint,
)
from .kernels import KERNELS, KernelError, get_kernel
from .manifest import ManifestError, load_manifest, new_manifest, write_manifest
from .render import read_events, render_experiment_readme
from .state import StateError


class CliError(RuntimeError):
    """Raised for a user-actionable CLI failure."""


def append_event(
    path: Path,
    *,
    at: str,
    event: str,
    result: str,
    exit_code: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    allowed_metadata = {"attempt", "job_id", "state"}
    record: dict[str, Any] = {"at": at, "event": event, "result": result}
    if exit_code is not None:
        record["exit_code"] = exit_code
    for key, value in (metadata or {}).items():
        if key not in allowed_metadata:
            raise CliError(f"event metadata field is not allowed: {key}")
        record[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def refresh_readme(paths: ExperimentPaths, manifest: dict[str, Any]) -> None:
    text = render_experiment_readme(manifest, read_events(paths.events))
    (paths.root / "README.md").write_text(text, encoding="utf-8")


def discover_repo_root(start: Path | None = None) -> Path:
    candidate = (start or Path(__file__)).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / "Cargo.toml").is_file() and (directory / "src/ops.rs").is_file():
            return directory
    raise CliError("repository root not found (requires Cargo.toml and src/ops.rs)")


def _git_lines(repo: Path, *arguments: str) -> list[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise CliError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.splitlines()


def _create(args: argparse.Namespace, repo: Path, stdout: TextIO, now: Callable[[], datetime]) -> int:
    kernel = get_kernel(args.kernel)
    timestamp_value = now()
    timestamp = timestamp_value.strftime("%Y%m%d-%H%M%S")
    pipeline_root = repo / "target/pipeline"
    existing = {path.name for path in pipeline_root.glob("*/*") if path.is_dir()}
    experiment_id = make_experiment_id(args.name, timestamp, existing)
    paths = ExperimentPaths.create(repo, kernel.name, experiment_id)
    paths.root.mkdir(parents=True, exist_ok=False)
    at = timestamp_value.isoformat()
    manifest = new_manifest(
        experiment_id=experiment_id,
        kernel=kernel,
        name=args.name,
        hypothesis=args.hypothesis,
        now=at,
        base_commit=_git_lines(repo, "rev-parse", "HEAD")[0],
        initial_status=_git_lines(repo, "status", "--short"),
    )
    manifest["source"]["initial_fingerprint"] = source_fingerprint(repo)
    write_manifest(paths.manifest, manifest)
    append_event(paths.events, at=at, event="CREATED", result="success")
    refresh_readme(paths, manifest)
    stdout.write(f"Created {experiment_id}\n")
    stdout.write(f"Next: python3 pipeline/optimize.py baseline {experiment_id}\n")
    return 0


def _iter_manifests(repo: Path):
    for path in sorted((repo / "target/pipeline").glob("*/*/manifest.json")):
        yield load_manifest(path)


def _list(repo: Path, stdout: TextIO) -> int:
    manifests = list(_iter_manifests(repo))
    if not manifests:
        stdout.write("No experiments.\n")
        return 0
    stdout.write("EXPERIMENT  KERNEL  STATE  DECISION  HYPOTHESIS\n")
    for manifest in manifests:
        decision = manifest["decision"]["result"] if manifest["decision"] else "-"
        stdout.write(
            f"{manifest['experiment_id']}  {manifest['kernel']['name']}  "
            f"{manifest['state']}  {decision}  {manifest['hypothesis']}\n"
        )
    return 0


def _show(experiment_id: str, repo: Path, stdout: TextIO) -> int:
    paths = find_experiment(repo, experiment_id)
    manifest = load_manifest(paths.manifest)
    text = render_experiment_readme(manifest, read_events(paths.events))
    stdout.write(text)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kernel optimization experiment pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create an optimization experiment")
    create.add_argument("--kernel", required=True, choices=sorted(KERNELS))
    create.add_argument("--name", required=True)
    create.add_argument("--hypothesis", required=True)
    subparsers.add_parser("list", help="list experiments")
    show = subparsers.add_parser("show", help="show one experiment")
    show.add_argument("experiment_id")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    now: Callable[[], datetime] = datetime.now().astimezone,
) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        repo = (repo_root or discover_repo_root()).resolve()
        if args.command == "create":
            return _create(args, repo, stdout, now)
        if args.command == "list":
            return _list(repo, stdout)
        if args.command == "show":
            return _show(args.experiment_id, repo, stdout)
        raise CliError(f"unsupported command: {args.command}")
    except (ArtifactError, CliError, KernelError, ManifestError, StateError) as error:
        stderr.write(f"error: {error}\n")
        return 1
