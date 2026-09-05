#!/usr/bin/env python3
"""
dbus-shelly-blu - read Shelly BLU H&T sensors on Venus OS (Victron GX).

Listens to BTHome v2 BLE advertisements with the GX device's onboard
Bluetooth adapter and publishes every sensor found as its own
com.victronenergy.temperature service, so it shows up under
Device List -> temperature sensors and on VRM.

Usage:
    python3 dbus_shelly_blu.py [-c config.json] [-d]
    python3 dbus_shelly_blu.py --scan       # list nearby BTHome sensors
    python3 dbus_shelly_blu.py --dump       # decode advertisements, no D-Bus

No pip packages required: it uses the dbus and gi bindings that ship with
Venus OS, plus velib_python from the Victron install.
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import OrderedDict

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

import bthome
from blescanner import BleScanner

VERSION = '1.0'
log = logging.getLogger('shelly-blu')

# Shelly BLU model prefixes as advertised in the BLE local name.
SHELLY_PREFIXES = ('SBHT', 'SBBT', 'SBDW', 'SBMO', 'SBWM', 'SBGW')

DEFAULT_CONFIG = {
    'adapter': None,            # None = first adapter, or e.g. "hci0"
    'auto_discover': True,      # publish sensors that are not in "devices"
    'base_instance': 60,        # first VRM device instance to hand out
    'timeout': 900,             # seconds without an advertisement -> offline
    'min_publish_interval': 0,  # seconds; 0 = publish every advertisement
    'scan_uuid_filter': False,  # let BlueZ pre-filter on the BTHome UUID
    'require_shelly_name': False,  # only accept SBxx-named devices
    'persist_discovered': True,    # write newly found sensors back to config.json
    'log_level': 'INFO',
    'devices': {},              # "AA:BB:CC:DD:EE:FF": {...}
}


def add_velib_to_path():
    """Locate velib_python (vedbus.py / settingsdevice.py) on Venus OS."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, 'ext', 'velib_python'),
        os.path.join(here, 'velib_python'),
        '/data/velib_python',
        '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python',
        '/opt/victronenergy/dbus-modbus-client/ext/velib_python',
        '/opt/victronenergy/dbus-fzsonick-48tl/ext/velib_python',
    ]
    try:
        import glob
        candidates += sorted(glob.glob('/opt/victronenergy/*/ext/velib_python'))
    except Exception:
        pass

    for path in candidates:
        if os.path.isfile(os.path.join(path, 'vedbus.py')):
            sys.path.insert(0, path)
            return path
    return None


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.isfile(path):
        user = None
        try:
            with open(path) as f:
                user = json.load(f)
        except ValueError as e:
            # A hand-edit typo (missing comma, trailing comma, ...) must not
            # crash-loop the service. Keep running on defaults and, crucially,
            # do not let auto-discovery overwrite the file the user is fixing.
            log.error('%s is not valid JSON: %s', path, e)
            log.error('keeping the service alive on built-in defaults; fix the '
                      'file and restart. Per-device settings are ignored until '
                      'then, and the file will not be modified.')
            cfg['persist_discovered'] = False
        except OSError as e:
            log.error('could not read %s: %s; using defaults', path, e)
        if isinstance(user, dict):
            unknown = set(user) - set(DEFAULT_CONFIG)
            if unknown:
                log.warning('ignoring unknown config keys: %s', ', '.join(sorted(unknown)))
            cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
        elif user is not None:
            log.error('%s must contain a JSON object, ignoring its contents', path)
            cfg['persist_discovered'] = False
    elif path:
        log.info('no config file at %s, using defaults', path)

    # normalise mac keys to upper case with colons
    devices = {}
    for mac, dev in (cfg.get('devices') or {}).items():
        devices[mac.replace('-', ':').upper()] = dev or {}
    cfg['devices'] = devices
    return cfg


def is_shelly(name):
    return bool(name) and name.upper().startswith(SHELLY_PREFIXES)


def fmt(spec):
    """A vedbus gettextcallback that tolerates an unset (None) value."""
    return lambda path, value: '' if value is None else spec % value


# ---------------------------------------------------------------------------
# one D-Bus service per sensor
# ---------------------------------------------------------------------------

class ShellyBluSensor(object):
    """A single Shelly BLU sensor exported as com.victronenergy.temperature."""

    def __init__(self, mac, model, cfg, dev_cfg, used_instances=None):
        from settingsdevice import SettingsDevice

        self.mac = mac
        self.model = model or 'Shelly BLU'
        self.cfg = cfg
        self.dev_cfg = dev_cfg
        self.offset = float(dev_cfg.get('offset', 0.0))
        self.last_seen = 0.0
        self.last_publish = 0.0
        self.last_packet_id = None
        self.online = False

        # Instances already handed out to sibling sensors in this process.
        # _pick_instance only sees services that are live on the bus, but a
        # sensor created moments earlier may not have claimed its bus name
        # yet, so two sensors discovered back-to-back could otherwise grab the
        # same instance and collide in the GUI/VRM.
        used_instances = set(used_instances or ())

        ident = 'shelly_blu_' + mac.replace(':', '').lower()
        bus = dbus.SystemBus()

        explicit = dev_cfg.get('instance')
        if explicit is not None:
            instance = int(explicit)
        else:
            instance = self._pick_instance(bus, cfg.get('base_instance', 60),
                                           used_instances)

        self.settings = SettingsDevice(bus, {
            'instance': ['/Settings/Devices/%s/ClassAndVrmInstance' % ident,
                         'temperature:%d' % int(instance), 0, 0],
            'customname': ['/Settings/Devices/%s/CustomName' % ident,
                           dev_cfg.get('name', ''), 0, 0],
            'temperaturetype': ['/Settings/Devices/%s/TemperatureType' % ident,
                                int(dev_cfg.get('temperature_type', 2)), 0, 2],
        }, eventCallback=None)

        try:
            instance = int(str(self.settings['instance']).split(':')[1])
        except (IndexError, ValueError):
            instance = int(instance)

        # Self-heal a duplicated instance. An earlier version could persist the
        # same instance for two sensors that raced onto it; that reload would
        # keep them colliding forever. If the instance we read back is already
        # taken, hand out a fresh one and persist it (auto-assigned only; an
        # explicitly configured instance is left as the user asked).
        if explicit is None and instance in used_instances:
            new_instance = self._pick_instance(
                bus, cfg.get('base_instance', 60), used_instances)
            try:
                self.settings['instance'] = 'temperature:%d' % new_instance
            except Exception:
                log.exception('%s: could not persist reassigned instance', mac)
            log.warning('%s had duplicate instance %d, reassigned to %d',
                        mac, instance, new_instance)
            instance = new_instance

        self.instance = instance

        service_name = 'com.victronenergy.temperature.%s' % ident
        self.service = self._create_service(service_name, bus)

        s = self.service
        s.add_path('/Mgmt/ProcessName', 'dbus-shelly-blu')
        s.add_path('/Mgmt/ProcessVersion', VERSION)
        s.add_path('/Mgmt/Connection', 'BLE %s' % mac)
        s.add_path('/DeviceInstance', instance)
        s.add_path('/ProductId', 0xB040)
        s.add_path('/ProductName', self.model)
        s.add_path('/FirmwareVersion', None)
        s.add_path('/HardwareVersion', None)
        s.add_path('/Serial', mac)
        s.add_path('/Connected', 0)

        s.add_path('/CustomName', self.settings['customname'],
                   writeable=True, onchangecallback=self._on_customname)
        s.add_path('/TemperatureType', int(self.settings['temperaturetype']),
                   writeable=True, onchangecallback=self._on_temperaturetype,
                   gettextcallback=lambda p, v: {0: 'Battery', 1: 'Fridge'}.get(v, 'Generic'))

        s.add_path('/Temperature', None, gettextcallback=fmt('%.1f C'))
        s.add_path('/Humidity', None, gettextcallback=fmt('%.0f %%'))
        s.add_path('/Status', 4)  # 0=ok 1=disconnected 4=unknown
        s.add_path('/BatterySoc', None, gettextcallback=fmt('%.0f %%'))
        s.add_path('/BatteryVoltage', None, gettextcallback=fmt('%.2f V'))
        s.add_path('/Rssi', None, gettextcallback=fmt('%d dBm'))

        self._register()
        log.info('registered %s (instance %d) for %s', service_name, instance, mac)

    # -- plumbing -------------------------------------------------------

    @staticmethod
    def _pick_instance(bus, base, extra_used=None):
        """First device instance not already used by a temperature service.

        extra_used seeds the set with instances already assigned in this
        process but perhaps not yet visible on the bus.
        """
        used = set(extra_used or ())
        try:
            for name in bus.list_names():
                if not str(name).startswith('com.victronenergy.temperature.'):
                    continue
                try:
                    obj = bus.get_object(str(name), '/DeviceInstance')
                    used.add(int(obj.GetValue()))
                except dbus.DBusException:
                    pass
        except dbus.DBusException:
            pass
        instance = int(base)
        while instance in used:
            instance += 1
        return instance

    def _create_service(self, name, bus):
        import inspect
        from vedbus import VeDbusService
        # velib_python >= v3.20 wants an explicit register() after the paths
        # are added; older versions register in the constructor and do not
        # accept the bus/register kwargs. Inspect the signature and pass only
        # what it supports: constructing-and-catching would leave a half-built
        # VeDbusService whose __del__ then raises a noisy AttributeError on
        # '_dbusnodes' when it is garbage collected.
        try:
            params = inspect.signature(VeDbusService.__init__).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs = {}
        if 'bus' in params:
            kwargs['bus'] = bus
        if 'register' in params:
            kwargs['register'] = False
        try:
            return VeDbusService(name, **kwargs)
        except TypeError:
            return VeDbusService(name)

    def _register(self):
        register = getattr(self.service, 'register', None)
        if callable(register):
            try:
                register()
            except Exception:
                log.debug('service.register() not needed')

    def _on_customname(self, path, value):
        self.settings['customname'] = str(value)
        return True

    def _on_temperaturetype(self, path, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return False
        if value not in (0, 1, 2):
            return False
        self.settings['temperaturetype'] = value
        return True

    # -- data -----------------------------------------------------------

    def update(self, data, rssi):
        now = time.time()
        self.last_seen = now

        pid = data.get('packet_id')
        if pid is not None and pid == self.last_packet_id:
            return  # same advertisement repeated by the radio
        self.last_packet_id = pid

        interval = self.cfg.get('min_publish_interval', 0)
        if interval and self.online and (now - self.last_publish) < interval:
            return
        self.last_publish = now

        s = self.service
        if 'temperature' in data:
            s['/Temperature'] = round(data['temperature'] + self.offset, 2)
        if 'humidity' in data:
            s['/Humidity'] = data['humidity']
        if 'battery' in data:
            s['/BatterySoc'] = data['battery']
        # The H&T reports percent, not volts. Only publish a voltage if the
        # device really sent one (BTHome object 0x0C / 0x4A).
        if 'voltage' in data:
            s['/BatteryVoltage'] = data['voltage']
        if rssi is not None:
            s['/Rssi'] = rssi

        if not self.online:
            self.online = True
            s['/Connected'] = 1
            log.info('%s online', self.mac)
        s['/Status'] = 0

        log.debug('%s %s', self.mac, {k: v for k, v in data.items()
                                      if k not in ('encrypted', 'trigger_based')})

    def check_timeout(self, now, timeout):
        if not self.online or (now - self.last_seen) <= timeout:
            return
        self.online = False
        s = self.service
        s['/Connected'] = 0
        s['/Status'] = 1
        s['/Temperature'] = None
        s['/Humidity'] = None
        log.warning('%s offline, no advertisement for %ds', self.mac, timeout)

    def __del__(self):
        try:
            self.service.__del__()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

class Driver(object):
    def __init__(self, cfg, dbus_enabled=True, config_path=None):
        self.cfg = cfg
        self.dbus_enabled = dbus_enabled
        self.config_path = config_path
        self.sensors = OrderedDict()
        self.ignored = set()
        self.used_instances = set()
        self.bus = dbus.SystemBus()
        uuids = [bthome.BTHOME_UUID] if cfg.get('scan_uuid_filter') else None
        self.scanner = BleScanner(self.bus, self.on_advertisement,
                                  adapter=cfg.get('adapter'), uuids=uuids)

    def start(self):
        self.scanner.start()
        GLib.timeout_add_seconds(10, self._timeout_tick)

    def _timeout_tick(self):
        now = time.time()
        timeout = self.cfg.get('timeout', 900)
        for sensor in list(self.sensors.values()):
            sensor.check_timeout(now, timeout)
        return True

    def _bindkey(self, mac):
        key = (self.cfg['devices'].get(mac) or {}).get('bindkey')
        if not key:
            return None
        key = bytes.fromhex(key.replace(' ', ''))
        if len(key) != 16:
            log.error('%s: bindkey must be 32 hex characters', mac)
            return None
        return key

    def _persist_device(self, mac, name):
        """Record a newly discovered sensor in config.json with show:true.

        Always registers the device in memory so it is not re-discovered (and
        re-written) on every advertisement; the on-disk write is best effort and
        gated by persist_discovered.
        """
        entry = {'name': name or '', 'show': True}
        self.cfg['devices'][mac] = entry

        if not (self.dbus_enabled and self.config_path
                and self.cfg.get('persist_discovered', True)):
            return

        try:
            data = {}
            if os.path.isfile(self.config_path):
                with open(self.config_path) as f:
                    data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            devices = data.setdefault('devices', {})
            if not isinstance(devices, dict):
                return
            norm = mac.replace('-', ':').upper()
            for existing in devices:
                if str(existing).replace('-', ':').upper() == norm:
                    return  # user already lists it, leave their entry alone
            devices[mac] = entry
            tmp = self.config_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
                f.write('\n')
            os.replace(tmp, self.config_path)
            log.info('added %s to %s (show:true)', mac, self.config_path)
        except Exception:
            log.exception('could not write %s to %s', mac, self.config_path)

    def on_advertisement(self, adv):
        payload = adv.service_data.get(bthome.BTHOME_UUID)
        if payload is None:
            return
        if adv.mac in self.ignored:
            return

        dev_cfg = self.cfg['devices'].get(adv.mac)
        newly_discovered = False
        if dev_cfg is None:
            if not self.cfg.get('auto_discover', True):
                return
            if self.cfg.get('require_shelly_name') and not is_shelly(adv.name):
                return
            dev_cfg = {}
            newly_discovered = True
        # "show": false hides a sensor (kept in config.json so it is easy to
        # turn back on); "ignore": true is the older spelling of the same thing.
        if dev_cfg.get('ignore') or dev_cfg.get('show') is False:
            self.ignored.add(adv.mac)
            log.info('hiding %s (show:false)', adv.mac)
            return

        try:
            data = bthome.parse(payload, bthome.mac_to_bytes(adv.mac),
                                self._bindkey(adv.mac))
        except bthome.BTHomeError as e:
            log.warning('%s (%s): %s [%s]', adv.mac, adv.name, e, payload.hex())
            return

        # Only publish devices that actually carry a temperature.
        if 'temperature' not in data:
            log.debug('%s has no temperature, skipping: %s', adv.mac, data)
            return

        # Remember a freshly discovered sensor in config.json so the user has a
        # line to edit (name, offset, show:false, ...) without hunting for MACs.
        if newly_discovered:
            self._persist_device(adv.mac, adv.name)
            dev_cfg = self.cfg['devices'].get(adv.mac, dev_cfg)

        self.handle(adv, data, dev_cfg)

    def handle(self, adv, data, dev_cfg):
        sensor = self.sensors.get(adv.mac)
        if sensor is None:
            if not self.dbus_enabled:
                self._print(adv, data)
                return
            try:
                sensor = ShellyBluSensor(adv.mac, adv.name, self.cfg, dev_cfg,
                                         used_instances=self.used_instances)
            except Exception:
                log.exception('failed to create service for %s', adv.mac)
                self.ignored.add(adv.mac)
                return
            self.sensors[adv.mac] = sensor
            self.used_instances.add(sensor.instance)
        if self.dbus_enabled:
            sensor.update(data, adv.rssi)
        else:
            self._print(adv, data)

    @staticmethod
    def _print(adv, data):
        parts = []
        if 'temperature' in data:
            parts.append('%.1f C' % data['temperature'])
        if 'humidity' in data:
            parts.append('%.0f %%rh' % data['humidity'])
        if 'battery' in data:
            parts.append('bat %d%%' % data['battery'])
        if 'button' in data and data['button']:
            parts.append('button=%s' % data['button'])
        parts.append('rssi %s' % adv.rssi)
        print('%s  %-18s %-10s %s' % (time.strftime('%H:%M:%S'), adv.mac,
                                      adv.name or '?', '  '.join(parts)))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# scan-only helper
# ---------------------------------------------------------------------------

def run_scan(cfg, seconds):
    found = OrderedDict()
    bus = dbus.SystemBus()

    def on_adv(adv):
        payload = adv.service_data.get(bthome.BTHOME_UUID)
        if payload is None:
            return
        try:
            data = bthome.parse(payload, bthome.mac_to_bytes(adv.mac))
            note = ', '.join('%s=%s' % (k, v) for k, v in data.items()
                             if k not in ('encrypted', 'trigger_based', 'packet_id'))
        except bthome.BTHomeError as e:
            note = '(%s)' % e
        found[adv.mac] = (adv.name, adv.rssi, note)

    scanner = BleScanner(bus, on_adv, adapter=cfg.get('adapter'))
    scanner.start()
    loop = GLib.MainLoop()
    print('scanning for BTHome sensors for %d seconds...' % seconds)
    GLib.timeout_add_seconds(seconds, lambda: (loop.quit(), False)[1])
    loop.run()
    scanner.stop()

    if not found:
        print('\nnothing found. Is the sensor in range and is Bluetooth enabled?')
        return 1
    print('\n%-18s %-12s %-6s %s' % ('MAC', 'NAME', 'RSSI', 'DATA'))
    for mac, (name, rssi, note) in found.items():
        print('%-18s %-12s %-6s %s' % (mac, name or '?', rssi, note))
    print('\nAdd the MACs you want to config.json under "devices".')
    return 0


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Shelly BLU H&T driver for Venus OS')
    ap.add_argument('-c', '--config', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'config.json'),
        help='path to config.json')
    ap.add_argument('-d', '--debug', action='store_true', help='debug logging')
    ap.add_argument('--scan', nargs='?', type=int, const=20, metavar='SECONDS',
                    help='list nearby BTHome sensors and exit')
    ap.add_argument('--dump', action='store_true',
                    help='print decoded advertisements, do not publish on D-Bus')
    args = ap.parse_args()

    logging.basicConfig(
        format='%(levelname)-8s %(name)s %(message)s',
        level=logging.DEBUG if args.debug else logging.INFO)

    cfg = load_config(args.config)
    if not args.debug:
        logging.getLogger().setLevel(
            getattr(logging, str(cfg.get('log_level', 'INFO')).upper(), logging.INFO))

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    if args.scan is not None:
        return run_scan(cfg, args.scan)

    if not args.dump:
        velib = add_velib_to_path()
        if velib is None:
            log.error('velib_python not found. Copy vedbus.py and '
                      'settingsdevice.py into ./ext/velib_python, or run with '
                      '--dump to test decoding only.')
            return 1
        log.info('using velib_python from %s', velib)

    driver = Driver(cfg, dbus_enabled=not args.dump, config_path=args.config)
    driver.start()

    log.info('dbus-shelly-blu %s started', VERSION)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    finally:
        driver.scanner.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
