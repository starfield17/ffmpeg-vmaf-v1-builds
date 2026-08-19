from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Protocol


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.json"
MACOS_LOCK_PATH = ROOT / "config" / "macos-source-lock.json"
MACOS_SOURCE_FILES = {
    "SDL2.tar.gz",
    "SVT-AV1.tar.gz",
    "cmake-release.tar.gz",
    "ffmpeg.tar.bz2",
    "fontconfig.tar.xz",
    "freetype.tar.gz",
    "fribidi.tar.xz",
    "harfbuzz.tar.xz",
    "libass.tar.gz",
    "libogg.tar.gz",
    "libvmaf.tar.gz",
    "libxml2.tar.xz",
    "meson-1.12.0-py3-none-any.whl",
    "nasm.tar.gz",
    "ninja.tar.gz",
    "openssl.tar.gz",
    "pkg-config.tar.gz",
    "x265.tar.gz",
    "zlib.tar.gz",
}
TARGETS = {
    "windows-x86_64",
    "windows-arm64",
    "linux-x86_64",
    "linux-arm64",
    "macos-arm64",
}
VMAF_MODELS = (
    ("vmaf_v1.0.16_3d0h", 1920, 1080, 30, 6),
    ("vmaf_v1.0.16_1d5h_2160", 3840, 2160, 30, 6),
    ("vmaf_v1.0.16_hfr_3d0h", 1920, 1080, 60, 12),
    ("vmaf_v1.0.16_hfr_1d5h_2160", 3840, 2160, 60, 12),
)
LICENSE_COMPONENTS = {"FFmpeg", "Netflix VMAF"}
LICENSE_NAMES = {"LICENSE.md", "COPYING.GPLv3", "VMAF-LICENSE.txt"}
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_VMAF_SCORE_RE = re.compile(
    r"VMAF score:\s*([+-]?(?:nan|inf(?:inity)?|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?))",
    re.IGNORECASE,
)
MACHINE_TYPES = {
    "x86_64": {"pe": 0x8664, "elf": 0x3E, "macho": 0x01000007},
    "arm64": {"pe": 0xAA64, "elf": 0xB7, "macho": 0x0100000C},
}
EXPECTED_RUNNER_CONTEXT = {
    "windows-x86_64": {"runner_os": "Windows", "runner_arch": "X64"},
    "windows-arm64": {"runner_os": "Windows", "runner_arch": "ARM64"},
    "linux-x86_64": {"runner_os": "Linux", "runner_arch": "X64"},
    "linux-arm64": {"runner_os": "Linux", "runner_arch": "ARM64"},
    "macos-arm64": {"runner_os": "macOS", "runner_arch": "ARM64"},
}


def load_config(path: Path = CONFIG_PATH) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 2:
        raise ValueError("Unsupported source config schema.")
    targets = data.get("targets")
    if not isinstance(targets, dict) or set(targets) != TARGETS:
        raise ValueError(f"Source config must define exactly: {sorted(TARGETS)}")
    return data


def load_macos_source_lock(path: Path = MACOS_LOCK_PATH) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported macOS source lock schema.")
    return data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_digest(value: object, label: str) -> str:
    digest = str(value)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"Invalid SHA-256 for {label}: {digest}")
    return digest


def _validate_commit(value: object, label: str) -> str:
    commit = str(value)
    if _COMMIT_RE.fullmatch(commit) is None:
        raise ValueError(f"Invalid commit for {label}: {commit}")
    return commit


def validate_config(data: dict[str, object]) -> None:
    if data.get("release_tag") != "ffmpeg-" + str(data.get("bundle_version")):
        raise ValueError("release_tag must be ffmpeg-<bundle_version>.")
    if data.get("verification_contract_version") != 2:
        raise ValueError("verification_contract_version must be 2.")
    licenses = data.get("licenses")
    if not isinstance(licenses, list) or len(licenses) != 3:
        raise ValueError("Exactly three FFmpeg and VMAF license inputs are required.")
    license_names: set[str] = set()
    license_components: set[str] = set()
    for entry in licenses:
        if not isinstance(entry, dict):
            raise ValueError("Invalid license entry.")
        license_names.add(str(entry.get("name")))
        license_components.add(str(entry.get("component")))
        _validate_digest(entry.get("sha256"), str(entry.get("name")))
    if license_names != LICENSE_NAMES or license_components != LICENSE_COMPONENTS:
        raise ValueError("License inputs must cover FFmpeg and Netflix VMAF.")
    macos_build = data.get("macos_build")
    if not isinstance(macos_build, dict) or not macos_build.get("binary_version"):
        raise ValueError("macOS build must declare its exact binary version.")
    for field in ("commit", "ffmpeg_commit", "libvmaf_commit"):
        _validate_commit(macos_build.get(field), f"macOS {field}")
    for field in ("ffmpeg_source", "libvmaf_source", "libvmaf_version"):
        if not macos_build.get(field):
            raise ValueError(f"macOS build must declare {field}.")
    if str(macos_build["ffmpeg_commit"]) not in str(macos_build["ffmpeg_source"]):
        raise ValueError("macOS FFmpeg source URL must identify the exact commit.")
    if str(macos_build["libvmaf_commit"]) not in str(macos_build["libvmaf_source"]):
        raise ValueError("macOS libvmaf source URL must identify the exact commit.")
    targets = data["targets"]
    assert isinstance(targets, dict)
    for name, raw in targets.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid target entry: {name}")
        if raw.get("source_kind") == "mirror":
            _validate_digest(raw.get("sha256"), name)
            if not raw.get("source_version") or not raw.get("binary_version"):
                raise ValueError(
                    f"Mirror target {name} must declare source and binary versions."
                )
            for field in ("ffmpeg_commit", "libvmaf_commit"):
                _validate_commit(raw.get(field), f"{name} {field}")
            for field in (
                "ffmpeg_source",
                "libvmaf_version",
                "libvmaf_source",
                "upstream_build_recipe",
                "upstream_release",
            ):
                if not raw.get(field):
                    raise ValueError(f"Mirror target {name} must declare {field}.")
            if str(raw["ffmpeg_commit"]) not in str(raw["ffmpeg_source"]):
                raise ValueError(f"Mirror target {name} FFmpeg source must identify its commit.")
            if str(raw["libvmaf_commit"]) not in str(raw["libvmaf_source"]):
                raise ValueError(f"Mirror target {name} libvmaf source must identify its commit.")
            if raw.get("format") not in {"zip", "tar.xz"}:
                raise ValueError(f"Unsupported archive format for {name}.")
        elif raw.get("source_kind") != "build":
            raise ValueError(f"Unsupported source kind for {name}.")


def validate_macos_source_lock(data: dict[str, object]) -> None:
    files = data.get("files")
    if not isinstance(files, dict) or set(files) != MACOS_SOURCE_FILES:
        raise ValueError(
            f"macOS source lock must define exactly: {sorted(MACOS_SOURCE_FILES)}"
        )
    for name, digest in files.items():
        _validate_digest(digest, str(name))


def _download(url: str, destination: Path, expected_sha256: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "ffmpeg-vmaf-v1-builds/1"})
    with urllib.request.urlopen(request) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)
    actual = sha256_file(destination)
    if actual != expected_sha256:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {actual}"
        )
    return destination


class BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


def _copy_stream(source: BinaryReader, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        shutil.copyfileobj(source, output)
    destination.chmod(destination.stat().st_mode | 0o755)


def extract_binaries(archive: Path, archive_format: str, output_dir: Path, platform: str) -> None:
    expected = {
        "ffmpeg.exe" if platform == "windows" else "ffmpeg",
        "ffprobe.exe" if platform == "windows" else "ffprobe",
    }
    found: set[str] = set()
    bin_dir = output_dir / "bin"
    if archive_format == "zip":
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                name = Path(member.filename).name.lower()
                if name not in expected or name in found or member.is_dir():
                    continue
                with package.open(member) as source:
                    _copy_stream(source, bin_dir / name)
                found.add(name)
    elif archive_format == "tar.xz":
        with tarfile.open(archive, mode="r:xz") as package:
            for member in package.getmembers():
                name = Path(member.name).name.lower()
                if name not in expected or name in found or not member.isfile():
                    continue
                source = package.extractfile(member)
                if source is not None:
                    with source:
                        _copy_stream(source, bin_dir / name)
                    found.add(name)
    else:
        raise ValueError(f"Unsupported archive format: {archive_format}")
    if found != expected:
        raise RuntimeError(f"Archive contains {sorted(found)}; expected {sorted(expected)}")


def fetch_target(target_name: str, output_dir: Path, data: dict[str, object]) -> Path:
    target = data["targets"][target_name]  # type: ignore[index]
    if not isinstance(target, dict) or target.get("source_kind") != "mirror":
        raise ValueError(f"Target is not a mirrored input: {target_name}")
    output_dir.mkdir(parents=True, exist_ok=False)
    archive_format = str(target["format"])
    suffix = ".zip" if archive_format == "zip" else ".tar.xz"
    archive = output_dir / ("upstream" + suffix)
    _download(str(target["url"]), archive, str(target["sha256"]))
    extract_binaries(archive, archive_format, output_dir, str(target["platform"]))
    archive.unlink()
    return output_dir / "bin"


def binary_architecture(path: Path) -> str:
    with path.open("rb") as handle:
        data = handle.read(4096)
        if data.startswith(b"MZ") and len(data) >= 64:
            pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
            handle.seek(pe_offset)
            header = handle.read(6)
            if header[:4] != b"PE\0\0":
                raise ValueError(f"Invalid PE executable: {path}")
            machine = struct.unpack_from("<H", header, 4)[0]
            binary_format = "pe"
        elif data.startswith(b"\x7fELF") and len(data) >= 20:
            endian = "<" if data[5] == 1 else ">"
            machine = struct.unpack_from(f"{endian}H", data, 18)[0]
            binary_format = "elf"
        elif data[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"}:
            endian = "<" if data[:4] == b"\xcf\xfa\xed\xfe" else ">"
            machine = struct.unpack_from(f"{endian}I", data, 4)[0]
            binary_format = "macho"
        else:
            raise ValueError(f"Unsupported executable format: {path}")
    for architecture, formats in MACHINE_TYPES.items():
        if formats[binary_format] == machine:
            return architecture
    raise ValueError(f"Unsupported {binary_format} machine 0x{machine:x}: {path}")


def _run(command: list[str], *, timeout: int = 180) -> str:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    output = result.stdout + "\n" + result.stderr
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}\n{output}")
    return output


def _normalization_filter(width: int, height: int) -> str:
    return (
        "scale=w='max(2,trunc(iw*sar/2)*2)':"
        "h='max(2,trunc(ih/2)*2)':flags=bicubic,setsar=1,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2:flags=bicubic,"
        f"pad={width}:{height}:x='trunc((ow-iw)/4)*2':"
        "y='trunc((oh-ih)/4)*2',"
        "setsar=1,format=yuv420p10le,settb=AVTB,setpts=PTS-STARTPTS"
    )


def _vmaf_graph(model: str, width: int, height: int) -> str:
    normalize = _normalization_filter(width, height)
    model_config = (
        f"version={model}\\:cambi.enc_width=320\\:"
        "cambi.enc_height=180\\:cambi.enc_bitdepth=8"
    )
    return (
        f"[0:v]{normalize}[dist];[1:v]{normalize}[ref];"
        f"[dist][ref]libvmaf=model='{model_config}':n_threads=2:n_subsample=1"
    )


def _parse_vmaf_score(output: str, model: str) -> float:
    matches = _VMAF_SCORE_RE.findall(output)
    if not matches:
        raise RuntimeError(f"VMAF model {model} did not produce a score.")
    score = float(matches[-1])
    if not math.isfinite(score) or not 0.0 <= score <= 100.0:
        raise RuntimeError(f"VMAF model {model} produced invalid score {score!r}.")
    return score


def _verify_anamorphic_normalization(ffmpeg: Path) -> dict[str, object]:
    output = _run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-f",
            "lavfi",
            "-i",
            "color=white:size=720x576:rate=1:duration=1",
            "-vf",
            "setsar=64/45,"
            + _normalization_filter(1920, 1080)
            + ",signalstats,metadata=mode=print:key=lavfi.signalstats.YMIN:file=-",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ]
    )
    matches = re.findall(r"lavfi\.signalstats\.YMIN=(\d+)", output)
    if not matches:
        raise RuntimeError("Anamorphic normalization did not report luma evidence.")
    luma_min = int(matches[-1])
    if luma_min < 800 or "1920x1080 [SAR 1:1 DAR 16:9]" not in output:
        raise RuntimeError(
            "Anamorphic normalization introduced padding or incorrect display geometry: "
            f"YMIN={luma_min}."
        )
    return {
        "source_geometry": "720x576 SAR 64:45",
        "output_geometry": "1920x1080 SAR 1:1",
        "luma_min": luma_min,
    }


def _expected_binary_version(
    target: dict[str, object], data: dict[str, object]
) -> str:
    if target["source_kind"] == "mirror":
        return str(target["binary_version"])
    macos_build = data["macos_build"]
    assert isinstance(macos_build, dict)
    return str(macos_build["binary_version"])


def verify_target(
    target_name: str, bin_dir: Path, data: dict[str, object]
) -> dict[str, object]:
    target = data["targets"][target_name]  # type: ignore[index]
    assert isinstance(target, dict)
    suffix = ".exe" if target["platform"] == "windows" else ""
    ffmpeg = bin_dir / f"ffmpeg{suffix}"
    ffprobe = bin_dir / f"ffprobe{suffix}"
    architectures = {}
    for binary in (ffmpeg, ffprobe):
        actual = binary_architecture(binary)
        if actual != target["architecture"]:
            raise RuntimeError(f"{binary} is {actual}; expected {target['architecture']}")
        architectures[binary.name] = actual

    version = _run([str(ffmpeg), "-hide_banner", "-version"])
    first_line = version.splitlines()[0].split()
    actual_version = first_line[2] if len(first_line) >= 3 else ""
    expected_version = _expected_binary_version(target, data)
    if actual_version != expected_version:
        raise RuntimeError(
            f"FFmpeg version is {actual_version or 'unparseable'}; "
            f"expected {expected_version}."
        )
    for option in ("--enable-gpl", "--enable-version3"):
        if option not in version:
            raise RuntimeError(f"FFmpeg is missing required configure option {option}.")
    filters = _run([str(ffmpeg), "-hide_banner", "-filters"])
    if "libvmaf" not in filters:
        raise RuntimeError("FFmpeg is missing libvmaf.")
    encoders = _run([str(ffmpeg), "-hide_banner", "-encoders"])
    for encoder in ("libx265", "libsvtav1"):
        if encoder not in encoders:
            raise RuntimeError(f"FFmpeg is missing encoder {encoder}.")
    _run([str(ffprobe), "-hide_banner", "-version"])

    vmaf_scores = {}
    vmaf_probes = {}
    for model, width, height, fps, frames in VMAF_MODELS:
        output = _run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=320x180:rate={fps}:duration=1",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=320x180:rate={fps}:duration=1",
                "-filter_complex",
                _vmaf_graph(model, width, height),
                "-frames:v",
                str(frames),
                "-an",
                "-f",
                "null",
                "-",
            ]
        )
        score = _parse_vmaf_score(output, model)
        vmaf_scores[model] = score
        vmaf_probes[model] = {"fps": fps, "frames": frames, "score": score}

    anamorphic_normalization = _verify_anamorphic_normalization(ffmpeg)

    for encoder in ("libx265", "libsvtav1"):
        _run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=128x128:duration=0.2:rate=5",
                "-frames:v",
                "1",
                "-c:v",
                encoder,
                "-f",
                "null",
                "-",
            ]
        )

    runtime_dependencies = {}
    if target["platform"] == "macos":
        for binary in (ffmpeg, ffprobe):
            linkage = _run(["otool", "-L", str(binary)])
            dependencies = [
                line.strip().split(" (", maxsplit=1)[0]
                for line in linkage.splitlines()[1:]
                if line.strip()
            ]
            unexpected = [
                dependency
                for dependency in dependencies
                if not dependency.startswith(("/System/Library/", "/usr/lib/"))
            ]
            if unexpected:
                raise RuntimeError(
                    f"{binary} has non-system runtime dependencies: {unexpected}"
                )
            runtime_dependencies[binary.name] = dependencies

    return {
        "binary_version": actual_version,
        "architectures": architectures,
        "vmaf_scores": vmaf_scores,
        "vmaf_probes": vmaf_probes,
        "anamorphic_normalization": anamorphic_normalization,
        "encoder_smokes": ["libx265", "libsvtav1"],
        "runtime_dependencies": runtime_dependencies,
    }


def _copy_verified_url(entry: dict[str, object], destination: Path) -> None:
    _download(str(entry["url"]), destination, str(entry["sha256"]))


def package_target(
    target_name: str,
    bin_dir: Path,
    output_dir: Path,
    data: dict[str, object],
) -> Path:
    target = data["targets"][target_name]  # type: ignore[index]
    assert isinstance(target, dict)
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_name = f"ffmpeg-{data['bundle_version']}-{target_name}"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / bundle_name
        suffix = ".exe" if target["platform"] == "windows" else ""
        (root / "bin").mkdir(parents=True)
        for name in (f"ffmpeg{suffix}", f"ffprobe{suffix}"):
            shutil.copy2(bin_dir / name, root / "bin" / name)
        licenses_dir = root / "LICENSES"
        licenses_dir.mkdir()
        for raw in data["licenses"]:  # type: ignore[index]
            assert isinstance(raw, dict)
            _copy_verified_url(raw, licenses_dir / str(raw["name"]))
        source = {
            "schema_version": 2,
            "bundle_version": data["bundle_version"],
            "target": target_name,
            "source": target,
            "macos_build": data["macos_build"] if target["source_kind"] == "build" else None,
            "bundle_recipe": _execution_context(),
        }
        (root / "SOURCE.json").write_text(
            json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if target["format"] == "zip":
            archive = output_dir / f"{bundle_name}.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        package.write(path, path.relative_to(root.parent))
        else:
            archive = output_dir / f"{bundle_name}.tar.xz"
            with tarfile.open(archive, "w:xz") as package:
                package.add(root, arcname=bundle_name)
    return archive


def _execution_context() -> dict[str, str]:
    return {
        "repository": os.environ.get("GITHUB_REPOSITORY", "local"),
        "commit": os.environ.get("GITHUB_SHA", "local"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
        "runner_name": os.environ.get("RUNNER_NAME", "local"),
        "runner_os": os.environ.get("RUNNER_OS", sys.platform),
        "runner_arch": os.environ.get("RUNNER_ARCH", "local"),
    }


def write_verification_report(
    target_name: str,
    archive: Path,
    data: dict[str, object],
    observations: dict[str, object],
) -> Path:
    report = {
        "schema_version": 2,
        "target": target_name,
        "archive": {"name": archive.name, "sha256": sha256_file(archive)},
        "execution": _execution_context(),
        "observed": observations,
        "contract": {
            "ffmpeg_version": data["ffmpeg_version"],
            "verification_contract_version": data["verification_contract_version"],
            "vmaf_models": [model for model, *_ in VMAF_MODELS],
            "vmaf_probe_frames": {
                model: {"fps": fps, "frames": frames}
                for model, _, _, fps, frames in VMAF_MODELS
            },
            "pixel_format": "yuv420p10le",
            "cambi_metadata": True,
            "anamorphic_normalization": True,
            "licenses": sorted(LICENSE_NAMES),
            "encoders": ["libx265", "libsvtav1"],
            "native_architecture": True,
            "macos_system_linkage_only": target_name.startswith("macos-"),
        },
    }
    report_path = archive.with_name(archive.name + ".verification.json")
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report_path


def verify_and_package_target(
    target_name: str,
    bin_dir: Path,
    output_dir: Path,
    data: dict[str, object],
) -> Path:
    observations = verify_target(target_name, bin_dir, data)
    archive = package_target(target_name, bin_dir, output_dir, data)
    write_verification_report(target_name, archive, data, observations)
    return archive


def release_metadata(assets_dir: Path, output_dir: Path, data: dict[str, object]) -> None:
    target_archives = {
        name: f"ffmpeg-{data['bundle_version']}-{name}."
        f"{'zip' if raw['format'] == 'zip' else 'tar.xz'}"
        for name, raw in data["targets"].items()  # type: ignore[union-attr]
    }
    expected_archives = set(target_archives.values())
    expected_reports = {name + ".verification.json" for name in expected_archives}
    expected = expected_archives | expected_reports
    found = {path.name for path in assets_dir.iterdir() if path.is_file()}
    if found != expected:
        raise RuntimeError(f"Release assets are {sorted(found)}; expected {sorted(expected)}")
    assets = [
        {"name": name, "sha256": sha256_file(assets_dir / name)} for name in sorted(found)
    ]
    reports = []
    release_execution = _execution_context()
    for target_name, archive_name in sorted(target_archives.items()):
        report_path = assets_dir / f"{archive_name}.verification.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("schema_version") != 2 or report.get("target") != target_name:
            raise RuntimeError(f"Invalid verification identity in {report_path.name}.")
        contract = report.get("contract")
        if (
            not isinstance(contract, dict)
            or contract.get("verification_contract_version") != 2
            or set(contract.get("licenses", [])) != LICENSE_NAMES
            or contract.get("anamorphic_normalization") is not True
        ):
            raise RuntimeError(f"Invalid verification contract in {report_path.name}.")
        archive = report.get("archive")
        if not isinstance(archive, dict) or archive.get("name") != archive_name:
            raise RuntimeError(f"Invalid verified archive in {report_path.name}.")
        actual_digest = sha256_file(assets_dir / archive_name)
        if archive.get("sha256") != actual_digest:
            raise RuntimeError(f"Verification digest mismatch in {report_path.name}.")
        execution = report.get("execution")
        if not isinstance(execution, dict) or not all(
            execution.get(key)
            for key in ("repository", "commit", "github_run_id", "runner_os", "runner_arch")
        ):
            raise RuntimeError(f"Missing execution evidence in {report_path.name}.")
        if release_execution["github_run_id"] != "local":
            for key in (
                "repository",
                "commit",
                "github_run_id",
                "github_run_attempt",
            ):
                if execution.get(key) != release_execution[key]:
                    raise RuntimeError(
                        f"Execution {key} mismatch in {report_path.name}."
                    )
            expected_runner = EXPECTED_RUNNER_CONTEXT[target_name]
            for key, expected_value in expected_runner.items():
                if execution.get(key) != expected_value:
                    raise RuntimeError(
                        f"Execution {key} in {report_path.name} is "
                        f"{execution.get(key)}; expected {expected_value}."
                    )
        observed = report.get("observed")
        target = data["targets"][target_name]  # type: ignore[index]
        assert isinstance(target, dict)
        if not isinstance(observed, dict):
            raise RuntimeError(f"Missing observations in {report_path.name}.")
        if observed.get("binary_version") != _expected_binary_version(target, data):
            raise RuntimeError(f"Binary version mismatch in {report_path.name}.")
        architectures = observed.get("architectures")
        if not isinstance(architectures, dict) or set(architectures.values()) != {
            target["architecture"]
        }:
            raise RuntimeError(f"Architecture evidence mismatch in {report_path.name}.")
        scores = observed.get("vmaf_scores")
        if not isinstance(scores, dict) or set(scores) != {
            model for model, *_ in VMAF_MODELS
        }:
            raise RuntimeError(f"VMAF evidence mismatch in {report_path.name}.")
        if any(
            not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 100
            for score in scores.values()
        ):
            raise RuntimeError(f"Invalid VMAF score evidence in {report_path.name}.")
        probes = observed.get("vmaf_probes")
        expected_probes = {
            model: {"fps": fps, "frames": frames}
            for model, _, _, fps, frames in VMAF_MODELS
        }
        if not isinstance(probes, dict) or set(probes) != set(expected_probes):
            raise RuntimeError(f"VMAF temporal evidence mismatch in {report_path.name}.")
        for model, expected_probe in expected_probes.items():
            probe = probes.get(model)
            if not isinstance(probe, dict) or any(
                probe.get(key) != value for key, value in expected_probe.items()
            ):
                raise RuntimeError(f"VMAF temporal evidence mismatch in {report_path.name}.")
            score = probe.get("score")
            if not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 100:
                raise RuntimeError(f"Invalid VMAF score evidence in {report_path.name}.")
        anamorphic = observed.get("anamorphic_normalization")
        if not isinstance(anamorphic, dict) or anamorphic.get("luma_min", 0) < 800:
            raise RuntimeError(f"Anamorphic normalization evidence mismatch in {report_path.name}.")
        if observed.get("encoder_smokes") != ["libx265", "libsvtav1"]:
            raise RuntimeError(f"Encoder evidence mismatch in {report_path.name}.")
        reports.append(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "SHA256SUMS").write_text(
        "".join(f"{entry['sha256']}  {entry['name']}\n" for entry in assets),
        encoding="utf-8",
    )
    provenance = {
        "schema_version": 2,
        "release_execution": release_execution,
        "sources": data,
        "macos_source_lock": load_macos_source_lock(),
        "assets": assets,
        "verification_reports": reports,
    }
    (output_dir / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and validate pinned FFmpeg bundles.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config")
    for name in ("fetch", "verify", "package", "verify-package"):
        command = subparsers.add_parser(name)
        command.add_argument("--target", required=True, choices=sorted(TARGETS))
        if name == "fetch":
            command.add_argument("--output", required=True, type=Path)
        else:
            command.add_argument("--bin-dir", required=True, type=Path)
            if name in {"package", "verify-package"}:
                command.add_argument("--output", required=True, type=Path)
    metadata = subparsers.add_parser("release-metadata")
    metadata.add_argument("--assets-dir", required=True, type=Path)
    metadata.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    data = load_config()
    validate_config(data)
    validate_macos_source_lock(load_macos_source_lock())
    if args.command == "validate-config":
        return 0
    if args.command == "fetch":
        print(fetch_target(args.target, args.output, data))
    elif args.command == "verify":
        verify_target(args.target, args.bin_dir, data)
    elif args.command == "package":
        print(package_target(args.target, args.bin_dir, args.output, data))
    elif args.command == "verify-package":
        print(verify_and_package_target(args.target, args.bin_dir, args.output, data))
    else:
        release_metadata(args.assets_dir, args.output, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
