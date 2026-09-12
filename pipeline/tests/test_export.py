import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.optcycle.artifacts import create_patch, snapshot_submission_source, source_fingerprint
from pipeline.optcycle.cli import main
from pipeline.optcycle.kernels import get_kernel
from pipeline.optcycle.manifest import new_manifest, write_manifest


class DecisionAndExportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        (self.repo / "src/device").mkdir(parents=True)
        (self.repo / "src/ops.rs").write_text("original ops\n", encoding="utf-8")
        (self.repo / "src/device/kernel.rs").write_text("original device\n", encoding="utf-8")
        (self.repo / "Cargo.toml").write_text("[package]\nname='fake'\nversion='0.1.0'\n", encoding="utf-8")
        (self.repo / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.85"\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
            cwd=self.repo,
            check=True,
        )
        self.base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        self.experiment_id = "20260912-153000-export-test"
        self.root = self.repo / "target/pipeline/decoder_feedforward" / self.experiment_id

        base_snapshot = Path(self.temp_dir.name) / "base"
        (base_snapshot / "src/device").mkdir(parents=True)
        (base_snapshot / "src/ops.rs").write_text("original ops\n", encoding="utf-8")
        (base_snapshot / "src/device/kernel.rs").write_text("original device\n", encoding="utf-8")
        (self.repo / "src/ops.rs").write_text("baseline ops\n", encoding="utf-8")
        snapshot_submission_source(self.repo, self.root / "source/baseline")
        create_patch(base_snapshot, self.root / "source/baseline", self.root / "source/baseline.patch")
        (self.repo / "src/device/kernel.rs").write_text("candidate device\n", encoding="utf-8")
        snapshot_submission_source(self.repo, self.root / "source/candidate")
        create_patch(self.root / "source/baseline", self.root / "source/candidate", self.root / "source/candidate.patch")

        (self.root / "schedule").mkdir(parents=True)
        baseline_schedule = self.root / "schedule/baseline.json"
        candidate_schedule = self.root / "schedule/candidate.json"
        baseline_schedule.write_text('{"makespan":200}\n', encoding="utf-8")
        candidate_schedule.write_text('{"makespan":180}\n', encoding="utf-8")
        manifest = new_manifest(
            experiment_id=self.experiment_id,
            kernel=get_kernel("decoder_feedforward"),
            name="export-test",
            hypothesis="reduce intermediate copies",
            now="2026-09-12T15:30:00+00:00",
            base_commit=self.base_commit,
            initial_status=["M src/ops.rs"],
        )
        manifest["state"] = "ARENA_PASSED"
        manifest["source"]["baseline_fingerprint"] = source_fingerprint(self.root / "source/baseline")
        manifest["source"]["candidate_fingerprint"] = source_fingerprint(self.root / "source/candidate")
        manifest["schedule"]["baseline"] = {"path": "schedule/baseline.json", "sha256": self.sha(baseline_schedule)}
        manifest["schedule"]["candidate"] = {"path": "schedule/candidate.json", "sha256": self.sha(candidate_schedule)}
        manifest["arena"]["attempts"] = [{
            "attempt": 1,
            "job_id": 18961,
            "job_name": f"{self.experiment_id}-a001",
            "status": "SUCCEEDED",
            "exit_code": 0,
            "accuracy_passed": True,
            "kernels": {
                "sliding_project_qkv": {"passed": True, "cycles": 250288},
                "sliding_attention_output": {"passed": True, "cycles": 409907},
                "decoder_feedforward": {"passed": True, "cycles": 3600000},
            },
        }]
        write_manifest(self.root / "manifest.json", manifest)
        (self.root / "events.jsonl").write_text("", encoding="utf-8")
        (self.root / "build").mkdir()
        (self.root / "build/private.log").write_text("TOKEN=do-not-export\n", encoding="utf-8")
        self.fixed_now = lambda: datetime(2026, 9, 12, 17, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def decide_keep(self):
        return main(
            ["decide", self.experiment_id, "--keep", "--reason", "accuracy PASS and lower cycle"],
            repo_root=self.repo,
            stdout=io.StringIO(),
            now=self.fixed_now,
        )

    def test_decision_enforces_evidence_and_records_without_changing_source(self):
        before = source_fingerprint(self.repo)
        self.assertEqual(self.decide_keep(), 0)
        self.assertEqual(source_fingerprint(self.repo), before)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "KEPT")
        self.assertEqual(manifest["decision"], {
            "result": "KEEP",
            "reason": "accuracy PASS and lower cycle",
            "at": "2026-09-12T17:00:00+00:00",
        })
        self.assertIn("KEEP", (self.root / "decision.md").read_text(encoding="utf-8"))

    def test_keep_fails_without_pass_but_reject_is_allowed_after_failure(self):
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["state"] = "ARENA_FAILED"
        manifest["arena"]["attempts"][0]["accuracy_passed"] = False
        write_manifest(manifest_path, manifest)

        self.assertEqual(main(["decide", self.experiment_id, "--keep", "--reason", "no proof"], repo_root=self.repo, stdout=io.StringIO(), stderr=io.StringIO()), 1)
        self.assertEqual(main(["decide", self.experiment_id, "--reject", "--reason", "accuracy failed"], repo_root=self.repo, stdout=io.StringIO()), 0)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "REJECTED")

    def test_keep_and_reject_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            main(["decide", self.experiment_id, "--keep", "--reject", "--reason", "invalid"], repo_root=self.repo)

    def test_export_contains_only_sanitized_summary_patches_and_reproduction(self):
        self.assertEqual(self.decide_keep(), 0)
        self.assertEqual(main(["export", self.experiment_id], repo_root=self.repo, stdout=io.StringIO()), 0)

        record = self.repo / "pipeline/records" / self.experiment_id
        files = sorted(path.name for path in record.iterdir())
        self.assertEqual(files, ["README.md", "REPRODUCE.md", "baseline.patch", "candidate.patch", "manifest.json"])
        combined = "\n".join(path.read_text(encoding="utf-8") for path in record.iterdir())
        self.assertNotIn(str(self.repo), combined)
        self.assertNotIn("do-not-export", combined)
        self.assertNotIn("initial_status", json.loads((record / "manifest.json").read_text(encoding="utf-8")))
        self.assertIn("git worktree add", (record / "REPRODUCE.md").read_text(encoding="utf-8"))
        self.assertIn("cargo furiosa-opt compile ops::decoder_feedforward --exact", (record / "REPRODUCE.md").read_text(encoding="utf-8"))

    def test_export_is_idempotent_but_refuses_modified_existing_record(self):
        self.assertEqual(self.decide_keep(), 0)
        self.assertEqual(main(["export", self.experiment_id], repo_root=self.repo, stdout=io.StringIO()), 0)
        self.assertEqual(main(["export", self.experiment_id], repo_root=self.repo, stdout=io.StringIO()), 0)
        record_readme = self.repo / "pipeline/records" / self.experiment_id / "README.md"
        record_readme.write_text("team edit\n", encoding="utf-8")

        result = main(["export", self.experiment_id], repo_root=self.repo, stdout=io.StringIO(), stderr=io.StringIO())

        self.assertEqual(result, 1)
        self.assertEqual(record_readme.read_text(encoding="utf-8"), "team edit\n")

    def test_exported_patches_reconstruct_candidate_fingerprint(self):
        self.assertEqual(self.decide_keep(), 0)
        self.assertEqual(main(["export", self.experiment_id], repo_root=self.repo, stdout=io.StringIO()), 0)
        record = self.repo / "pipeline/records" / self.experiment_id
        reproduced = Path(self.temp_dir.name) / "reproduced"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(reproduced)], check=True)
        subprocess.run(["git", "checkout", "-q", self.base_commit], cwd=reproduced, check=True)
        subprocess.run(["git", "apply", str(record / "baseline.patch")], cwd=reproduced, check=True)
        subprocess.run(["git", "apply", str(record / "candidate.patch")], cwd=reproduced, check=True)
        exported = json.loads((record / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(source_fingerprint(reproduced), exported["source"]["candidate_fingerprint"])


if __name__ == "__main__":
    unittest.main()
