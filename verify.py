# SPDX-License-Identifier: GPL-3.0-only
"""Re-run project tests and replace the three explicitly named evidence reports."""
import io
import base64
import json
import os
from pathlib import Path
import platform
import time
import tempfile
import shutil
import subprocess
import unittest

import numpy as np
import PIL
from PIL import Image
import optics as o
import raster_vm as vm


def main():
    root = Path(__file__).resolve().parent
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    capture = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    start = time.perf_counter()
    result = unittest.TextTestRunner(stream=capture, verbosity=2).run(suite)
    summary = {"schema": "photon-pretzel/test-evidence/1", "version": o.VERSION,
               "command": "python -B verify.py", "tests": result.testsRun,
               "failures": len(result.failures), "errors": len(result.errors),
               "skips": len(result.skipped), "passed": result.wasSuccessful() and not result.skipped,
               "python": platform.python_version(), "platform": platform.system(),
               "numpy": np.__version__, "pillow": PIL.__version__,
               "elapsed_seconds": round(time.perf_counter()-start, 4),
               "scope": "Local software, numerical model, reader and CLI tests; no physical or full-workstream qualification",
               "source_sha256": {name: o.sha256((root/name).read_bytes())
                                  for name in ("optics.py", "raster_vm.py", "run.py", "tests/test_optics.py",
                                               "tests/test_raster_vm.py", "tests/test_native.py", "tests/test_package_release.py",
                                               "dotnet/ArrayRuntime/Machine.cs", "dotnet/OpticalPlayer/Carrier.cs",
                                               "dotnet/OpticalPlayer/Program.cs", "dotnet/OpticalPlayer/ImagePreflight.cs",
                                               "package_release.py", "verify.py")}}
    # Successful evidence contains relative source identities and no personal machine paths.
    log = capture.getvalue().replace(str(root), "<repository>")
    (evidence/"test_log.txt").write_text(log, encoding="utf-8")
    (evidence/"test_results.json").write_text(json.dumps(summary, indent=2)+"\n", encoding="utf-8")
    if not summary["passed"]:
        print(json.dumps(summary, indent=2))
        return 1
    metrics = []
    native_programs = []
    for scene in ("training", "holdout"):
        for z in (-.02, 0, .01, .02):
            params = {**o.PARAMS, "distance_m": z}
            target = o.target_field(scene)
            samples, metadata, _ = o.synthesize(target, params)
            program = vm.compile_reconstruction(samples,metadata)
            native_programs.append(program)
            recovered,_ = vm.execute(vm.decode(vm.encode(Image.new("L",vm.SIZE),program)))
            metrics.append({"scene": scene, "distance_m": z,
                            "relative_complex_l2_error": float(np.linalg.norm(recovered-target)/np.linalg.norm(target))})
    report = {"schema": "photon-pretzel/numerical-evidence/1", "version": o.VERSION,
              "acceptance_frozen_in_source": o.ACCEPTANCE, "reconstruction_cases": metrics,
              "all_reconstruction_cases_pass": all(r["relative_complex_l2_error"] <= .001 for r in metrics),
              "analytic_reference": "15 plane-wave cases: bins (0,0),(3,0),(2,-4), distances -0.02,-0.01,0,0.01,0.02 m",
              "physical_measurements": 0, "scope": "Discrete periodic scalar Fourier model"}
    (evidence/"numerical_results.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    dotnet=os.environ.get("DOTNET") or shutil.which("dotnet")
    native=subprocess.run([dotnet,str(root/"dotnet/RuntimeTests/bin/Release/net10.0/RuntimeTests.dll")],
        input="".join(o.canonical(p).decode("ascii")+"\n" for p in native_programs),
        capture_output=True,text=True,timeout=60,check=True)
    rows=[json.loads(line) for line in native.stdout.splitlines()]
    if len(rows)!=len(native_programs):raise ValueError("Native evidence result count differs")
    cases=[]
    for p,row,metric in zip(native_programs,rows,metrics):
        if not row["ok"]:raise ValueError("Native reference calculation failed")
        calculated=np.frombuffer(base64.b64decode(row["receipt"]["data"]),dtype="<c16").reshape(128,128)
        reference,_=vm.execute(p)
        np.testing.assert_allclose(calculated,reference,rtol=2e-10,atol=1e-8)
        cases.append({"scene":metric["scene"],"distance_m":metric["distance_m"],
                      "max_abs_error_against_numpy":float(np.max(np.abs(calculated-reference))),
                      "native_result_sha256":row["receipt"]["sha256"]})
    native_report={"version":o.VERSION,"module_sha256":vm.runtime_payload()["sha256"],
                   "comparison":"C# radix-2 array machine against independent NumPy FFT interpreter",
                   "tolerance":{"rtol":2e-10,"atol":1e-8},"cases":cases,"all_pass":True,
                   "additional_test_suites":"15 analytic plane-wave cases and 20 random permitted programs in tests/test_native.py"}
    (evidence/"native_numerical_results.json").write_text(json.dumps(native_report,indent=2)+"\n",encoding="utf-8")
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        generated = o.generate(folder/"generated")
        cases = []
        for extension in ("gif","tiff"):
            source = folder/"generated"/("processing."+extension)
            replay = vm.replay(source,folder/("replay_"+extension))
            edit = vm.edit_gain(source,folder/("edited_"+extension),.5)
            edited_again = vm.replay(folder/("edited_"+extension)/("processing."+extension),folder/("edited_replay_"+extension))
            cases.append({"format":extension,"frames_checked":replay["source_frames_checked"],
                          "original_result_sha256":generated["execution"]["result_sha256"],
                          "replayed_result_sha256":replay["execution"]["result_sha256"],
                          "edited_result_sha256":edit["execution"]["result_sha256"],
                          "edited_replay_sha256":edited_again["execution"]["result_sha256"],
                          "output_changed":edit["output_changed"],"runtime_unchanged":edit["runtime_unchanged"]})
    processable = {"schema":"photon-pretzel/executable-evidence/1","version":o.VERSION,
                  "source":"exact uint16 sensor samples and operator DAG decoded from pixel cells",
                  "machine":"host CPU bounded straight-line array interpreter", "cases":cases,
                  "edit":"append scale(output,0.5), same interpreter; field halves, intensity quarters",
                  "all_pass":all(c["original_result_sha256"] == c["replayed_result_sha256"]
                                 and c["edited_result_sha256"] == c["edited_replay_sha256"]
                                 and c["output_changed"] and c["runtime_unchanged"] for c in cases)}
    (evidence/"executable_results.json").write_text(json.dumps(processable,indent=2)+"\n",encoding="utf-8")
    if not processable["all_pass"]:
        print(json.dumps(processable,indent=2))
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
