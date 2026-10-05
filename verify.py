# SPDX-License-Identifier: GPL-3.0-only
"""Re-run project tests and replace the three explicitly named evidence reports."""
import io
import json
from pathlib import Path
import platform
import time
import tempfile
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
                                               "tests/test_raster_vm.py", "verify.py")}}
    # Successful evidence contains relative source identities and no personal machine paths.
    log = capture.getvalue().replace(str(root), "<repository>")
    (evidence/"test_log.txt").write_text(log, encoding="utf-8")
    (evidence/"test_results.json").write_text(json.dumps(summary, indent=2)+"\n", encoding="utf-8")
    if not summary["passed"]:
        print(json.dumps(summary, indent=2))
        return 1
    metrics = []
    for scene in ("training", "holdout"):
        for z in (-.02, 0, .01, .02):
            params = {**o.PARAMS, "distance_m": z}
            target = o.target_field(scene)
            samples, metadata, _ = o.synthesize(target, params)
            program = vm.compile_reconstruction(samples,metadata)
            recovered,_ = vm.execute(vm.decode(vm.encode(Image.new("L",vm.SIZE),program)))
            metrics.append({"scene": scene, "distance_m": z,
                            "relative_complex_l2_error": float(np.linalg.norm(recovered-target)/np.linalg.norm(target))})
    report = {"schema": "photon-pretzel/numerical-evidence/1", "version": o.VERSION,
              "acceptance_frozen_in_source": o.ACCEPTANCE, "reconstruction_cases": metrics,
              "all_reconstruction_cases_pass": all(r["relative_complex_l2_error"] <= .001 for r in metrics),
              "analytic_reference": "15 plane-wave cases: bins (0,0),(3,0),(2,-4), distances -0.02,-0.01,0,0.01,0.02 m",
              "physical_measurements": 0, "scope": "Discrete periodic scalar Fourier model"}
    (evidence/"numerical_results.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
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
