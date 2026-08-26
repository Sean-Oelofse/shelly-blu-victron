#!/usr/bin/env python3
"""Self-test for the BTHome decoder. No hardware or D-Bus needed.

    python3 test_bthome.py
"""

import sys

import bthome

FAILED = []


def check(label, got, expected):
    ok = got == expected
    print('%-4s %s' % ('ok' if ok else 'FAIL', label))
    if not ok:
        print('       got      %r' % (got,))
        print('       expected %r' % (expected,))
        FAILED.append(label)


def sub(data, keys):
    return {k: data[k] for k in keys if k in data}


def main():
    # Shelly BLU H&T: packet id, battery %, temperature (0.1 C), humidity (1 %)
    d = bthome.parse(bytes.fromhex('40004e016445d0002e2f'))
    check('Shelly BLU H&T', sub(d, ('packet_id', 'battery', 'temperature', 'humidity')),
          {'packet_id': 0x4E, 'battery': 100, 'temperature': 20.8, 'humidity': 47})

    # Negative temperature (signed 16 bit, 0.1 C)
    d = bthome.parse(bytes.fromhex('4045ceff'))
    check('negative temperature', d['temperature'], -5.0)

    # BTHome spec example: temperature 0x02 (0.01 C) + humidity 0x03 (0.01 %)
    d = bthome.parse(bytes.fromhex('4002c40903bf13'))
    check('spec unencrypted', sub(d, ('temperature', 'humidity')),
          {'temperature': 25.0, 'humidity': 50.55})

    # Trigger based flag (Shelly BLU Button and friends)
    d = bthome.parse(bytes.fromhex('44002a3a01'))
    check('button press', sub(d, ('trigger_based', 'button')),
          {'trigger_based': True, 'button': 'press'})

    # Repeated measurement types get numbered suffixes
    d = bthome.parse(bytes.fromhex('4002c4090245cc'))
    check('two temperatures', sub(d, ('temperature', 'temperature_2')),
          {'temperature': 25.0, 'temperature_2': -132.43})

    # An unknown object id must raise rather than emit misaligned values
    try:
        bthome.parse(bytes.fromhex('40ff0102'))
        check('unknown object id raises', 'no exception', 'BTHomeError')
    except bthome.BTHomeError:
        check('unknown object id raises', True, True)

    # Wrong BTHome version
    try:
        bthome.parse(bytes.fromhex('2002c409'))
        check('version check', 'no exception', 'BTHomeError')
    except bthome.BTHomeError:
        check('version check', True, True)

    # Encrypted example from the BTHome spec
    mac = bthome.mac_to_bytes('A4:C1:38:8D:5B:AA')
    key = bytes.fromhex('231d39c1d7cc1ab1aee224cd096db932')
    try:
        d = bthome.parse(bytes.fromhex('41a47266c95f730011223378237214'), mac, key)
        check('spec encrypted', sub(d, ('temperature', 'humidity')),
              {'temperature': 25.06, 'humidity': 50.55})
    except bthome.BTHomeError as e:
        if 'cryptography' in str(e):
            print('skip encrypted test: %s' % e)
        else:
            check('spec encrypted', str(e), 'decrypted payload')

    print()
    if FAILED:
        print('%d test(s) failed' % len(FAILED))
        return 1
    print('all tests passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
