"""
Passive BLE advertisement scanner built on the BlueZ D-Bus API.

Venus OS already runs bluetoothd and ships python3-dbus and PyGObject, so
this needs nothing from pip. We ask BlueZ to discover LE devices and report
duplicate advertisements, then hand every ServiceData update to a callback.
"""

import logging

import dbus
from gi.repository import GLib

log = logging.getLogger(__name__)

BLUEZ = 'org.bluez'
ADAPTER_IFACE = 'org.bluez.Adapter1'
DEVICE_IFACE = 'org.bluez.Device1'
OM_IFACE = 'org.freedesktop.DBus.ObjectManager'
PROPS_IFACE = 'org.freedesktop.DBus.Properties'


class Advertisement(object):
    __slots__ = ('mac', 'name', 'rssi', 'service_data', 'path')

    def __init__(self, path, mac, name, rssi, service_data):
        self.path = path
        self.mac = mac
        self.name = name
        self.rssi = rssi
        self.service_data = service_data  # {uuid: bytes}

    def __repr__(self):
        return '<Advertisement %s %s rssi=%s>' % (self.mac, self.name, self.rssi)


class BleScanner(object):
    """Keeps a BlueZ LE discovery running and calls back on advertisements.

    callback(Advertisement) is invoked from the GLib main loop.
    """

    def __init__(self, bus, callback, adapter=None, uuids=None, restart_interval=600):
        self.bus = bus
        self.callback = callback
        self.adapter_name = adapter          # e.g. 'hci0', None = first found
        self.uuids = uuids or []             # optional BlueZ-side UUID filter
        self.restart_interval = restart_interval
        self.adapter_path = None
        self._discovering = False
        self._cache = {}                     # path -> (mac, name)

    # -- setup ----------------------------------------------------------

    def start(self):
        self.bus.add_signal_receiver(
            self._on_interfaces_added, dbus_interface=OM_IFACE,
            signal_name='InterfacesAdded')
        self.bus.add_signal_receiver(
            self._on_properties_changed, dbus_interface=PROPS_IFACE,
            signal_name='PropertiesChanged', arg0=DEVICE_IFACE,
            path_keyword='path')
        self.bus.add_signal_receiver(
            self._on_bluez_owner_changed, dbus_interface='org.freedesktop.DBus',
            signal_name='NameOwnerChanged', arg0=BLUEZ)

        self._ensure_discovery()
        # bluetoothd occasionally drops discovery (a connect attempt, a
        # suspend, another process fiddling with the adapter). Re-arm it.
        GLib.timeout_add_seconds(30, self._ensure_discovery_tick)
        if self.restart_interval:
            GLib.timeout_add_seconds(self.restart_interval, self._restart_tick)

    def _find_adapter(self):
        om = dbus.Interface(self.bus.get_object(BLUEZ, '/'), OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if ADAPTER_IFACE not in ifaces:
                continue
            if self.adapter_name and not path.endswith('/' + self.adapter_name):
                continue
            return str(path)
        return None

    def _ensure_discovery(self):
        try:
            if self.adapter_path is None:
                self.adapter_path = self._find_adapter()
                if self.adapter_path is None:
                    log.warning('no bluetooth adapter found yet')
                    return False
                log.info('using adapter %s', self.adapter_path)

            obj = self.bus.get_object(BLUEZ, self.adapter_path)
            props = dbus.Interface(obj, PROPS_IFACE)

            if not bool(props.Get(ADAPTER_IFACE, 'Powered')):
                log.info('powering on adapter')
                props.Set(ADAPTER_IFACE, 'Powered', dbus.Boolean(True))

            if bool(props.Get(ADAPTER_IFACE, 'Discovering')):
                self._discovering = True
                return True

            adapter = dbus.Interface(obj, ADAPTER_IFACE)
            flt = {
                'Transport': dbus.String('le'),
                # Report every advertisement, not just the first one, so we
                # see each new BTHome packet.
                'DuplicateData': dbus.Boolean(True),
            }
            if self.uuids:
                flt['UUIDs'] = dbus.Array(self.uuids, signature='s')
            adapter.SetDiscoveryFilter(flt)
            adapter.StartDiscovery()
            self._discovering = True
            log.info('LE discovery started')

            # Pick up devices bluetoothd already knows about.
            self._scan_existing()
            return True
        except dbus.DBusException as e:
            log.warning('could not start discovery: %s', e)
            self._discovering = False
            self.adapter_path = None
            return False

    def _scan_existing(self):
        om = dbus.Interface(self.bus.get_object(BLUEZ, '/'), OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if DEVICE_IFACE in ifaces:
                self._handle_device(str(path), ifaces[DEVICE_IFACE])

    def _ensure_discovery_tick(self):
        if not self._discovering:
            self._ensure_discovery()
        return True

    def _restart_tick(self):
        """Periodically cycle discovery and drop BlueZ's device cache.

        Long running discoveries slowly fill bluetoothd with every passing
        beacon; cycling keeps memory flat on a GX device.
        """
        if not self._discovering or not self.adapter_path:
            return True
        try:
            adapter = dbus.Interface(
                self.bus.get_object(BLUEZ, self.adapter_path), ADAPTER_IFACE)
            adapter.StopDiscovery()
            self._discovering = False
            self._cache.clear()
        except dbus.DBusException as e:
            log.debug('stop discovery failed: %s', e)
        GLib.timeout_add_seconds(2, self._ensure_discovery_tick)
        return True

    # -- signals --------------------------------------------------------

    def _on_bluez_owner_changed(self, name, old, new):
        log.info('bluez owner changed (%s -> %s), re-arming discovery',
                 old or 'none', new or 'none')
        self.adapter_path = None
        self._discovering = False
        self._cache.clear()
        if new:
            GLib.timeout_add_seconds(3, self._ensure_discovery_tick)

    def _on_interfaces_added(self, path, interfaces):
        if DEVICE_IFACE in interfaces:
            self._handle_device(str(path), interfaces[DEVICE_IFACE])

    def _on_properties_changed(self, interface, changed, invalidated, path=None):
        if interface != DEVICE_IFACE or not changed:
            return
        self._handle_device(str(path), changed, partial=True)

    # -- device handling ------------------------------------------------

    def _handle_device(self, path, props, partial=False):
        mac, name = self._cache.get(path, (None, None))

        if 'Address' in props:
            mac = str(props['Address']).upper()
        if 'Name' in props:
            name = str(props['Name'])
        elif 'Alias' in props and name is None:
            name = str(props['Alias'])

        if mac is None:
            # A PropertiesChanged for a device we have not seen announced.
            # Derive the address from the object path: .../dev_AA_BB_CC_..
            leaf = path.rsplit('/', 1)[-1]
            if leaf.startswith('dev_'):
                mac = leaf[4:].replace('_', ':').upper()
            else:
                return

        self._cache[path] = (mac, name)

        if 'ServiceData' not in props:
            return

        service_data = {}
        for uuid, value in props['ServiceData'].items():
            service_data[str(uuid).lower()] = bytes(bytearray(value))
        if not service_data:
            return

        rssi = int(props['RSSI']) if 'RSSI' in props else None

        try:
            self.callback(Advertisement(path, mac, name, rssi, service_data))
        except Exception:
            log.exception('advertisement callback failed for %s', mac)

    def stop(self):
        if self._discovering and self.adapter_path:
            try:
                dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path),
                               ADAPTER_IFACE).StopDiscovery()
            except dbus.DBusException:
                pass
            self._discovering = False
