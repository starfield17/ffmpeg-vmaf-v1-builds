#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 BUILD_ROOT" >&2
    exit 2
fi

repository_root="$(cd "$(dirname "$0")/.." && pwd)"
build_parent="$(dirname "$1")"
build_name="$(basename "$1")"
mkdir -p "$build_parent"
build_root="$(cd "$build_parent" && pwd)/$build_name"
recipe_root="$build_root/recipe"
compile_root="$build_root/compile"
upstream_commit="f63b8aab8f5ce1a067da86ba69e34a36a7e217e5"

report_failure() {
    status=$?
    if [[ $status -ne 0 && -d "$compile_root/log" ]]; then
        log_files=("$compile_root"/log/*.log)
        if [[ -f "${log_files[0]}" ]]; then
            latest_log="$(ls -t "${log_files[@]}" | head -n 1)"
            echo "::group::Latest failed build log: $latest_log"
            tail -n 240 "$latest_log"
            echo "::endgroup::"
        fi
    fi
    exit "$status"
}
trap report_failure EXIT

if [[ -e "$build_root" ]]; then
    echo "build root already exists: $build_root" >&2
    exit 1
fi

mkdir -p "$build_root"
git clone --filter=blob:none \
    https://git.martin-riedl.de/ffmpeg/build-script.git "$recipe_root"
git -C "$recipe_root" checkout "$upstream_commit"
git -C "$recipe_root" apply "$repository_root/patches/macos-vmaf-v1.patch"

mkdir "$compile_root"
cd "$compile_root"
export VMAF_SOURCE_LOCK="$repository_root/config/macos-source-lock.json"
"$recipe_root/build.sh" \
    -SKIP_TEST=YES \
    -SKIP_LIBBLURAY=YES \
    -SKIP_SNAPPY=YES \
    -SKIP_SRT=YES \
    -SKIP_ZIMG=YES \
    -SKIP_ZVBI=YES \
    -SKIP_AOM=YES \
    -SKIP_DAV1D=YES \
    -SKIP_OPEN_H264=YES \
    -SKIP_OPEN_JPEG=YES \
    -SKIP_RAV1E=YES \
    -SKIP_LIBTHEORA=YES \
    -SKIP_VPX=YES \
    -SKIP_VVENC=YES \
    -SKIP_LIBWEBP=YES \
    -SKIP_X264=YES \
    -SKIP_X265_MULTIBIT=YES \
    -SKIP_LAME=YES \
    -SKIP_OPUS=YES \
    -SKIP_LIBVORBIS=YES \
    -SKIP_LIBKLVANC=YES

echo "$compile_root/out/bin"
