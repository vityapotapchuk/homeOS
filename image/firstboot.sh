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

# ---- a failed step leaves the setup unfinished: it runs again at the next start,
# up to three times; after that it gives up and says so (and the secrets go).
STATE=/var/lib/homeos
mkdir -p "$STATE"
ATTEMPT=$(( $(cat "$STATE/firstboot.attempts" 2>/dev/null || echo 0) + 1 ))
echo "$ATTEMPT" > "$STATE/firstboot.attempts"
FAILED=""
fail() { log "FAILED: $*"; FAILED="${FAILED:+$FAILED; }$*"; }

# ---- HomeOS itself, from the root-owned copy that Settings > Updates also uses
SRC_DIR=$STATE/src
if [ -d /opt/homeos-src ] && [ ! -e "$SRC_DIR" ]; then
    mv /opt/homeos-src "$SRC_DIR"
fi
chown -R root:root "$SRC_DIR" && chmod -R go-w "$SRC_DIR"
# shellcheck disable=SC2086
HOMEOS_USER="$USER_NAME" "$SRC_DIR/install.sh" --no-packages $FLAGS </dev/null \
    || fail "install.sh did not finish (see the lines above)"

# ---- web password
PW=$(getv HOMEOS_PASSWORD)
if [ -n "$PW" ]; then
    TMP=$(mktemp)
    printf '%s\n' "$PW" > "$TMP"
    chown "$USER_NAME" "$TMP"
    as_user python3 /opt/homeos/shell/webshell.py --password-file "$TMP" || fail "the web password was not saved"
    rm -f "$TMP"
fi

# ---- where web access listens
KEY=$(getv TAILSCALE_AUTHKEY)
if [ -n "$KEY" ]; then
    systemctl start tailscaled.service || true
    tailscale up --authkey="$KEY" --hostname="$(hostname)" || fail "Tailscale did not accept the key"
    as_user python3 /opt/homeos/shell/webshell.py --set-bind tailscale >/dev/null || true
else
    as_user python3 /opt/homeos/shell/webshell.py --set-bind all >/dev/null || true
    as_user python3 /opt/homeos/shell/webshell.py --allow-host "$(hostname).local" >/dev/null || true
fi

if [ -n "$FAILED" ] && [ "$ATTEMPT" -lt 3 ]; then
    log "setup is not finished ($FAILED); it will be tried again at the next start (attempt $ATTEMPT of 3)"
    exit 1
fi

# ---- the secrets leave the card
if [ -f "$CONF" ]; then
    sed -i -E 's/^(HOMEOS_PASSWORD|TAILSCALE_AUTHKEY|USER_PASSWORD|WIFI_PASSWORD)=.*/\1=/' "$CONF"
fi

# ---- motion detection, prepared in the full image
if [ "${DETECT:-0}" -eq 1 ] && [ -d /opt/esp32cam ] && [ ! -e "$HOME_DIR/esp32cam" ] && [ -x "$SRC_DIR/setup-detect.sh" ]; then
    mv /opt/esp32cam "$HOME_DIR/esp32cam"
    chown -R "$USER_NAME": "$HOME_DIR/esp32cam"
    HOMEOS_USER="$USER_NAME" "$SRC_DIR/setup-detect.sh" --no-packages </dev/null \
        || log "motion detection was not started"
fi

systemctl restart homeos-web.service 2>/dev/null || true
if [ -n "$FAILED" ]; then
    # three tries are over: stop trying, and leave a note where it is easy to find
    mkdir -p /etc/motd.d
    printf 'HomeOS setup gave up after %s attempts: %s\nSee: journalctl -u homeos-firstboot\n' "$ATTEMPT" "$FAILED" \
        | tee "$STATE/firstboot.failed" > /etc/motd.d/homeos-setup 2>/dev/null || true
    touch "$STATE/firstboot.done"
    systemctl disable homeos-firstboot.service >/dev/null 2>&1 || true
    log "gave up: $FAILED"
    exit 1
fi
touch "$STATE/firstboot.done"
rm -f "$STATE/firstboot.attempts"
systemctl disable homeos-firstboot.service >/dev/null 2>&1 || true
log "done"

case " $FLAGS " in
    *" --web-only "*) ;;
    *) log "restarting into the HomeOS desktop"; systemctl reboot ;;
esac
