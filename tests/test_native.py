# SPDX-License-Identifier: GPL-3.0-only
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

import numpy as np
from PIL import Image
import optics as o
import raster_vm as vm

ROOT=Path(__file__).resolve().parents[1]
DOTNET=os.environ.get('DOTNET') or shutil.which('dotnet')
PLAYER=ROOT/'dotnet/OpticalPlayer/bin/Release/net10.0-windows/OpticalPlayer.dll'
HARNESS=ROOT/'dotnet/RuntimeTests/bin/Release/net10.0/RuntimeTests.dll'

def setup_native(windows=False):
    if not DOTNET or not HARNESS.exists() or (windows and (os.name!='nt' or not PLAYER.exists())):
        if os.environ.get('REQUIRE_NATIVE_TESTS')=='1': raise AssertionError('Required compiled native player/runtime unavailable')
        raise unittest.SkipTest('Build with .NET 10 and set DOTNET to run the native tests')

def run_player(*args,ok=True,cwd=ROOT,player=PLAYER):
    result=subprocess.run([DOTNET,str(player),*map(str,args)],cwd=cwd,capture_output=True,text=True,timeout=45)
    if ok and result.returncode: raise AssertionError(result.stderr or result.stdout)
    if not ok:
        if not result.returncode: raise AssertionError('Host accepted invalid image')
        return result.stderr
    return json.loads(result.stdout)

def program():
    samples,metadata,_=o.synthesize(o.target_field(),dict(o.PARAMS))
    return vm.compile_reconstruction(samples,metadata)

def raw_carrier(payload,path):
    raw=o.canonical(payload);packet=vm.MAGIC+struct.pack('>I',len(raw))+hashlib.sha256(raw).digest()+raw
    cells=np.full((192,384),255,dtype=np.uint8);cells.flat[:len(packet)]=np.frombuffer(packet,dtype=np.uint8)
    image=Image.new('L',vm.SIZE);image.paste(Image.fromarray(np.repeat(np.repeat(cells,2,0),2,1)),(0,384));image.save(path)

class NativeArrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): setup_native()
    def batch(self,programs):
        result=subprocess.run([DOTNET,str(HARNESS)],input=''.join(json.dumps(p,allow_nan=True,separators=(',',':'))+'\n' for p in programs),capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        rows=[json.loads(line) for line in result.stdout.splitlines()];self.assertEqual(len(rows),len(programs));return rows
    def compare(self,programs):
        rows=self.batch(programs)
        for p,row in zip(programs,rows):
            self.assertTrue(row['ok'],row)
            actual=np.frombuffer(base64.b64decode(row['receipt']['data']),dtype='<c16').reshape(128,128)
            expected,trace=vm.execute(p)
            np.testing.assert_allclose(actual,expected,rtol=2e-10,atol=1e-8)
            self.assertEqual([n['op'] for n in p['nodes']],[n['op'] for n in row['receipt']['trace']])
        return rows
    def test_training_and_holdout_eight_optical_cases(self):
        programs=[]
        for scene in ('training','holdout'):
            for distance in (-.02,0,.01,.02):
                samples,metadata,_=o.synthesize(o.target_field(scene),{**o.PARAMS,'distance_m':distance})
                programs.append(vm.compile_reconstruction(samples,metadata))
        self.compare(programs)
    def test_fifteen_analytic_plane_waves(self):
        programs=[];expected=[];y,x=np.mgrid[:128,:128]
        for bx,by in ((0,0),(3,0),(2,-4)):
            for distance in (-.02,-.01,0,.01,.02):
                p=program();p['input']['data']=base64.b64encode(np.ones((128,128),dtype='<u2').tobytes()).decode()
                p['nodes']=[{'id':'delta','op':'fft2','src':'sensor'},{'id':'shifted','op':'roll','src':'delta','shifts':[by,bx]},
                    {'id':'plane','op':'ifft2','src':'shifted'},{'id':'spectrum','op':'fft2','src':'plane'},
                    {'id':'transfer','op':'transfer','pitch_m':8e-6,'wavelength_m':532e-9,'distance_m':distance},
                    {'id':'product','op':'multiply','a':'spectrum','b':'transfer'},{'id':'result','op':'ifft2','src':'product'}];p['output']='result';programs.append(p)
                kz=np.sqrt((1/532e-9)**2-(bx/(128*8e-6))**2-(by/(128*8e-6))**2)
                expected.append(np.exp(2j*np.pi*(bx*x+by*y)/128)*np.exp(2j*np.pi*distance*kz))
        for result,want in zip(self.compare(programs),expected):
            actual=np.frombuffer(base64.b64decode(result['receipt']['data']),dtype='<c16').reshape(128,128)
            self.assertLess(np.max(np.abs(actual-want)),1e-8)
    def test_generic_operator_order_random_samples_and_mutations(self):
        programs=[];rng=np.random.default_rng(514)
        for i in range(20):
            p=program();p['input']['data']=base64.b64encode(rng.integers(0,100,(128,128),dtype='<u2').tobytes()).decode()
            p['nodes']=[{'id':'a','op':'scale','src':'sensor','factor':.01*(i+1)},
                {'id':'b','op':'abs2','src':'a'},{'id':'c','op':'fft2','src':'b'},
                {'id':'d','op':'roll','src':'c','shifts':[i-10,10-i]}, {'id':'e','op':'ifft2','src':'d'}];p['output']='e';programs.append(p)
        self.compare(programs)
    def test_closed_schema_rejects_hostile_operators_and_parameters(self):
        p=program();cases=[]
        for change in ({'op':'exec'},{'src':'field'},{'factor':True},{'factor':17},{'id':'sensor'},{'extra':1},{'id':'a\n'},{'factor':float('nan')}):
            v=copy.deepcopy(p);v['nodes'][0].update(change);cases.append(v)
        for index,key,value in ((2,'shifts',[0,1.0]),(2,'shifts',[0,128]),(3,'radius',-1),(6,'distance_m',.03),(6,'wavelength_m',True)):
            v=copy.deepcopy(p);v['nodes'][index][key]=value;cases.append(v)
        for shape in ([128.0,128],[True,128],[128,128,1]):
            v=copy.deepcopy(p);v['input']['shape']=shape;cases.append(v)
        for nodes in ([],p['nodes']*3):cases.append({**p,'nodes':nodes})
        self.assertTrue(all(not row['ok'] for row in self.batch(cases)))
    def test_result_growth_and_bad_sensor_rejected(self):
        p=program();p['nodes']=[{'id':'a','op':'multiply','a':'sensor','b':'sensor'},{'id':'b','op':'multiply','a':'a','b':'a'}];p['output']='b'
        v=program();v['input']['data']='!'*43692
        self.assertTrue(all(not row['ok'] for row in self.batch([p,v])))
    def test_duplicate_json_members_and_depth_rejected(self):
        p=o.canonical(program()).decode();malformed=['{"schema":"ignored",'+p[1:], '['*20+'0'+']'*20]
        result=subprocess.run([DOTNET,str(HARNESS)],input='\n'.join(malformed)+'\n',capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr);self.assertTrue(all(not json.loads(line)['ok'] for line in result.stdout.splitlines()))

class NativePlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):setup_native(True)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
        self.p=program();self.source=self.folder/'input.tiff';vm.render(self.p,np.zeros((128,128))).save(self.source)
    def test_both_formats_native_output_reference_and_reopen(self):
        before=self.source.read_bytes();out=self.folder/'result';receipt=run_player('--execute',self.source,'--out',out)
        expected,_=vm.execute(self.p);actual=np.fromfile(out/'field.complex128-le',dtype='<c16').reshape(128,128)
        np.testing.assert_allclose(actual,expected,rtol=2e-10,atol=1e-8)
        for ext in ('tiff','gif'):
            repeated=run_player('--execute',out/('processing.'+ext));self.assertEqual(repeated['result_sha256'],receipt['result_sha256'])
            payload,_,_=vm.read_envelope(out/('processing.'+ext));self.assertEqual(payload['runtime'],vm.runtime_payload())
        self.assertEqual(before,self.source.read_bytes())
    def test_gain_edit_changes_pixel_program_not_runtime_and_exactly_halves_field(self):
        a=self.folder/'a';b=self.folder/'b';run_player('--execute',self.source,'--out',a);run_player('--execute',self.source,'--gain','.5','--out',b)
        original=np.fromfile(a/'field.complex128-le',dtype='<c16');changed=np.fromfile(b/'field.complex128-le',dtype='<c16');np.testing.assert_array_equal(changed,original*.5)
        payload,_,_=vm.read_envelope(b/'processing.gif');self.assertEqual(payload['runtime'],vm.runtime_payload());self.assertEqual(vm.validate_envelope(payload)['nodes'][-1]['factor'],.5)
    def test_gui_autostart_edit_refresh_save_and_reopen(self):
        report=run_player('--smoke-ui',self.source,'--export-dir',self.folder/'ui');self.assertTrue(report['ui_constructed']);self.assertTrue(report['program_edit_changed_output']);self.assertTrue(report['runtime_preserved']);self.assertEqual(report['pixel_refreshes'],3)
        self.assertEqual(run_player('--execute',self.folder/'ui/processing.gif')['result_sha256'],report['result_sha256'])
    def test_default_beside_executable_and_gif_fallback(self):
        isolated=self.folder/'host';isolated.mkdir()
        for file in PLAYER.parent.iterdir():
            if file.name.startswith('OpticalPlayer.') and file.suffix in ('.dll','.json'):shutil.copyfile(file,isolated/file.name)
        with Image.open(self.source) as image:image.convert('L').convert('P').save(isolated/'game.gif',optimize=False)
        report=run_player('--smoke-ui',player=isolated/PLAYER.name,cwd=self.folder);self.assertTrue(report['automatic_computation'])
    def test_unapproved_corrupt_and_expansion_runtime_rejected(self):
        import gzip
        payload=vm.envelope(self.p);variants=[]
        for key,value in (('sha256','0'*64),('data','!!!!'),('data',base64.b64encode(gzip.compress(b'not an approved assembly')).decode()),('data',base64.b64encode(gzip.compress(bytes(131073))).decode())):
            p=copy.deepcopy(payload);p['runtime'][key]=value;variants.append(p)
        for i,p in enumerate(variants):
            path=self.folder/f'bad{i}.tiff';raw_carrier(p,path);run_player('--execute',path,ok=False)
    def test_unknown_instruction_and_duplicates_cannot_reach_display(self):
        for i,raw in enumerate((o.canonical({**self.p,'nodes':[{'id':'x','op':'exec'}]}).decode(),'{"schema":"bad",'+o.canonical(self.p).decode()[1:])):
            payload=vm.envelope(self.p);payload['program_json']=raw;path=self.folder/f'bad{i}.tiff';raw_carrier(payload,path);run_player('--execute',path,ok=False)
    def test_preflight_malformed_cycles_dimensions_truncation_and_frames(self):
        for i,data in enumerate((b'II*\0'+struct.pack('<I',8)+b'\1\0'+bytes(12)+struct.pack('<I',8), b'GIF89a'+bytes(7),self.source.read_bytes()[:100])):
            path=self.folder/f'bad{i}.tiff';path.write_bytes(data);run_player('--execute',path,ok=False)
        image=vm.render(self.p,np.zeros((128,128)));path=self.folder/'many.tiff';image.save(path,save_all=True,append_images=[image]*32);run_player('--execute',path,ok=False)
    def test_existing_output_and_bad_cli_leave_source_unchanged(self):
        before=self.source.read_bytes();run_player('--execute',self.source,'--out',self.folder,ok=False);run_player('--execute',self.source,'--gain','NaN',ok=False)
        run_player('--execute',self.source,'--gain','1','--gain','2',ok=False);self.assertEqual(before,self.source.read_bytes())

if __name__=='__main__':unittest.main(verbosity=2)
