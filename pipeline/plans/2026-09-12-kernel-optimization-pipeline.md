# Kernel Optimization Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Stage 1 커널 최적화 실험을 생성하고 baseline/candidate schedule, 정적 승인, 명시적 Arena 검증, keep/reject 및 공유 record까지 추적하는 반자동 Python CLI를 구현한다.

**Architecture:** `pipeline/optimize.py`가 `pipeline/optcycle` 패키지의 CLI를 호출한다. 도메인 상태와 manifest, 파일 artifact, 외부 프로세스, Arena 파싱, Markdown 렌더링을 독립 모듈로 분리하고, 로컬 원본은 `target/pipeline/`, 공유 요약은 `pipeline/records/`에 저장한다. 자동 schedule analyzer는 이번 범위에서 구현하지 않고 `analysis.md`, `analysis.json`, `comparison.json` 파일 계약만 유지한다.

**Tech Stack:** Python 3 표준 라이브러리(`argparse`, `dataclasses`, `enum`, `json`, `hashlib`, `pathlib`, `subprocess`, `unittest`), 기존 `cargo furiosa-opt`, `scripts/rngd_test.sh`, `furiosa-arena` CLI

**Spec:** `pipeline/specs/2026-09-12-kernel-optimization-pipeline-design.md`

## Global Constraints

- 구현과 테스트는 모두 `pipeline/` 아래에 둔다.
- Python 외부 패키지를 추가하지 않는다.
- 지원 커널은 `sliding_project_qkv`, `sliding_attention_output`, `decoder_feedforward` 세 개로 고정한다.
- 제출 대상 source snapshot 범위는 `src/ops.rs`와 `src/device/**`이다.
- 원격 Arena 실행은 `READY_FOR_ARENA` 상태에서 사용자가 `arena`를 명시적으로 호출할 때만 수행한다.
- 실행 제한 70초와 polling 정책은 기존 `scripts/rngd_test.sh`를 재사용한다.
- 파이프라인은 Git commit, checkout, reset, revert, merge와 `moa-submitter submit`을 실행하지 않는다.
- 기존 artifact와 attempt를 덮어쓰지 않는다.
- raw schedule, build/Arena log와 source snapshot은 `target/pipeline/`에만 둔다.
- `pipeline/records/`에는 요약, 정제 manifest, patch와 재현 문서만 export한다.
- 자동 schedule parser와 AI candidate 생성은 후속 구현 범위다.

---

### Task 1: Domain model, manifest, and state machine

**Files:**
- Create: `pipeline/optcycle/__init__.py`
- Create: `pipeline/optcycle/kernels.py`
- Create: `pipeline/optcycle/state.py`
- Create: `pipeline/optcycle/manifest.py`
- Create: `pipeline/tests/__init__.py`
- Create: `pipeline/tests/test_state.py`
- Create: `pipeline/tests/test_manifest.py`

**Interfaces:**
- Produces: `KernelSpec(name: str, rust_path: str)` and `get_kernel(name: str) -> KernelSpec`
- Produces: `ExperimentState(str, Enum)` and `require_state(current, allowed, command) -> None`
- Produces: `load_manifest(path: Path) -> dict`, `write_manifest(path: Path, manifest: dict) -> None`, `validate_manifest(manifest: dict) -> None`
- Consumes: no earlier task interfaces

- [x] **Step 1: Write failing state-machine tests**

```python
class StateTests(unittest.TestCase):
    def test_arena_requires_ready_state(self):
        require_state(
            ExperimentState.READY_FOR_ARENA,
            {ExperimentState.READY_FOR_ARENA},
            "arena",
        )
        with self.assertRaisesRegex(StateError, "arena requires"):
            require_state(
                ExperimentState.CANDIDATE_READY,
                {ExperimentState.READY_FOR_ARENA},
                "arena",
            )

    def test_keep_requires_a_passing_attempt(self):
        self.assertTrue(can_keep({"arena": {"attempts": [{"accuracy_passed": True}]}}))
        self.assertFalse(can_keep({"arena": {"attempts": [{"accuracy_passed": False}]}}))
```

- [x] **Step 2: Write failing manifest tests**

```python
class ManifestTests(unittest.TestCase):
    def test_atomic_round_trip(self):
        manifest = new_manifest(
            experiment_id="20260912-153000-copy-test",
            kernel=get_kernel("decoder_feedforward"),
            name="copy-test",
            hypothesis="중간 복사 제거",
            now="2026-09-12T15:30:00+09:00",
            base_commit="a" * 40,
            initial_status=[],
        )
        write_manifest(self.path, manifest)
        self.assertEqual(load_manifest(self.path), manifest)

    def test_rejects_unknown_schema(self):
        with self.assertRaisesRegex(ManifestError, "schema_version"):
            validate_manifest({"schema_version": 99})
```

- [x] **Step 3: Run tests and verify the imports fail**

Run:

```bash
python3 -m unittest pipeline.tests.test_state pipeline.tests.test_manifest -v
```

Expected: FAIL because `pipeline.optcycle.state` and `pipeline.optcycle.manifest` do not exist.

- [x] **Step 4: Implement kernel mapping and state rules**

```python
@dataclass(frozen=True)
class KernelSpec:
    name: str
    rust_path: str

KERNELS = {
    "sliding_project_qkv": KernelSpec("sliding_project_qkv", "ops::sliding_project_qkv"),
    "sliding_attention_output": KernelSpec("sliding_attention_output", "ops::sliding_attention_output"),
    "decoder_feedforward": KernelSpec("decoder_feedforward", "ops::decoder_feedforward"),
}
```

Define the persistent states exactly as `CREATED`, `BASELINE_READY`, `CANDIDATE_READY`, `READY_FOR_ARENA`, `ARENA_RUNNING`, `ARENA_PASSED`, `ARENA_FAILED`, `KEPT`, and `REJECTED`. `can_keep()` returns true only when at least one attempt has `accuracy_passed is True` and three kernel cycle values.

- [x] **Step 5: Implement schema-1 manifest with atomic writes**

Write JSON to `manifest.json.tmp`, flush and `os.fsync`, then replace with `os.replace`. Validate the required top-level keys `schema_version`, `experiment_id`, `kernel`, `name`, `hypothesis`, `state`, `created_at`, `updated_at`, `git`, `source`, `schedule`, `arena`, and `decision` before every write.

- [x] **Step 6: Run Task 1 tests**

Run:

```bash
python3 -m unittest pipeline.tests.test_state pipeline.tests.test_manifest -v
```

Expected: all tests PASS.

- [x] **Step 7: Commit Task 1**

```bash
git add pipeline/optcycle pipeline/tests
git commit -m "Add optimization pipeline state model"
```

### Task 2: Experiment paths, source snapshots, fingerprints, and patches

**Files:**
- Create: `pipeline/optcycle/artifacts.py`
- Create: `pipeline/tests/test_artifacts.py`

**Interfaces:**
- Consumes: `KernelSpec` from `kernels.py`
- Produces: `ExperimentPaths`, `make_experiment_id()`, `find_experiment()`, `source_fingerprint()`, `snapshot_submission_source()`, `create_patch()`

- [x] **Step 1: Write failing path and ID tests**

```python
def test_make_experiment_id_uses_safe_slug(self):
    self.assertEqual(
        make_experiment_id("Down Project Copy", "20260912-153000", set()),
        "20260912-153000-down-project-copy",
    )

def test_find_experiment_rejects_traversal(self):
    with self.assertRaisesRegex(ArtifactError, "invalid experiment id"):
        find_experiment(self.repo, "../outside")
```

- [x] **Step 2: Write failing fingerprint and snapshot tests**

Create a temporary fake repository with `src/ops.rs` and two `src/device/*.rs` files. Assert that changing file timestamps does not change the fingerprint, changing content does, and snapshot contains only the submission scope.

- [x] **Step 3: Write failing patch-reproduction test**

Create baseline and candidate snapshots, call `create_patch(baseline, candidate, patch_path)`, apply it to a copy of baseline using `git apply`, and assert the resulting fingerprint equals the candidate fingerprint.

- [x] **Step 4: Run artifact tests and verify failure**

Run:

```bash
python3 -m unittest pipeline.tests.test_artifacts -v
```

Expected: FAIL because `pipeline.optcycle.artifacts` does not exist.

- [x] **Step 5: Implement safe experiment paths**

`ExperimentPaths` exposes typed `Path` properties for `manifest`, `events`, `source`, `schedule`, `build`, `arena`, and `decision`. Resolve every derived path and verify it is below `<repo>/target/pipeline` using `Path.relative_to()`.

- [x] **Step 6: Implement deterministic submission fingerprint**

Hash each relative POSIX path, a NUL separator, file bytes, and another NUL in lexical path order. Hash the concatenated per-file digests once more. Do not include timestamps, permissions, absolute paths, symlink targets outside the source root, `__pycache__`, or non-file entries.

- [x] **Step 7: Implement snapshots and patches without changing the working tree**

Use `shutil.copy2` for `src/ops.rs` and `src/device/**`. Generate patch text with `git diff --no-index --binary --src-prefix=a/ --dst-prefix=b/`, normalize snapshot prefixes to repository-relative `src/...`, and accept exit code 1 as “differences found.” Exit codes above 1 are errors.

- [x] **Step 8: Run Task 2 tests**

Run:

```bash
python3 -m unittest pipeline.tests.test_artifacts -v
```

Expected: all tests PASS, including patch reproduction.

- [x] **Step 9: Commit Task 2**

```bash
git add pipeline/optcycle/artifacts.py pipeline/tests/test_artifacts.py
git commit -m "Add optimization experiment artifacts"
```

### Task 3: Experiment lifecycle CLI and human-readable index

**Files:**
- Create: `pipeline/optimize.py`
- Create: `pipeline/optcycle/cli.py`
- Create: `pipeline/optcycle/render.py`
- Create: `pipeline/templates/experiment-readme.md`
- Create: `pipeline/tests/test_cli.py`

**Interfaces:**
- Consumes: manifest and artifact interfaces from Tasks 1–2
- Produces: `create`, `list`, and `show` CLI subcommands; `append_event()`; `render_experiment_readme()`

- [x] **Step 1: Write failing CLI create test**

Invoke `main([...], repo_root=temp_repo, stdout=StringIO(), now=fixed_clock)` with `create --kernel decoder_feedforward --name copy-test --hypothesis 중간복사제거`. Assert exit code 0, state `CREATED`, one experiment directory, one `CREATED` JSONL event, and a README containing the hypothesis.

- [x] **Step 2: Write failing list/show tests**

Create two manifests in different states. Assert `list` prints ID, kernel and state for both. Assert `show <ID>` prints the hypothesis and the exact next command `python3 pipeline/optimize.py baseline <ID>`.

- [x] **Step 3: Run CLI tests and verify failure**

Run:

```bash
python3 -m unittest pipeline.tests.test_cli -v
```

Expected: FAIL because `pipeline.optcycle.cli` and renderer do not exist.

- [x] **Step 4: Implement dependency-injected CLI entrypoint**

```python
def main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    now: Callable[[], datetime] = datetime.now().astimezone,
) -> int:
    ...
```

Discover the real repo by walking upward from `pipeline/optimize.py` until `Cargo.toml` and `src/ops.rs` are found. Tests always inject `repo_root`.

- [x] **Step 5: Add the executable entrypoint**

```python
#!/usr/bin/env python3
from optcycle.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 6: Implement create/list/show and append-only events**

Events contain only `at`, `event`, `result`, optional `exit_code`, and allowlisted metadata. README is regenerated from manifest and events after every successful state-changing command.

- [x] **Step 7: Run CLI tests and CLI help**

Run:

```bash
python3 -m unittest pipeline.tests.test_cli -v
python3 pipeline/optimize.py --help
```

Expected: tests PASS and help lists `create`, `list`, and `show`.

- [x] **Step 8: Commit Task 3**

```bash
git add pipeline/optimize.py pipeline/optcycle/cli.py pipeline/optcycle/render.py pipeline/templates pipeline/tests/test_cli.py
git commit -m "Add optimization experiment CLI"
```

### Task 4: External command runner and baseline/candidate schedule stages

**Files:**
- Create: `pipeline/optcycle/process.py`
- Modify: `pipeline/optcycle/cli.py`
- Modify: `pipeline/optcycle/render.py`
- Create: `pipeline/tests/test_schedule_commands.py`

**Interfaces:**
- Produces: `CommandResult(argv, exit_code, started_at, finished_at, log_path)`
- Produces: `run_streaming(argv, cwd, log_path, env, stdout) -> CommandResult`
- Produces: `baseline` and `candidate` CLI commands
- Consumes: snapshot, patch, manifest, event, and state interfaces from Tasks 1–3

- [x] **Step 1: Write failing streaming-runner test**

Run a temporary executable that prints one line to stdout, one to stderr, and exits 7. Assert both lines are present in the log, the terminal sink receives them, and `CommandResult.exit_code == 7`.

- [x] **Step 2: Write failing baseline integration test**

Use a fake `cargo` executable that locates the `--dump-schedule` argument and writes a non-empty JSON file. Run `baseline <ID>` and assert:

- baseline snapshot and fingerprint exist;
- `build/baseline/attempt-001.log` exists;
- `schedule/baseline.json` is non-empty;
- state becomes `BASELINE_READY`;
- compile argv contains the exact kernel path and `--exact`.

- [x] **Step 3: Write failing baseline error test**

Fake cargo exits 2 without a schedule. Assert state remains `CREATED`, the failed attempt log and event remain, and a second call writes `attempt-002.log` without modifying attempt 1.

- [x] **Step 4: Write failing candidate integration test**

Modify fake `src/device/kernel.rs`, run candidate, and assert candidate snapshot, fingerprint, patch, schedule, and log exist and state becomes `CANDIDATE_READY`.

- [x] **Step 5: Run tests and verify failure**

Run:

```bash
python3 -m unittest pipeline.tests.test_schedule_commands -v
```

Expected: FAIL because runner and schedule commands are missing.

- [x] **Step 6: Implement streaming subprocess execution**

Use `subprocess.Popen` with `stderr=subprocess.STDOUT`, text mode and line buffering. Write each line to terminal and log, preserve exit code, and forward `KeyboardInterrupt` by sending SIGINT then waiting for the child.

- [x] **Step 7: Implement baseline and candidate as transactional stages**

Compile into attempt-specific temporary schedule paths. On success, validate non-empty JSON, compute SHA-256, and atomically move to the canonical baseline/candidate schedule path. On failure, keep attempt logs and temporary diagnostic files but do not update successful artifact fields or state.

- [x] **Step 8: Enforce one candidate per experiment**

After a successful candidate stage, later source changes cause Arena preflight to fail with a message directing the user to create a new experiment. `candidate` cannot overwrite an existing successful candidate.

- [x] **Step 9: Run Task 4 tests**

Run:

```bash
python3 -m unittest pipeline.tests.test_schedule_commands -v
```

Expected: all tests PASS.

- [x] **Step 10: Commit Task 4**

```bash
git add pipeline/optcycle pipeline/tests/test_schedule_commands.py
git commit -m "Add schedule generation stages"
```

### Task 5: Manual analysis template and static approval gate

**Files:**
- Create: `pipeline/templates/analysis.md`
- Modify: `pipeline/optcycle/cli.py`
- Modify: `pipeline/optcycle/render.py`
- Create: `pipeline/tests/test_static_review.py`

**Interfaces:**
- Produces: `analyze` and `approve-static` CLI commands
- Produces: `render_analysis_template(manifest) -> str`
- Consumes: canonical schedule paths and SHA-256 values from Task 4

- [x] **Step 1: Write failing analysis-template test**

Run `analyze <ID>` for a `CANDIDATE_READY` experiment. Assert `schedule/analysis.md` contains baseline/candidate paths and sections for makespan, context occupancy, overlap, source hotspots, memory, hypothesis evaluation, and Arena recommendation.

- [x] **Step 2: Write failing no-overwrite test**

Edit `analysis.md`, call `analyze` again, and assert the command fails without changing the file. A `--print` option may render to stdout but must not overwrite the saved review.

- [x] **Step 3: Write failing approval-gate tests**

Assert approval fails when analysis is absent, when either schedule hash no longer matches, and when the current source fingerprint differs from the recorded candidate. Assert successful approval records note and analysis SHA-256 and transitions to `READY_FOR_ARENA`.

- [x] **Step 4: Run tests and verify failure**

Run:

```bash
python3 -m unittest pipeline.tests.test_static_review -v
```

Expected: FAIL because `analyze` and `approve-static` are missing.

- [x] **Step 5: Implement template rendering and approval checks**

The template contains no computed claims. It presents fill-in fields as Markdown checklist items and explicitly labels user-entered conclusions as review decisions. `approve-static` hashes the completed file and stores the approval note and timestamp.

- [x] **Step 6: Run Task 5 tests and help**

Run:

```bash
python3 -m unittest pipeline.tests.test_static_review -v
python3 pipeline/optimize.py analyze --help
python3 pipeline/optimize.py approve-static --help
```

Expected: tests PASS and both commands document their gates.

- [x] **Step 7: Commit Task 5**

```bash
git add pipeline/templates/analysis.md pipeline/optcycle pipeline/tests/test_static_review.py
git commit -m "Add static schedule review gate"
```

### Task 6: Explicit Arena execution, parsing, and synchronization

**Files:**
- Create: `pipeline/optcycle/arena.py`
- Modify: `pipeline/optcycle/cli.py`
- Modify: `pipeline/optcycle/render.py`
- Create: `pipeline/tests/fixtures/arena-success.log`
- Create: `pipeline/tests/fixtures/arena-accuracy-failure.log`
- Create: `pipeline/tests/test_arena.py`

**Interfaces:**
- Produces: `ArenaAttempt`, `parse_arena_log(text: str) -> ArenaAttempt`
- Produces: `arena` and `sync-arena` CLI commands
- Consumes: `run_streaming`, current source fingerprint, and `scripts/rngd_test.sh`

- [x] **Step 1: Write failing Arena parser tests**

Use the real output format already observed in this repository. Assert the success fixture extracts numeric Job ID, all three PASS values and cycles. Assert an accuracy failure does not set `accuracy_passed`, and a missing cycle raises `ArenaParseError` while preserving parsed partial data.

- [x] **Step 2: Write failing preflight tests**

Assert `arena` refuses states other than `READY_FOR_ARENA`, refuses a modified current source fingerprint, and does not invoke the fake runner in either case.

- [x] **Step 3: Write failing successful Arena command test**

Inject a fake runner that emits the success fixture. Assert the command uses:

```text
env RNGD_JOB_NAME=<EXPERIMENT_ID>-a001 ./scripts/rngd_test.sh
```

Assert `arena/attempt-001/result.log` and `result.json` exist, state becomes `ARENA_PASSED`, and all kernel cycles appear in README.

- [x] **Step 4: Write failing submit/interruption/sync tests**

Test these cases independently:

- command fails before a Job ID: experiment returns to `READY_FOR_ARENA` and attempt is retained;
- Job ID is captured before interruption: state remains `ARENA_RUNNING`;
- `sync-arena` calls `furiosa-arena status <ID>` and `furiosa-arena logs <ID>` without submitting a new Job;
- terminal failed Job becomes `ARENA_FAILED`;
- `--new-attempt` creates `attempt-002` and preserves attempt 1.

- [x] **Step 5: Run tests and verify failure**

Run:

```bash
python3 -m unittest pipeline.tests.test_arena -v
```

Expected: FAIL because Arena integration is missing.

- [x] **Step 6: Implement incremental Job ID capture**

Extend `run_streaming` with an optional `on_line(line: str)` callback. When a line matches `submitted job ([0-9]+)`, store the Job ID immediately with an atomic manifest update and transition to `ARENA_RUNNING`.

- [x] **Step 7: Implement strict result parsing**

Accept `ARENA_PASSED` only when the command or synchronized Job succeeds, all three named kernels contain `->PASS`, and all three cycle values are present. Keep raw result logs locally for every other outcome.

- [x] **Step 8: Run Task 6 tests**

Run:

```bash
python3 -m unittest pipeline.tests.test_arena -v
```

Expected: all tests PASS.

- [x] **Step 9: Commit Task 6**

```bash
git add pipeline/optcycle pipeline/tests
git commit -m "Add gated Arena verification"
```

### Task 7: Decision, export, and reproducibility record

**Files:**
- Create: `pipeline/templates/decision.md`
- Create: `pipeline/templates/reproduce.md`
- Modify: `pipeline/optcycle/cli.py`
- Modify: `pipeline/optcycle/artifacts.py`
- Modify: `pipeline/optcycle/render.py`
- Create: `pipeline/tests/test_export.py`

**Interfaces:**
- Produces: `decide` and `export` CLI commands
- Produces: `build_export_manifest(local_manifest) -> dict`
- Consumes: source patches, schedule hashes, Arena results, and state rules from prior tasks

- [x] **Step 1: Write failing decision tests**

Assert `--keep` fails without a valid PASS attempt, `--reject` is allowed after Arena failure, keep/reject are mutually exclusive, and a successful decision records reason and timestamp without modifying working source.

- [x] **Step 2: Write failing export-content test**

Export a final experiment and assert the record contains exactly `README.md`, `manifest.json`, `baseline.patch`, `candidate.patch`, and `REPRODUCE.md`. Assert it does not contain raw schedules, logs, snapshots, fixture, binaries, home-directory paths, environment maps, or token-like keys.

- [x] **Step 3: Write failing export no-overwrite test**

Export twice. The second call succeeds only when every existing file hash equals the newly rendered content; otherwise it fails and changes no file.

- [x] **Step 4: Write failing reproduction test**

Apply `baseline.patch` and `candidate.patch` in order to a temporary checkout of the recorded base commit. Assert the reconstructed submission source fingerprint equals the exported candidate fingerprint.

- [x] **Step 5: Run tests and verify failure**

Run:

```bash
python3 -m unittest pipeline.tests.test_export -v
```

Expected: FAIL because decision/export commands are missing.

- [x] **Step 6: Implement decision rendering and sanitized export**

Construct export manifest from an explicit allowlist. Before writing, recursively reject absolute paths, keys containing `token`, `secret`, `credential`, or `environment`, and values containing the repository absolute path.

- [x] **Step 7: Implement safe worktree reproduction instructions**

Render `REPRODUCE.md` with `git worktree add`, `git apply --check`, `git apply`, the exact Rust toolchain and `cargo furiosa-opt compile <rust_path> --exact --dump-schedule ...` command. Do not generate an executable script.

- [x] **Step 8: Run Task 7 tests**

Run:

```bash
python3 -m unittest pipeline.tests.test_export -v
```

Expected: all tests PASS.

- [x] **Step 9: Commit Task 7**

```bash
git add pipeline/templates pipeline/optcycle pipeline/tests/test_export.py
git commit -m "Add experiment decisions and records"
```

### Task 8: Documentation, full regression, and local smoke test

**Files:**
- Create: `pipeline/README.md`
- Modify: `README.md`
- Modify: `docs/STAGE1_EXECUTION_GUIDE_KO.md`
- Modify: `pipeline/specs/2026-09-12-kernel-optimization-pipeline-design.md` only if implementation exposes a verified contract difference

**Interfaces:**
- Consumes: every CLI command from Tasks 1–7
- Produces: team-facing quick start and verified end-to-end local workflow

- [x] **Step 1: Write the team quick start**

Document prerequisites, the complete `create → baseline → candidate → analyze → approve-static → arena → decide → export` flow, status meanings, local versus shared artifact locations, recovery commands, and the fact that Arena and `moa-submitter` are different systems.

- [x] **Step 2: Add top-level documentation links**

Link `pipeline/README.md` from the repository README and Stage 1 Korean guide without duplicating the full reference.

- [x] **Step 3: Run every Python test**

Run:

```bash
python3 -m unittest discover -s pipeline/tests -v
```

Expected: all tests PASS with no real Arena submission.

- [x] **Step 4: Run syntax and repository checks**

Run:

```bash
python3 -m compileall -q pipeline
bash -n scripts/rngd_test.sh scripts/rngd/remote_entrypoint.sh
git diff --check
```

Expected: all commands exit 0.

- [x] **Step 5: Run a local CLI smoke flow through baseline schedule**

Create a real experiment for `decoder_feedforward`, run `baseline`, and verify:

```bash
python3 pipeline/optimize.py create \
  --kernel decoder_feedforward \
  --name pipeline-smoke \
  --hypothesis "파이프라인 baseline 생성 smoke test"

python3 pipeline/optimize.py baseline <PRINTED_EXPERIMENT_ID>
python3 pipeline/optimize.py show <PRINTED_EXPERIMENT_ID>
```

Expected: state `BASELINE_READY`, non-empty baseline schedule and compile log under `target/pipeline/decoder_feedforward/<ID>/`. Do not modify the kernel and do not run Arena during this smoke test.

- [x] **Step 6: Inspect final change scope**

Run:

```bash
git status --short
git diff --stat
git diff -- pipeline README.md docs/STAGE1_EXECUTION_GUIDE_KO.md
```

Expected: only planned pipeline implementation/documentation changes plus the pre-existing user changes are present; no generated `target/pipeline` artifact is staged.

- [x] **Step 7: Commit Task 8**

```bash
git add pipeline README.md docs/STAGE1_EXECUTION_GUIDE_KO.md
git commit -m "Document optimization pipeline workflow"
```

- [x] **Step 8: Run final verification after the commit**

Run the complete test, syntax, shell and diff-check commands again from the committed tree. Record the test count, smoke experiment ID and commit IDs in the implementation handoff.
