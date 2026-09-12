import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.optcycle.arena import ArenaParseError, parse_arena_log
from pipeline.optcycle.artifacts import snapshot_submission_source, source_fingerprint
from pipeline.optcycle.cli import main
from pipeline.optcycle.kernels import get_kernel
from pipeline.optcycle.manifest import new_manifest, write_manifest
from pipeline.optcycle.process import CommandResult


FIXTURES = Path(__file__).parent / "fixtures"


class ArenaParserTests(unittest.TestCase):
    def test_success_extracts_job_accuracy_and_all_cycles(self):
        parsed = parse_arena_log((FIXTURES / "arena-success.log").read_text(encoding="utf-8"))

        self.assertEqual(parsed.job_id, 18961)
        self.assertEqual(parsed.status, "SUCCEEDED")
        self.assertTrue(parsed.accuracy_passed)
        self.assertEqual(
            {name: result["cycles"] for name, result in parsed.kernels.items()},
            {
                "sliding_project_qkv": 250288,
                "sliding_attention_output": 409907,
                "decoder_feedforward": 3704175,
            },
        )
        self.assertTrue(all(result["passed"] for result in parsed.kernels.values()))

    def test_accuracy_failure_is_not_a_pass(self):
        parsed = parse_arena_log((FIXTURES / "arena-accuracy-failure.log").read_text(encoding="utf-8"))
        self.assertFalse(parsed.accuracy_passed)
        self.assertFalse(parsed.kernels["sliding_attention_output"]["passed"])

    def test_missing_cycle_raises_with_partial_parse(self):
        text = (FIXTURES / "arena-success.log").read_text(encoding="utf-8")
        text = text.replace("    cycles=3704175\n", "")

        with self.assertRaises(ArenaParseError) as raised:
            parse_arena_log(text)

        self.assertEqual(raised.exception.partial.job_id, 18961)
        self.assertIn("sliding_project_qkv", raised.exception.partial.kernels)


class ArenaCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        (self.repo / "src/device").mkdir(parents=True)
        (self.repo / "scripts").mkdir()
        (self.repo / "src/ops.rs").write_text("candidate ops\n", encoding="utf-8")
        (self.repo / "src/device/kernel.rs").write_text("candidate device\n", encoding="utf-8")
        (self.repo / "Cargo.toml").write_text("[package]\nname='fake'\n", encoding="utf-8")
        (self.repo / "scripts/rngd_test.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        self.experiment_id = "20260912-153000-arena-test"
        self.root = self.repo / "target/pipeline/decoder_feedforward" / self.experiment_id
        snapshot_submission_source(self.repo, self.root / "source/candidate")
        manifest = new_manifest(
            experiment_id=self.experiment_id,
            kernel=get_kernel("decoder_feedforward"),
            name="arena-test",
            hypothesis="reduce copies",
            now="2026-09-12T15:30:00+00:00",
            base_commit="a" * 40,
            initial_status=[],
        )
        manifest["state"] = "READY_FOR_ARENA"
        manifest["source"]["candidate_fingerprint"] = source_fingerprint(self.repo)
        write_manifest(self.root / "manifest.json", manifest)
        (self.root / "events.jsonl").write_text("", encoding="utf-8")
        self.fixed_now = lambda: datetime(2026, 9, 12, 16, 30, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp_dir.cleanup()

    def fake_runner(self, text: str, exit_code: int, calls: list):
        def run(argv, cwd, log_path, env, stdout, on_line=None):
            calls.append((list(argv), dict(env or {})))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(text, encoding="utf-8")
            stdout.write(text)
            if on_line:
                for line in text.splitlines(keepends=True):
                    on_line(line)
            return CommandResult(tuple(argv), exit_code, "start", "finish", log_path)

        return run

    def test_preflight_rejects_wrong_state_and_modified_source_without_runner(self):
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["state"] = "CANDIDATE_READY"
        write_manifest(manifest_path, manifest)
        with patch("pipeline.optcycle.cli.run_streaming", side_effect=AssertionError("runner called")):
            self.assertEqual(main(["arena", self.experiment_id], repo_root=self.repo, stdout=io.StringIO(), stderr=io.StringIO()), 1)

        manifest["state"] = "READY_FOR_ARENA"
        write_manifest(manifest_path, manifest)
        (self.repo / "src/ops.rs").write_text("modified\n", encoding="utf-8")
        with patch("pipeline.optcycle.cli.run_streaming", side_effect=AssertionError("runner called")):
            self.assertEqual(main(["arena", self.experiment_id], repo_root=self.repo, stdout=io.StringIO(), stderr=io.StringIO()), 1)

    def test_successful_arena_records_command_result_and_readme(self):
        text = (FIXTURES / "arena-success.log").read_text(encoding="utf-8")
        calls = []
        with patch("pipeline.optcycle.cli.run_streaming", self.fake_runner(text, 0, calls)):
            result = main(["arena", self.experiment_id], repo_root=self.repo, stdout=io.StringIO(), now=self.fixed_now)

        self.assertEqual(result, 0)
        self.assertEqual(calls[0][0], [str(self.repo / "scripts/rngd_test.sh")])
        self.assertEqual(calls[0][1], {"RNGD_JOB_NAME": f"{self.experiment_id}-a001"})
        attempt_dir = self.root / "arena/attempt-001"
        self.assertTrue((attempt_dir / "result.log").is_file())
        parsed = json.loads((attempt_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(parsed["job_id"], 18961)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "ARENA_PASSED")
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("decoder_feedforward cycle: 3704175", readme)

    def test_submit_failure_without_job_is_retryable(self):
        calls = []
        with patch("pipeline.optcycle.cli.run_streaming", self.fake_runner("submit denied\n", 1, calls)):
            result = main(["arena", self.experiment_id], repo_root=self.repo, stdout=io.StringIO(), stderr=io.StringIO())

        self.assertEqual(result, 1)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "READY_FOR_ARENA")
        self.assertEqual(manifest["arena"]["attempts"][0]["status"], "SUBMIT_FAILED")
        self.assertTrue((self.root / "arena/attempt-001/result.log").is_file())

    def test_job_capture_keeps_running_and_sync_does_not_resubmit(self):
        submit_calls = []
        submit_text = "submitted job 19123\nlocal wait interrupted\n"
        with patch("pipeline.optcycle.cli.run_streaming", self.fake_runner(submit_text, 1, submit_calls)):
            self.assertEqual(main(["arena", self.experiment_id], repo_root=self.repo, stdout=io.StringIO(), stderr=io.StringIO()), 1)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "ARENA_RUNNING")
        self.assertEqual(manifest["arena"]["attempts"][0]["job_id"], 19123)

        sync_calls = []
        status_text = '{"id":19123,"status":"FAILED","exit_code":1}\n'
        failure_log = (FIXTURES / "arena-accuracy-failure.log").read_text(encoding="utf-8")
        responses = iter([(status_text, 0), (failure_log, 0)])

        def sync_runner(argv, cwd, log_path, env, stdout, on_line=None):
            text, code = next(responses)
            sync_calls.append(list(argv))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(text, encoding="utf-8")
            stdout.write(text)
            return CommandResult(tuple(argv), code, "start", "finish", log_path)

        with patch("pipeline.optcycle.cli.run_streaming", sync_runner):
            self.assertEqual(main(["sync-arena", self.experiment_id], repo_root=self.repo, stdout=io.StringIO()), 0)

        self.assertEqual(sync_calls, [["furiosa-arena", "status", "19123"], ["furiosa-arena", "logs", "19123"]])
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "ARENA_FAILED")

    def test_new_attempt_preserves_previous_pass(self):
        success = (FIXTURES / "arena-success.log").read_text(encoding="utf-8")
        calls = []
        with patch("pipeline.optcycle.cli.run_streaming", self.fake_runner(success, 0, calls)):
            self.assertEqual(main(["arena", self.experiment_id], repo_root=self.repo, stdout=io.StringIO()), 0)
            second_text = success.replace("18961", "18962")
            with patch("pipeline.optcycle.cli.run_streaming", self.fake_runner(second_text, 0, calls)):
                self.assertEqual(main(["arena", self.experiment_id, "--new-attempt"], repo_root=self.repo, stdout=io.StringIO()), 0)

        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([attempt["attempt"] for attempt in manifest["arena"]["attempts"]], [1, 2])
        self.assertTrue((self.root / "arena/attempt-001/result.json").is_file())
        self.assertTrue((self.root / "arena/attempt-002/result.json").is_file())


if __name__ == "__main__":
    unittest.main()
