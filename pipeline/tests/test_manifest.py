import tempfile
import unittest
from pathlib import Path

from pipeline.optcycle.kernels import get_kernel
from pipeline.optcycle.manifest import (
    ManifestError,
    load_manifest,
    new_manifest,
    validate_manifest,
    write_manifest,
)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "manifest.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_atomic_round_trip_preserves_manifest(self):
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
        self.assertFalse(self.path.with_name("manifest.json.tmp").exists())

    def test_rejects_unknown_schema(self):
        with self.assertRaisesRegex(ManifestError, "schema_version"):
            validate_manifest({"schema_version": 99})

    def test_rejects_missing_required_top_level_field(self):
        manifest = new_manifest(
            experiment_id="20260912-153000-copy-test",
            kernel=get_kernel("decoder_feedforward"),
            name="copy-test",
            hypothesis="중간 복사 제거",
            now="2026-09-12T15:30:00+09:00",
            base_commit="a" * 40,
            initial_status=[],
        )
        del manifest["arena"]

        with self.assertRaisesRegex(ManifestError, "arena"):
            write_manifest(self.path, manifest)


if __name__ == "__main__":
    unittest.main()
