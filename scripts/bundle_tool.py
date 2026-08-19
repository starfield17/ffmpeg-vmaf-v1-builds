from __future__ import annotations

import argparse
import hashlib
import json
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
TARGETS = {
    "windows-x86_64",
    "windows-arm64",
    "linux-x86_64",
    "linux-arm64",
    "macos-x86_64",
    "macos-arm64",
}
VMAF_MODELS = (
    ("vmaf_v1.0.16_3d0h", 1920, 1080),
    ("vmaf_v1.0.16_1d5h_2160", 3840, 2160),
    ("vmaf_v1.0.16_hfr_3d0h", 1920, 1080),
    ("vmaf_v1.0.16_hfr_1d5h_2160", 3840, 2160),
)
MACHINE_TYPES = {
    "x86_64": {"pe": 0x8664, "elf": 0x3E, "macho": 0x01000007},
    "arm64": {"pe": 0xAA64, "elf": 0xB7, "macho": 0x0100000C},
}


def load_config(path: Path = CONFIG_PATH) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported source config schema.")
    targets = data.get("targets")
    if not isinstance(targets, dict) or set(targets) != TARGETS:
        raise ValueError(f"Source config must define exactly: {sorted(TARGETS)}")
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


def validate_config(data: dict[str, object]) -> None:
    if data.get("release_tag") != "ffmpeg-" + str(data.get("bundle_version")):
        raise ValueError("release_tag must be ffmpeg-<bundle_version>.")
    licenses = data.get("licenses")
    if not isinstance(licenses, list) or len(licenses) != 2:
        raise ValueError("Exactly two FFmpeg license inputs are required.")
    for entry in licenses:
        if not isinstance(entry, dict):
            raise ValueError("Invalid license entry.")
        _validate_digest(entry.get("sha256"), str(entry.get("name")))
    targets = data["targets"]
    assert isinstance(targets, dict)
    for name, raw in targets.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid target entry: {name}")
        if raw.get("source_kind") == "mirror":
            _validate_digest(raw.get("sha256"), name)
            if raw.get("format") not in {"zip", "tar.xz"}:
                raise ValueError(f"Unsupported archive format for {name}.")
        elif raw.get("source_kind") != "build":
            raise ValueError(f"Unsupported source kind for {name}.")


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


def _vmaf_graph(model: str, width: int, height: int) -> str:
    normalize = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=bicubic,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,format=yuv420p10le,settb=AVTB,setpts=PTS-STARTPTS"
    )
    model_config = (
        f"version={model}\\:cambi.enc_width=320\\:"
        "cambi.enc_height=180\\:cambi.enc_bitdepth=8"
    )
    return (
        f"[0:v]{normalize}[dist];[1:v]{normalize}[ref];"
        f"[dist][ref]libvmaf=model='{model_config}':n_threads=2:n_subsample=1"
    )


def verify_target(target_name: str, bin_dir: Path, data: dict[str, object]) -> None:
    target = data["targets"][target_name]  # type: ignore[index]
    assert isinstance(target, dict)
    suffix = ".exe" if target["platform"] == "windows" else ""
    ffmpeg = bin_dir / f"ffmpeg{suffix}"
    ffprobe = bin_dir / f"ffprobe{suffix}"
    for binary in (ffmpeg, ffprobe):
        actual = binary_architecture(binary)
        if actual != target["architecture"]:
            raise RuntimeError(f"{binary} is {actual}; expected {target['architecture']}")

    version = _run([str(ffmpeg), "-hide_banner", "-version"])
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

    for model, width, height in VMAF_MODELS:
        output = _run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=1:duration=1",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=1:duration=1",
                "-filter_complex",
                _vmaf_graph(model, width, height),
                "-an",
                "-f",
                "null",
                "-",
            ]
        )
        if "VMAF score:" not in output:
            raise RuntimeError(f"VMAF model {model} did not produce a score.")

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

    if target["platform"] == "macos":
        linkage = _run(["otool", "-L", str(ffmpeg)])
        if "/opt/homebrew" in linkage or "/usr/local" in linkage:
            raise RuntimeError("macOS FFmpeg has a package-manager runtime dependency.")


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
            "schema_version": 1,
            "bundle_version": data["bundle_version"],
            "target": target_name,
            "source": target,
            "macos_build": data["macos_build"] if target["source_kind"] == "build" else None,
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


def release_metadata(assets_dir: Path, output_dir: Path, data: dict[str, object]) -> None:
    expected = {
        f"ffmpeg-{data['bundle_version']}-{name}.{'zip' if raw['format'] == 'zip' else 'tar.xz'}"
        for name, raw in data["targets"].items()  # type: ignore[union-attr]
    }
    found = {path.name for path in assets_dir.iterdir() if path.is_file()}
    if found != expected:
        raise RuntimeError(f"Release assets are {sorted(found)}; expected {sorted(expected)}")
    assets = [
        {"name": name, "sha256": sha256_file(assets_dir / name)} for name in sorted(found)
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "SHA256SUMS").write_text(
        "".join(f"{entry['sha256']}  {entry['name']}\n" for entry in assets),
        encoding="utf-8",
    )
    provenance = {"schema_version": 1, "sources": data, "assets": assets}
    (output_dir / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and validate pinned FFmpeg bundles.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config")
    for name in ("fetch", "verify", "package"):
        command = subparsers.add_parser(name)
        command.add_argument("--target", required=True, choices=sorted(TARGETS))
        if name == "fetch":
            command.add_argument("--output", required=True, type=Path)
        else:
            command.add_argument("--bin-dir", required=True, type=Path)
            if name == "package":
                command.add_argument("--output", required=True, type=Path)
    metadata = subparsers.add_parser("release-metadata")
    metadata.add_argument("--assets-dir", required=True, type=Path)
    metadata.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    data = load_config()
    validate_config(data)
    if args.command == "validate-config":
        return 0
    if args.command == "fetch":
        print(fetch_target(args.target, args.output, data))
    elif args.command == "verify":
        verify_target(args.target, args.bin_dir, data)
    elif args.command == "package":
        print(package_target(args.target, args.bin_dir, args.output, data))
    else:
        release_metadata(args.assets_dir, args.output, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
