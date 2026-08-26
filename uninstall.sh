#!/bin/sh
# Remove the dbus-shelly-blu service. Leaves /data/dbus-shelly-blu in place.

SERVICE=/service/dbus-shelly-blu
TARGET=/data/dbus-shelly-blu

svc -d "$SERVICE" 2>/dev/null
svc -d "$SERVICE/log" 2>/dev/null
rm -f "$SERVICE"

if [ -f /data/rc.local ]; then
    sed -i '/dbus-shelly-blu/d' /data/rc.local
fi

echo "Service removed. Files are still in $TARGET (rm -rf it to remove them)."
echo "Device instance settings stay in localsettings under"
echo "/Settings/Devices/shelly_blu_*; use dbus-spy to delete them if you like."
