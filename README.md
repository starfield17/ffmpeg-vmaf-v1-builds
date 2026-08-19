# FFmpeg VMAF v1 builds

Pinned FFmpeg dependency bundles for
[Video Compressor](https://github.com/starfield17/Video_compress_Encoder_gui).
The release contract covers native x86_64 and arm64 targets for Windows and
Linux, plus native arm64 for macOS. Intel macOS is not supported.

macOS binaries are built from FFmpeg 9.0.1 and libvmaf 3.2.0 with the VMAF v1
models and floating-point feature extractors enabled. Windows and Linux
binaries are pinned BtbN GPL builds, mirrored only after checksum validation
and execution on their native GitHub-hosted runners. They are not rebuilt by
this repository; the exact FFmpeg, libvmaf, BtbN recipe, and upstream release
commits are recorded in the release provenance.

Every target must provide:

- `ffmpeg` and `ffprobe` for the declared native architecture;
- GPLv3 configuration with `libx265` and `libsvtav1`;
- CPU `libvmaf` capable of running all four pinned VMAF v1.0.16 production
  models through 30/60 fps multi-frame probes, SAR-aware 10-bit display
  normalization, and CAMBI encode metadata.
- Exact `libvmaf`, `siti`, and `scdet` filters. Native verification also runs a
  synthetic Scout smoke that proves SI, TI, and scene-boundary metadata are
  emitted before a bundle can be released.

The repository orchestration is MIT-licensed. Released FFmpeg GPL bundles are
distributed under their included upstream license notices. Source provenance,
archive SHA-256 values, and upstream inputs are attached to every release.
Each bundle includes the FFmpeg license files and Netflix VMAF's
BSD-2-Clause-Patent license.
The macOS build rejects any source download not listed in
`config/macos-source-lock.json`, and every GitHub Action is pinned by commit.

## Release

Run the **Build and release bundles** workflow manually. It creates the exact
tag declared by `config/sources.json` only after all five native validation jobs
succeed. It uploads to a draft, downloads and compares every published asset,
and only then makes the Release public. Existing tags and releases are never
replaced.
