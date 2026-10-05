# Changelog

## 0.3.0

- Replaced the scene-selector job with an executable operator graph and exact uint16 sensor samples carried entirely in TIFF/GIF pixel cells.
- Added a bounded array interpreter for scale, FFT/IFFT, roll, disk mask, multiply, transfer function and absolute square. Each decoded instruction specifies its operands, order and parameters.
- Added repeated execution and image refresh: exported results remain executable with the original embedded program and samples.
- Added `inspect-carrier` and `edit-carrier --gain` to inspect instructions, append an encoded scale operation, and demonstrate changed numerical and visible output with the same runtime.
- Added execution traces, program/result hashes, instruction/sample tamper rejection, resource limits, program-edit and arbitrary permitted graph tests.
- Upgraded every newly generated or reconstructed output to executable v0.3 carriers. v0.2 jobs are retained as historical files and rejected by the v0.3 executable loader.
- Preserved the frozen physical model and numerical acceptance thresholds; no physical self-execution or optical-hardware claim.

## 0.2.0

- Extracted the optical demonstration into a standalone project with `generate`, `inspect`, `reconstruct`, and `replay-carrier` commands.
- Preserved processable TIFF/GIF previews with bounded checksum-verified pixel jobs in every frame; replay regenerates the same numerical simulation on the host CPU.
- Added strict unsigned 16-bit sample validation, finite bounded fields and scale, exact integer frequency bins, closed metadata fields and duplicate-key rejection.
- Added bounded single-snapshot TIFF loading, a second-plane probe, file and metadata limits, and create-only output directories and TIFF writes.
- Fixed a minimum-signed-integer absolute-value overflow that could bypass a field-amplitude bound.
- Ensured both numerical reconstruction and GIF views use the quantized TIFF data path.
- Added analytic plane-wave, held-out reconstruction, input-corruption, CLI, endianness, per-frame carrier recovery, replay and source-preservation tests.
- Added relative-path evidence, generated reference assets and GPL-3.0-only licensing for the whole project.

## 0.1.0

- Initial optical kernel within the combined TIFF/GIF Compute Lab research demonstration.
