import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.optcycle.artifacts import snapshot_submission_source, source_fingerprint
from pipeline.optcycle.cli import main
from pipeline.optcycle.kernels import get_kernel
from pipeline.optcycle.manifest import new_manifest, write_manifest


class StaticReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        (self.repo / "src/device").mkdir(parents=True)
        (self.repo / "src/ops.rs").write_text("ops\n", encoding="utf-8")
        (self.repo / "src/device/kernel.rs").write_text("candidate\n", encoding="utf-8")
        (self.repo / "Cargo.toml").write_text("[package]\nname='fake'\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.experiment_id = "20260912-153000-static-test"
        self.root = self.repo / "target/pipeline/decoder_feedforward" / self.experiment_id
        (self.root / "schedule").mkdir(parents=True)
        baseline = self.root / "schedule/baseline.json"
        candidate = self.root / "schedule/candidate.json"
        baseline.write_text('{"makespan":200}\n', encoding="utf-8")
        candidate.write_text('{"makespan":180}\n', encoding="utf-8")
        snapshot_submission_source(self.repo, self.root / "source/candidate")
        manifest = new_manifest(
            experiment_id=self.experiment_id,
            kernel=get_kernel("decoder_feedforward"),
            name="static-test",
            hypothesis="reduce copies",
            now="2026-09-12T15:30:00+00:00",
            base_commit="a" * 40,
            initial_status=[],
        )
        manifest["state"] = "CANDIDATE_READY"
        manifest["source"]["candidate_fingerprint"] = source_fingerprint(self.repo)
        manifest["schedule"]["baseline"] = {"path": "schedule/baseline.json", "sha256": self.sha(baseline)}
        manifest["schedule"]["candidate"] = {"path": "schedule/candidate.json", "sha256": self.sha(candidate)}
        write_manifest(self.root / "manifest.json", manifest)
        (self.root / "events.jsonl").write_text("", encoding="utf-8")
        self.fixed_now = lambda: datetime(2026, 9, 12, 16, 20, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_analyze_creates_manual_review_template_with_required_sections(self):
        output = io.StringIO()
        result = main(["analyze", self.experiment_id], repo_root=self.repo, stdout=output)

        self.assertEqual(result, 0)
        analysis = (self.root / "schedule/analysis.md").read_text(encoding="utf-8")
        for expected in (
            "schedule/baseline.json",
            "schedule/candidate.json",
            "Makespan",
            "Context occupancy",
            "Overlap",
            "Source hotspots",
            "Memory",
            "Hypothesis evaluation",
            "Arena recommendation",
        ):
            self.assertIn(expected, analysis)

    def test_analyze_does_not_overwrite_human_edits(self):
        self.assertEqual(main(["analyze", self.experiment_id], repo_root=self.repo, stdout=io.StringIO()), 0)
        analysis_path = self.root / "schedule/analysis.md"
        analysis_path.write_text("human review\n", encoding="utf-8")

        result = main(["analyze", self.experiment_id], repo_root=self.repo, stdout=io.StringIO(), stderr=io.StringIO())

        self.assertEqual(result, 1)
        self.assertEqual(analysis_path.read_text(encoding="utf-8"), "human review\n")

    def test_approval_rejects_absent_analysis_and_changed_evidence(self):
        missing = main(
            ["approve-static", self.experiment_id, "--note", "looks better"],
            repo_root=self.repo,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(missing, 1)

        self.assertEqual(main(["analyze", self.experiment_id], repo_root=self.repo, stdout=io.StringIO()), 0)
        candidate_schedule = self.root / "schedule/candidate.json"
        candidate_schedule.write_text('{"makespan":999}\n', encoding="utf-8")
        changed_schedule = main(
            ["approve-static", self.experiment_id, "--note", "looks better"],
            repo_root=self.repo,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(changed_schedule, 1)
        candidate_schedule.write_text('{"makespan":180}\n', encoding="utf-8")

        (self.repo / "src/ops.rs").write_text("changed after candidate\n", encoding="utf-8")
        changed_source = main(
            ["approve-static", self.experiment_id, "--note", "looks better"],
            repo_root=self.repo,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(changed_source, 1)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "CANDIDATE_READY")

    def test_approval_records_review_hash_note_and_ready_state(self):
        self.assertEqual(main(["analyze", self.experiment_id], repo_root=self.repo, stdout=io.StringIO()), 0)
        result = main(
            ["approve-static", self.experiment_id, "--note", "makespan and TDMA reviewed"],
            repo_root=self.repo,
            stdout=io.StringIO(),
            now=self.fixed_now,
        )

        self.assertEqual(result, 0)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "READY_FOR_ARENA")
        self.assertEqual(manifest["schedule"]["approved_note"], "makespan and TDMA reviewed")
        self.assertEqual(manifest["schedule"]["analysis_sha256"], self.sha(self.root / "schedule/analysis.md"))
        self.assertEqual(manifest["schedule"]["approved_at"], "2026-09-12T16:20:00+00:00")


if __name__ == "__main__":
    unittest.main()
