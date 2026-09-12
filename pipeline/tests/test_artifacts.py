import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.optcycle.artifacts import (
    ArtifactError,
    commit_artifacts,
    ExperimentPaths,
    create_patch,
    find_experiment,
    make_experiment_id,
    snapshot_submission_source,
    source_fingerprint,
)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        (self.repo / "src/device/nested").mkdir(parents=True)
        (self.repo / "src/ops.rs").write_text("baseline ops\n", encoding="utf-8")
        (self.repo / "src/device/a.rs").write_text("device a\n", encoding="utf-8")
        (self.repo / "src/device/nested/b.rs").write_text(
            "device b\n", encoding="utf-8"
        )
        (self.repo / "src/not-submitted.rs").write_text(
            "ignored\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_make_experiment_id_uses_safe_slug_and_collision_suffix(self):
        existing = {"20260912-153000-down-project-copy"}
        self.assertEqual(
            make_experiment_id("Down Project Copy", "20260912-153000", set()),
            "20260912-153000-down-project-copy",
        )
        self.assertEqual(
            make_experiment_id("Down Project Copy", "20260912-153000", existing),
            "20260912-153000-down-project-copy-02",
        )

    def test_find_experiment_rejects_traversal(self):
        with self.assertRaisesRegex(ArtifactError, "invalid experiment id"):
            find_experiment(self.repo, "../outside")

    def test_experiment_paths_stay_under_target_pipeline(self):
        paths = ExperimentPaths.create(
            self.repo,
            "decoder_feedforward",
            "20260912-153000-copy-test",
        )
        self.assertEqual(
            paths.manifest,
            self.repo
            / "target/pipeline/decoder_feedforward/20260912-153000-copy-test/manifest.json",
        )
        self.assertEqual(paths.schedule, paths.root / "schedule")

    def test_fingerprint_ignores_timestamps_but_detects_content(self):
        before = source_fingerprint(self.repo)
        os.utime(self.repo / "src/ops.rs", (1_800_000_000, 1_800_000_000))
        self.assertEqual(source_fingerprint(self.repo), before)

        (self.repo / "src/device/a.rs").write_text("changed\n", encoding="utf-8")
        self.assertNotEqual(source_fingerprint(self.repo), before)

    def test_snapshot_contains_only_submission_scope(self):
        destination = self.repo / "snapshot"
        snapshot_submission_source(self.repo, destination)

        files = sorted(
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file()
        )
        self.assertEqual(
            files,
            ["src/device/a.rs", "src/device/nested/b.rs", "src/ops.rs"],
        )
        self.assertEqual(source_fingerprint(destination), source_fingerprint(self.repo))

    def test_patch_reproduces_candidate_snapshot(self):
        baseline = self.repo / "baseline"
        candidate = self.repo / "candidate"
        reproduced = self.repo / "reproduced"
        patch_path = self.repo / "candidate.patch"
        snapshot_submission_source(self.repo, baseline)
        shutil.copytree(baseline, candidate)
        (candidate / "src/ops.rs").write_text("candidate ops\n", encoding="utf-8")
        (candidate / "src/device/new.rs").write_text("new file\n", encoding="utf-8")

        create_patch(baseline, candidate, patch_path)
        shutil.copytree(baseline, reproduced)
        result = subprocess.run(
            ["git", "apply", str(patch_path)],
            cwd=reproduced,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(source_fingerprint(reproduced), source_fingerprint(candidate))

    def test_artifact_commit_rolls_back_all_moves_after_partial_failure(self):
        sources = []
        destinations = []
        for number in range(3):
            source = self.repo / f"temporary-{number}"
            destination = self.repo / "canonical" / f"artifact-{number}"
            source.write_text(f"content-{number}\n", encoding="utf-8")
            sources.append(source)
            destinations.append(destination)
        real_replace = os.replace
        calls = 0

        def fail_second_move(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected move failure")
            real_replace(source, destination)

        with patch("pipeline.optcycle.artifacts.os.replace", fail_second_move):
            with self.assertRaisesRegex(ArtifactError, "artifact commit failed"):
                commit_artifacts(list(zip(sources, destinations)))

        self.assertTrue(all(path.is_file() for path in sources))
        self.assertTrue(all(not path.exists() for path in destinations))


if __name__ == "__main__":
    unittest.main()
