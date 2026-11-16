#!/usr/bin/env bash
# Removes HomeOS. Installed packages, automatic console login and your
# settings in ~/.config/homeos are left alone.
#
#   sudo ./uninstall.sh

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run this with sudo:  sudo ./uninstall.sh" >&2; exit 1; }

TARGET_USER=${HOMEOS_USER:-${SUDO_USER:-}}
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ] && id "$TARGET_USER" >/dev/null 2>&1; then
    TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
    for f in "$TARGET_HOME/.bash_profile" "$TARGET_HOME/.profile"; do
        if [ -f "$f" ]; then
            sed -i '/^# >>> homeos >>>$/,/^# <<< homeos <<<$/d' "$f"
        fi
    done
fi

if [ -f /etc/systemd/system/homeos-radio.service ]; then
    systemctl disable --now homeos-radio.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/homeos-radio.service
fi
if [ -f /etc/systemd/system/homeos-web.service ]; then
    systemctl disable --now homeos-web.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/homeos-web.service
    systemctl daemon-reload >/dev/null 2>&1 || true
fi
rm -f /etc/sudoers.d/homeos-update
rm -f /usr/local/bin/homeos /usr/local/bin/homeos-shell /usr/local/bin/homeos-web
rm -f /etc/systemd/system/getty@tty1.service.d/homeos-autologin.conf
rm -rf /opt/homeos/shell /opt/homeos/config /opt/homeos/bin
rmdir /opt/homeos 2>/dev/null || true

echo "HomeOS was removed. After a restart you get the plain console."
