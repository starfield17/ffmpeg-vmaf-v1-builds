from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.bundle_tool import (
    EXPECTED_RUNNER_CONTEXT,
    MACOS_SOURCE_FILES,
    REQUIRED_FILTERS,
    TARGETS,
    VMAF_MODELS,
    _parse_filter_names,
    _parse_vmaf_score,
    _vmaf_graph,
    extract_binaries,
    load_config,
    load_macos_source_lock,
    release_metadata,
    validate_config,
    validate_macos_source_lock,
    write_verification_report,
)


class BundleConfigTestCase(unittest.TestCase):
    def test_config_has_five_targets_and_pinned_release(self) -> None:
        data = load_config()
        validate_config(data)
        targets = data["targets"]
        self.assertIsInstance(targets, dict)
        assert isinstance(targets, dict)
        self.assertEqual(set(targets), TARGETS)
        self.assertEqual(data["release_tag"], "ffmpeg-9.0.1-vmaf-v1.0.16-r3")
        self.assertEqual(data["verification_contract_version"], 3)
        licenses = data["licenses"]
        self.assertIsInstance(licenses, list)
        assert isinstance(licenses, list)
        self.assertEqual(
            {entry["name"] for entry in licenses},
            {"LICENSE.md", "COPYING.GPLv3", "VMAF-LICENSE.txt"},
        )
        macos_build = data["macos_build"]
        self.assertIsInstance(macos_build, dict)
        assert isinstance(macos_build, dict)
        self.assertEqual(
            macos_build["binary_version"], "9.0.1-https://www.martin-riedl.de"
        )
        windows = targets["windows-x86_64"]
        self.assertIsInstance(windows, dict)
        assert isinstance(windows, dict)
        self.assertEqual(str(windows["sha256"])[:8], "2b17b617")
        self.assertEqual(
            windows["binary_version"], "n9.0.1-6-g9d4ca21220-20260818"
        )
        self.assertEqual(
            windows["ffmpeg_commit"],
            "9d4ca21220bfd3f06fc8bfc90ddf0f6d0a484611",
        )
        self.assertEqual(
            windows["libvmaf_commit"],
            "e80d6c593e6e2327687dccd00b7cc9c91036d79f",
        )
        self.assertIn("d5e1920c45f0cdc418a39754e33dedd9483063a6", windows["upstream_build_recipe"])

    def test_production_model_contract_is_exact(self) -> None:
        self.assertEqual(
            [model for model, *_ in VMAF_MODELS],
            [
                "vmaf_v1.0.16_3d0h",
                "vmaf_v1.0.16_1d5h_2160",
                "vmaf_v1.0.16_hfr_3d0h",
                "vmaf_v1.0.16_hfr_1d5h_2160",
            ],
        )
        graph = _vmaf_graph(*VMAF_MODELS[0][:3])
        for token in (
            "trunc(iw*sar/2)*2",
            "scale=1920:1080",
            "force_original_aspect_ratio=decrease",
            "force_divisible_by=2",
            "flags=bicubic",
            "pad=1920:1080",
            "trunc((ow-iw)/4)*2",
            "setsar=1",
            "format=yuv420p10le",
            "cambi.enc_width=320",
            "cambi.enc_height=180",
            "cambi.enc_bitdepth=8",
            "libvmaf",
        ):
            self.assertIn(token, graph)

        self.assertEqual(
            [(model, fps, frames) for model, _, _, fps, frames in VMAF_MODELS],
            [
                ("vmaf_v1.0.16_3d0h", 30, 6),
                ("vmaf_v1.0.16_1d5h_2160", 30, 6),
                ("vmaf_v1.0.16_hfr_3d0h", 60, 12),
                ("vmaf_v1.0.16_hfr_1d5h_2160", 60, 12),
            ],
        )

    def test_vmaf_score_parser_rejects_nonfinite_and_out_of_range_values(self) -> None:
        self.assertEqual(_parse_vmaf_score("VMAF score: 99.5", "model"), 99.5)
        for score in ("nan", "Infinity", "-0.1", "100.1"):
            with self.subTest(score=score), self.assertRaisesRegex(
                RuntimeError, "invalid score"
            ):
                _parse_vmaf_score(f"VMAF score: {score}", "model")

    def test_filter_parser_matches_exact_capability_names(self) -> None:
        output = """
            .. libvmaf           VV->V      Calculate VMAF.
            .. siti              V->V       Calculate SI/TI.
            .. scdet             V->V       Detect scene changes.
            .. notsiti           V->V       Unrelated filter.
        """
        names = _parse_filter_names(output)
        self.assertTrue(set(REQUIRED_FILTERS).issubset(names))
        self.assertNotIn("notsc det", names)
        self.assertNotIn("not", names)

    def test_macos_source_lock_is_complete_and_hash_pinned(self) -> None:
        source_lock = load_macos_source_lock()
        validate_macos_source_lock(source_lock)
        files = source_lock["files"]
        self.assertIsInstance(files, dict)
        assert isinstance(files, dict)
        self.assertEqual(set(files), MACOS_SOURCE_FILES)
        self.assertEqual(
            files["ffmpeg.tar.bz2"],
            "3317ad21d5e2c2eab8423ae6f49b12463960055925f39a025496964afeb9042c",
        )

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

    def test_release_metadata_requires_five_matching_verification_reports(self) -> None:
        data = load_config()
        targets = data["targets"]
        assert isinstance(targets, dict)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            assets.mkdir()
            for target_name, raw in targets.items():
                assert isinstance(raw, dict)
                extension = "zip" if raw["format"] == "zip" else "tar.xz"
                archive = assets / (
                    f"ffmpeg-{data['bundle_version']}-{target_name}.{extension}"
                )
                archive.write_bytes(target_name.encode("ascii"))
                target = targets[target_name]
                assert isinstance(target, dict)
                source_version = (
                    target["binary_version"]
                    if target["source_kind"] == "mirror"
                    else "9.0.1-https://www.martin-riedl.de"
                )
                runner = EXPECTED_RUNNER_CONTEXT[target_name]
                with patch.dict(
                    "os.environ",
                    {
                        "GITHUB_REPOSITORY": "starfield17/test-builds",
                        "GITHUB_SHA": "a" * 40,
                        "GITHUB_RUN_ID": "42",
                        "GITHUB_RUN_ATTEMPT": "1",
                        "RUNNER_NAME": f"runner-{target_name}",
                        "RUNNER_OS": runner["runner_os"],
                        "RUNNER_ARCH": runner["runner_arch"],
                    },
                    clear=True,
                ):
                    write_verification_report(
                        target_name,
                        archive,
                        data,
                        {
                            "binary_version": source_version,
                            "architectures": {
                                "ffmpeg": target["architecture"],
                                "ffprobe": target["architecture"],
                            },
                            "required_filters": list(REQUIRED_FILTERS),
                            "available_filters": list(REQUIRED_FILTERS),
                            "scout_smoke": {
                                "frames": 12,
                                "si_samples": 12,
                                "ti_samples": 12,
                                "si_max": 184.31,
                                "ti_max": 56.74,
                                "scene_score_max": 57.681,
                                "scene_times": [0.5],
                            },
                            "vmaf_scores": {
                                model: 100.0 for model, *_ in VMAF_MODELS
                            },
                            "vmaf_probes": {
                                model: {"fps": fps, "frames": frames, "score": 100.0}
                                for model, _, _, fps, frames in VMAF_MODELS
                            },
                            "anamorphic_normalization": {
                                "source_geometry": "720x576 SAR 64:45",
                                "output_geometry": "1920x1080 SAR 1:1",
                                "luma_min": 940,
                            },
                            "encoder_smokes": ["libx265", "libsvtav1"],
                            "runtime_dependencies": {},
                        },
                    )

            metadata = root / "metadata"
            with patch.dict(
                "os.environ",
                {
                    "GITHUB_REPOSITORY": "starfield17/test-builds",
                    "GITHUB_SHA": "a" * 40,
                    "GITHUB_RUN_ID": "42",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "RUNNER_NAME": "release-runner",
                    "RUNNER_OS": "Linux",
                    "RUNNER_ARCH": "X64",
                },
                clear=True,
            ):
                release_metadata(assets, metadata, data)
            provenance = json.loads(
                (metadata / "provenance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(provenance["verification_reports"]), 5)
            self.assertEqual(provenance["schema_version"], 2)
            self.assertEqual(
                set(provenance["macos_source_lock"]["files"]), MACOS_SOURCE_FILES
            )
            self.assertEqual(
                len((metadata / "SHA256SUMS").read_text(encoding="utf-8").splitlines()),
                10,
            )

            report_path = next(assets.glob("*linux-arm64*.verification.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["execution"]["runner_arch"] = "X64"
            report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            with (
                patch.dict(
                    "os.environ",
                    {
                        "GITHUB_REPOSITORY": "starfield17/test-builds",
                        "GITHUB_SHA": "a" * 40,
                        "GITHUB_RUN_ID": "42",
                        "GITHUB_RUN_ATTEMPT": "1",
                        "RUNNER_OS": "Linux",
                        "RUNNER_ARCH": "X64",
                    },
                    clear=True,
                ),
                self.assertRaisesRegex(RuntimeError, "runner_arch"),
            ):
                release_metadata(assets, root / "rejected", data)

    def test_release_workflow_uses_all_native_runners_and_atomic_publish(self) -> None:
        workflow = (
            Path(__file__).resolve().parent.parent / ".github/workflows/build-release.yml"
        ).read_text(encoding="utf-8")
        for token in (
            "macos-15",
            "windows-2022",
            "windows-11-arm",
            "ubuntu-22.04",
            "ubuntu-24.04-arm",
            "needs: [macos, btbntargets]",
            "release-metadata",
            "github.ref == 'refs/heads/main'",
            "gh release create \"$tag\" --target \"$GITHUB_SHA\" --draft",
            "gh release download",
            "diff -u workdir/local-release-sha256 workdir/remote-release-sha256",
            "gh release edit \"$tag\" --draft=false",
            "draft_created=true",
            "refusing to clean a draft not owned by this run",
            "--cleanup-tag",
        ):
            self.assertIn(token, workflow)
        self.assertNotIn("actions/checkout@v", workflow)
        self.assertNotIn("actions/setup-python@v", workflow)
        self.assertNotIn("actions/upload-artifact@v", workflow)
        self.assertNotIn("actions/download-artifact@v", workflow)

        build_script = (
            Path(__file__).resolve().parent.parent / "scripts/build_macos.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("Latest failed build log", build_script)

    def test_macos_patch_enforces_locked_inputs_and_float_features(self) -> None:
        patch = (
            Path(__file__).resolve().parent.parent / "patches/macos-vmaf-v1.patch"
        ).read_text(encoding="utf-8")
        for token in (
            "-# detect existing installation of cmake",
            "VMAF_SOURCE_LOCK is required",
            "unlocked build input",
            "SHA-256 mismatch",
            "meson-1.12.0-py3-none-any.whl",
            "-Dbuilt_in_models=true -Denable_float=true",
        ):
            self.assertIn(token, patch)
        self.assertNotIn("+    python3 -m virtualenv", patch)
        self.assertNotIn("+        MESON_VERSION=$(meson -v", patch)


if __name__ == "__main__":
    unittest.main()
