#!/usr/bin/env bash
# HomeOS installer - turns Raspberry Pi OS Lite (64-bit) into HomeOS.
#
#   sudo ./install.sh                  install and start the desktop at boot
#   sudo ./install.sh --no-autostart   install, but start by typing "homeos"
#   sudo ./install.sh --no-packages    only copy files (packages already there)
#   sudo ./install.sh --web            also turn on web access over Tailscale (port 8790)
#   sudo ./install.sh --web-only       web access without the desktop: nothing about
#                                      how the Pi boots is changed
#
# Safe to run again: it updates the files in /opt/homeos and keeps your settings.

set -euo pipefail

PREFIX=/opt/homeos
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
AUTOSTART=1
PACKAGES=1
WEB=0
DESKTOP=1
WEB_PORT=8790
LITE=0          # no browser, no camera: for small boards such as the Pi Zero 2 W
CHROMIUM=1
PACKAGES_ONLY=0 # used when building an SD card image, before any user exists

FLAGS=""       # remembered for "Install update" in Settings
for arg in "$@"; do
    case "$arg" in
        --no-autostart) AUTOSTART=0; FLAGS="$FLAGS $arg" ;;
        --no-packages) PACKAGES=0 ;;
        --web) WEB=1; FLAGS="$FLAGS $arg" ;;
        --web-only) WEB=1; DESKTOP=0; AUTOSTART=0; FLAGS="$FLAGS $arg" ;;
        --lite) LITE=1; CHROMIUM=0; FLAGS="$FLAGS $arg" ;;
        --no-chromium) CHROMIUM=0; FLAGS="$FLAGS $arg" ;;
        --packages-only) PACKAGES_ONLY=1 ;;
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this with sudo:  sudo ./install.sh"

if [ "$PACKAGES_ONLY" -eq 0 ]; then
    TARGET_USER=${HOMEOS_USER:-${SUDO_USER:-}}
    if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = root ]; then
        die "Log in as your normal user and run:  sudo ./install.sh"
    fi
    id "$TARGET_USER" >/dev/null 2>&1 || die "User '$TARGET_USER' does not exist."
    TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
    [ -d "$TARGET_HOME" ] || die "Home folder of '$TARGET_USER' was not found."
fi

# Chromium (the Pi browser of the web version) needs more memory than small boards have.
MEM_KB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$CHROMIUM" -eq 1 ] && [ "${MEM_KB:-0}" -gt 0 ] && [ "${MEM_KB:-0}" -lt 1500000 ] && [ "$PACKAGES_ONLY" -eq 0 ]; then
    warn "This board has less than 1.5 GB of memory, so the Pi browser (Chromium) is left out."
    CHROMIUM=0
fi

for part in shell/homeos_shell.py shell/sysapi.py shell/apps.py shell/webshell.py shell/ui/index.html config/labwc/rc.xml bin/homeos; do
    [ -f "$SRC/$part" ] || die "Missing file: $part (run install.sh from the unpacked homeos folder)."
done

MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
case "$MODEL" in
    *"Raspberry Pi 5"*|*"Compute Module 5"*) ;;
    *"Raspberry Pi Zero 2"*) [ "$LITE" -eq 1 ] || warn "On a Pi Zero 2 W use:  sudo ./install.sh --web-only --lite" ;;
    "") warn "This does not look like a Raspberry Pi. Continuing anyway." ;;
    *)  warn "Built for the Raspberry Pi 5; this is: $MODEL. Continuing anyway." ;;
esac
[ "$(uname -m)" = aarch64 ] || warn "Expected a 64-bit system (aarch64), found $(uname -m)."

# ---------------------------------------------------------------- packages
if [ "$PACKAGES" -eq 1 ]; then
    command -v apt-get >/dev/null || die "apt-get not found. HomeOS needs Raspberry Pi OS or Debian."
    export DEBIAN_FRONTEND=noninteractive

    say "Updating the package list"
    apt-get update

    if apt-cache show gir1.2-webkit2-4.1 >/dev/null 2>&1; then
        WEBKIT=gir1.2-webkit2-4.1
    else
        WEBKIT=gir1.2-webkit2-4.0
    fi

    # Without these the desktop cannot start.
    REQUIRED=(
        labwc
        python3 python3-gi python3-gi-cairo
        gir1.2-gtk-3.0 gir1.2-vte-2.91 "$WEBKIT"
        glib-networking ca-certificates
        dbus-user-session gsettings-desktop-schemas
        libgl1-mesa-dri
        fonts-noto-core
    )
    # Nice to have: on-screen keyboard, sound, video, fonts, Wi-Fi control.
    OPTIONAL=(
        squeekboard dconf-gsettings-backend
        network-manager bluez
        pipewire pipewire-pulse wireplumber
        gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav
        gstreamer1.0-pulseaudio gstreamer1.0-gl
        fonts-roboto fonts-noto-color-emoji
        xdg-user-dirs
    )

    if [ "$DESKTOP" -eq 1 ]; then
        say "Installing the desktop packages (this can take a while)"
        apt-get install -y "${REQUIRED[@]}"
    else
        apt-get install -y python3 util-linux git
        OPTIONAL=(network-manager bluez rfkill)
    fi
    OPTIONAL+=(ffmpeg yt-dlp)          # the radio: ffmpeg plays it, yt-dlp adds tracks from a link
    # the Pi browser of the web version: a headless Chromium on the Pi
    if [ "$WEB" -eq 1 ] && [ "$CHROMIUM" -eq 1 ]; then
        OPTIONAL+=(chromium)
    fi

    say "Installing extras"
    MISSING=()
    for pkg in "${OPTIONAL[@]}"; do
        if ! apt-get install -y "$pkg" >/dev/null 2>&1; then
            MISSING+=("$pkg")
        fi
    done
    if [ "${#MISSING[@]}" -gt 0 ]; then
        warn "Could not install: ${MISSING[*]}. HomeOS works without them, with fewer features."
    fi
fi
# updates run without packages: still bring in what the radio (and printer cameras) need
if [ "$PACKAGES" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
    NEED=()
    command -v ffmpeg >/dev/null 2>&1 || NEED+=(ffmpeg)
    command -v yt-dlp >/dev/null 2>&1 || NEED+=(yt-dlp)
    for pkg in "${NEED[@]}"; do
        say "Installing $pkg (for the radio)"
        DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg" >/dev/null 2>&1 \
            || { apt-get update >/dev/null 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg" >/dev/null 2>&1; } \
            || warn "Could not install $pkg: sudo apt install $pkg"
    done
fi

if [ "$PACKAGES_ONLY" -eq 1 ]; then
    say "Packages installed. HomeOS itself is set up for a user on first boot."
    exit 0
fi

# ---------------------------------------------------------------- upgrade from Malyna OS
# HomeOS used to be called Malyna OS: remove the old copy and keep its settings and password.
LEGACY=malyna
if [ -d "/opt/$LEGACY" ] || [ -f "/etc/systemd/system/$LEGACY-web.service" ]; then
    say "Upgrading from Malyna OS"
    if [ -f "/etc/systemd/system/$LEGACY-web.service" ]; then
        systemctl disable --now "$LEGACY-web.service" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$LEGACY-web.service"
        systemctl daemon-reload >/dev/null 2>&1 || true
        WEB=1          # web access was on, so keep it on
    fi
    rm -rf "/opt/$LEGACY/shell" "/opt/$LEGACY/config" "/opt/$LEGACY/bin"
    rmdir "/opt/$LEGACY" 2>/dev/null || true
    rm -f "/usr/local/bin/$LEGACY" "/usr/local/bin/$LEGACY-shell" "/usr/local/bin/$LEGACY-web"
    for f in "$TARGET_HOME/.bash_profile" "$TARGET_HOME/.profile"; do
        if [ -f "$f" ]; then
            sed -i "/^# >>> $LEGACY-os >>>\$/,/^# <<< $LEGACY-os <<<\$/d" "$f"
        fi
    done
    if [ -f "/etc/systemd/system/getty@tty1.service.d/$LEGACY-autologin.conf" ]; then
        mv "/etc/systemd/system/getty@tty1.service.d/$LEGACY-autologin.conf" \
           /etc/systemd/system/getty@tty1.service.d/homeos-autologin.conf
    fi
fi
for d in .config .local/state; do
    if [ -d "$TARGET_HOME/$d/$LEGACY" ] && [ ! -e "$TARGET_HOME/$d/homeos" ]; then
        mv "$TARGET_HOME/$d/$LEGACY" "$TARGET_HOME/$d/homeos"
    fi
done

# ---------------------------------------------------------------- files
say "Copying HomeOS to $PREFIX"
install -d "$PREFIX"
rm -rf "${PREFIX:?}/shell" "${PREFIX:?}/config" "${PREFIX:?}/bin"
cp -r "$SRC/shell" "$SRC/config" "$SRC/bin" "$PREFIX/"
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +
chown -R root:root "$PREFIX"
chmod -R u=rwX,go=rX "$PREFIX"
chmod 755 "$PREFIX"/bin/*
ln -sf "$PREFIX/bin/homeos" /usr/local/bin/homeos
ln -sf "$PREFIX/bin/homeos-shell" /usr/local/bin/homeos-shell
ln -sf "$PREFIX/bin/homeos-web" /usr/local/bin/homeos-web

# ---------------------------------------------------------------- user
say "Setting up user '$TARGET_USER'"
for grp in video input render audio netdev; do
    if getent group "$grp" >/dev/null; then
        usermod -aG "$grp" "$TARGET_USER"
    fi
done

if [ -f "$TARGET_HOME/.bash_profile" ]; then
    PROFILE="$TARGET_HOME/.bash_profile"
else
    PROFILE="$TARGET_HOME/.profile"
fi
as_user() { runuser -u "$TARGET_USER" -- "$@"; }

# First install only: put the Home Ecosystem dashboard on the home screen if it is here.
if [ ! -f "$TARGET_HOME/.config/homeos/settings.json" ] && [ -f "$TARGET_HOME/home-ecosystem/server.py" ]; then
    as_user python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import sysapi
s = sysapi.Settings(sysapi.default_config_dir())
s.update({"tiles": [{"name": "Home Ecosystem", "url": "http://localhost:8787"}] + s.get()["tiles"]})
' "$PREFIX/shell" || warn "Could not add the Home Ecosystem tile."
fi

touch "$PROFILE"
if [ "$DESKTOP" -eq 1 ]; then
    sed -i '/^# >>> homeos >>>$/,/^# <<< homeos <<<$/d' "$PROFILE"
fi

if [ "$AUTOSTART" -eq 1 ]; then
    cat >> "$PROFILE" <<'EOF'
# >>> homeos >>>
# Start the HomeOS desktop on the first console. Remove this block (or run
# uninstall.sh) to boot to a plain console instead.
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ] && [ "$(tty)" = /dev/tty1 ]; then
    homeos
fi
# <<< homeos <<<
EOF
    chown "$TARGET_USER": "$PROFILE"

    say "Turning on automatic login on the console"
    if command -v raspi-config >/dev/null 2>&1 && SUDO_USER="$TARGET_USER" raspi-config nonint do_boot_behaviour B2; then
        :
    else
        install -d /etc/systemd/system/getty@tty1.service.d
        cat > /etc/systemd/system/getty@tty1.service.d/homeos-autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $TARGET_USER --noclear %I \$TERM
EOF
        systemctl set-default multi-user.target >/dev/null 2>&1 || true
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi
else
    chown "$TARGET_USER": "$PROFILE"
fi

# Bluetooth in Settings uses bluetoothctl, which BlueZ allows to the "bluetooth" group.
if getent group bluetooth >/dev/null 2>&1; then
    usermod -aG bluetooth "$TARGET_USER" || warn "Could not add $TARGET_USER to the bluetooth group."
fi

# The lite edition hides the browser and the camera.
if [ "$LITE" -eq 1 ]; then echo lite > "$PREFIX/edition"; else echo full > "$PREFIX/edition"; fi
chmod 644 "$PREFIX/edition"

# Where this copy came from, so Settings > Updates can fetch a newer one.
# Updates are not taken from this folder later: bin/homeos-update fetches the
# recorded remote and branch, as root, into /var/lib/homeos/src (a copy only
# root can change) and runs install.sh from there. A folder the user can write
# to is never run as root by an update.
src_git() {     # git in the folder install.sh runs from, as the user who owns it
    if [ "$(stat -c %u "$SRC")" -eq 0 ]; then git -C "$SRC" "$@"; else as_user git -C "$SRC" "$@"; fi
}
REMOTE="" BRANCH="" COMMIT=""
if src_git rev-parse --git-dir >/dev/null 2>&1; then
    REMOTE=$(src_git remote get-url origin 2>/dev/null || true)
    BRANCH=$(src_git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    COMMIT=$(src_git rev-parse HEAD 2>/dev/null || true)
    case "$REMOTE" in https://*|ssh://*|git@*) ;; *) REMOTE="" ;; esac
    printf '%s' "$BRANCH" | grep -Eq '^[A-Za-z0-9._/-]+$' || BRANCH=""
fi
{
    echo "SRC=$SRC"
    echo "REMOTE=$REMOTE"
    echo "BRANCH=$BRANCH"
    echo "FLAGS=${FLAGS# }"
    echo "OWNER=$TARGET_USER"
    echo "COMMIT=$COMMIT"
} > "$PREFIX/source"
chmod 644 "$PREFIX/source"

# Root has to be able to fetch updates on its own. A private GitHub repository
# needs a token: the one the user signed in with (gh auth login) is copied to a
# file only root can read.
CRED=/etc/homeos/git-credentials
root_git() {
    local c=(-c credential.helper=)
    [ -f "$CRED" ] && c+=(-c "credential.helper=store --file=$CRED")
    HOME=/root GIT_TERMINAL_PROMPT=0 git "${c[@]}" "$@"
}
if [ -n "$REMOTE" ] && [ -n "$BRANCH" ] && ! root_git ls-remote --exit-code "$REMOTE" "refs/heads/$BRANCH" >/dev/null 2>&1; then
    case "$REMOTE" in
        https://github.com/*)
            TOKEN=$(as_user gh auth token 2>/dev/null || true)
            if [ -n "$TOKEN" ]; then
                install -d -m 0700 -o root -g root /etc/homeos
                ( umask 077; printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > "$CRED" )
            fi
            ;;
    esac
    root_git ls-remote --exit-code "$REMOTE" "refs/heads/$BRANCH" >/dev/null 2>&1 \
        || warn "Updates from Settings cannot reach $REMOTE yet (for a private repository: gh auth login, then run install.sh again)."
fi

# Lets "Install update" in Settings and the Motion button in the Camera app work:
# this user may run the update helper and switch the detection service on or
# off (and nothing else) with sudo and no password.
if [ -d /etc/sudoers.d ] && command -v visudo >/dev/null 2>&1; then
    RULE=$(mktemp)
    echo "$TARGET_USER ALL=(root) NOPASSWD: $PREFIX/bin/homeos-update \"\"" > "$RULE"
    for ctl in /usr/bin/systemctl /bin/systemctl; do
        for verb in enable disable; do
            echo "$TARGET_USER ALL=(root) NOPASSWD: $ctl $verb --now homeos-detect.service" >> "$RULE"
        done
    done
    if visudo -cf "$RULE" >/dev/null 2>&1; then
        install -m 0440 -o root -g root "$RULE" /etc/sudoers.d/homeos-update
    else
        warn "Could not allow updates from Settings (the sudo rule was refused)."
    fi
    rm -f "$RULE"
fi

# ---------------------------------------------------------------- radio
# The radio station (shell/radio.py) runs as its own service for the user, on
# port 8081 (only on this Pi unless sharing is switched on in the Radio app).
# An earlier stand-alone radio server (radio-server.service) is switched off
# here, and the radio takes over its playlist and files once.
for old in radio-server.service; do
    if [ -f "/etc/systemd/system/$old" ]; then
        say "Switching off the old radio server ($old); HomeOS Radio takes over its playlist"
        systemctl disable --now "$old" >/dev/null 2>&1 || true
    fi
done
RADIO_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
cat > /etc/systemd/system/homeos-radio.service <<UNIT
[Unit]
Description=HomeOS Radio (a looping radio station, port 8081)
After=network-online.target
Wants=network-online.target

[Service]
User=$TARGET_USER
Environment=PYTHONUNBUFFERED=1
Environment=HOME=$RADIO_HOME
ExecStart=/usr/bin/python3 $PREFIX/shell/radio.py --config $RADIO_HOME/.config/homeos
Restart=always
RestartSec=3
Nice=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload || true
systemctl enable homeos-radio.service >/dev/null 2>&1 || true
systemctl restart homeos-radio.service || warn "The radio did not start. See: journalctl -u homeos-radio"

# ---------------------------------------------------------------- web access
if [ "$WEB" -eq 1 ]; then
    say "Setting up web access (port $WEB_PORT)"
    TARGET_UID=$(id -u "$TARGET_USER")
    cat > /etc/systemd/system/homeos-web.service <<UNIT
[Unit]
Description=HomeOS web access (home screen and terminal over Tailscale)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=$TARGET_USER
Environment=XDG_RUNTIME_DIR=/run/user/$TARGET_UID
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $PREFIX/shell/webshell.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload || true

    if ! as_user python3 "$PREFIX/shell/webshell.py" --status | grep -q '^password set: yes'; then
        if [ -t 0 ]; then
            echo "Choose a password for web access (at least 8 characters)."
            echo "It protects the settings and a terminal on this Pi, so make it a strong one."
            as_user python3 "$PREFIX/shell/webshell.py" --set-password \
                || warn "No password was saved. Run later:  homeos-web --set-password"
        else
            warn "No terminal to ask for a password. Run later:  homeos-web --set-password"
        fi
    fi
    systemctl enable homeos-web.service >/dev/null 2>&1 || true
    systemctl restart homeos-web.service || warn "The web service did not start. See: journalctl -u homeos-web"
    echo
    as_user python3 "$PREFIX/shell/webshell.py" --status || true
fi

say "HomeOS is installed."
if [ "$AUTOSTART" -eq 1 ]; then
    echo "Restart to boot into the desktop:   sudo reboot"
    echo "Leave the desktop any time with Power > Exit to console (or Ctrl+Alt+Backspace)."
elif [ "$DESKTOP" -eq 1 ]; then
    echo "Start the desktop from the console by typing:   homeos"
else
    echo "Only web access was set up. The way this Pi boots was not changed."
fi
