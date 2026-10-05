# SPDX-License-Identifier: GPL-3.0-only
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import package_release as release


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_manifest_detects_edits_and_extra_files(self):
        (self.root / "player.txt").write_text("original")
        release.write_json(self.root / "manifest.json", release.manifest(self.root, "0.4.0", "a" * 64, {"status": "not_run"}))
        release.verify_manifest(self.root)
        (self.root / "player.txt").write_text("changed")
        with self.assertRaises(release.PackageError):
            release.verify_manifest(self.root)
        (self.root / "player.txt").write_text("original")
        (self.root / "extra.txt").write_text("extra")
        with self.assertRaises(release.PackageError):
            release.verify_manifest(self.root)

    def test_nested_manifest_is_verified(self):
        nested = self.root / 'nested'
        nested.mkdir()
        payload = nested / 'manifest.json'
        payload.write_text('original')
        release.write_json(self.root / 'manifest.json', release.manifest(self.root, '0.4.0', 'a'*64, {'status': 'not_run'}))
        release.verify_manifest(self.root)
        payload.write_text('modified')
        with self.assertRaises(release.PackageError):
            release.verify_manifest(self.root)

    def test_manifest_rejects_duplicate_and_traversal_entries(self):
        (self.root / "player.txt").write_text("original")
        value = release.manifest(self.root, "0.4.0", "a" * 64, {"status": "not_run"})
        value["files"][0]["path"] = "../player.txt"
        release.write_json(self.root / "manifest.json", value)
        with self.assertRaises(release.PackageError):
            release.verify_manifest(self.root)

    def test_archive_is_reproducible_and_preserves_existing_output(self):
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "a.txt").write_text("one")
        (stage / "b.txt").write_text("two")
        first, second = self.root / "first.zip", self.root / "second.zip"
        release.make_zip(stage, first)
        release.make_zip(stage, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        before = first.read_bytes()
        with self.assertRaises(release.PackageError):
            release.make_zip(stage, first)
        self.assertEqual(before, first.read_bytes())

    def test_source_excludes_runs_caches_generated_payload_and_binary(self):
        for name in ("run.py", "LICENSE", "runtime-payload.json", "secret.exe", "audit.json"):
            (self.root / name).write_text("fixture")
        for folder, name in (("dotnet/Player", "Program.cs"), ("dotnet/Player/obj", "Generated.cs"),
                             ("user_runs", "secret.py"), ("packaging/__pycache__", "cache.py")):
            path = self.root / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        paths = {path.relative_to(self.root).as_posix() for path in release.source_files(self.root)}
        self.assertEqual(paths, {"run.py", "LICENSE", "dotnet/Player/Program.cs"})

    def test_source_archive_contains_corresponding_code_and_images(self):
        (self.root / "run.py").write_text("pass")
        game = self.root / "game.tiff"
        game.write_bytes(b"fixture")
        output = self.root / "source.zip"
        release.write_source_archive(self.root, output, [game])
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {"run.py", "game.tiff"})

    def fake_publish(self):
        for name in ("OpticalPlayer.exe", "OpticalPlayer.dll", "OpticalPlayer.deps.json", "coreclr.dll",
                     "hostfxr.dll", "hostpolicy.dll", "System.Private.CoreLib.dll", "System.Windows.Forms.dll"):
            (self.root / name).write_bytes(b"test fixture, not executable")
        release.write_json(self.root / "OpticalPlayer.runtimeconfig.json", {"runtimeOptions": {
            "includedFrameworks": [{"name": "Microsoft.NETCore.App"}, {"name": "Microsoft.WindowsDesktop.App"}]}})

    def test_publish_requires_self_contained_frameworks(self):
        self.fake_publish()
        release.check_publish(self.root)
        release.write_json(self.root / "OpticalPlayer.runtimeconfig.json", {"runtimeOptions": {
            "frameworks": [{"name": "Microsoft.NETCore.App"}]}})
        with self.assertRaises(release.PackageError):
            release.check_publish(self.root)

    def test_publish_rejects_hidden_disk_interpreter(self):
        self.fake_publish()
        (self.root / "ArrayRuntime.dll").write_bytes(b"forbidden")
        with self.assertRaisesRegex(release.PackageError, "fallback"):
            release.check_publish(self.root)

    def test_file_count_and_bytes_are_bounded(self):
        (self.root / "one").write_bytes(b"12345")
        with self.assertRaises(release.PackageError):
            release.safe_files(self.root, limit=4)
        with patch.object(release, "MAX_FILES", 0):
            with self.assertRaises(release.PackageError):
                release.safe_files(self.root)

    def test_policy_denial_is_specific(self):
        self.assertTrue(release.policy_block(returncode=0x800711C7))
        self.assertTrue(release.policy_block(returncode=-1073740760))
        self.assertFalse(release.policy_block(returncode=1))
        self.assertFalse(release.policy_block(error=PermissionError("ordinary filesystem denial")))

    def test_clean_environment_omits_installed_toolchains(self):
        with patch.dict(os.environ, {"DOTNET_ROOT": "private", "PYTHONPATH": "private", "PATH": "private"}):
            environment = release.clean_environment()
        self.assertNotIn("DOTNET_ROOT", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("private", environment["PATH"])

    def test_images_must_match_program_and_built_runtime(self):
        first = {"runtime": {}, "program": {}, "params": {"tick": 0, "game_over": False}}
        with patch.object(release, "decoded_image", side_effect=[first, first]), patch.object(release, "runtime_hash", return_value="a" * 64):
            release.validate_images(None, None, "a" * 64)
        with patch.object(release, "decoded_image", side_effect=[first, first]), patch.object(release, "runtime_hash", return_value="b" * 64):
            with self.assertRaises(release.PackageError):
                release.validate_images(None, None, "a" * 64)
        with patch.object(release, "decoded_image", side_effect=[first, {**first, "program": {"different": True}}]):
            with self.assertRaises(release.PackageError):
                release.validate_images(None, None, "a" * 64)


if __name__ == "__main__":
    unittest.main()

