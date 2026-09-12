import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.optcycle.cli import main
from pipeline.optcycle.kernels import get_kernel
from pipeline.optcycle.manifest import new_manifest, write_manifest


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        (self.repo / "src/device").mkdir(parents=True)
        (self.repo / "src/ops.rs").write_text("ops\n", encoding="utf-8")
        (self.repo / "src/device/mod.rs").write_text("device\n", encoding="utf-8")
        (self.repo / "Cargo.toml").write_text("[package]\nname='fake'\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pipeline Test",
                "-c",
                "user.email=pipeline@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=self.repo,
            check=True,
        )
        self.fixed_now = lambda: datetime(
            2026, 9, 12, 15, 30, 0, tzinfo=timezone.utc
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_writes_manifest_event_and_human_readable_index(self):
        stdout = io.StringIO()

        exit_code = main(
            [
                "create",
                "--kernel",
                "decoder_feedforward",
                "--name",
                "copy-test",
                "--hypothesis",
                "중간복사제거",
            ],
            repo_root=self.repo,
            stdout=stdout,
            now=self.fixed_now,
        )

        self.assertEqual(exit_code, 0)
        experiment_id = "20260912-153000-copy-test"
        root = self.repo / "target/pipeline/decoder_feedforward" / experiment_id
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        events = [
            json.loads(line)
            for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(manifest["state"], "CREATED")
        self.assertEqual(events, [{"at": "2026-09-12T15:30:00+00:00", "event": "CREATED", "result": "success"}])
        self.assertIn("중간복사제거", (root / "README.md").read_text(encoding="utf-8"))
        self.assertIn(experiment_id, stdout.getvalue())
        self.assertIn(f"baseline {experiment_id}", stdout.getvalue())

    def test_list_and_show_expose_state_hypothesis_and_next_command(self):
        self._write_experiment("20260912-153000-first", "CREATED", "첫 가설")
        self._write_experiment("20260912-153001-second", "BASELINE_READY", "둘째 가설")
        listed = io.StringIO()
        shown = io.StringIO()

        self.assertEqual(main(["list"], repo_root=self.repo, stdout=listed), 0)
        self.assertEqual(
            main(
                ["show", "20260912-153000-first"],
                repo_root=self.repo,
                stdout=shown,
            ),
            0,
        )

        list_text = listed.getvalue()
        self.assertIn("20260912-153000-first", list_text)
        self.assertIn("20260912-153001-second", list_text)
        self.assertIn("decoder_feedforward", list_text)
        self.assertIn("BASELINE_READY", list_text)
        show_text = shown.getvalue()
        self.assertIn("첫 가설", show_text)
        self.assertIn(
            "python3 pipeline/optimize.py baseline 20260912-153000-first",
            show_text,
        )

    def _write_experiment(self, experiment_id: str, state: str, hypothesis: str):
        root = self.repo / "target/pipeline/decoder_feedforward" / experiment_id
        manifest = new_manifest(
            experiment_id=experiment_id,
            kernel=get_kernel("decoder_feedforward"),
            name=experiment_id.rsplit("-", 1)[-1],
            hypothesis=hypothesis,
            now="2026-09-12T15:30:00+00:00",
            base_commit="a" * 40,
            initial_status=[],
        )
        manifest["state"] = state
        write_manifest(root / "manifest.json", manifest)
        (root / "events.jsonl").write_text("", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
