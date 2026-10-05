"""PH clan codec from proto_c2zn.tdr metadata (see docs/CLANS.md).

No native code or game assets are needed at runtime. Live UI validation is
still required; metadata validation does not prove callback acceptance.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

SCHEMA = json.loads(Path(__file__).with_name('clan_schema.json').read_text())['structures']
FORMATS = {2: 'b', 3: 'B', 5: 'h', 6: 'H', 7: 'i', 8: 'I', 11: 'q', 12: 'Q'}


class ClanWireError(ValueError):
    pass


def _count(field, values):
    n = int(values.get(field['count_field'], 0)) if field['count_field'] else field['capacity']
    if not 0 <= n <= field['capacity']:
        raise ClanWireError('array count exceeds ' + field['name'])
    return n


def encode(name, values=None):
    values = values or {}
    out = bytearray()
    for f in SCHEMA[name]:
        n = _count(f, values)
        array = bool(f['count_field']) or f['capacity'] > 1
        value = values.get(f['name'])
        if array:
            if value is None:
                items = [None] * n
            else:
                items = list(value)
                if len(items) != n:
                    raise ClanWireError('array length mismatch: ' + f['name'])
        else:
            items = [value]
        for item in items:
            t = f['type']
            if t == 1:
                out += encode(f['nested'], item)
            elif t == 21:
                try:
                    raw = (item or '').encode('latin1')
                except (UnicodeError, AttributeError) as exc:
                    raise ClanWireError('invalid PH string') from exc
                if b'\0' in raw or len(raw) >= f['string_capacity']:
                    raise ClanWireError('invalid string length: ' + f['name'])
                out += struct.pack('>I', len(raw) + 1) + raw + b'\0'
            elif t == 15:
                # Existing PH server uses an all-zero TDR datetime for unset.
                raw = bytes(item or bytes(8))
                if len(raw) != 8:
                    raise ClanWireError('invalid TDR datetime')
                out += raw
            else:
                try:
                    v = int(item or 0)
                    if t == 2 and 128 <= v <= 255:
                        v -= 256
                    out += struct.pack('>' + FORMATS[t], v)
                except (ValueError, TypeError, struct.error, KeyError) as exc:
                    raise ClanWireError('invalid scalar: ' + f['name']) from exc
    return bytes(out)


def _decode(name, body, off):
    values = {}
    for f in SCHEMA[name]:
        n = _count(f, values)
        items = []
        for _ in range(n):
            t = f['type']
            if t == 1:
                value, off = _decode(f['nested'], body, off)
            elif t == 21:
                if off + 4 > len(body):
                    raise ClanWireError('truncated string length')
                size = struct.unpack_from('>I', body, off)[0]
                off += 4
                if not 1 <= size <= f['string_capacity'] or off + size > len(body):
                    raise ClanWireError('invalid string length')
                raw = body[off:off + size]
                if raw[-1] != 0 or b'\0' in raw[:-1]:
                    raise ClanWireError('invalid string terminator')
                value = raw[:-1].decode('latin1')
                off += size
            elif t == 15:
                if off + 8 > len(body):
                    raise ClanWireError('truncated datetime')
                value = body[off:off + 8]
                off += 8
            else:
                try:
                    fmt = '>' + FORMATS[t]
                    value = struct.unpack_from(fmt, body, off)[0]
                    off += struct.calcsize(fmt)
                except (struct.error, KeyError) as exc:
                    raise ClanWireError('truncated or unsupported scalar') from exc
            items.append(value)
        values[f['name']] = items if f['count_field'] or f['capacity'] > 1 else items[0]
    return values, off


def decode(name, body):
    values, off = _decode(name, bytes(body), 0)
    if off != len(body):
        raise ClanWireError('unexpected trailing bytes')
    return values
