# SPDX-License-Identifier: GPL-3.0-only
import base64
import copy
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

import optics as o
import raster_vm as vm


class ArrayProgramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.samples,self.metadata,_ = o.synthesize(o.target_field(),dict(o.PARAMS))
        self.program = vm.compile_reconstruction(self.samples,self.metadata)

    def test_program_is_entire_computation_no_scene_or_hidden_reconstructor(self):
        expected = o.reconstruct(self.samples,self.metadata)
        with patch.object(o,"target_field",side_effect=AssertionError("hidden scene")), \
             patch.object(o,"reconstruct",side_effect=AssertionError("hidden routine")), \
             patch.object(o,"demodulate",side_effect=AssertionError("hidden demodulator")), \
             patch.object(o,"propagate",side_effect=AssertionError("hidden pipeline")), \
             patch.object(vm,"compile_reconstruction",side_effect=AssertionError("hidden compiler")):
            recovered = vm.decode(vm.encode(Image.new("L",vm.SIZE),self.program))
            actual,trace = vm.execute(recovered)
        np.testing.assert_allclose(actual,expected,rtol=0,atol=1e-15)
        self.assertEqual([n["op"] for n in self.program["nodes"]],[t["op"] for t in trace])

    def test_all_numerical_scenes_and_distances_use_image_program(self):
        for scene in ("training","holdout"):
            for distance in (-.02,0,.01,.02):
                target = o.target_field(scene)
                samples,metadata,_ = o.synthesize(target,{**o.PARAMS,"distance_m":distance})
                program = vm.compile_reconstruction(samples,metadata)
                recovered,_ = vm.execute(vm.decode(vm.encode(Image.new("L",vm.SIZE),program)))
                self.assertLess(np.linalg.norm(recovered-target)/np.linalg.norm(target),.001)

    def test_vm_transfer_matches_analytic_plane_waves_both_signs_and_zero(self):
        y,x = np.mgrid[:128,:128]
        for bx,by in ((0,0),(3,0),(2,-4)):
            plane = np.exp(2j*np.pi*(bx*x+by*y)/128)
            kz = np.sqrt((1/o.PARAMS["wavelength_m"])**2 -
                         (bx/(128*o.PARAMS["pitch_m"]))**2 - (by/(128*o.PARAMS["pitch_m"]))**2)
            for distance in (-.02,-.01,0,.01,.02):
                program = copy.deepcopy(self.program)
                program["input"]["data"] = base64.b64encode(np.ones((128,128),dtype="<u2").tobytes()).decode("ascii")
                program["nodes"] = [
                    {"id":"delta","op":"fft2","src":"sensor"},
                    {"id":"shifted","op":"roll","src":"delta","shifts":[by,bx]},
                    {"id":"plane","op":"ifft2","src":"shifted"},
                    {"id":"spectrum","op":"fft2","src":"plane"},
                    {"id":"transfer","op":"transfer","pitch_m":8e-6,"wavelength_m":532e-9,"distance_m":distance},
                    {"id":"product","op":"multiply","a":"spectrum","b":"transfer"},
                    {"id":"result","op":"ifft2","src":"product"}]
                program["output"] = "result"
                actual,_ = vm.execute(program)
                expected = plane*np.exp(2j*np.pi*distance*kz)
                self.assertLess(np.max(np.abs(actual-expected)),1e-8)

    def test_arbitrary_allowed_program_executes_order_without_optical_dispatch(self):
        program = copy.deepcopy(self.program)
        program["nodes"] = [{"id":"scaled","op":"scale","src":"sensor","factor":.25},
                            {"id":"squared","op":"abs2","src":"scaled"}]
        program["output"] = "squared"
        actual,_ = vm.execute(vm.decode(vm.encode(Image.new("L",vm.SIZE),program)))
        np.testing.assert_array_equal(actual,(self.samples.astype(float)*.25)**2)
        changed = copy.deepcopy(program)
        changed["nodes"] = [{"id":"squared","op":"abs2","src":"sensor"},
                            {"id":"scaled","op":"scale","src":"squared","factor":.25}]
        changed["output"] = "scaled"
        modified,_ = vm.execute(changed)
        np.testing.assert_array_equal(modified,actual*4)

    def test_edit_encoded_instruction_changes_output_with_runtime_unchanged(self):
        baseline,_ = vm.execute(self.program)
        source_dir = o.create_output(self.root/"source")
        vm.write_results(source_dir,self.program)
        runtime_before = o.sha256(Path(vm.__file__).read_bytes())
        for suffix in ("gif","tiff"):
            source = source_dir/("processing."+suffix)
            report = vm.edit_gain(source,self.root/("edited_"+suffix),.5)
            self.assertTrue(report["output_changed"])
            self.assertTrue(report["runtime_unchanged"])
            for export in ("gif","tiff"):
                program,_,_ = vm.read(self.root/("edited_"+suffix)/("processing."+export))
                actual,_ = vm.execute(program)
                np.testing.assert_array_equal(actual,baseline*.5)
            with Image.open(source) as before, Image.open(self.root/("edited_"+suffix)/"processing.gif") as after:
                self.assertFalse(np.array_equal(np.asarray(before.convert("L"))[58:314,448:704],
                                                np.asarray(after.convert("L"))[58:314,448:704]))
        self.assertEqual(runtime_before,o.sha256(Path(vm.__file__).read_bytes()))

    def test_encoded_input_changes_output_and_no_external_files_are_needed(self):
        changed = copy.deepcopy(self.program)
        changed["input"]["data"] = base64.b64encode(bytes(32768)).decode("ascii")
        program = vm.decode(vm.encode(Image.new("L",vm.SIZE),changed))
        actual,_ = vm.execute(program)
        np.testing.assert_array_equal(actual,np.zeros((128,128)))

    def test_mask_and_propagation_edits_affect_result(self):
        original,_ = vm.execute(self.program)
        for index,key,value in ((3,"radius",2),(6,"distance_m",0)):
            changed = copy.deepcopy(self.program)
            changed["nodes"][index][key] = value
            actual,_ = vm.execute(vm.decode(vm.encode(Image.new("L",vm.SIZE),changed)))
            self.assertGreater(np.linalg.norm(actual-original),.01)

    def test_reject_unknown_ops_fields_forward_refs_duplicate_ids_and_bad_output(self):
        variants = []
        for change in ({"op":"exec"},{"path":"secret"},{"src":"field"},{"id":"sensor"},
                       {"id":"x"*25},{"factor":float("nan")},{"factor":True},{"factor":17}):
            changed = copy.deepcopy(self.program); changed["nodes"][0].update(change); variants.append(changed)
        for output in ("missing","sensor",False,[]):
            changed = copy.deepcopy(self.program); changed["output"] = output; variants.append(changed)
        for changed in variants:
            with self.subTest(program=changed["output"]),self.assertRaises(o.Rejected): vm.validate(changed)

    def test_reject_budget_shape_input_and_scalar_type_violations(self):
        variants = []
        for nodes in ([],self.program["nodes"]*3,"bad"):
            variants.append({**self.program,"nodes":nodes})
        for value in ({}, {**self.program["input"],"shape":[128.0,128]},
                      {**self.program["input"],"data":"/"*43692},
                      {**self.program["input"],"data":"!"*43692}):
            variants.append({**self.program,"input":value})
        for index,key,value in ((2,"shifts",[0,40.0]),(2,"shifts",[0,True]),(2,"shifts",[0,128]),
                                 (3,"radius",33),(3,"radius",-1),(3,"radius",float("inf")),
                                 (6,"distance_m",.03),(6,"pitch_m",1e-5),(6,"wavelength_m",True)):
            changed = copy.deepcopy(self.program); changed["nodes"][index][key] = value; variants.append(changed)
        for changed in variants:
            with self.assertRaises(o.Rejected): vm.validate(changed)

    def test_reject_arithmetic_growth_before_output_creation(self):
        program = copy.deepcopy(self.program)
        program["nodes"] = [{"id":"one","op":"multiply","a":"sensor","b":"sensor"},
                            {"id":"two","op":"multiply","a":"one","b":"one"}]
        program["output"] = "two"
        image = vm.encode(Image.new("L",vm.SIZE),program)
        source = self.root/"huge.tiff"; image.save(source)
        with self.assertRaises(o.Rejected): vm.replay(source,self.root/"must_not_exist")
        self.assertFalse((self.root/"must_not_exist").exists())

    def test_reject_packet_header_length_noncanonical_and_deep_payloads(self):
        def packet_image(raw,length=None):
            packet = vm.MAGIC+struct.pack(">I",len(raw) if length is None else length)+hashlib.sha256(raw).digest()+raw
            values = np.full((384,768),255,dtype=np.uint8)
            cells = values[::2,::2].copy(); cells.flat[:len(packet)] = np.frombuffer(packet,dtype=np.uint8)
            values = np.repeat(np.repeat(cells,2,0),2,1)
            image = Image.new("L",vm.SIZE); image.paste(Image.fromarray(values),(0,vm.DATA_Y)); return image
        for image in (packet_image(b"{}",vm.MAX_PACKET+1),packet_image(b"{}",0),
                      packet_image(o.canonical(self.program)+b" "),packet_image(b"["*1100+b"0"+b"]"*1100),
                      packet_image(b'{"a":NaN}')):
            with self.assertRaises(o.Rejected): vm.decode(image)

    def test_replay_refuses_existing_outputs_and_leaves_source_unchanged(self):
        folder = o.create_output(self.root/"source")
        vm.write_results(folder,self.program)
        source = folder/"processing.gif"; before = source.read_bytes()
        for output in (source,folder,self.root):
            with self.assertRaises(o.Rejected): vm.replay(source,output)
        self.assertEqual(source.read_bytes(),before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
