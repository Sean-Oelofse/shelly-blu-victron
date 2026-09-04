# dbus-shelly-blu

Read **Shelly BLU H&T** sensors on a **Venus OS** (Victron GX) device using the
GX's onboard Bluetooth, and publish each one as a native
`com.victronenergy.temperature` service — so they appear in the Device List,
in Settings → I/O, and on VRM alongside everything else.

No Shelly gateway, no MQTT bridge, no Node-RED, no pip packages.

## How it works

Shelly BLU devices broadcast their readings as **BTHome v2** BLE advertisements
under service UUID `0xFCD2`. The driver:

1. asks BlueZ (already running on the GX) for LE discovery over D-Bus,
2. decodes each `0xFCD2` advertisement (`bthome.py`),
3. creates one `com.victronenergy.temperature.shelly_blu_<mac>` service per
   sensor and keeps `/Temperature`, `/Humidity`, `/BatterySoc` and `/Rssi`
   updated.

Everything used — `dbus`, `gi.repository.GLib`, `velib_python` — already ships
with Venus OS.

## Files

| File | Purpose |
| --- | --- |
| `dbus_shelly_blu.py` | main driver, D-Bus services, config, CLI |
| `bthome.py` | BTHome v2 decoder (all object ids, optional encryption) |
| `blescanner.py` | BlueZ D-Bus LE scanner that keeps discovery alive |
| `test_bthome.py` | decoder self-test, runs anywhere, no hardware needed |
| `get.sh` | one-line installer that pulls this repo from GitHub |
| `install.sh` / `uninstall.sh` | daemontools service setup |
| `config.sample.json` | annotated example config |

## Install

Install straight from GitHub — ssh into the GX device and run:

```sh
wget -qO- https://raw.githubusercontent.com/Sean-Oelofse/shelly-blu-victron/main/get.sh | sh
```
(or `curl -fsSL https://raw.githubusercontent.com/Sean-Oelofse/shelly-blu-victron/main/get.sh | sh`)

`get.sh` downloads this repo, unpacks it to `/data/dbus-shelly-blu`, and runs
`install.sh`. Re-running it upgrades in place and keeps your existing
`config.json`. Install a branch or tag with `REF`, e.g.
`... | REF=v1.0 sh`.

Or copy the folder over yourself:

```sh
scp -r "Shelly BLU" root@<gx-ip>:/data/dbus-shelly-blu
ssh root@<gx-ip>
cd /data/dbus-shelly-blu && sh install.sh
```

`install.sh` copies `velib_python` out of the firmware into `ext/velib_python`,
creates `config.json`, links `/service/dbus-shelly-blu`, and adds the link to
`/data/rc.local` so it survives reboots **and firmware updates**.

### Find your sensors

```sh
svc -d /service/dbus-shelly-blu          # stop the service first, one scanner at a time
python3 /data/dbus-shelly-blu/dbus_shelly_blu.py --scan 30
```

```
MAC                NAME         RSSI   DATA
7C:C6:B6:6D:2A:1C  SBHT-003C    -62    battery=100, temperature=20.8, humidity=47
```

Put the MACs into `config.json`, then:

```sh
svc -u /service/dbus-shelly-blu
tail -F /var/log/dbus-shelly-blu/current
```

### Watch decoded data without touching D-Bus

```sh
python3 /data/dbus-shelly-blu/dbus_shelly_blu.py --dump -d
```

## Configuration

`config.json` (copied from `config.sample.json`):

| Key | Default | Meaning |
| --- | --- | --- |
| `adapter` | `null` | `"hci0"` to pin a specific adapter |
| `auto_discover` | `true` | publish sensors not listed in `devices` |
| `base_instance` | `60` | first VRM device instance handed out |
| `timeout` | `900` | seconds without an advertisement before a sensor is marked disconnected |
| `min_publish_interval` | `0` | throttle D-Bus updates, in seconds |
| `scan_uuid_filter` | `false` | let BlueZ pre-filter on the BTHome UUID (less D-Bus traffic, but some adapters then miss packets) |
| `require_shelly_name` | `false` | only accept devices whose BLE name starts with `SBHT`, `SBBT`, … |
| `log_level` | `"INFO"` | `DEBUG` logs every decoded advertisement |

Per device, under `devices`:

| Key | Meaning |
| --- | --- |
| `name` | initial custom name shown in the GUI |
| `instance` | fixed VRM device instance (otherwise auto-assigned) |
| `temperature_type` | `0` battery, `1` fridge, `2` generic |
| `offset` | °C calibration added to the reading |
| `bindkey` | 32 hex chars, only for sensors set to encrypted BTHome |
| `ignore` | `true` to skip this device entirely |

Custom name and temperature type are also writable from the GX GUI and VRM;
changes are stored in localsettings under `/Settings/Devices/shelly_blu_*`, so
they survive restarts and override the config values after first run.

Encrypted BTHome needs the `cryptography` module. It is not in the Venus OS
base image (`opkg install python3-cryptography` on a large image, or leave your
sensors unencrypted, which is the Shelly default).

## Testing the decoder

```sh
python3 test_bthome.py
```

Runs known-good BTHome v2 vectors, including the encrypted example from the
spec, and needs neither Bluetooth nor D-Bus.

## Notes and gotchas

- **Run one BLE scanner at a time.** Venus OS has its own `dbus-ble-sensors`
  (Ruuvi, Mopeka, …). If your sensors never show up, check whether it is
  holding the radio: `svstat /service/dbus-ble-sensors`, and try
  `svc -d /service/dbus-ble-sensors`. Also check Settings → I/O → Bluetooth
  sensors first — if your Venus OS version already supports BTHome natively,
  you do not need this driver at all.
- **The GX Bluetooth radio is also used for the VictronConnect Bluetooth
  interface.** Running discovery continuously is fine, but expect the occasional
  dropped advertisement; the driver keeps re-arming discovery and cycles it
  every 10 minutes so bluetoothd's device cache does not grow unbounded.
- **Range.** The Cerbo GX antenna is not great. A Shelly BLU H&T two rooms away
  will drop out; watch `/Rssi` (below about -90 dBm gets unreliable).
- **Reporting rate.** The H&T only advertises when a value changes or roughly
  once an hour, to save its CR2032. That is why `timeout` defaults to 900 s —
  do not set it below the sensor's own reporting interval or the sensor will
  flap between connected and disconnected.
- **Battery.** The H&T reports percent, not volts, so `/BatteryVoltage` stays
  empty unless a device really sends a voltage object; the percentage is on
  `/BatterySoc`.

## Status

Written against Venus OS 3.x and the BTHome v2 spec. The decoder is covered by
`test_bthome.py`; the D-Bus and BlueZ paths have not been run on hardware here,
so treat the first install as a shakedown and run with `-d` if anything looks
off.
