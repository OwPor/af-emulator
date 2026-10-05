"""UIN-based login nicknames for PH 1.0.0.24 across all game modes."""
from collections import OrderedDict
import os
from pathlib import Path
import sqlite3
import struct
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
DECODE_KEY = b"\x00" * 16

def decrypt_wire(wire):
    if len(wire) < 20:
        raise ValueError("short DS datagram")
    n = struct.unpack_from("<I", wire, 0)[0]
    enc = wire[4:]
    if len(enc) % 16:
        raise ValueError("ciphertext not block aligned")
    dec = Cipher(
        algorithms.AES(DECODE_KEY), modes.ECB(),
        backend=default_backend()
    ).decryptor()
    padded = dec.update(enc) + dec.finalize()
    if n > len(padded):
        raise ValueError(f"clear length {n} > decrypted {len(padded)}")
    return padded[:n]

def bits_from_bytes(data):
    return [(b >> bit) & 1 for b in data for bit in range(8)]

def bits_to_bytes(bits):
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)

def read_bits(bits, pos, count):
    if pos + count > len(bits):
        raise ValueError("bitstream truncated")
    v = 0
    for i in range(count):
        v |= bits[pos + i] << i
    return v, pos + count

def read_int(bits, pos, maximum):
    v = 0
    mask = 1
    while mask < maximum:
        b, pos = read_bits(bits, pos, 1)
        if b:
            v |= mask
        mask <<= 1
    return v, pos

def parse_packet(plain):
    bits = bits_from_bytes(plain)
    stop = next(
        (i for i in range(len(bits)-1, -1, -1) if bits[i]), None
    )
    if stop is None:
        raise ValueError("no stop bit")

    pos = 0
    packet_id, pos = read_int(bits, pos, 16384)
    records = []

    while pos < stop:
        is_ack, pos = read_bits(bits, pos, 1)
        if is_ack:
            aid, pos = read_int(bits, pos, 16384)
            records.append(("ACK", aid))
            continue

        ctl, pos = read_bits(bits, pos, 1)
        op = cl = 0
        if ctl:
            op, pos = read_bits(bits, pos, 1)
            cl, pos = read_bits(bits, pos, 1)

        rel, pos = read_bits(bits, pos, 1)
        ch, pos = read_int(bits, pos, 1023)

        seq = 0
        if rel:
            seq, pos = read_int(bits, pos, 1024)

        ctype = 0
        if rel or op:
            ctype, pos = read_int(bits, pos, 8)

        nbits, pos = read_int(bits, pos, 4096)
        if pos + nbits > stop:
            raise ValueError("bunch overrun")
        payload = bits_to_bytes(bits[pos:pos+nbits])
        pos += nbits

        records.append((
            "BUNCH", ctl, op, cl, rel, ch, seq, ctype, nbits, payload
        ))

    return packet_id, records

def encrypt_wire(plain):
    """Rebuild the AF DS zero-key AES-ECB wrapper after the login edit."""
    plain = bytes(plain)
    padded = plain + (b"\x00" * ((-len(plain)) & 0x0F))
    enc = Cipher(
        algorithms.AES(DECODE_KEY), modes.ECB(),
        backend=default_backend()
    ).encryptor()
    ciphertext = enc.update(padded) + enc.finalize()
    return struct.pack("<I", len(plain)) + ciphertext

def write_int(bits, value, maximum):
    mask = 1
    while mask < maximum:
        bits.append(1 if (int(value) & mask) else 0)
        mask <<= 1

def build_packet(packet_id, records):
    """Inverse of parse_packet(), preserving ACK and bunch metadata."""
    bits = []
    write_int(bits, int(packet_id) & 0x3FFF, 16384)

    for r in records:
        if r[0] == "ACK":
            bits.append(1)
            write_int(bits, int(r[1]) & 0x3FFF, 16384)
            continue

        _, ctl, op, cl, rel, ch, seq, ctype, nbits, payload = r
        if not 0 <= int(nbits) < 4096:
            raise ValueError("login bunch exceeds the PH packet bit limit")
        bits.append(0)
        bits.append(1 if ctl else 0)
        if ctl:
            bits.append(1 if op else 0)
            bits.append(1 if cl else 0)
        bits.append(1 if rel else 0)
        write_int(bits, int(ch), 1023)
        if rel:
            write_int(bits, int(seq), 1024)
        if rel or op:
            write_int(bits, int(ctype), 8)
        write_int(bits, int(nbits), 4096)
        payload_bits = bits_from_bytes(bytes(payload))
        if int(nbits) > len(payload_bits):
            raise ValueError("payload shorter than declared bit count")
        bits.extend(payload_bits[:int(nbits)])

    bits.append(1)
    return bits_to_bytes(bits)


def read_string(data, offset, limit):
    if offset + 4 > limit:
        raise ValueError('truncated FString count')
    count = struct.unpack_from('<i', data, offset)[0]
    start = offset + 4
    if count == 0:
        return '', start, False
    wide = count < 0
    size = abs(count) * (2 if wide else 1)
    if not 1 <= abs(count) <= 4096 or start + size > limit:
        raise ValueError('truncated FString contents')
    raw = data[start:start + size]
    suffix = b'\0\0' if wide else b'\0'
    if not raw.endswith(suffix):
        raise ValueError('unterminated FString')
    return raw[:-len(suffix)].decode('utf-16-le' if wide else 'latin1'), start + size, wide


def write_string(value, wide=False):
    if not wide:
        try:
            raw = value.encode('latin1') + b'\0'
            return struct.pack('<i', len(raw)) + raw
        except UnicodeEncodeError:
            pass
    raw = value.encode('utf-16-le') + b'\0\0'
    return struct.pack('<i', -(len(raw) // 2)) + raw


def valid_nickname(value):
    value = str(value or '').strip()
    if not 1 <= len(value) <= 31 or any(ord(c) < 32 or ord(c) == 127 or c in '?\0' for c in value):
        return None
    value.encode('utf-16-le')
    return value


def nickname_from_db(db_path, uin):
    path = Path(db_path).resolve()
    if not path.is_file():
        return None
    # Never create, migrate, or update the account database from the relay.
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.05)
    try:
        row = conn.execute('SELECT nickname FROM player_profiles WHERE uin = ?', (uin,)).fetchone()
    finally:
        conn.close()
    return valid_nickname(row[0]) if row and row[0] is not None else None


def rewrite_url(url, lookup):
    parts = url.split('?')
    uins, names = [], []
    for index, option in enumerate(parts[1:], 1):
        key, sep, value = option.partition('=')
        if key.casefold() == 'uin':
            if not sep or not value.isascii() or not value.isdecimal():
                raise ValueError('invalid login UIN')
            uins.append(int(value, 10))
        if key.casefold() == 'name':
            names.append(index)
    if len(uins) != 1 or len(names) > 1:
        return url, None
    uin = uins[0]
    if not 10001 <= uin <= 0x7FFFFFFFFFFFFFFF:
        return url, None
    wanted = lookup(uin)
    if wanted is None:
        return url, {'uin': uin, 'changed': False, 'missing_profile': True}
    old_name = parts[names[0]].partition('=')[2] if names else ''
    if old_name == wanted and names:
        return url, {'uin': uin, 'changed': False, 'nickname': wanted}
    if names:
        key = parts[names[0]].partition('=')[0]
        parts[names[0]] = key + '=' + wanted
    else:
        parts.append('Name=' + wanted)
    return '?'.join(parts), {'uin': uin, 'changed': True, 'nickname': wanted, 'old_name': old_name}


def rewrite_control(payload, valid_bits, lookup):
    limit, pos = valid_bits // 8, 0
    edits, infos = [], []
    joined = False
    while pos < limit:
        msg = payload[pos]
        pos += 1
        if msg == 4:
            if pos + 4 > limit:
                raise ValueError('truncated NMT_Netspeed')
            pos += 4
        elif msg == 5:
            _, url_start, _ = read_string(payload, pos, limit)
            url, url_end, wide = read_string(payload, url_start, limit)
            new_url, info = rewrite_url(url, lookup)
            if info:
                infos.append(info)
                if info.get('changed'):
                    edits.append((url_start, url_end, write_string(new_url, wide)))
            pos = url_end
        elif msg == 9:
            joined = True
            break
        else:
            break
    # Preserve opaque trailing bytes and non-byte-aligned bits exactly.
    bits = bits_from_bytes(payload)[:valid_bits]
    for start, end, replacement in reversed(edits):
        bits[start * 8:end * 8] = bits_from_bytes(replacement)
    return bits_to_bytes(bits), len(bits), infos, joined


def rewrite_wire(wire, lookup):
    plain = decrypt_wire(wire)
    packet, records = parse_packet(plain)
    changed, joined, hello = False, False, False
    infos, rebuilt = [], []
    for record in records:
        if record[0] != 'BUNCH' or record[5] != 0:
            rebuilt.append(record)
            continue
        _, ctl, op, cl, rel, ch, seq, ctype, nbits, payload = record
        hello |= bool(op and payload and payload[0] == 0)
        new_payload, new_bits, hits, saw_join = rewrite_control(payload, nbits, lookup)
        infos.extend(hits)
        joined |= saw_join
        if any(info.get('changed') for info in hits):
            changed = True
            rebuilt.append(('BUNCH', ctl, op, cl, rel, ch, seq, ctype, new_bits, new_payload))
        else:
            rebuilt.append(record)
    if not changed:
        return wire, infos, joined, hello
    result = encrypt_wire(build_packet(packet, rebuilt))
    if parse_packet(decrypt_wire(result)) != (packet, rebuilt):
        raise ValueError('nickname packet rebuild verification failed')
    return result, infos, joined, hello


class LoginNameRewriter:
    """Per-UIN logins and retransmits; bypass established gameplay packets."""
    def __init__(self, repo_root, *, db_path=None, log=print):
        configured = db_path or os.environ.get('AF_ACCOUNT_DB')
        self.db_path = Path(configured) if configured else Path(repo_root) / 'server/assaultfire_accounts.sqlite3'
        self.log = log
        self.peers = OrderedDict()

    def rewrite(self, wire, peer):
        state = self.peers.setdefault(peer, {'joined': False, 'packets': OrderedDict(), 'logged': set()})
        self.peers.move_to_end(peer)
        while len(self.peers) > 256:
            self.peers.popitem(last=False)
        cached = state['packets'].get(wire)
        if cached is not None:
            return cached
        # A reconnect Hello can include ACKs; do not require one exact packet size.
        possible_hello = (20 <= len(wire) <= 132
                          and struct.unpack_from('<I', wire)[0] <= 128)
        if state['joined'] and not possible_hello:
            return wire
        try:
            result, infos, joined, hello = rewrite_wire(wire,
                lambda uin: nickname_from_db(self.db_path, uin))
            if hello and state['joined']:
                state.update(joined=False, packets=OrderedDict(), logged=set())
            state['joined'] |= joined
            for info in infos:
                key = (info.get('uin'), info.get('nickname'), info.get('missing_profile'))
                if key not in state['logged']:
                    state['logged'].add(key)
                    if info.get('changed'):
                        self.log(f"[LOGIN-PATCH] peer={peer} uin={info['uin']} "
                                 f"Name {info['old_name']!r} -> {info['nickname']!r}", flush=True)
                    elif info.get('missing_profile'):
                        self.log(f"[LOGIN-PATCH] no persisted nickname for uin={info['uin']}; login preserved", flush=True)
            if result != wire:
                state['packets'][wire] = result
                while len(state['packets']) > 16:
                    state['packets'].popitem(last=False)
            return result
        except (ValueError, UnicodeError, sqlite3.Error, OSError, struct.error) as exc:
            if 'error' not in state['logged']:
                state['logged'].add('error')
                self.log(f'[LOGIN-PATCH] safe skip peer={peer}: {type(exc).__name__}: {exc}', flush=True)
            return wire
