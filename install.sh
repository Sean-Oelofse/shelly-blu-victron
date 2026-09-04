#!/bin/sh
# Install dbus-shelly-blu on Venus OS.
#
#   scp -r "Shelly BLU" root@venus:/data/dbus-shelly-blu
#   ssh root@venus "cd /data/dbus-shelly-blu && sh install.sh"
#
# /data survives a Venus OS firmware update; the rc.local hook re-creates the
# /service symlink after every reboot and firmware update.

set -e

TARGET=/data/dbus-shelly-blu
SRC=$(cd "$(dirname "$0")" && pwd)
SERVICE=/service/dbus-shelly-blu

if [ ! -f /opt/victronenergy/version ]; then
    echo "This does not look like a Venus OS device (no /opt/victronenergy)."
    echo "Aborting."
    exit 1
fi

if [ "$SRC" != "$TARGET" ]; then
    echo "Copying $SRC -> $TARGET"
    mkdir -p "$TARGET"
    cp -r "$SRC"/. "$TARGET"/
fi

cd "$TARGET"

# Files edited on Windows arrive with CRLF, which busybox sh will not run.
for f in install.sh uninstall.sh get.sh service/run service/log/run \
         dbus_shelly_blu.py bthome.py blescanner.py test_bthome.py; do
    [ -f "$f" ] && sed -i 's/\r$//' "$f"
done

chmod 755 service/run service/log/run dbus_shelly_blu.py

# velib_python: prefer a local copy so a firmware update cannot move it away.
mkdir -p ext/velib_python
for f in vedbus.py settingsdevice.py ve_utils.py; do
    if [ ! -f "ext/velib_python/$f" ]; then
        for d in /opt/victronenergy/*/ext/velib_python; do
            if [ -f "$d/$f" ]; then
                cp "$d/$f" ext/velib_python/
                break
            fi
        done
    fi
done

if [ ! -f ext/velib_python/vedbus.py ]; then
    echo "WARNING: could not find vedbus.py on this system."
    echo "Copy velib_python from https://github.com/victronenergy/velib_python"
    echo "into $TARGET/ext/velib_python before starting the service."
fi

[ -f config.json ] || cp config.sample.json config.json

# daemontools service
ln -sfn "$TARGET/service" "$SERVICE"

# re-create the symlink after reboot / firmware update
touch /data/rc.local
chmod 755 /data/rc.local
if ! grep -q "dbus-shelly-blu" /data/rc.local; then
    echo "ln -sfn $TARGET/service $SERVICE" >> /data/rc.local
    echo "Added the service link to /data/rc.local"
fi

echo
echo "Installed in $TARGET"
echo
echo "Next:"
echo "  1. Find your sensors:  python3 $TARGET/dbus_shelly_blu.py --scan"
echo "  2. Edit               $TARGET/config.json"
echo "  3. Restart:           svc -t $SERVICE"
echo "  4. Watch the log:     tail -F /var/log/dbus-shelly-blu/current"
