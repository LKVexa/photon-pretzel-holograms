# SPDX-License-Identifier: GPL-3.0-only
"""A bounded straight-line array machine; its program and input live in image pixels."""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import warnings

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import optics as o

SCHEMA = "photon-pretzel/array-program/1"
MAGIC = b"PPHVM003"
SIZE = (768, 768)
DATA_Y, CELL = 384, 2
MAX_PACKET = 56000
MAX_NODES, MAX_FRAMES = 24, 32
MAX_MAGNITUDE = 1e12
OP_FIELDS = {
    "scale": {"src", "factor"}, "fft2": {"src"}, "ifft2": {"src"},
    "roll": {"src", "shifts"}, "disk": {"radius"},
    "multiply": {"a", "b"}, "abs2": {"src"},
    "transfer": {"pitch_m", "wavelength_m", "distance_m"},
}


def reject(message):
    raise o.Rejected(message)


def number(value, limit):
    return o.finite_number(value) and abs(value) <= limit


def validate(program):
    if (type(program) is not dict or set(program) != {"schema", "input", "nodes", "output"}
            or program["schema"] != SCHEMA):
        reject("Unsupported array program schema")
    data = program["input"]
    if (type(data) is not dict or set(data) != {"encoding", "shape", "data"}
            or data["encoding"] != "uint16-le/base64"
            or type(data["shape"]) is not list or data["shape"] != [128, 128]
            or any(type(n) is not int for n in data["shape"])
            or type(data["data"]) is not str or len(data["data"]) != 43692):
        reject("Expected exact 128 by 128 embedded uint16 input")
    try:
        raw = base64.b64decode(data["data"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise o.Rejected("Malformed sensor sample encoding") from exc
    if len(raw) != 32768 or base64.b64encode(raw).decode("ascii") != data["data"]:
        reject("Noncanonical or incorrectly sized sensor samples")
    nodes = program["nodes"]
    if type(nodes) is not list or not 1 <= len(nodes) <= MAX_NODES:
        reject("Program requires 1 to 24 straight-line instructions")
    known = {"sensor"}
    for node in nodes:
        if type(node) is not dict or type(node.get("op")) is not str or node["op"] not in OP_FIELDS:
            reject("Unsupported array instruction")
        op = node["op"]
        if set(node) != {"id", "op"} | OP_FIELDS[op]:
            reject("Unknown or missing instruction fields")
        name = node["id"]
        if type(name) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name) or name in known:
            reject("Instruction identifiers must be unique bounded names")
        for ref in ("src", "a", "b"):
            if ref in node and (type(node[ref]) is not str or node[ref] not in known):
                reject("Instructions may reference only earlier arrays")
        if op == "scale" and not number(node["factor"], 16):
            reject("Scale factor must be finite and within +/-16")
        if op == "roll" and (type(node["shifts"]) is not list or len(node["shifts"]) != 2
                             or any(type(n) is not int or abs(n) > 127 for n in node["shifts"])):
            reject("Roll requires two integer shifts within +/-127")
        if op == "disk" and (not number(node["radius"], 32) or node["radius"] < 0):
            reject("Disk radius must be finite and in [0,32] Fourier bins")
        if op == "transfer" and (not number(node["distance_m"], .02)
                or not o.finite_number(node["pitch_m"]) or node["pitch_m"] != 8e-6
                or not o.finite_number(node["wavelength_m"]) or node["wavelength_m"] != 532e-9):
            reject("Transfer parameters are outside the frozen sampling envelope")
        known.add(name)
    if type(program["output"]) is not str or program["output"] not in known - {"sensor"}:
        reject("Program output must name a computed array")
    try:
        payload = o.canonical(program)
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise o.Rejected("Unserializable program") from exc
    if len(payload) > MAX_PACKET:
        reject("Program payload limit exceeded")
    return program


def compile_reconstruction(samples, metadata):
    """Author a DAG; this compiler is never called by the carrier interpreter."""
    o.validate_metadata(metadata)
    o.validate_samples(samples, metadata["scale"], metadata["params"])
    p = metadata["params"]
    program = {"schema": SCHEMA,
        "input": {"encoding": "uint16-le/base64", "shape": [128, 128],
                  "data": base64.b64encode(samples.astype("<u2").tobytes()).decode("ascii")},
        "nodes": [
            {"id":"intensity", "op":"scale", "src":"sensor", "factor":metadata["scale"]/65535},
            {"id":"spectrum", "op":"fft2", "src":"intensity"},
            {"id":"shifted", "op":"roll", "src":"spectrum", "shifts":[0,p["carrier_bin"]]},
            {"id":"aperture", "op":"disk", "radius":p["band_bins"]},
            {"id":"selected", "op":"multiply", "a":"shifted", "b":"aperture"},
            {"id":"sensor_field", "op":"ifft2", "src":"selected"},
            {"id":"transfer", "op":"transfer", "pitch_m":p["pitch_m"],
             "wavelength_m":p["wavelength_m"], "distance_m":-p["distance_m"]},
            {"id":"field_fft", "op":"fft2", "src":"sensor_field"},
            {"id":"propagated_fft", "op":"multiply", "a":"field_fft", "b":"transfer"},
            {"id":"field", "op":"ifft2", "src":"propagated_fft"}], "output":"field"}
    return validate(program)


def execute(program):
    """Interpret only the supplied instructions, with fixed array and resource bounds."""
    validate(program)
    sensor = np.frombuffer(base64.b64decode(program["input"]["data"]), dtype="<u2").reshape(128,128)
    arrays = {"sensor":sensor.astype(np.complex128)}
    trace = []
    with np.errstate(all="raise"):
        try:
            for node in program["nodes"]:
                op = node["op"]
                if op == "scale": value = arrays[node["src"]] * node["factor"]
                elif op == "fft2": value = np.fft.fft2(arrays[node["src"]])
                elif op == "ifft2": value = np.fft.ifft2(arrays[node["src"]])
                elif op == "roll": value = np.roll(arrays[node["src"]], node["shifts"], axis=(0,1))
                elif op == "multiply": value = arrays[node["a"]] * arrays[node["b"]]
                elif op == "abs2": value = np.abs(arrays[node["src"]]) ** 2
                elif op == "disk":
                    f = np.fft.fftfreq(128)*128
                    fx, fy = np.meshgrid(f,f)
                    value = (fx*fx+fy*fy <= node["radius"]**2).astype(np.complex128)
                elif op == "transfer":
                    f = np.fft.fftfreq(128, d=node["pitch_m"])
                    fx, fy = np.meshgrid(f,f)
                    square = (1/node["wavelength_m"])**2 - fx*fx - fy*fy
                    admitted = square >= 0
                    value = np.zeros((128,128), dtype=np.complex128)
                    if node["distance_m"] == 0: value.fill(1)
                    else: value[admitted] = np.exp(2j*np.pi*node["distance_m"]*np.sqrt(square[admitted]))
                else: reject("Unknown instruction")  # Validation also enforces this.
                value = np.asarray(value, dtype=np.complex128)
                if value.shape != (128,128) or not np.isfinite(value).all() or np.max(np.abs(value)) > MAX_MAGNITUDE:
                    reject("Instruction result exceeds finite-array bounds")
                arrays[node["id"]] = value
                trace.append({"id":node["id"], "op":op, "sha256":array_digest(value)})
        except (FloatingPointError, OverflowError) as exc:
            raise o.Rejected("Instruction arithmetic rejected") from exc
    return arrays[program["output"]].copy(), trace


def array_digest(array):
    return o.sha256(np.asarray(array,dtype="<c16").tobytes())


def encode(image, program):
    validate(program)
    if image.size != SIZE: reject("Wrong executable carrier dimensions")
    raw = o.canonical(program)
    packet = MAGIC + struct.pack(">I",len(raw)) + hashlib.sha256(raw).digest() + raw
    columns = SIZE[0]//CELL
    rows = (len(packet)+columns-1)//columns
    values = np.full((rows,columns),255,dtype=np.uint8)
    values.flat[:len(packet)] = np.frombuffer(packet,dtype=np.uint8)
    tile = Image.fromarray(np.repeat(np.repeat(values,CELL,0),CELL,1))
    result = image.convert("L").copy()
    result.paste(tile,(0,DATA_Y))
    return result


def decode(image):
    if image.size != SIZE: reject("Wrong executable carrier dimensions")
    rgb = np.asarray(image.convert("RGB"))
    columns = SIZE[0]//CELL
    def take(count):
        rows = (count+columns-1)//columns
        if DATA_Y+rows*CELL > SIZE[1]: reject("Truncated executable cells")
        cells = rgb[DATA_Y:DATA_Y+rows*CELL].reshape(rows,CELL,columns,CELL,3)
        cells = cells.transpose(0,2,1,3,4).reshape(-1,CELL*CELL*3)[:count]
        if not np.all(cells == cells[:,:1]): reject("Executable cells damaged or resampled")
        return cells[:,0].tobytes()
    header = take(44)
    if header[:8] != MAGIC: reject("Not a v0.3 executable optical carrier")
    length = struct.unpack(">I",header[8:12])[0]
    if not 1 <= length <= MAX_PACKET: reject("Executable payload length limit exceeded")
    raw = take(44+length)[44:]
    if hashlib.sha256(raw).digest() != header[12:44]: reject("Executable packet checksum mismatch")
    try:
        program = json.loads(raw)
        if o.canonical(program) != raw: reject("Noncanonical executable JSON")
    except (ValueError,TypeError,RecursionError,UnicodeError,OverflowError) as exc:
        raise o.Rejected("Malformed executable program") from exc
    return validate(program)


def read(path):
    raw = o.read_snapshot(path)
    program, count = None, 0
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error",UserWarning)
            warnings.simplefilter("error",Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ("TIFF","GIF"): reject("Expected executable GIF or TIFF")
                for index in range(MAX_FRAMES+1):
                    try: image.seek(index)
                    except EOFError: break
                    if index == MAX_FRAMES: reject("Executable frame limit exceeded")
                    recovered = decode(image)
                    if program is not None and recovered != program: reject("Frames carry different executable programs")
                    program,count = recovered,count+1
    except o.Rejected: raise
    except (OSError,ValueError,TypeError,SyntaxError,EOFError,OverflowError,
            Image.DecompressionBombError,Image.DecompressionBombWarning,UserWarning) as exc:
        raise o.Rejected("Malformed executable image") from exc
    if program is None: reject("Empty executable carrier")
    return program,o.sha256(raw),count


def render(program, result, step=0):
    frame = Image.new("L",SIZE,20)
    draw = ImageDraw.Draw(frame)
    draw.text((24,14),"PHOTON PRETZEL / PIXEL PROGRAM",font=ImageFont.load_default(size=24),fill=255)
    sensor = np.frombuffer(base64.b64decode(program["input"]["data"]),dtype="<u2").reshape(128,128)
    display = Image.fromarray(np.round(np.clip(np.abs(result)**2,0,1)*255).astype(np.uint8))
    for tile, x in ((o.gray(sensor).convert("L"),48),(display,448)):
        frame.paste(tile.resize((256,256),Image.Resampling.NEAREST),(x,58))
    draw.text((48,320),"Embedded sensor samples",font=ImageFont.load_default(size=16),fill=220)
    draw.text((448,320),"|output|^2 (0..1 clipped)",font=ImageFont.load_default(size=16),fill=220)
    draw.text((24,349),f"{len(program['nodes'])} pixel-carried operators | refresh {step} | host CPU interpreter",fill=220)
    draw.text((24,367),"Program + exact samples below. Load either TIFF or GIF to execute.",fill=220)
    return encode(frame,program)


def write_results(folder, program, refresh=0):
    """Encode, decode and execute again; displayed output is freshly raster-derived."""
    initial = render(program,np.zeros((128,128)),refresh)
    recovered = decode(initial)
    result, trace = execute(recovered)
    frames = [render(recovered,result,refresh+i) for i in range(2)]
    gif_frames = [image.convert("P") for image in frames]
    # L -> P retains the exact 256-level grayscale palette without dithering.
    gif_frames[0].save(folder/"processing.gif",save_all=True,append_images=gif_frames[1:],
                       duration=500,loop=0,disposal=2,optimize=False)
    frames[0].save(folder/"processing.tiff",save_all=True,append_images=frames[1:],compression="tiff_deflate")
    exports = {}
    for name in ("processing.gif","processing.tiff"):
        decoded,digest,count = read(folder/name)
        if decoded != program: reject("Exported executable program changed")
        actual,_ = execute(decoded)
        if not np.array_equal(result,actual): reject("Exported program result changed")
        exports[name] = {"sha256":digest,"frames_checked":count,"result_exact":True}
    o.gray(np.abs(result)**2).save(folder/"reconstructed_intensity.png")
    np.savez(folder/"reconstruction.npz",real=result.real,imaginary=result.imag)
    o.write_json(folder/"program.json",program)
    return result,{"program_sha256":o.sha256(o.canonical(program)),"result_sha256":array_digest(result),
                   "instruction_count":len(program["nodes"]),"trace":trace,"carriers":exports,
                   "computation_location":"host CPU bounded array interpreter"}


def replay(source,output):
    program,digest,count = read(source)
    # Validate and execute before creating an output destination.
    execute(program)
    folder = o.create_output(output)
    _,execution = write_results(folder,program,1)
    report = {"version":o.VERSION,"operation":"replay-carrier","source_sha256":digest,
              "source_frames_checked":count,"source_file_preserved":True,"execution":execution,
              "simulation_only":True,"quality_against_unknown_target":"NOT_ASSESSED"}
    o.write_json(folder/"replay_receipt.json",report)
    return report


def edit_gain(source,output,gain):
    program,digest,count = read(source)
    changed = copy.deepcopy(program)
    if not number(gain,16): reject("Gain must be finite and within +/-16")
    used = {node["id"] for node in changed["nodes"]}
    name = next((f"gain_{i}" for i in range(24) if f"gain_{i}" not in used),None)
    if name is None: reject("No instruction capacity for edit")
    changed["nodes"].append({"id":name,"op":"scale","src":changed["output"],"factor":gain})
    changed["output"] = name
    original,_ = execute(program)
    edited,_ = execute(changed)
    folder = o.create_output(output)
    _,execution = write_results(folder,changed)
    report = {"version":o.VERSION,"operation":"edit-carrier","source_sha256":digest,
              "source_frames_checked":count,"source_file_preserved":True,"edit":{"append_scale":gain},
              "runtime_unchanged":True,"original_result_sha256":array_digest(original),
              "output_changed":not np.array_equal(original,edited),"execution":execution,
              "simulation_only":True,"quality_against_unknown_target":"NOT_ASSESSED"}
    o.write_json(folder/"edit_receipt.json",report)
    return report
