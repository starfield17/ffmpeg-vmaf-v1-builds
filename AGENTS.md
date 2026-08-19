# FFmpeg VMAF v1 bundle repository guide

## Purpose and map

This repository produces the pinned FFmpeg dependency bundles consumed by
Video Compressor. `config/sources.json` is the source-of-truth for release and
mirror inputs; `config/macos-source-lock.json` locks every macOS download.
`scripts/` owns fetch, validation, packaging, and the macOS source build.
`patches/` contains narrowly scoped upstream patches.

## Invariants

- Never publish an archive unless its native target job passes the exact four
  VMAF v1 model probes, 10-bit display normalization, CAMBI metadata parsing,
  `libx265`, and `libsvtav1` smoke tests.
- macOS binaries are built here. Windows and Linux binaries retain explicit
  BtbN provenance and are mirrored only after SHA-256 and native validation.
- All upstream commits, URLs, and hashes are pinned. Do not use floating
  `latest` downloads or model aliases.
- A release is an atomic six-target set. One failed target blocks publication.
- Keep build-provider details out of the verifier; it validates the common
  runtime contract from the normalized `bin/` directory.

## Canonical checks

```text
python -m compileall -q scripts test
python -m unittest discover -s test -p "test_*.py" -v
python scripts/bundle_tool.py validate-config
```
