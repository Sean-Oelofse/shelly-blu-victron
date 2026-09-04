#!/bin/sh
# Bootstrap installer for dbus-shelly-blu: download this project straight from
# GitHub and run install.sh, so you do not have to scp anything to the GX device.
#
# On the Venus OS device (ssh root@venus), run one of:
#
#   wget -qO- https://raw.githubusercontent.com/Sean-Oelofse/shelly-blu-victron/main/get.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/Sean-Oelofse/shelly-blu-victron/main/get.sh | sh
#
# Install a different branch or tag by passing it in REF, e.g.:
#
#   wget -qO- https://raw.githubusercontent.com/Sean-Oelofse/shelly-blu-victron/main/get.sh | REF=v1.0 sh
#
# /data survives a Venus OS firmware update; install.sh wires up the service and
# an rc.local hook so it comes back after a reboot or firmware update.

set -e

REPO=${REPO:-Sean-Oelofse/shelly-blu-victron}
REF=${REF:-main}
TARGET=/data/dbus-shelly-blu
TARBALL="https://codeload.github.com/$REPO/tar.gz/$REF"

if [ ! -f /opt/victronenergy/version ]; then
    echo "This does not look like a Venus OS device (no /opt/victronenergy)."
    echo "Run this on the GX device over ssh. Aborting."
    exit 1
fi

# Pick whatever download tool the image ships with.
if command -v wget >/dev/null 2>&1; then
    fetch() { wget -qO "$2" "$1"; }
elif command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL -o "$2" "$1"; }
else
    echo "Neither wget nor curl is available. Aborting."
    exit 1
fi

TMP=$(mktemp -d /data/.shelly-blu-XXXXXX 2>/dev/null || mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

echo "Downloading $REPO@$REF ..."
fetch "$TARBALL" "$TMP/src.tar.gz"

echo "Extracting ..."
mkdir -p "$TMP/src"
tar -xzf "$TMP/src.tar.gz" -C "$TMP/src"

# The tarball unpacks to a single <repo>-<ref>/ directory.
SRC=$(find "$TMP/src" -maxdepth 1 -mindepth 1 -type d | head -n1)
if [ -z "$SRC" ] || [ ! -f "$SRC/install.sh" ]; then
    echo "Downloaded archive does not contain install.sh. Aborting."
    exit 1
fi

# Preserve an existing config.json across a re-install / upgrade.
if [ -f "$TARGET/config.json" ] && [ ! -f "$SRC/config.json" ]; then
    cp "$TARGET/config.json" "$SRC/config.json"
fi

echo "Running install.sh ..."
sh "$SRC/install.sh"
