#!/bin/bash
# Builds a HomeOS SD card image from the latest Raspberry Pi OS Lite (64-bit).
#
#   sudo image/build.sh zero2w      lite edition: web access only, no browser, no camera
#   sudo image/build.sh pi5         full edition: desktop, web access, Pi browser, motion detection
#
# Needs a Linux x86-64 or arm64 machine with root, loop devices, qemu-user-static
# (on x86-64), parted, e2fsprogs, zerofree and xz. GitHub Actions runs this for
# every images-* tag (see .github/workflows/images.yml). The finished image is
# written to out/.
set -euo pipefail

VARIANT=${1:-}
case "$VARIANT" in
    zero2w) GROW=1536M ;;
    pi5)    GROW=5G ;;
    *) echo "Usage: sudo $0 zero2w|pi5" >&2; exit 2 ;;
esac
[ "$(id -u)" -eq 0 ] || { echo "Run this with sudo." >&2; exit 1; }

HERE=$(cd "$(dirname "$0")/.." && pwd)
WORK=${WORK:-$HERE/work}
OUT=${OUT:-$HERE/out}
BASE_URL=${BASE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}
ROOT=$WORK/root
mkdir -p "$WORK" "$OUT" "$ROOT"

say() { printf '\n\033[1;35m==>\033[0m %s\n' "$*"; }

LOOP=""
cleanup() {
    set +e
    for m in boot/firmware dev/pts dev proc sys run; do
        mountpoint -q "$ROOT/$m" && umount -l "$ROOT/$m"
    done
    mountpoint -q "$ROOT" && umount -l "$ROOT"
    [ -n "$LOOP" ] && losetup -d "$LOOP"
}
trap cleanup EXIT

say "Downloading Raspberry Pi OS Lite (64-bit)"
if [ ! -f "$WORK/base.img" ]; then
    curl -fL --retry 3 -o "$WORK/base.img.xz" "$BASE_URL"
    xz -d -T0 -f "$WORK/base.img.xz"
fi
cp --sparse=always "$WORK/base.img" "$WORK/homeos.img"
IMG=$WORK/homeos.img

say "Making room for HomeOS ($GROW)"
truncate -s "+$GROW" "$IMG"
parted -s "$IMG" resizepart 2 100%
LOOP=$(losetup -fP --show "$IMG")
partprobe "$LOOP" 2>/dev/null || true
sleep 1
e2fsck -fy "${LOOP}p2" || true
resize2fs "${LOOP}p2"

say "Mounting the image"
mount "${LOOP}p2" "$ROOT"
mount "${LOOP}p1" "$ROOT/boot/firmware"
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
mount -t proc proc "$ROOT/proc"
mount -t sysfs sys "$ROOT/sys"
mount -t tmpfs tmpfs "$ROOT/run"
if [ "$(uname -m)" != aarch64 ]; then
    cp /usr/bin/qemu-aarch64-static "$ROOT/usr/bin/" 2>/dev/null || true
fi
mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.homeos" 2>/dev/null || true
cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"

say "Copying HomeOS into the image"
mkdir -p "$ROOT/opt/homeos-src"
tar -C "$HERE" --exclude=./work --exclude=./out -cf - . | tar -C "$ROOT/opt/homeos-src" -xf -

say "Setting up inside the image (this takes a while)"
chroot "$ROOT" /bin/bash /opt/homeos-src/image/customize.sh "$VARIANT"

say "Tidying up"
rm -f "$ROOT/etc/resolv.conf"
mv "$ROOT/etc/resolv.conf.homeos" "$ROOT/etc/resolv.conf" 2>/dev/null || true
rm -f "$ROOT/usr/bin/qemu-aarch64-static"
cleanup
trap - EXIT
LOOP=$(losetup -fP --show "$IMG")
zerofree "${LOOP}p2" || true          # free space compresses to almost nothing
losetup -d "$LOOP"
LOOP=""

NAME=homeos-$VARIANT-$(date -u +%Y-%m-%d)
say "Compressing $NAME.img.xz"
mv "$IMG" "$OUT/$NAME.img"
xz -T0 -6 -f "$OUT/$NAME.img"
(cd "$OUT" && sha256sum "$NAME.img.xz" > "$NAME.img.xz.sha256")
ls -lh "$OUT"
