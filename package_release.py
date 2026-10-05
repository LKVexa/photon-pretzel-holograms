# SPDX-License-Identifier: GPL-3.0-only
"""Build an extract-and-play Windows release, including corresponding source.

Python and the .NET SDK are BUILD dependencies only. The resulting package runs
OpticalPlayer.exe directly and includes its Windows x64 .NET framework files.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent
SOURCE_URL = "https://github.com/LKVexa/photon-pretzel-holograms"
MAX_FILES = 2048
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_BYTES = 768 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
RUNTIME_LIMIT = 131072
ARCHIVE_TIME = (2026, 1, 1, 0, 0, 0)


class PackageError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_files(folder, *, limit=MAX_PACKAGE_BYTES):
    """Enumerate bounded ordinary files without following links or junctions."""
    folder = folder.resolve()
    result, total = [], 0
    for current, directories, filenames in os.walk(folder, followlinks=False):
        current = Path(current)
        for name in directories + filenames:
            path = current / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise PackageError("Package trees must not contain links or junctions")
            if not path.resolve().is_relative_to(folder):
                raise PackageError("Package path leaves its staging directory")
        for name in filenames:
            path = current / name
            size = path.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > limit or len(result) >= MAX_FILES:
                raise PackageError("Package file/count/byte limit exceeded")
            result.append(path)
    return sorted(result, key=lambda path: path.relative_to(folder).as_posix())


def make_zip(folder, output):
    """Exclusive output, fixed entry timestamps and sorted paths; no stale files."""
    files = safe_files(folder)
    if output.exists():
        raise PackageError("Existing release ZIP is preserved; choose a new output")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            entry = zipfile.ZipInfo(path.relative_to(folder).as_posix(), ARCHIVE_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            with path.open("rb") as source, archive.open(entry, "w") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
    return {"name": output.name, "bytes": output.stat().st_size,
            "sha256": hash_file(output)}


def source_files(root):
    """Explicit source allowlist excludes private runs, caches and built binaries."""
    extensions = {".py", ".cs", ".csproj", ".md", ".txt", ".yml", ".yaml", ".json", ".cmd", ".toml"}
    names = {"LICENSE", "VERSION", ".gitignore", "NuGet.Config"}
    files = []
    for path in root.iterdir():
        if path.is_file() and (path.suffix in extensions or path.name in names):
            # Runtime payload is generated data. The full corresponding source
            # includes its interpreter and authoring program instead.
            if path.name not in {"runtime-payload.json", "audit.json"}:
                files.append(path)
    for folder in (root / "dotnet", root / "packaging", root / "tests", root / ".github"):
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            relative = path.relative_to(root)
            if any(part in {"bin", "obj", "__pycache__", ".git"} for part in relative.parts):
                continue
            if path.is_file() and (path.suffix in extensions or path.name in names):
                if path.name.endswith("-report.json"):
                    continue
                files.append(path)
    total = 0
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise PackageError("Source archive contains a link or outside path")
        total += path.stat().st_size
    if total > MAX_SOURCE_BYTES or len(files) > MAX_FILES:
        raise PackageError("Corresponding source exceeds its size/count limit")
    return sorted(set(files), key=lambda path: path.relative_to(root).as_posix())


def write_source_archive(root, destination, game_files=()):
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in source_files(root):
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), ARCHIVE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
        for path in game_files:
            info = zipfile.ZipInfo(path.name, ARCHIVE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def decoded_image(path):
    import raster_vm
    return raster_vm.read_envelope(path)[0]


def runtime_hash(job):
    runtime = job.get("runtime", {})
    if runtime.get("format") != "dotnet-il/gzip-base64":
        raise PackageError("Image is missing the compiled interpreter")
    try:
        compressed = base64.b64decode(runtime["data"], validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            binary = stream.read(RUNTIME_LIMIT + 1)
    except (ValueError, KeyError, OSError) as error:
        raise PackageError("Invalid image runtime encoding") from error
    if not 1 <= len(binary) <= RUNTIME_LIMIT or sha256(binary) != runtime.get("sha256"):
        raise PackageError("Image runtime size or checksum mismatch")
    return sha256(binary)


def validate_images(tiff, gif, trusted_hash):
    first, second = decoded_image(tiff), decoded_image(gif)
    if first != second:
        raise PackageError("TIFF and GIF must carry identical runtime, program and initial state")
    if runtime_hash(first) != trusted_hash:
        raise PackageError("Default image runtime differs from the approved host runtime")
    return first


def check_publish(folder):
    files = safe_files(folder)
    names = {path.name.lower() for path in files}
    required = {"opticalplayer.exe", "opticalplayer.dll", "opticalplayer.runtimeconfig.json",
                "opticalplayer.deps.json", "coreclr.dll", "hostfxr.dll", "hostpolicy.dll",
                "system.private.corelib.dll", "system.windows.forms.dll"}
    if missing := required - names:
        raise PackageError("Self-contained publish is missing: " + ", ".join(sorted(missing)))
    if "arrayruntime.dll" in names:
        raise PackageError("A disk-based ArrayRuntime.dll fallback must never be packaged")
    config = json.loads((folder / "OpticalPlayer.runtimeconfig.json").read_text(encoding="utf-8"))["runtimeOptions"]
    if config.get("framework") or config.get("frameworks"):
        raise PackageError("Player still depends on an externally installed .NET runtime")
    included = {item["name"] for item in config.get("includedFrameworks", [])}
    if not {"Microsoft.NETCore.App", "Microsoft.WindowsDesktop.App"} <= included:
        raise PackageError("Self-contained runtime framework declaration is incomplete")


def build_environment(root):
    environment = os.environ.copy()
    environment.update(DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1",
                       DOTNET_GENERATE_ASPNET_CERTIFICATE="false", DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE="true")
    environment.setdefault("DOTNET_CLI_HOME", str(root / "user_runs" / "dotnet-home"))
    environment.setdefault("NUGET_PACKAGES", str(root / "user_runs" / "nuget-packages"))
    if os.name == "nt":
        # This build needs only public framework packages. Keep NuGet config and
        # generated cache state in the project, without reading private feeds.
        environment["APPDATA"] = str(root / "user_runs" / "dotnet-home" / "AppData")
        Path(environment["APPDATA"]).mkdir(parents=True, exist_ok=True)
    return environment


def invoke(command, *, cwd, environment, timeout=180):
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True,
                            text=True, timeout=timeout, encoding="utf-8", errors="replace")
    if result.returncode:
        # Build diagnostics may contain local paths and belong in build output,
        # never in the distributed player manifest.
        raise PackageError((result.stdout + "\n" + result.stderr)[-12000:])
    return result.stdout


def publish(root, dotnet, stage, package_source=None):
    environment = build_environment(root)
    restore = []
    if package_source:
        source = Path(package_source).resolve()
        if not source.is_dir():
            raise PackageError("--package-source must name an existing local NuGet directory")
        restore = ["--source", str(source)]
    runtime_project = root / "dotnet/ArrayRuntime/ArrayRuntime.csproj"
    player_project = root / "dotnet/OpticalPlayer/OpticalPlayer.csproj"
    invoke([dotnet, "restore", str(runtime_project), *restore, "--nologo"], cwd=root, environment=environment)
    invoke([dotnet, "build", str(runtime_project), "-c", "Release", "--no-restore", "--nologo"],
           cwd=root, environment=environment)
    binary = root / "dotnet/ArrayRuntime/bin/Release/net10.0/ArrayRuntime.dll"
    trusted_hash = sha256(binary.read_bytes())
    trusted_source = (root / "dotnet/OpticalPlayer/TrustedRuntime.cs").read_text(encoding="utf-8")
    match = re.search(r'Sha256\s*=\s*"([0-9a-f]{64})"', trusted_source)
    if not match or match.group(1) != trusted_hash:
        raise PackageError("Rebuild the approved runtime payload and images before packaging: host trust differs from source build")
    invoke([dotnet, "restore", str(player_project), "-r", "win-x64", "-p:SelfContained=true", *restore, "--nologo"],
           cwd=root, environment=environment)
    invoke([dotnet, "publish", str(player_project), "-c", "Release", "-r", "win-x64", "--self-contained", "true",
            "--no-restore", "-p:PublishSingleFile=false", "-p:PublishTrimmed=false", "-p:DebugType=None",
            "-p:DebugSymbols=false", "-o", str(stage), "--nologo"], cwd=root, environment=environment)
    check_publish(stage)
    return trusted_hash


def copy_notices(root, stage):
    # Copy the actual runtime packages' MIT licenses/notices, not the SDK's
    # separate development-tool distribution terms.
    cache = Path(build_environment(root)["NUGET_PACKAGES"])
    frameworks = json.loads((stage / "OpticalPlayer.runtimeconfig.json").read_text())["runtimeOptions"]["includedFrameworks"]
    folder = stage / "licenses"
    folder.mkdir()
    records = []
    for framework in frameworks:
        name = framework["name"].lower() + ".runtime.win-x64"
        version = framework["version"]
        package = cache / name / version
        files = {path.name.upper(): path for path in package.iterdir() if path.is_file()} if package.is_dir() else {}
        license_path = files.get("LICENSE.TXT") or files.get("LICENSE")
        if license_path is None:
            raise PackageError("The official runtime package license is missing: " + name)
        destination = folder / (name + "-LICENSE.txt")
        shutil.copyfile(license_path, destination)
        notice = files.get("THIRD-PARTY-NOTICES.TXT")
        if notice:
            shutil.copyfile(notice, folder / (name + "-THIRD-PARTY-NOTICES.txt"))
        if name == "microsoft.netcore.app.runtime.win-x64":
            if notice is None:
                raise PackageError("The .NET runtime package third-party notices are missing")
            shutil.copyfile(license_path, stage / "DOTNET-LICENSE.txt")
            shutil.copyfile(notice, stage / "DOTNET-THIRD-PARTY-NOTICES.txt")
        records.append({"package": name, "version": version, "license_sha256": hash_file(license_path)})
    write_json(folder / "upstream-packages.json", records)


def clean_environment():
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("DOTNET_", "PYTHON", "VIRTUAL_ENV"))}
    system_root = environment.get("SystemRoot", environment.get("SYSTEMROOT", "C:\\Windows"))
    environment["PATH"] = str(Path(system_root) / "System32")
    environment["DOTNET_MULTILEVEL_LOOKUP"] = "0"
    return environment


def policy_block(error=None, returncode=None):
    # Recognize policy denial without retrying through another execution path.
    if isinstance(error, OSError) and getattr(error, "winerror", None) in {4551, 577}:
        return True
    return returncode is not None and (returncode & 0xffffffff) in {0x800711C7, 0xC0000428}


def smoke(stage, initial, trusted_hash):
    import numpy as np
    import raster_vm
    checks=[];environment=clean_environment()
    output_dir=stage.parent/"smoke-output";output_dir.mkdir(exist_ok=False)
    executable=stage/"OpticalPlayer.exe"
    def run(arguments):
        try:
            process=subprocess.run([str(executable),*arguments],cwd=stage,env=environment,
                capture_output=True,encoding="utf-8",errors="replace",timeout=45)
        except OSError as error:
            if policy_block(error=error):return None
            raise
        if policy_block(returncode=process.returncode):return None
        if process.returncode:raise PackageError("Packaged reader smoke failed: "+process.stderr[-2000:])
        return json.loads(process.stdout)
    def blocked():return {"status":"blocked_by_application_control","checks":checks,
                         "note":"Launch denied by Windows policy; no bypass or alternative launch attempted."}
    expected,_=raster_vm.execute(raster_vm.validate_envelope(initial))
    for extension in ("tiff","gif"):
        output=output_dir/extension
        receipt=run(["--execute","game."+extension,"--out",str(output)])
        if receipt is None:return blocked()
        actual=np.fromfile(output/"field.complex128-le",dtype="<c16").reshape(128,128)
        if not np.allclose(actual,expected,rtol=2e-10,atol=1e-8):raise PackageError("Packaged computation differs from NumPy oracle")
        if receipt["runtime_sha256"]!=trusted_hash:raise PackageError("Unexpected runtime")
        for ext in ("tiff","gif"):
            exported=decoded_image(output/("processing."+ext))
            if exported!=initial:raise PackageError("Export lost program, input or runtime")
            repeated=run(["--execute",str(output/("processing."+ext))])
            if repeated is None:return blocked()
            if repeated["result_sha256"]!=receipt["result_sha256"]:raise PackageError("Reopened image result changed")
        checks.append({"format":extension,"computation":"passed","export_and_reopen":"passed","runtime_preserved":True})
    ui=run(["--smoke-ui","--export-dir",str(output_dir/"ui-export")])
    if ui is None:return blocked()
    if not ui.get("automatic_computation") or not ui.get("program_edit_changed_output") or ui.get("pixel_refreshes")!=3 or not ui.get("runtime_preserved"):
        raise PackageError("Packaged UI did not compute, edit and refresh from pixels")
    return {"status":"passed","checks":checks,"ui_receipt":ui,
            "environment":"PATH limited to Windows System32; no installed dotnet/Python command required"}


def manifest(folder, version, runtime_sha, validation):
    files = safe_files(folder)
    return {"schema": "photon-pretzel-windows-release/1", "version": version, "target": "win-x64",
            "self_contained": True, "entrypoint": "Play.cmd", "player": "OpticalPlayer.exe",
            "images": ["game.tiff", "game.gif"], "image_runtime_sha256": runtime_sha,
            "source": {"archive": "source.zip", "repository": SOURCE_URL},
            "execution_validation": validation,
            "files": [{"path": path.relative_to(folder).as_posix(), "bytes": path.stat().st_size,
                       "sha256": hash_file(path)} for path in files if path.relative_to(folder).as_posix() != "manifest.json"]}


def verify_manifest(folder):
    recorded = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    actual = {path.relative_to(folder).as_posix(): path for path in safe_files(folder) if path.relative_to(folder).as_posix() != "manifest.json"}
    entries = recorded.get("files", [])
    if len(entries) != len(actual) or len({entry["path"] for entry in entries}) != len(entries):
        raise PackageError("Release file manifest count or uniqueness mismatch")
    for entry in entries:
        path = actual.get(entry["path"])
        if path is None or path.stat().st_size != entry["bytes"] or hash_file(path) != entry["sha256"]:
            raise PackageError("Release file manifest mismatch")
    return recorded


def package(root, dotnet, output, tiff, gif, *, package_source=None, skip_execution=False):
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise PackageError("Existing release ZIP is preserved; choose a new output")
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise PackageError("VERSION must contain a plain semantic version")
    workspace = root / "user_runs" / ("release-" + uuid.uuid4().hex)
    stage = workspace / ("Photon-Pretzel-" + version + "-win-x64")
    stage.mkdir(parents=True, exist_ok=False)
    runtime_sha = publish(root, dotnet, stage, package_source)
    initial = validate_images(tiff, gif, runtime_sha)
    shutil.copyfile(tiff, stage / "game.tiff")
    shutil.copyfile(gif, stage / "game.gif")
    shutil.copyfile(root / "LICENSE", stage / "LICENSE")
    copy_notices(root, stage)
    shutil.copyfile(root / "packaging/THIRD_PARTY_NOTICES.txt", stage / "THIRD_PARTY_NOTICES.txt")
    shutil.copyfile(root / "packaging/PLAY-README.txt", stage / "READ-ME-FIRST.txt")
    shutil.copyfile(root / "packaging/Play.cmd", stage / "Play.cmd")
    write_source_archive(root, stage / "source.zip", (stage / "game.tiff", stage / "game.gif"))
    validation = {"status": "not_run", "reason": "Builder requested --skip-execution"}
    if not skip_execution:
        if os.name != "nt":
            raise PackageError("Execution verification requires Windows; use --skip-execution for a clearly marked build-only archive")
        validation = smoke(stage, initial, runtime_sha)
    write_json(stage / "manifest.json", manifest(stage, version, runtime_sha, validation))
    verify_manifest(stage)
    archive = make_zip(stage, output)
    receipt = {"schema": "photon-pretzel-release-build/1", "version": version,
               "utc": datetime.now(timezone.utc).isoformat(), "archive": archive,
               "image_runtime_sha256": runtime_sha, "execution_validation": validation,
               "files": len(safe_files(stage)), "stage": str(stage)}
    write_json(output.with_suffix(".build.json"), receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dotnet", default=shutil.which("dotnet"))
    parser.add_argument("--out", type=Path, default=ROOT / "releases/Photon-Pretzel-0.4.0-win-x64.zip")
    parser.add_argument("--tiff", type=Path, default=ROOT / "game.tiff")
    parser.add_argument("--gif", type=Path, default=ROOT / "game.gif")
    parser.add_argument("--package-source", type=Path, help="Offline local directory containing official runtime .nupkg files")
    parser.add_argument("--skip-execution", action="store_true", help="Mark launch verification as not_run; intended for build-only environments")
    args = parser.parse_args()
    if not args.dotnet:
        parser.error("Build machine requires .NET SDK 10.0.401 or --dotnet; players do not")
    dotnet = str(Path(args.dotnet).resolve())
    try:
        receipt = package(ROOT, dotnet, args.out, args.tiff.resolve(), args.gif.resolve(),
                          package_source=args.package_source, skip_execution=args.skip_execution)
    except (PackageError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
