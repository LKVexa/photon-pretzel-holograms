# SPDX-License-Identifier: GPL-3.0-only
"""Generate, inspect, or reconstruct a bounded uint16 hologram TIFF."""
import argparse
import json
from pathlib import Path
import sys
import optics
import raster_vm


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=optics.VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="Generate numerical TIFF, GIF and a receipt")
    generate.add_argument("--out", type=Path, required=True, help="New output directory; existing paths are refused")
    generate.add_argument("--scene", choices=("training", "holdout"), default="training")
    generate.add_argument("--distance", type=float, default=.01, help="Metres, finite and within +/-0.02")
    inspect = commands.add_parser("inspect", help="Validate a uint16 TIFF and display its profile")
    inspect.add_argument("image", type=Path)
    reconstruct = commands.add_parser("reconstruct", help="Recover the complex field from a uint16 TIFF")
    reconstruct.add_argument("image", type=Path)
    reconstruct.add_argument("--out", type=Path, required=True)
    replay = commands.add_parser("replay-carrier", help="Execute pixel-carried program and samples from GIF/TIFF; export fresh executable images")
    replay.add_argument("image", type=Path)
    replay.add_argument("--out", type=Path, required=True)
    edit = commands.add_parser("edit-carrier", help="Append a scale instruction to the encoded program and execute it")
    edit.add_argument("image", type=Path)
    edit.add_argument("--out", type=Path, required=True)
    edit.add_argument("--gain", type=float, required=True)
    program = commands.add_parser("inspect-carrier", help="Show executable instructions and input digest from GIF/TIFF")
    program.add_argument("image", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            result = optics.generate(args.out, args.scene, args.distance)
        elif args.command == "inspect":
            samples, metadata, digest = optics.read_hologram(args.image)
            result = {"profile": metadata, "sha256": digest, "shape": list(samples.shape),
                      "sample_min": int(samples.min()), "sample_max": int(samples.max())}
        elif args.command == "reconstruct":
            result = optics.reconstruct_file(args.image, args.out)
        elif args.command == "replay-carrier":
            result = optics.replay_carrier(args.image, args.out)
        elif args.command == "edit-carrier":
            result = raster_vm.edit_gain(args.image, args.out, args.gain)
        else:
            program, digest, count = raster_vm.read(args.image)
            result = {"source_sha256": digest, "frames_checked": count,
                      "schema": program["schema"], "nodes": program["nodes"], "output": program["output"],
                      "input_sha256": optics.sha256(program["input"]["data"].encode("ascii"))}
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (optics.Rejected, OSError) as exc:
        print(json.dumps({"status": "REJECTED", "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
