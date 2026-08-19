from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.bundle_tool import (
    TARGETS,
    VMAF_MODELS,
    _vmaf_graph,
    extract_binaries,
    load_config,
    validate_config,
)


class BundleConfigTestCase(unittest.TestCase):
    def test_config_has_six_targets_and_pinned_release(self) -> None:
        data = load_config()
        validate_config(data)
        self.assertEqual(set(data["targets"]), TARGETS)
        self.assertEqual(data["release_tag"], "ffmpeg-9.0.1-vmaf-v1.0.16-r1")
        self.assertEqual(data["targets"]["windows-x86_64"]["sha256"][:8], "2b17b617")

    def test_production_model_contract_is_exact(self) -> None:
        self.assertEqual(
            [model for model, _, _ in VMAF_MODELS],
            [
                "vmaf_v1.0.16_3d0h",
                "vmaf_v1.0.16_1d5h_2160",
                "vmaf_v1.0.16_hfr_3d0h",
                "vmaf_v1.0.16_hfr_1d5h_2160",
            ],
        )
        graph = _vmaf_graph(*VMAF_MODELS[0])
        for token in (
            "scale=1920:1080",
            "force_original_aspect_ratio=decrease",
            "flags=bicubic",
            "pad=1920:1080",
            "setsar=1",
            "format=yuv420p10le",
            "cambi.enc_width=320",
            "cambi.enc_height=180",
            "cambi.enc_bitdepth=8",
            "libvmaf",
        ):
            self.assertIn(token, graph)

    def test_archive_extraction_only_accepts_expected_binary_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bundle.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("nested/bin/ffmpeg", "ffmpeg")
                package.writestr("nested/bin/ffprobe", "ffprobe")
                package.writestr("nested/bin/README.txt", "ignored")
            output = root / "output"
            extract_binaries(archive, "zip", output, "macos")
            self.assertEqual(
                {path.name for path in (output / "bin").iterdir()}, {"ffmpeg", "ffprobe"}
            )

    def test_release_workflow_uses_all_native_runners_and_atomic_publish(self) -> None:
        workflow = (
            Path(__file__).resolve().parent.parent / ".github/workflows/build-release.yml"
        ).read_text(encoding="utf-8")
        for token in (
            "macos-15-intel",
            "macos-15",
            "windows-2022",
            "windows-11-arm",
            "ubuntu-22.04",
            "ubuntu-24.04-arm",
            "needs: [macos, btbntargets]",
            "release-metadata",
            "gh release create",
        ):
            self.assertIn(token, workflow)


if __name__ == "__main__":
    unittest.main()
