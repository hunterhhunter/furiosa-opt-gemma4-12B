import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.optcycle.cli import main
from pipeline.optcycle.process import run_streaming


class ScheduleCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        (self.repo / "src/device").mkdir(parents=True)
        (self.repo / "scripts").mkdir()
        (self.repo / "src/ops.rs").write_text("ops baseline\n", encoding="utf-8")
        (self.repo / "src/device/kernel.rs").write_text("device baseline\n", encoding="utf-8")
        (self.repo / "Cargo.toml").write_text("[package]\nname='fake'\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
            cwd=self.repo,
            check=True,
        )
        self.bin_dir = self.repo / "bin"
        self.bin_dir.mkdir()
        self.args_log = self.repo / "cargo-args.log"
        cargo = self.bin_dir / "cargo"
        cargo.write_text(
            """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CARGO_ARGS"
if [ "${FAKE_CARGO_EXIT:-0}" -ne 0 ]; then
  echo "fake compile failed" >&2
  exit "$FAKE_CARGO_EXIT"
fi
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--dump-schedule" ]; then
    shift
    mkdir -p "$(dirname "$1")"
    printf '{"makespan":123}\\n' > "$1"
    exit 0
  fi
  shift
done
echo "missing --dump-schedule" >&2
exit 9
""",
            encoding="utf-8",
        )
        cargo.chmod(cargo.stat().st_mode | stat.S_IXUSR)
        self.fixed_now = lambda: datetime(2026, 9, 12, 15, 30, tzinfo=timezone.utc)
        self.experiment_id = "20260912-153000-schedule-test"

    def tearDown(self):
        self.temp_dir.cleanup()

    @contextmanager
    def fake_cargo(self, exit_code: int = 0):
        with patch.dict(
            os.environ,
            {
                "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
                "FAKE_CARGO_ARGS": str(self.args_log),
                "FAKE_CARGO_EXIT": str(exit_code),
            },
        ):
            yield

    def create_experiment(self):
        result = main(
            ["create", "--kernel", "decoder_feedforward", "--name", "schedule-test", "--hypothesis", "copy removal"],
            repo_root=self.repo,
            stdout=io.StringIO(),
            now=self.fixed_now,
        )
        self.assertEqual(result, 0)
        return self.repo / "target/pipeline/decoder_feedforward" / self.experiment_id

    def test_streaming_runner_captures_stdout_stderr_and_exit_code(self):
        script = self.repo / "emit.sh"
        script.write_text("#!/bin/sh\necho stdout-line\necho stderr-line >&2\nexit 7\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        terminal = io.StringIO()
        log = self.repo / "combined.log"

        result = run_streaming([str(script)], self.repo, log, None, terminal)

        self.assertEqual(result.exit_code, 7)
        self.assertIn("stdout-line", terminal.getvalue())
        self.assertIn("stderr-line", terminal.getvalue())
        self.assertEqual(log.read_text(encoding="utf-8"), terminal.getvalue())

    def test_baseline_records_snapshot_schedule_log_and_exact_command(self):
        root = self.create_experiment()
        output = io.StringIO()
        with self.fake_cargo():
            result = main(["baseline", self.experiment_id], repo_root=self.repo, stdout=output)

        self.assertEqual(result, 0, output.getvalue())
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "BASELINE_READY")
        self.assertTrue(manifest["source"]["baseline_fingerprint"])
        self.assertTrue((root / "source/baseline/src/ops.rs").is_file())
        self.assertTrue((root / "build/baseline/attempt-001.log").is_file())
        self.assertGreater((root / "schedule/baseline.json").stat().st_size, 0)
        args = self.args_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(args[:4], ["furiosa-opt", "compile", "ops::decoder_feedforward", "--exact"])
        self.assertIn("--dump-schedule", args)

    def test_failed_baseline_is_retryable_without_overwriting_attempt_log(self):
        root = self.create_experiment()
        with self.fake_cargo(exit_code=2):
            first = main(["baseline", self.experiment_id], repo_root=self.repo, stdout=io.StringIO(), stderr=io.StringIO())
            second = main(["baseline", self.experiment_id], repo_root=self.repo, stdout=io.StringIO(), stderr=io.StringIO())

        self.assertEqual(first, 1)
        self.assertEqual(second, 1)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "CREATED")
        first_log = root / "build/baseline/attempt-001.log"
        second_log = root / "build/baseline/attempt-002.log"
        self.assertIn("fake compile failed", first_log.read_text(encoding="utf-8"))
        self.assertIn("fake compile failed", second_log.read_text(encoding="utf-8"))
        events = (root / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(events.count('"event":"BASELINE_COMPILE"'), 2)

    def test_candidate_records_changed_snapshot_patch_and_schedule(self):
        root = self.create_experiment()
        with self.fake_cargo():
            self.assertEqual(main(["baseline", self.experiment_id], repo_root=self.repo, stdout=io.StringIO()), 0)
            (self.repo / "src/device/kernel.rs").write_text("candidate device\n", encoding="utf-8")
            result = main(["candidate", self.experiment_id], repo_root=self.repo, stdout=io.StringIO())

        self.assertEqual(result, 0)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "CANDIDATE_READY")
        self.assertNotEqual(manifest["source"]["candidate_fingerprint"], manifest["source"]["baseline_fingerprint"])
        self.assertTrue((root / "source/candidate/src/device/kernel.rs").is_file())
        self.assertGreater((root / "source/candidate.patch").stat().st_size, 0)
        self.assertGreater((root / "schedule/candidate.json").stat().st_size, 0)
        self.assertTrue((root / "build/candidate/attempt-001.log").is_file())


if __name__ == "__main__":
    unittest.main()
