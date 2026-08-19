# FFmpeg VMAF v1 builds

Pinned FFmpeg dependency bundles for
[Video Compressor](https://github.com/starfield17/Video_compress_Encoder_gui).
The release contract covers native x86_64 and arm64 targets for Windows,
Linux, and macOS.

macOS binaries are built from FFmpeg 9.0.1 and libvmaf 3.2.0 with the VMAF v1
models and floating-point feature extractors enabled. Windows and Linux
binaries are pinned BtbN GPL builds, mirrored only after checksum validation
and execution on their native GitHub-hosted runners.

Every target must provide:

- `ffmpeg` and `ffprobe` for the declared native architecture;
- GPLv3 configuration with `libx265` and `libsvtav1`;
- CPU `libvmaf` capable of running all four pinned VMAF v1.0.16 production
  models through 10-bit display normalization and CAMBI encode metadata.

The repository orchestration is MIT-licensed. Released FFmpeg GPL bundles are
distributed under their included upstream license notices. Source provenance,
archive SHA-256 values, and upstream inputs are attached to every release.

## Release

Run the **Build and release bundles** workflow manually. It creates the exact
tag declared by `config/sources.json` only after all six native validation jobs
succeed. Existing tags and releases are never replaced.

