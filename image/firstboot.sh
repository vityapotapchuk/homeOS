#!/bin/bash
# HomeOS first-boot setup (homeos-firstboot.service). Runs at every start until it
# has finished once: it waits for the user that Raspberry Pi Imager (or homeos.txt)
# creates, installs HomeOS for that user, and applies /boot/firmware/homeos.txt.
set -uo pipefail
CONF=/boot/firmware/homeos.txt
. /etc/homeos-image
log() { echo "homeos-firstboot: $*"; }

# KEY=value lines, read without running them as shell code
getv() { [ -f "$CONF" ] && sed -n "s/^$1=//p" "$CONF" | head -n1 | tr -d '\r' || true; }

# ---- Wi-Fi, when homeos.txt names one
SSID=$(getv WIFI_SSID)
if [ -n "$SSID" ] && command -v nmcli >/dev/null 2>&1; then
    COUNTRY=$(getv WIFI_COUNTRY)
    [ -n "$COUNTRY" ] && { raspi-config nonint do_wifi_country "$COUNTRY" >/dev/null 2>&1 || true; }
    rfkill unblock wifi 2>/dev/null || true
    if ! nmcli -t -f NAME connection show | grep -qx homeos-wifi; then
        log "adding Wi-Fi $SSID"
        nmcli connection add type wifi con-name homeos-wifi ifname wlan0 ssid "$SSID" \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$(getv WIFI_PASSWORD)" connection.autoconnect yes || true
        nmcli connection up homeos-wifi || true
    fi
fi

# ---- the user: a normal account with a password set
find_user() {
    awk -F: '$3 >= 1000 && $3 < 60000 {print $1}' /etc/passwd | while read -r u; do
        pw=$(getent shadow "$u" | cut -d: -f2)
        case "$pw" in ""|"*"|"!"*) continue ;; esac
        echo "$u"
        break
    done
}
USER_NAME=$(find_user)
if [ -z "$USER_NAME" ] && [ -n "$(getv USER_NAME)" ] && [ -n "$(getv USER_PASSWORD)" ]; then
    NEW=$(getv USER_NAME)
    HASH=$(openssl passwd -6 "$(getv USER_PASSWORD)")
    log "creating the user $NEW"
    if [ -x /usr/lib/userconf-pi/userconf ]; then
        /usr/lib/userconf-pi/userconf "$NEW" "$HASH" || true
    fi
    if ! id "$NEW" >/dev/null 2>&1; then
        useradd -m -s /bin/bash "$NEW"
        for g in sudo adm video audio plugdev netdev bluetooth gpio i2c spi input render users; do
            getent group "$g" >/dev/null && usermod -aG "$g" "$NEW"
        done
    fi
    echo "$NEW:$HASH" | chpasswd -e
    USER_NAME=$(find_user)
fi
for _ in $(seq 60); do          # Raspberry Pi Imager's own first-boot setup may still be running
    [ -n "$USER_NAME" ] && break
    sleep 10
    USER_NAME=$(find_user)
done
if [ -z "$USER_NAME" ]; then
    log "no user yet; HomeOS will be set up at the next start"
    exit 0
fi
HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
as_user() { runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" "$@"; }
log "setting HomeOS up for $USER_NAME"

# ---- HomeOS itself, from a copy in the user's home (so Settings > Updates can pull)
if [ -d /opt/homeos-src ] && [ ! -e "$HOME_DIR/homeos-src" ]; then
    mv /opt/homeos-src "$HOME_DIR/homeos-src"
fi
chown -R "$USER_NAME": "$HOME_DIR/homeos-src"
# shellcheck disable=SC2086
HOMEOS_USER="$USER_NAME" "$HOME_DIR/homeos-src/install.sh" --no-packages $FLAGS </dev/null \
    || log "install.sh reported a problem; see the lines above"

# ---- web password
PW=$(getv HOMEOS_PASSWORD)
if [ -n "$PW" ]; then
    TMP=$(mktemp)
    printf '%s\n' "$PW" > "$TMP"
    chown "$USER_NAME" "$TMP"
    as_user python3 /opt/homeos/shell/webshell.py --password-file "$TMP" || log "the web password was not saved"
    rm -f "$TMP"
fi

# ---- where web access listens
KEY=$(getv TAILSCALE_AUTHKEY)
if [ -n "$KEY" ]; then
    systemctl start tailscaled.service || true
    tailscale up --authkey="$KEY" --hostname="$(hostname)" || log "Tailscale did not accept the key"
    as_user python3 /opt/homeos/shell/webshell.py --set-bind tailscale >/dev/null || true
else
    as_user python3 /opt/homeos/shell/webshell.py --set-bind all >/dev/null || true
    as_user python3 /opt/homeos/shell/webshell.py --allow-host "$(hostname).local" >/dev/null || true
fi

# ---- the secrets leave the card
if [ -f "$CONF" ]; then
    sed -i -E 's/^(HOMEOS_PASSWORD|TAILSCALE_AUTHKEY|USER_PASSWORD|WIFI_PASSWORD)=.*/\1=/' "$CONF"
fi

# ---- motion detection, prepared in the full image
if [ "${DETECT:-0}" -eq 1 ] && [ -d /opt/esp32cam ] && [ ! -e "$HOME_DIR/esp32cam" ] && [ -x "$HOME_DIR/homeos-src/setup-detect.sh" ]; then
    mv /opt/esp32cam "$HOME_DIR/esp32cam"
    chown -R "$USER_NAME": "$HOME_DIR/esp32cam"
    HOMEOS_USER="$USER_NAME" "$HOME_DIR/homeos-src/setup-detect.sh" --no-packages </dev/null \
        || log "motion detection was not started"
fi

systemctl restart homeos-web.service 2>/dev/null || true
mkdir -p /var/lib/homeos
touch /var/lib/homeos/firstboot.done
systemctl disable homeos-firstboot.service >/dev/null 2>&1 || true
log "done"

case " $FLAGS " in
    *" --web-only "*) ;;
    *) log "restarting into the HomeOS desktop"; systemctl reboot ;;
esac
