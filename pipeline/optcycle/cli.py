import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence, TextIO, Any

from .artifacts import (
    ArtifactError,
    build_export_manifest,
    ExperimentPaths,
    find_experiment,
    make_experiment_id,
    create_patch,
    snapshot_submission_source,
    snapshot_git_submission_source,
    source_fingerprint,
    validate_export_content,
)
from .arena import ArenaParseError, parse_arena_log, parse_job_id_line
from .kernels import KERNELS, KernelError, get_kernel
from .manifest import ManifestError, load_manifest, new_manifest, write_manifest
from .render import (
    read_events,
    render_analysis_template,
    render_decision,
    render_experiment_readme,
    render_reproduce,
)
from .process import run_streaming
from .state import ExperimentState, StateError, can_keep, require_state


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


def _next_attempt(directory: Path) -> int:
    numbers = []
    for path in directory.glob("attempt-*.log"):
        try:
            numbers.append(int(path.stem.rsplit("-", 1)[1]))
        except ValueError:
            continue
    return max(numbers, default=0) + 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_schedule(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise CliError("compiler did not create a non-empty schedule")
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CliError(f"compiler produced invalid schedule JSON: {error}") from error


def _schedule_stage(
    stage: str,
    experiment_id: str,
    repo: Path,
    stdout: TextIO,
    now: Callable[[], datetime],
) -> int:
    paths = find_experiment(repo, experiment_id)
    manifest = load_manifest(paths.manifest)
    expected = ExperimentState.CREATED if stage == "baseline" else ExperimentState.BASELINE_READY
    require_state(ExperimentState(manifest["state"]), {expected}, stage)
    build_dir = paths.build / stage
    build_dir.mkdir(parents=True, exist_ok=True)
    attempt = _next_attempt(build_dir)
    label = f"attempt-{attempt:03d}"
    log_path = build_dir / f"{label}.log"
    temporary_schedule = build_dir / f"{label}.schedule.json"
    temporary_source = build_dir / f"{label}-source"
    snapshot_submission_source(repo, temporary_source)
    argv = [
        "cargo",
        "furiosa-opt",
        "compile",
        manifest["kernel"]["rust_path"],
        "--exact",
        "--dump-schedule",
        str(temporary_schedule),
    ]
    result = run_streaming(argv, repo, log_path, None, stdout)
    at = now().isoformat()
    event_name = f"{stage.upper()}_COMPILE"
    if result.exit_code != 0:
        append_event(
            paths.events,
            at=at,
            event=event_name,
            result="failure",
            exit_code=result.exit_code,
        )
        refresh_readme(paths, manifest)
        raise CliError(f"{stage} compile failed with exit code {result.exit_code}")
    try:
        _validate_schedule(temporary_schedule)
        canonical_source = paths.source / stage
        canonical_schedule = paths.schedule / f"{stage}.json"
        if canonical_source.exists() or canonical_schedule.exists():
            raise CliError(f"successful {stage} artifact already exists")
        candidate_patch = paths.source / "candidate.patch"
        temporary_patch = build_dir / f"{label}.patch"
        if stage == "candidate":
            create_patch(paths.source / "baseline", temporary_source, temporary_patch)
        else:
            temporary_base = build_dir / f"{label}-base"
            snapshot_git_submission_source(
                repo, manifest["git"]["base_commit"], temporary_base
            )
            create_patch(temporary_base, temporary_source, temporary_patch)
        canonical_source.parent.mkdir(parents=True, exist_ok=True)
        canonical_schedule.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_source, canonical_source)
        os.replace(temporary_schedule, canonical_schedule)
        if stage == "candidate":
            os.replace(temporary_patch, candidate_patch)
        else:
            os.replace(temporary_patch, paths.source / "baseline.patch")
        fingerprint = source_fingerprint(canonical_source)
        manifest["source"][f"{stage}_fingerprint"] = fingerprint
        manifest["schedule"][stage] = {
            "path": f"schedule/{stage}.json",
            "sha256": _sha256(canonical_schedule),
        }
        manifest["state"] = (
            ExperimentState.BASELINE_READY.value
            if stage == "baseline"
            else ExperimentState.CANDIDATE_READY.value
        )
        manifest["updated_at"] = at
        write_manifest(paths.manifest, manifest)
        append_event(
            paths.events,
            at=at,
            event=event_name,
            result="success",
            exit_code=0,
        )
        refresh_readme(paths, manifest)
    except (ArtifactError, CliError, OSError) as error:
        append_event(
            paths.events,
            at=at,
            event=event_name,
            result="failure",
            exit_code=result.exit_code,
        )
        raise CliError(str(error)) from error
    stdout.write(f"{stage} ready: {canonical_schedule.relative_to(paths.root)}\n")
    return 0


def _analyze(
    args: argparse.Namespace,
    repo: Path,
    stdout: TextIO,
    now: Callable[[], datetime],
) -> int:
    paths = find_experiment(repo, args.experiment_id)
    manifest = load_manifest(paths.manifest)
    require_state(
        ExperimentState(manifest["state"]),
        {ExperimentState.CANDIDATE_READY},
        "analyze",
    )
    text = render_analysis_template(manifest)
    if args.print_only:
        stdout.write(text)
        return 0
    analysis_path = paths.schedule / "analysis.md"
    if analysis_path.exists():
        raise CliError("analysis already exists; use --print to preview without overwriting")
    analysis_path.write_text(text, encoding="utf-8")
    at = now().isoformat()
    manifest["schedule"]["analysis_path"] = "schedule/analysis.md"
    manifest["updated_at"] = at
    write_manifest(paths.manifest, manifest)
    append_event(paths.events, at=at, event="ANALYSIS_CREATED", result="success")
    refresh_readme(paths, manifest)
    stdout.write(f"Created {analysis_path.relative_to(paths.root)}\n")
    return 0


def _resolve_artifact(paths: ExperimentPaths, relative: str) -> Path:
    resolved = (paths.root / relative).resolve()
    try:
        resolved.relative_to(paths.root.resolve())
    except ValueError as error:
        raise CliError(f"artifact path escapes experiment: {relative}") from error
    return resolved


def _approve_static(
    args: argparse.Namespace,
    repo: Path,
    stdout: TextIO,
    now: Callable[[], datetime],
) -> int:
    paths = find_experiment(repo, args.experiment_id)
    manifest = load_manifest(paths.manifest)
    require_state(
        ExperimentState(manifest["state"]),
        {ExperimentState.CANDIDATE_READY},
        "approve-static",
    )
    for stage in ("baseline", "candidate"):
        record = manifest["schedule"].get(stage)
        if not isinstance(record, dict) or not record.get("path") or not record.get("sha256"):
            raise CliError(f"{stage} schedule record is incomplete")
        artifact = _resolve_artifact(paths, record["path"])
        if not artifact.is_file() or _sha256(artifact) != record["sha256"]:
            raise CliError(f"{stage} schedule hash mismatch")
    analysis_relative = manifest["schedule"].get("analysis_path")
    if not analysis_relative:
        raise CliError("analysis is missing; run analyze first")
    analysis_path = _resolve_artifact(paths, analysis_relative)
    if not analysis_path.is_file():
        raise CliError("analysis file is missing; run analyze first")
    recorded_fingerprint = manifest["source"].get("candidate_fingerprint")
    if source_fingerprint(repo) != recorded_fingerprint:
        raise CliError("current source differs from the candidate; create a new experiment")
    candidate_snapshot = paths.source / "candidate"
    if source_fingerprint(candidate_snapshot) != recorded_fingerprint:
        raise CliError("candidate snapshot fingerprint mismatch")
    at = now().isoformat()
    manifest["schedule"]["approved_note"] = args.note
    manifest["schedule"]["analysis_sha256"] = _sha256(analysis_path)
    manifest["schedule"]["approved_at"] = at
    manifest["state"] = ExperimentState.READY_FOR_ARENA.value
    manifest["updated_at"] = at
    write_manifest(paths.manifest, manifest)
    append_event(paths.events, at=at, event="STATIC_APPROVED", result="success")
    refresh_readme(paths, manifest)
    stdout.write("Static review approved; experiment is READY_FOR_ARENA.\n")
    return 0


def _attempt_record(manifest: dict[str, Any], number: int) -> dict[str, Any]:
    for attempt in manifest["arena"]["attempts"]:
        if attempt.get("attempt") == number:
            return attempt
    raise CliError(f"Arena attempt {number} not found")


def _write_result_json(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _arena(
    args: argparse.Namespace,
    repo: Path,
    stdout: TextIO,
    now: Callable[[], datetime],
) -> int:
    paths = find_experiment(repo, args.experiment_id)
    manifest = load_manifest(paths.manifest)
    current_state = ExperimentState(manifest["state"])
    if args.new_attempt:
        require_state(
            current_state,
            {ExperimentState.ARENA_PASSED, ExperimentState.ARENA_FAILED},
            "arena --new-attempt",
        )
    else:
        require_state(current_state, {ExperimentState.READY_FOR_ARENA}, "arena")
    candidate_fingerprint = manifest["source"].get("candidate_fingerprint")
    if source_fingerprint(repo) != candidate_fingerprint:
        raise CliError("current source differs from the candidate; create a new experiment")

    number = max(
        (item.get("attempt", 0) for item in manifest["arena"]["attempts"]),
        default=0,
    ) + 1
    attempt_dir = paths.arena / f"attempt-{number:03d}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    job_name = f"{args.experiment_id}-a{number:03d}"
    attempt = {
        "attempt": number,
        "job_id": None,
        "job_name": job_name,
        "status": "SUBMITTING",
        "exit_code": None,
        "accuracy_passed": False,
        "kernels": {},
    }
    manifest["arena"]["attempts"].append(attempt)
    write_manifest(paths.manifest, manifest)
    job_captured = False

    def capture_job(line: str) -> None:
        nonlocal job_captured
        job_id = parse_job_id_line(line)
        if job_id is None or job_captured:
            return
        job_captured = True
        attempt["job_id"] = job_id
        attempt["status"] = "RUNNING"
        manifest["state"] = ExperimentState.ARENA_RUNNING.value
        manifest["updated_at"] = now().isoformat()
        write_manifest(paths.manifest, manifest)
        append_event(
            paths.events,
            at=manifest["updated_at"],
            event="ARENA_JOB_CAPTURED",
            result="success",
            metadata={"attempt": number, "job_id": job_id},
        )

    result_path = attempt_dir / "result.log"
    result = run_streaming(
        [str(repo / "scripts/rngd_test.sh")],
        repo,
        result_path,
        {"RNGD_JOB_NAME": job_name},
        stdout,
        on_line=capture_job,
    )
    text = result_path.read_text(encoding="utf-8")
    parse_error: ArenaParseError | None = None
    try:
        parsed = parse_arena_log(text)
    except ArenaParseError as error:
        parsed = error.partial
        parse_error = error
    if parsed.job_id is not None and attempt["job_id"] is None:
        attempt["job_id"] = parsed.job_id
    attempt.update(parsed.to_dict())
    attempt["job_id"] = attempt["job_id"] or parsed.job_id
    attempt["exit_code"] = result.exit_code
    if attempt["job_id"] is None:
        attempt["status"] = "SUBMIT_FAILED"
        manifest["state"] = (
            ExperimentState.ARENA_PASSED.value
            if can_keep(manifest)
            else ExperimentState.READY_FOR_ARENA.value
        )
    elif (
        result.exit_code == 0
        and parse_error is None
        and parsed.status in {"SUCCEEDED", "COMPLETED"}
        and parsed.accuracy_passed
    ):
        attempt["status"] = "SUCCEEDED"
        manifest["state"] = ExperimentState.ARENA_PASSED.value
    elif parsed.status in {"FAILED", "CANCELLED", "CANCELED"}:
        attempt["status"] = parsed.status
        manifest["state"] = (
            ExperimentState.ARENA_PASSED.value
            if can_keep(manifest)
            else ExperimentState.ARENA_FAILED.value
        )
    else:
        attempt["status"] = "RUNNING"
        manifest["state"] = ExperimentState.ARENA_RUNNING.value
    at = now().isoformat()
    manifest["updated_at"] = at
    write_manifest(paths.manifest, manifest)
    _write_result_json(attempt_dir / "result.json", attempt)
    append_event(
        paths.events,
        at=at,
        event="ARENA_ATTEMPT",
        result="success" if manifest["state"] == ExperimentState.ARENA_PASSED.value else "failure",
        exit_code=result.exit_code,
        metadata={"attempt": number, **({"job_id": attempt["job_id"]} if attempt["job_id"] else {})},
    )
    refresh_readme(paths, manifest)
    if manifest["state"] != ExperimentState.ARENA_PASSED.value:
        detail = str(parse_error) if parse_error else f"exit code {result.exit_code}"
        raise CliError(f"Arena attempt did not pass: {detail}")
    stdout.write(f"Arena PASS: job {attempt['job_id']}\n")
    return 0


def _sync_arena(
    experiment_id: str,
    repo: Path,
    stdout: TextIO,
    now: Callable[[], datetime],
) -> int:
    paths = find_experiment(repo, experiment_id)
    manifest = load_manifest(paths.manifest)
    require_state(
        ExperimentState(manifest["state"]),
        {ExperimentState.ARENA_RUNNING},
        "sync-arena",
    )
    if not manifest["arena"]["attempts"]:
        raise CliError("no Arena attempt to synchronize")
    attempt = manifest["arena"]["attempts"][-1]
    job_id = attempt.get("job_id")
    if not isinstance(job_id, int):
        raise CliError("running Arena attempt has no numeric job id")
    attempt_dir = paths.arena / f"attempt-{attempt['attempt']:03d}"
    sequence = max(
        (int(path.stem.rsplit("-", 1)[1]) for path in attempt_dir.glob("sync-status-*.log")),
        default=0,
    ) + 1
    status_path = attempt_dir / f"sync-status-{sequence:03d}.log"
    logs_path = attempt_dir / f"sync-result-{sequence:03d}.log"
    status_result = run_streaming(
        ["furiosa-arena", "status", str(job_id)], repo, status_path, None, stdout
    )
    logs_result = run_streaming(
        ["furiosa-arena", "logs", str(job_id)], repo, logs_path, None, stdout
    )
    combined = status_path.read_text(encoding="utf-8") + logs_path.read_text(encoding="utf-8")
    with (attempt_dir / "result.log").open("a", encoding="utf-8") as handle:
        handle.write(combined)
    try:
        parsed = parse_arena_log(combined)
        parse_error = None
    except ArenaParseError as error:
        parsed = error.partial
        parse_error = error
    parsed.job_id = job_id
    attempt.update(parsed.to_dict())
    attempt["job_id"] = job_id
    if parsed.status in {"SUCCEEDED", "COMPLETED"} and parsed.accuracy_passed and parse_error is None:
        manifest["state"] = ExperimentState.ARENA_PASSED.value
    elif parsed.status in {"FAILED", "CANCELLED", "CANCELED"}:
        manifest["state"] = (
            ExperimentState.ARENA_PASSED.value
            if can_keep(manifest)
            else ExperimentState.ARENA_FAILED.value
        )
    else:
        manifest["state"] = ExperimentState.ARENA_RUNNING.value
    at = now().isoformat()
    manifest["updated_at"] = at
    write_manifest(paths.manifest, manifest)
    _write_result_json(attempt_dir / "result.json", attempt)
    append_event(
        paths.events,
        at=at,
        event="ARENA_SYNC",
        result="success" if status_result.exit_code == 0 and logs_result.exit_code == 0 else "failure",
        metadata={"attempt": attempt["attempt"], "job_id": job_id},
    )
    refresh_readme(paths, manifest)
    stdout.write(f"Arena state: {manifest['state']}\n")
    return 0


def _decide(
    args: argparse.Namespace,
    repo: Path,
    stdout: TextIO,
    now: Callable[[], datetime],
) -> int:
    paths = find_experiment(repo, args.experiment_id)
    manifest = load_manifest(paths.manifest)
    current = ExperimentState(manifest["state"])
    if args.keep:
        require_state(current, {ExperimentState.ARENA_PASSED}, "decide --keep")
        if not can_keep(manifest):
            raise CliError("decide --keep requires a complete passing Arena attempt")
        result = "KEEP"
        next_state = ExperimentState.KEPT
    else:
        require_state(
            current,
            {ExperimentState.ARENA_PASSED, ExperimentState.ARENA_FAILED},
            "decide --reject",
        )
        result = "REJECT"
        next_state = ExperimentState.REJECTED
    at = now().isoformat()
    manifest["decision"] = {"result": result, "reason": args.reason, "at": at}
    manifest["state"] = next_state.value
    manifest["updated_at"] = at
    write_manifest(paths.manifest, manifest)
    paths.decision.write_text(render_decision(manifest), encoding="utf-8")
    append_event(
        paths.events,
        at=at,
        event=f"DECISION_{result}",
        result="success",
    )
    refresh_readme(paths, manifest)
    stdout.write(f"Decision recorded: {result}\n")
    return 0


def _export(experiment_id: str, repo: Path, stdout: TextIO) -> int:
    paths = find_experiment(repo, experiment_id)
    manifest = load_manifest(paths.manifest)
    require_state(
        ExperimentState(manifest["state"]),
        {ExperimentState.KEPT, ExperimentState.REJECTED},
        "export",
    )
    patches = {
        "baseline.patch": paths.source / "baseline.patch",
        "candidate.patch": paths.source / "candidate.patch",
    }
    for name, path in patches.items():
        if not path.is_file():
            raise CliError(f"required export artifact is missing: {name}")
    exported = build_export_manifest(manifest)
    exported["artifacts"] = {
        name: {"sha256": _sha256(path)} for name, path in patches.items()
    }
    validate_export_content(exported, repo)
    desired: dict[str, bytes] = {
        "README.md": render_experiment_readme(
            manifest, read_events(paths.events)
        ).encode("utf-8"),
        "manifest.json": (
            json.dumps(exported, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8"),
        "baseline.patch": patches["baseline.patch"].read_bytes(),
        "candidate.patch": patches["candidate.patch"].read_bytes(),
        "REPRODUCE.md": render_reproduce(exported).encode("utf-8"),
    }
    records_root = repo / "pipeline/records"
    destination = records_root / experiment_id
    if destination.exists():
        existing_names = {path.name for path in destination.iterdir() if path.is_file()}
        if existing_names != set(desired):
            raise CliError("existing export differs; refusing to overwrite")
        if any((destination / name).read_bytes() != content for name, content in desired.items()):
            raise CliError("existing export differs; refusing to overwrite")
        stdout.write(f"Export already matches: pipeline/records/{experiment_id}\n")
        return 0
    records_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{experiment_id}-", dir=records_root))
    try:
        for name, content in desired.items():
            (staging / name).write_bytes(content)
        os.replace(staging, destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    stdout.write(f"Exported pipeline/records/{experiment_id}\n")
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
    baseline = subparsers.add_parser("baseline", help="generate the baseline schedule")
    baseline.add_argument("experiment_id")
    candidate = subparsers.add_parser("candidate", help="generate one candidate schedule")
    candidate.add_argument("experiment_id")
    analyze = subparsers.add_parser("analyze", help="create a manual schedule review")
    analyze.add_argument("experiment_id")
    analyze.add_argument(
        "--print", dest="print_only", action="store_true", help="print a fresh template without writing it"
    )
    approve = subparsers.add_parser(
        "approve-static", help="approve unchanged static evidence for Arena"
    )
    approve.add_argument("experiment_id")
    approve.add_argument("--note", required=True, help="human review conclusion")
    arena = subparsers.add_parser("arena", help="explicitly submit a READY experiment to Arena")
    arena.add_argument("experiment_id")
    arena.add_argument("--new-attempt", action="store_true", help="remeasure a terminal Arena experiment")
    sync = subparsers.add_parser("sync-arena", help="synchronize an already submitted Arena job")
    sync.add_argument("experiment_id")
    decide = subparsers.add_parser("decide", help="record the final keep or reject decision")
    decide.add_argument("experiment_id")
    decision = decide.add_mutually_exclusive_group(required=True)
    decision.add_argument("--keep", action="store_true")
    decision.add_argument("--reject", action="store_true")
    decide.add_argument("--reason", required=True)
    export = subparsers.add_parser("export", help="create a sanitized shared record")
    export.add_argument("experiment_id")
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
        if args.command in {"baseline", "candidate"}:
            return _schedule_stage(args.command, args.experiment_id, repo, stdout, now)
        if args.command == "analyze":
            return _analyze(args, repo, stdout, now)
        if args.command == "approve-static":
            return _approve_static(args, repo, stdout, now)
        if args.command == "arena":
            return _arena(args, repo, stdout, now)
        if args.command == "sync-arena":
            return _sync_arena(args.experiment_id, repo, stdout, now)
        if args.command == "decide":
            return _decide(args, repo, stdout, now)
        if args.command == "export":
            return _export(args.experiment_id, repo, stdout)
        raise CliError(f"unsupported command: {args.command}")
    except (ArtifactError, CliError, KernelError, ManifestError, StateError) as error:
        stderr.write(f"error: {error}\n")
        return 1
