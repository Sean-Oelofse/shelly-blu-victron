"""
BTHome v2 advertisement decoder.

Shelly BLU devices (H&T, Button, Door/Window, Motion) broadcast their
measurements as BTHome v2 service data under UUID 0xFCD2.

Spec: https://bthome.io/format/
"""

import struct

BTHOME_UUID = '0000fcd2-0000-1000-8000-00805f9b34fb'
BTHOME_UUID16 = 0xFCD2


class BTHomeError(Exception):
    pass


# object id -> (name, length, signed, factor)
SENSOR_OBJECTS = {
    0x00: ('packet_id',        1, False, 1),
    0x01: ('battery',          1, False, 1),
    0x02: ('temperature',      2, True,  0.01),
    0x03: ('humidity',         2, False, 0.01),
    0x04: ('pressure',         3, False, 0.01),
    0x05: ('illuminance',      3, False, 0.01),
    0x06: ('mass_kg',          2, False, 0.01),
    0x07: ('mass_lb',          2, False, 0.01),
    0x08: ('dewpoint',         2, True,  0.01),
    0x09: ('count',            1, False, 1),
    0x0A: ('energy',           3, False, 0.001),
    0x0B: ('power',            3, False, 0.01),
    0x0C: ('voltage',          2, False, 0.001),
    0x0D: ('pm2_5',            2, False, 1),
    0x0E: ('pm10',             2, False, 1),
    0x12: ('co2',              2, False, 1),
    0x13: ('tvoc',             2, False, 1),
    0x14: ('moisture',         2, False, 0.01),
    0x2E: ('humidity',         1, False, 1),
    0x2F: ('moisture',         1, False, 1),
    0x3D: ('count',            2, False, 1),
    0x3E: ('count',            4, False, 1),
    0x3F: ('rotation',         2, True,  0.1),
    0x40: ('distance_mm',      2, False, 1),
    0x41: ('distance_m',       2, False, 0.1),
    0x42: ('duration',         3, False, 0.001),
    0x43: ('current',          2, False, 0.001),
    0x44: ('speed',            2, False, 0.01),
    0x45: ('temperature',      2, True,  0.1),
    0x46: ('uv_index',         1, False, 0.1),
    0x47: ('volume',           2, False, 0.1),
    0x48: ('volume_ml',        2, False, 1),
    0x49: ('volume_flow_rate', 2, False, 0.001),
    0x4A: ('voltage',          2, False, 0.1),
    0x4B: ('gas',              3, False, 0.001),
    0x4C: ('gas',              4, False, 0.001),
    0x4D: ('energy',           4, False, 0.001),
    0x4E: ('volume',           4, False, 0.001),
    0x4F: ('water',            4, False, 0.001),
    0x50: ('timestamp',        4, False, 1),
    0x51: ('acceleration',     2, False, 0.001),
    0x52: ('gyroscope',        2, False, 0.001),
}

# object id -> name, each carries a single 0/1 byte
BINARY_OBJECTS = {
    0x0F: 'generic_boolean',
    0x10: 'power_on',
    0x11: 'opening',
    0x15: 'battery_low',
    0x16: 'battery_charging',
    0x17: 'carbon_monoxide',
    0x18: 'cold',
    0x19: 'connectivity',
    0x1A: 'door',
    0x1B: 'garage_door',
    0x1C: 'gas_detected',
    0x1D: 'heat',
    0x1E: 'light',
    0x1F: 'lock',
    0x20: 'moisture_detected',
    0x21: 'motion',
    0x22: 'moving',
    0x23: 'occupancy',
    0x24: 'plug',
    0x25: 'presence',
    0x26: 'problem',
    0x27: 'running',
    0x28: 'safety',
    0x29: 'smoke',
    0x2A: 'sound',
    0x2B: 'tamper',
    0x2C: 'vibration',
    0x2D: 'window',
}

BUTTON_EVENTS = {
    0x00: None,
    0x01: 'press',
    0x02: 'double_press',
    0x03: 'triple_press',
    0x04: 'long_press',
    0x05: 'long_double_press',
    0x06: 'long_triple_press',
    0xFE: 'hold_press',
}

DIMMER_EVENTS = {0x00: None, 0x01: 'rotate_left', 0x02: 'rotate_right'}


def _uint(data):
    return int.from_bytes(data, 'little', signed=False)


def _int(data):
    return int.from_bytes(data, 'little', signed=True)


def decrypt(payload, mac, bindkey):
    """Decrypt an encrypted BTHome v2 payload (AES-CCM, 4 byte MIC).

    payload is the raw service data including the device info byte.
    Returns the decrypted measurement bytes.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    except ImportError:
        raise BTHomeError('encrypted BTHome needs the python3 cryptography module')

    if len(payload) < 15:
        raise BTHomeError('encrypted payload too short')

    device_info = payload[0:1]
    ciphertext = payload[1:-8]
    counter = payload[-8:-4]
    mic = payload[-4:]

    nonce = mac + struct.pack('<H', BTHOME_UUID16) + device_info + counter
    try:
        return AESCCM(bindkey, tag_length=4).decrypt(nonce, ciphertext + mic, b'\x11')
    except Exception as e:
        raise BTHomeError('decryption failed: %s' % e)


def parse(payload, mac=None, bindkey=None):
    """Decode a BTHome v2 service data payload.

    payload  raw bytes of the 0xFCD2 service data
    mac      6 bytes, big endian, only needed for encrypted devices
    bindkey  16 byte encryption key, only for encrypted devices

    Returns a dict of measurements, e.g.
        {'packet_id': 12, 'battery': 100, 'temperature': 21.3, 'humidity': 47}
    """
    if not payload:
        raise BTHomeError('empty payload')

    device_info = payload[0]
    version = (device_info >> 5) & 0x07
    if version != 2:
        raise BTHomeError('unsupported BTHome version %d' % version)

    encrypted = bool(device_info & 0x01)
    result = {
        'encrypted': encrypted,
        'trigger_based': bool(device_info & 0x04),
    }

    if encrypted:
        if not bindkey:
            raise BTHomeError('device is encrypted but no bindkey configured')
        if mac is None:
            raise BTHomeError('mac address required to decrypt')
        data = decrypt(payload, mac, bindkey)
    else:
        data = payload[1:]

    i = 0
    seen = {}
    while i < len(data):
        obj_id = data[i]
        i += 1

        if obj_id in SENSOR_OBJECTS:
            name, length, signed, factor = SENSOR_OBJECTS[obj_id]
            if i + length > len(data):
                raise BTHomeError('truncated object 0x%02X' % obj_id)
            raw = data[i:i + length]
            i += length
            value = _int(raw) if signed else _uint(raw)
            if factor != 1:
                value = round(value * factor, 3)

        elif obj_id in BINARY_OBJECTS:
            name = BINARY_OBJECTS[obj_id]
            if i + 1 > len(data):
                raise BTHomeError('truncated object 0x%02X' % obj_id)
            value = bool(data[i])
            i += 1

        elif obj_id == 0x3A:  # button event
            if i + 1 > len(data):
                raise BTHomeError('truncated button event')
            name = 'button'
            value = BUTTON_EVENTS.get(data[i], 'unknown')
            i += 1

        elif obj_id == 0x3B:  # dimmer event
            if i + 2 > len(data):
                raise BTHomeError('truncated dimmer event')
            name = 'dimmer'
            event = DIMMER_EVENTS.get(data[i])
            steps = data[i + 1]
            value = None if event is None else '%s:%d' % (event, steps)
            i += 2

        elif obj_id in (0x53, 0x54):  # text / raw, length prefixed
            if i >= len(data):
                raise BTHomeError('truncated text/raw object')
            name = 'text' if obj_id == 0x53 else 'raw'
            length = data[i]
            i += 1
            raw = data[i:i + length]
            i += length
            value = raw.decode('utf-8', 'replace') if obj_id == 0x53 else raw.hex()

        else:
            # Unknown id: its length is unknown, so stop rather than emit
            # garbage from a misaligned parse.
            raise BTHomeError('unknown object id 0x%02X at offset %d' % (obj_id, i - 1))

        # BTHome allows repeated measurement types; they arrive in ascending
        # object id order and become temperature, temperature_2, ...
        if name in seen:
            seen[name] += 1
            name = '%s_%d' % (name, seen[name])
        else:
            seen[name] = 1
        result[name] = value

    return result


def mac_to_bytes(mac):
    """Turn AA:BB:CC:DD:EE:FF into the matching 6 raw bytes."""
    return bytes.fromhex(mac.replace(':', '').replace('-', ''))
