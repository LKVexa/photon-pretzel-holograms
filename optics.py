# SPDX-License-Identifier: GPL-3.0-only
"""Bounded off-axis holography on a discrete periodic computational grid."""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import stat
import warnings

import numpy as np
from PIL import Image, TiffImagePlugin

VERSION = "0.4.0"
PROFILE = "photon-pretzel/off-axis/1"
MEANING = "intensity = stored_uint16 * scale / 65535"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 2048
MAX_SCALE = 16.0
PARAMS = {"size": 128, "pitch_m": 8e-6, "wavelength_m": 532e-9,
          "distance_m": .01, "carrier_bin": 40, "band_bins": 10}
ACCEPTANCE = {"relative_complex_l2_max": .001, "plane_wave_max_abs": 1e-8,
              "zero_distance_max_abs": 1e-12}


class Rejected(ValueError):
    """Input or result is outside the declared research profile."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def finite_number(value):
    return type(value) in (int, float) and abs(value) <= 1e100 and math.isfinite(value)


def validate_params(params):
    if type(params) is not dict or set(params) != set(PARAMS):
        raise Rejected("Unexpected optical parameter fields")
    for key in ("size", "carrier_bin", "band_bins"):
        if type(params[key]) is not int or params[key] != PARAMS[key]:
            raise Rejected("Grid and frequency bins must be the exact profile integers")
    if not all(finite_number(value) for value in params.values()):
        raise Rejected("Optical parameters must be finite numbers")
    if (params["pitch_m"] != PARAMS["pitch_m"] or
            params["wavelength_m"] != PARAMS["wavelength_m"] or
            abs(params["distance_m"]) > .02):
        raise Rejected("Outside the fixed optical profile")
    return dict(params)


def validate_samples(samples, scale, params):
    validate_params(params)
    if (not isinstance(samples, np.ndarray) or samples.shape != (128, 128) or
            samples.dtype.kind != "u" or samples.dtype.itemsize != 2):
        raise Rejected("Samples must be a 128 by 128 unsigned 16-bit array")
    if not finite_number(scale) or not 0 < scale <= MAX_SCALE:
        raise Rejected("Intensity scale must be finite and in (0, 16]")


def validate_metadata(metadata):
    fields = {"profile", "scale", "params", "meaning", "physical_measurement"}
    if type(metadata) is not dict or set(metadata) != fields:
        raise Rejected("Unexpected TIFF metadata fields")
    if (metadata["profile"] != PROFILE or metadata["meaning"] != MEANING or
            metadata["physical_measurement"] is not False):
        raise Rejected("Unsupported profile or physical-measurement claim")
    validate_params(metadata["params"])
    if not finite_number(metadata["scale"]) or not 0 < metadata["scale"] <= MAX_SCALE:
        raise Rejected("Intensity scale must be finite and in (0, 16]")
    return metadata


def parse_metadata(text):
    if type(text) is not str:
        raise Rejected("TIFF ImageDescription must contain the profile JSON text")
    try:
        raw = text.encode("ascii")
    except UnicodeError as exc:
        raise Rejected("Metadata must be ASCII JSON") from exc
    if len(raw) > MAX_METADATA_BYTES:
        raise Rejected("TIFF metadata size limit exceeded")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Rejected("Duplicate metadata key")
            result[key] = value
        return result

    def no_constants(value):
        raise Rejected("Nonfinite JSON constants are forbidden")
    try:
        metadata = json.loads(raw, object_pairs_hook=unique, parse_constant=no_constants)
    except (ValueError, TypeError, RecursionError) as exc:
        raise Rejected("Malformed or excessive TIFF metadata") from exc
    return validate_metadata(metadata)


def read_snapshot(path):
    """Read one bounded immutable byte snapshot."""
    path = Path(path)
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise Rejected("Input must be a regular file, not a link or device")
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise Rejected("Cannot read input image") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise Rejected("Image file size limit exceeded")
    return raw


def read_hologram(path):
    """Validate a numerical TIFF before decoding its samples."""
    raw = read_snapshot(path)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            warnings.simplefilter("error", UserWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != "TIFF" or image.size != (128, 128):
                    raise Rejected("Expected a 128 by 128 TIFF")
                # Probe just the second plane; n_frames would walk an entire hostile IFD chain.
                try:
                    image.seek(1)
                except EOFError:
                    image.seek(0)
                else:
                    raise Rejected("Expected one TIFF plane")
                if image.mode not in ("I;16", "I;16L", "I;16B", "I;16N"):
                    raise Rejected("Expected unsigned 16-bit grayscale TIFF")
                metadata = parse_metadata(image.tag_v2.get(270))
                samples = np.asarray(image).copy()
                validate_samples(samples, metadata["scale"], metadata["params"])
    except Rejected:
        raise
    except (OSError, ValueError, TypeError, SyntaxError, EOFError, OverflowError,
            Image.DecompressionBombError, Image.DecompressionBombWarning, UserWarning) as exc:
        raise Rejected("Malformed or unsupported TIFF") from exc
    return samples, metadata, sha256(raw)


def propagate(field, params, distance=None):
    params = validate_params(params)
    z = params["distance_m"] if distance is None else distance
    if not finite_number(z) or abs(z) > .02:
        raise Rejected("Propagation distance must be finite and within 20 mm")
    if (not isinstance(field, np.ndarray) or field.shape != (128, 128) or
            field.dtype.kind not in "fci" or not np.isfinite(field).all()):
        raise Rejected("Expected a finite bounded 128 by 128 real or complex field")
    field = field.astype(np.complex128)
    if np.max(np.abs(field), initial=0) > 16:
        raise Rejected("Field amplitude exceeds the profile bound")
    if z == 0:
        return field.copy()
    f = np.fft.fftfreq(128, d=params["pitch_m"])
    fx, fy = np.meshgrid(f, f)
    square = (1 / params["wavelength_m"]) ** 2 - fx ** 2 - fy ** 2
    admitted = square >= 0
    transfer = np.zeros_like(square, dtype=np.complex128)
    transfer[admitted] = np.exp(2j * np.pi * z * np.sqrt(square[admitted]))
    result = np.fft.ifft2(np.fft.fft2(field) * transfer)
    if not np.isfinite(result).all():
        raise Rejected("Propagation produced a nonfinite field")
    return result


def support_mask():
    f = np.fft.fftfreq(128) * 128
    fx, fy = np.meshgrid(f, f)
    return fx ** 2 + fy ** 2 <= 10 ** 2


def target_field(variant="training"):
    if variant not in ("training", "holdout"):
        raise Rejected("Unknown scene variant")
    yy, xx = np.mgrid[:128, :128]
    points = [(42, 44), (84, 44), (64, 82)] if variant == "training" else [(36, 70), (84, 60)]
    amplitude = sum(np.exp(-((xx-x)**2 + (yy-y)**2) / 50) for x, y in points)
    field = np.fft.ifft2(np.fft.fft2(amplitude) * support_mask())
    return field / np.max(np.abs(field))


def synthesize(target, params):
    sensor = propagate(target, params)
    reference = np.exp(2j * np.pi * params["carrier_bin"] * np.arange(128)[None, :] / 128)
    intensity = np.abs(sensor + reference) ** 2
    scale = float(intensity.max())
    samples = np.round(intensity / scale * 65535).astype(np.uint16)
    validate_samples(samples, scale, params)
    metadata = {"profile": PROFILE, "scale": scale, "params": dict(params),
                "meaning": MEANING, "physical_measurement": False}
    return samples, metadata, sensor


def demodulate(samples, metadata):
    metadata = validate_metadata(metadata)
    params, scale = metadata["params"], metadata["scale"]
    validate_samples(samples, scale, params)
    intensity = samples.astype(np.float64) * (scale / 65535)
    sideband = np.roll(np.fft.fft2(intensity), params["carrier_bin"], axis=1) * support_mask()
    return np.fft.ifft2(sideband)


def reconstruct(samples, metadata):
    return propagate(demodulate(samples, metadata), metadata["params"], -metadata["params"]["distance_m"])


def save_hologram(path, samples, metadata):
    validate_metadata(metadata)
    validate_samples(samples, metadata["scale"], metadata["params"])
    tags = TiffImagePlugin.ImageFileDirectory_v2()
    tags[270] = canonical(metadata).decode("ascii")
    # Create-only output protects existing files, including caller-selected input paths.
    with Path(path).open("xb") as stream:
        Image.fromarray(samples.astype(np.uint16)).save(stream, format="TIFF", tiffinfo=tags,
                                                      compression="tiff_deflate")


def gray(values):
    values = np.asarray(values, dtype=np.float64)
    if values.shape != (128, 128) or not np.isfinite(values).all():
        raise Rejected("Invalid display field")
    span = float(values.max() - values.min())
    scaled = (values - values.min()) / max(span, 1e-15)
    return Image.fromarray(np.round(scaled * 255).astype(np.uint8)).convert("RGB")


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def create_output(path):
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise Rejected("Output directory already exists; choose a new directory")
    path.mkdir(parents=True, exist_ok=False)
    return path


def simulation_contract(params):
    return {"version": VERSION, "profile": PROFILE, "params": validate_params(params),
            "harmonic_convention": "exp(-i*omega*t)", "medium_index": 1,
            "propagator": "exp(+i*2*pi*z*sqrt(1/lambda^2-fx^2-fy^2))",
            "fft": "forward unnormalized; inverse 1/N^2; unshifted order",
            "axes": "array[y,x]; metres; target z=0; sensor z=distance_m",
            "boundary": "periodic finite grid; no padding or crop; no device aperture",
            "spectral_support": "object radius10 bins; reference +40 x-bin; selected sideband -40",
            "evanescent": "discard for nonzero z; exact identity at z=0; not present in this fixed pitch/wavelength envelope",
            "quantization": "uint16 intensity; nearest ties-to-even; no clipping",
            "acceptance": dict(ACCEPTANCE), "simulation_only": True, "device_ready": False,
            "scope": "Discrete periodic scalar model only; not continuous free-space or device qualification"}


def generate(output, variant="training", distance=.01):
    import raster_vm as vm
    params = {**PARAMS, "distance_m": distance}
    validate_params(params)
    target = target_field(variant)
    samples, metadata, _ = synthesize(target, params)
    program = vm.compile_reconstruction(samples, metadata)
    recovered, _ = vm.execute(program)
    error = float(np.linalg.norm(recovered-target) / np.linalg.norm(target))
    if not math.isfinite(error) or error > ACCEPTANCE["relative_complex_l2_max"]:
        raise Rejected("Reconstruction misses the frozen numerical threshold")
    folder = create_output(output)
    save_hologram(folder / "hologram_uint16.tiff", samples, metadata)
    reread, rebound, source_hash = read_hologram(folder / "hologram_uint16.tiff")
    if not np.array_equal(samples, reread) or rebound != metadata:
        raise Rejected("TIFF sample or metadata roundtrip changed")
    program = vm.compile_reconstruction(reread, rebound)
    recovered, execution = vm.write_results(folder, program)
    gray(np.abs(target) ** 2).save(folder / "target_intensity.png")
    write_json(folder / "optical_profile.json", simulation_contract(params))
    report = {"version": VERSION, "operation": "generate", "scene": variant,
              "hologram_file": "hologram_uint16.tiff", "hologram_sha256": source_hash,
              "relative_complex_l2_error": error, "threshold": .001,
              "sample_roundtrip_exact": True, "simulation_only": True, "execution": execution}
    write_json(folder / "receipt.json", report)
    return report


def reconstruct_file(source, output):
    import raster_vm as vm
    samples, metadata, source_hash = read_hologram(source)
    program = vm.compile_reconstruction(samples, metadata)
    vm.execute(program)
    folder = create_output(output)
    _, execution = vm.write_results(folder, program)
    report = {"version": VERSION, "operation": "reconstruct", "source_sha256": source_hash,
              "profile": metadata, "simulation_only": True, "execution": execution,
              "quality_against_unknown_target": "NOT_ASSESSED", "source_file_preserved": True}
    write_json(folder / "receipt.json", report)
    return report


def read_carrier(source):
    import raster_vm as vm
    return vm.read(source)


def replay_carrier(source, output):
    import raster_vm as vm
    return vm.replay(source, output)

