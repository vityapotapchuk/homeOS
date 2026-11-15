#!/bin/bash
# Runs inside the image (chroot) while it is built. See image/build.sh.
set -euo pipefail
VARIANT=$1
SRC=/opt/homeos-src
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8

# services must not start inside the chroot
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod +x /usr/sbin/policy-rc.d

case "$VARIANT" in
    zero2w) FLAGS="--web-only --lite"; DETECT=0 ;;
    pi5)    FLAGS="--web"; DETECT=1 ;;
esac

apt-get update
apt-get install -y git curl ca-certificates gnupg openssl
apt-get install -y gh || echo "gh is not in the package lists; updates from Settings will need a git login another way."

echo "==> HomeOS packages ($FLAGS)"
# shellcheck disable=SC2086
"$SRC/install.sh" --packages-only $FLAGS

echo "==> Tailscale"
. /etc/os-release
curl -fsSL "https://pkgs.tailscale.com/stable/debian/$VERSION_CODENAME.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL "https://pkgs.tailscale.com/stable/debian/$VERSION_CODENAME.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list
apt-get update
apt-get install -y tailscale
systemctl enable tailscaled.service

if [ "$DETECT" -eq 1 ]; then
    echo "==> Motion detection (prepared now, switched on at first boot)"
    apt-get install -y python3-venv libgl1 libglib2.0-0
    rm -rf /opt/esp32cam
    # the detector is a separate project: give its git address in DETECT_REPO to include it
    [ -n "${DETECT_REPO:-}" ] || { echo "DETECT_REPO not set: motion detection left out"; DETECT=0; }
    [ "$DETECT" -eq 1 ] && git clone --depth 1 "$DETECT_REPO" /opt/esp32cam
    [ "$DETECT" -eq 1 ] && python3 -m venv /opt/esp32cam/.venv
    [ "$DETECT" -eq 1 ] && /opt/esp32cam/.venv/bin/python -m pip install --upgrade pip
    [ "$DETECT" -eq 1 ] && /opt/esp32cam/.venv/bin/python -m pip install numpy opencv-python-headless onnxruntime flask requests imageio-ffmpeg
fi

echo "==> First-boot setup"
cat > /etc/homeos-image <<CONF
VARIANT=$VARIANT
FLAGS="$FLAGS"
DETECT=$DETECT
CONF
install -m 0755 "$SRC/image/firstboot.sh" /usr/local/sbin/homeos-firstboot
install -m 0644 "$SRC/image/homeos-firstboot.service" /etc/systemd/system/homeos-firstboot.service
systemctl enable homeos-firstboot.service
install -m 0600 "$SRC/image/homeos.txt" /boot/firmware/homeos.txt

echo homeos > /etc/hostname
sed -i 's/^127\.0\.1\.1.*/127.0.1.1\thomeos/' /etc/hosts
grep -q '^127\.0\.1\.1' /etc/hosts || echo -e "127.0.1.1\thomeos" >> /etc/hosts

apt-get clean
rm -rf /var/lib/apt/lists/* /root/.cache
rm -f /usr/sbin/policy-rc.d
echo "==> Done"
