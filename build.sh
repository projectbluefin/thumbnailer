#!/usr/bin/env bash
# Build the ffmpeg-thumbnailer bundle.
#
#   ./build.sh              -> vaapi variant  (AMD + Intel)
#   ./build.sh nvidia       -> nvidia variant (NVIDIA + AMD + Intel)
#   ./build.sh both
#
# podman builds the OCI image, apptainer turns it into a SIF. Both steps run
# unprivileged: no --fakeroot, no setuid apptainer, no root. The def file has no
# %post for exactly this reason -- every package install happens in the Containerfile.
set -euo pipefail

cd "$(dirname "$0")"

: "${TMPDIR:=/var/tmp}"
export APPTAINER_TMPDIR="${APPTAINER_TMPDIR:-$TMPDIR}"

build_variant() {
    local variant="$1"
    local image="localhost/ffmpeg-thumbnailer:${variant}"
    local archive="${TMPDIR}/ffmpeg-thumbnailer-${variant}.oci"
    local sif="ffmpeg-thumbnailer-${variant}.sif"

    echo "==> podman build (${variant})"
    podman build -t "$image" -f "Containerfile.${variant}" .

    echo "==> export to OCI archive"
    rm -f "$archive"
    podman save --format oci-archive -o "$archive" "$image"

    echo "==> apptainer build"
    apptainer build -F \
        --build-arg "OCI_ARCHIVE=${archive}" \
        --build-arg "VARIANT=${variant}" \
        "$sif" ffmpeg-thumbnailer.def

    rm -f "$archive"
    echo "==> built $sif ($(du -h "$sif" | cut -f1))"
}

check_squashfuse() {
    if ! command -v squashfuse >/dev/null 2>&1 && ! command -v squashfuse_ll >/dev/null 2>&1; then
        cat >&2 <<'EOF'

warning: squashfuse is not installed.

Apptainer mounts a SIF through squashfuse. Without it, every start extracts the
whole image into $APPTAINER_TMPDIR instead -- hundreds of megabytes of copying,
and a hard failure if that path is a small tmpfs.

  Fedora/Bluefin:  sudo dnf install squashfuse   (or: brew install squashfuse)
  Debian/Ubuntu:   sudo apt install squashfuse

EOF
    fi
}

check_squashfuse

case "${1:-vaapi}" in
    vaapi)  build_variant vaapi ;;
    nvidia) build_variant nvidia ;;
    both)   build_variant vaapi; build_variant nvidia ;;
    *)      echo "usage: $0 [vaapi|nvidia|both]" >&2; exit 1 ;;
esac
