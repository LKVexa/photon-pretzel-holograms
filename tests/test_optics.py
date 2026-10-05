# SPDX-License-Identifier: GPL-3.0-only
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image, TiffImagePlugin

import optics as o
import raster_vm as vm


class OpticalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.params = dict(o.PARAMS)
        self.samples, self.metadata, _ = o.synthesize(o.target_field(), self.params)

    def write_tiff(self, name="fixture.tiff", array=None, metadata=None, append=None):
        path = self.root / name
        tags = TiffImagePlugin.ImageFileDirectory_v2()
        tags[270] = metadata if metadata is not None else o.canonical(self.metadata).decode()
        im = Image.fromarray(self.samples if array is None else array)
        options = {"save_all": True, "append_images": [append]} if append else {}
        im.save(path, tiffinfo=tags, **options)
        return path

    def test_analytic_plane_waves_both_directions_and_zero(self):
        y, x = np.mgrid[:128, :128]
        for bx, by in ((0, 0), (3, 0), (2, -4)):
            plane = np.exp(2j * np.pi * (bx*x+by*y) / 128)
            kz = np.sqrt((1/self.params["wavelength_m"])**2 -
                         (bx/(128*self.params["pitch_m"]))**2 -
                         (by/(128*self.params["pitch_m"]))**2)
            for z in (-.02, -.01, 0, .01, .02):
                expected = plane * np.exp(2j*np.pi*z*kz)
                error = float(np.max(np.abs(o.propagate(plane, self.params, z)-expected)))
                self.assertLess(error, o.ACCEPTANCE["plane_wave_max_abs"])

    def test_zero_distance_exact_identity_and_copy(self):
        field = o.target_field()
        actual = o.propagate(field, self.params, 0)
        np.testing.assert_array_equal(actual, field)
        self.assertFalse(np.shares_memory(actual, field))

    def test_training_and_holdout_quantized_reconstruction(self):
        for scene in ("training", "holdout"):
            for z in (-.02, 0, .01, .02):
                params = {**self.params, "distance_m": z}
                target = o.target_field(scene)
                samples, metadata, _ = o.synthesize(target, params)
                actual = o.reconstruct(samples, metadata)
                error = np.linalg.norm(actual-target)/np.linalg.norm(target)
                self.assertLess(error, o.ACCEPTANCE["relative_complex_l2_max"])

    def test_sample_and_metadata_tiff_roundtrip(self):
        path = self.root / "exact.tiff"
        o.save_hologram(path, self.samples, self.metadata)
        actual, metadata, digest = o.read_hologram(path)
        np.testing.assert_array_equal(actual, self.samples)
        self.assertEqual(metadata, self.metadata)
        self.assertEqual(digest, o.sha256(path.read_bytes()))

    def test_reader_accepts_big_endian_unsigned_samples(self):
        path = self.write_tiff(array=self.samples.astype(">u2"))
        actual, _, _ = o.read_hologram(path)
        np.testing.assert_array_equal(actual, self.samples)

    def test_reject_sample_shape_and_dtype(self):
        for array in (np.zeros((2, 3), dtype="uint16"), self.samples.astype("float64"),
                      np.full((128,128), -1, dtype="int16"), np.ones((128,128), dtype=bool),
                      np.zeros((128,128,1), dtype="uint16"), self.samples.tolist()):
            with self.subTest(type=type(array), dtype=getattr(array, "dtype", None)):
                with self.assertRaises(o.Rejected):
                    o.validate_samples(array, 1, self.params)

    def test_reject_bad_scale(self):
        for scale in (0, -1, float("nan"), float("inf"), 1e200, 17, True, "1", None):
            with self.subTest(scale=scale), self.assertRaises(o.Rejected):
                o.validate_samples(self.samples, scale, self.params)

    def test_reject_float_or_boolean_frequency_bins(self):
        for name in ("size", "band_bins", "carrier_bin"):
            for value in (float(self.params[name]), True):
                params = {**self.params, name: value}
                with self.assertRaises(o.Rejected):
                    o.validate_params(params)

    def test_reject_nonfinite_or_out_of_profile_parameters(self):
        for name, value in (("distance_m", .020001), ("distance_m", float("nan")),
                            ("distance_m", 10**500), ("pitch_m", 1e-8), ("wavelength_m", 0)):
            with self.subTest(name=name, value=value), self.assertRaises(o.Rejected):
                o.validate_params({**self.params, name: value})

    def test_reject_metadata_duplicates_nonfinite_deep_and_oversize(self):
        good = o.canonical(self.metadata).decode()
        values = [good[:-1]+',"scale":1}', good.replace('"scale":', '"scale":NaN,"old":'),
                  '['*900+'0'+']'*900, ' '*2049, '{}', good.replace(o.PROFILE, "other")]
        for value in values:
            with self.subTest(prefix=value[:40]), self.assertRaises(o.Rejected):
                o.read_hologram(self.write_tiff(metadata=value))

    def test_reject_physical_claim_and_unknown_fields(self):
        for metadata in ({**self.metadata, "physical_measurement": True},
                         {**self.metadata, "command": "must never execute"},
                         {**self.metadata, "scale": float("inf")}):
            with self.assertRaises(o.Rejected):
                o.validate_metadata(metadata)

    def test_reject_multiframe_and_wrong_image_types(self):
        extra = Image.fromarray(self.samples)
        with self.assertRaises(o.Rejected):
            o.read_hologram(self.write_tiff(append=extra))
        with self.assertRaises(o.Rejected):
            o.read_hologram(self.write_tiff(array=np.zeros((128,128), dtype="uint8")))
        with self.assertRaises(o.Rejected):
            o.read_hologram(self.write_tiff(array=np.zeros((64,64), dtype="uint16")))
        png = self.root / "fake.tiff"
        Image.fromarray(self.samples).save(png, format="PNG")
        with self.assertRaises(o.Rejected):
            o.read_hologram(png)

    def test_reject_truncated_or_oversize_files(self):
        truncated = self.root / "truncated.tiff"
        truncated.write_bytes(b"II*\0\x08\0\0\0")
        with self.assertRaises(o.Rejected):
            o.read_hologram(truncated)
        oversized = self.root / "huge.tiff"
        with oversized.open("wb") as stream:
            stream.seek(o.MAX_FILE_BYTES)
            stream.write(b"x")
        with self.assertRaises(o.Rejected):
            o.read_hologram(oversized)

    def test_reject_invalid_fields_including_int_min_overflow(self):
        for field in (np.full((128,128), np.inf), np.full((128,128), np.nan+0j),
                      np.full((128,128), np.iinfo(np.int64).min), np.zeros((2,3)),
                      np.full((128,128), "x"), np.ones((128,128))*17):
            with self.assertRaises(o.Rejected):
                o.propagate(field, self.params)

    def test_zero_hologram_is_finite_without_quality_claim(self):
        actual = o.reconstruct(np.zeros((128,128), dtype="uint16"), self.metadata)
        np.testing.assert_array_equal(actual, np.zeros((128,128)))

    def test_generated_assets_and_reconstruction_preserve_source(self):
        report = o.generate(self.root / "generated")
        source = self.root / "generated/hologram_uint16.tiff"
        before = source.read_bytes()
        reconstructed = o.reconstruct_file(source, self.root / "reconstructed")
        self.assertEqual(before, source.read_bytes())
        self.assertEqual(reconstructed["source_sha256"], report["hologram_sha256"])
        self.assertEqual(reconstructed["quality_against_unknown_target"], "NOT_ASSESSED")
        with Image.open(self.root / "generated/processing.gif") as gif:
            self.assertEqual(gif.n_frames, 2)

    def test_executable_gif_tiff_every_frame_and_reexecution(self):
        expected = o.generate(self.root/"carriers")
        program = vm.compile_reconstruction(self.samples,self.metadata)
        for suffix in ("gif", "tiff"):
            path = self.root/"carriers"/("processing."+suffix)
            recovered, digest, count = vm.read(path)
            self.assertEqual(recovered, program)
            self.assertEqual(count, 2)
            replay = vm.replay(path, self.root/("replay_"+suffix))
            self.assertEqual(replay["source_sha256"], digest)
            self.assertEqual(replay["execution"]["result_sha256"],expected["execution"]["result_sha256"])
            self.assertEqual(o.sha256(path.read_bytes()), digest)
            refreshed = self.root/("replay_"+suffix)/"processing.gif"
            again = vm.replay(refreshed,self.root/("again_"+suffix))
            self.assertEqual(again["execution"]["result_sha256"],replay["execution"]["result_sha256"])

    def test_raster_checksum_damage_dimensions_and_program_boundaries(self):
        program = vm.compile_reconstruction(self.samples,self.metadata)
        image = vm.encode(Image.new("L",vm.SIZE),program)
        self.assertEqual(vm.decode(image),program)
        damaged = image.copy(); damaged.putpixel((0,vm.DATA_Y),127)
        with self.assertRaises(o.Rejected): vm.decode(damaged)
        changed = image.copy()
        for x in (0,1):
            for y in (vm.DATA_Y+4,vm.DATA_Y+5):
                changed.putpixel((x,y),(changed.getpixel((x,y))+1)%256)
        with self.assertRaises(o.Rejected): vm.decode(changed)
        with self.assertRaises(o.Rejected): vm.decode(image.resize((384,384)))
        with self.assertRaises(o.Rejected): vm.validate({**program,"operation":"shell"})
        with self.assertRaises(o.Rejected): vm.validate({**program,"input":{}})

    def test_reject_inconsistent_frame_programs_and_frame_limit(self):
        program = vm.compile_reconstruction(self.samples,self.metadata)
        a = vm.encode(Image.new("L",vm.SIZE),program)
        changed = copy.deepcopy(program); changed["nodes"][3]["radius"] = 8
        b = vm.encode(Image.new("L",vm.SIZE),changed)
        path = self.root/"mixed.tiff"
        a.save(path,save_all=True,append_images=[b],compression="tiff_deflate")
        with self.assertRaises(o.Rejected): vm.read(path)
        many = self.root/"many.tiff"
        a.save(many,save_all=True,append_images=[a]*32,compression="tiff_deflate")
        with self.assertRaises(o.Rejected): vm.read(many)

    def test_refuse_existing_outputs_and_input_alias(self):
        path = self.root / "source.tiff"
        o.save_hologram(path, self.samples, self.metadata)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            o.save_hologram(path, self.samples, self.metadata)
        with self.assertRaises(o.Rejected):
            o.reconstruct_file(path, path)
        with self.assertRaises(o.Rejected):
            o.reconstruct_file(path, self.root)
        self.assertEqual(before, path.read_bytes())

    def test_cli_generate_inspect_reconstruct_and_error_exit(self):
        base = Path(o.__file__).parent
        def run(*args):
            return subprocess.run([sys.executable, "-B", str(base/"run.py"), *map(str,args)],
                                  cwd=self.root, capture_output=True, text=True, timeout=30)
        result = run("generate", "--out", self.root/"cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.root/"cli/hologram_uint16.tiff"
        self.assertEqual(run("inspect", path).returncode, 0)
        self.assertEqual(run("reconstruct", path, "--out", self.root/"replay").returncode, 0)
        self.assertEqual(run("replay-carrier",self.root/"cli/processing.gif","--out",self.root/"pixel_replay").returncode,0)
        self.assertEqual(run("inspect-carrier",self.root/"cli/processing.tiff").returncode,0)
        self.assertEqual(run("edit-carrier",self.root/"cli/processing.gif","--gain","0.5","--out",self.root/"edited").returncode,0)
        bad = run("generate", "--out", self.root/"cli")
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stderr)["status"], "REJECTED")
        self.assertEqual(run("generate", "--out", self.root/"bad", "--distance", "nan").returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
