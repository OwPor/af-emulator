import argparse
import json
import os
import subprocess
import sys
import socket
import struct
import select
import time
from pathlib import Path
from collections import deque

DS_DIAGNOSTICS = None
LOADER_DIAGNOSTIC_TAIL = deque(maxlen=14)
LOADER_DIAGNOSTIC_THREAD = None
if os.environ.get("AF_DS_DIAGNOSTICS_DB"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
    from assaultfire_ds_diagnostics import from_environment, capture_pipe
    DS_DIAGNOSTICS = from_environment()


def diagnostic_context():
    return (os.environ.get("AF_DS_DIAGNOSTICS_SESSION"),
            int(os.environ.get("AF_DS_DIAGNOSTICS_ROOM", "0")),
            int(os.environ.get("AF_DS_DIAGNOSTICS_UIN", "0")))

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 65008
TARGET = ("127.0.0.1", 7777)

# The current AFDEV listen server selected the dynamic all-zero 128-bit DS key.
# The key also decodes per-UIN Login Name updates.
# Other packets are forwarded unchanged.
DECODE_KEY = b"\x00" * 16

SIO_UDP_CONNRESET = 0x9800000C

# v5 diagnostic actor-channel capture. Login Name is rewritten per UIN;
# other datagrams are forwarded unchanged.
ACTOR_DUMP_PATH = Path(__file__).with_name("af_actor_payloads.log")
ACTOR_SEEN = {}
CH2_DUMP_LIMIT = 12
OTHER_CHANNEL_EARLY_DUMP_LIMIT = 2


def append_actor_dump(line):
    if DS_DIAGNOSTICS:
        DS_DIAGNOSTICS.append(*diagnostic_context(), "actor", line)
        return
    try:
        with ACTOR_DUMP_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        print(f"[ACTOR-DUMP] write failed: {exc}")


def actor_payload_detail(direction, packet_id, ctl, op, cl, rel, ch, seq, ctype, nbits, payload):
    """Return/log payload details only when diagnostically useful.

    Always dump channel opens (the key event for actor creation), and also the
    first few messages on every non-control channel.  Channel 2 gets a larger
    early window because the real AF DS trace shows the initial two-way player/
    connection exchange there.

    UE3 bunch payloads are bit-packed.  payload.hex() contains zero padding in
    the unused high bits of the last byte; nbits is authoritative.
    """
    key = (direction, ch)
    count = ACTOR_SEEN.get(key, 0) + 1
    ACTOR_SEEN[key] = count

    limit = CH2_DUMP_LIMIT if ch == 2 else OTHER_CHANNEL_EARLY_DUMP_LIMIT
    reason = None
    if op:
        reason = "OPEN"
    elif count <= limit:
        reason = "EARLY"

    if reason is None:
        return ""

    tail_bits = nbits & 7
    if tail_bits == 0 and nbits:
        tail_bits = 8
    detail = (
        f"ACTOR_{reason}#{count} dumped "
        f"payload_bytes={len(payload)} valid_bits={nbits} "
        f"last_byte_valid_bits={tail_bits}"
    )

    append_actor_dump(
        f"{time.time():.6f} {direction} packet={packet_id} ch={ch} seq={seq} "
        f"ctl={ctl} open={op} close={cl} rel={rel} type={ctype} "
        f"nbits={nbits} bytes={len(payload)} reason={reason} "
        f"payload={payload.hex()}"
    )
    return detail


def disable_udp_connreset(sock):
    try:
        sock.ioctl(SIO_UDP_CONNRESET, False)
        return True
    except Exception:
        return False


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


CONTROL_NAMES = {
    0: "NMT_Hello",
    1: "NMT_Welcome",
    3: "NMT_Challenge",
    4: "NMT_Netspeed",
    5: "NMT_Login",
    6: "NMT_Failure",
    9: "NMT_Join",
}


def decode_fstring(buf, off=0):
    if off + 4 > len(buf):
        return None, off
    n = struct.unpack_from("<i", buf, off)[0]
    off += 4
    if n == 0:
        return "", off
    if n > 0:
        end = off + n
        if end > len(buf):
            return None, off
        raw = buf[off:end]
        return raw.rstrip(b"\x00").decode("latin1", "replace"), end
    count = -n
    end = off + count * 2
    if end > len(buf):
        return None, off
    raw = buf[off:end]
    return raw.decode("utf-16le", "replace").rstrip("\x00"), end


def summarize(direction, wire):
    try:
        plain = decrypt_wire(wire)
        packet_id, records = parse_packet(plain)
    except Exception as exc:
        return f"{direction} decode-error={exc}"

    parts = [f"{direction} PacketId={packet_id} clear={len(plain)}B"]
    for r in records:
        if r[0] == "ACK":
            parts.append(f"ACK({r[1]})")
            continue

        _, ctl, op, cl, rel, ch, seq, ctype, nbits, payload = r
        base = (
            f"BUNCH(ch={ch} seq={seq} ctl={ctl} open={op} "
            f"close={cl} rel={rel} type={ctype} bits={nbits})"
        )

        if ch == 0 and payload:
            msg = payload[0]
            name = CONTROL_NAMES.get(msg, f"NMT_{msg}")
            detail = name

            if msg == 3 and len(payload) >= 5:
                challenge_u32 = struct.unpack_from("<I", payload, 1)[0]
                s, _ = decode_fstring(payload, 5)
                detail += f"(u32=0x{challenge_u32:08X}, str={s!r})"

            elif msg == 6:
                s, _ = decode_fstring(payload, 1)
                detail += f"({s!r})"

            elif msg == 1:
                level, p = decode_fstring(payload, 1)
                game, _ = decode_fstring(payload, p)
                detail += f"(Level={level!r}, Game={game!r})"

            elif msg == 4 and len(payload) >= 5:
                rate = struct.unpack_from("<I", payload, 1)[0]
                detail += f"(rate={rate})"

            parts.append(base + " " + detail)
        elif ch != 0:
            detail = actor_payload_detail(
                direction, packet_id, ctl, op, cl, rel, ch, seq, ctype,
                nbits, payload
            )
            parts.append(base + ((" " + detail) if detail else ""))
        else:
            parts.append(base)

    return " | ".join(parts)



def atomic_write_json(path, payload, replace_retries=20, direct_retries=6):
    """Publish JSON without letting Windows sharing locks kill the relay.

    Windows can transiently reject os.replace() with WinError 5/32/33 while
    the parent spawner is polling the destination.  r10 treated that telemetry
    failure as fatal.  r11 uses a unique temp file, retries the atomic replace,
    then falls back to a short direct rewrite (readers already tolerate partial
    JSON and retry on their next poll).  Returns True on success, False if the
    state file could not be updated; packet relay must continue either way.
    """
    path = Path(path)
    data = json.dumps(payload, indent=2, sort_keys=True)
    unique = f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    tmp = path.with_name(unique)
    last_exc = None

    try:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8", newline="\n") as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        except OSError:
            return False

        for attempt in range(max(1, int(replace_retries))):
            try:
                os.replace(tmp, path)
                return True
            except OSError as exc:
                last_exc = exc
                # WinError 5=access denied, 32=sharing violation, 33=lock violation.
                winerr = getattr(exc, "winerror", None)
                if os.name == "nt" and winerr not in (5, 32, 33, None):
                    break
                time.sleep(min(0.005 * (attempt + 1), 0.075))

        # Rename may require DELETE sharing on Windows.  A normal rewrite does
        # not, so use it as a best-effort fallback.  The spawner's JSON reader
        # is already tolerant of a transient parse failure.
        for attempt in range(max(1, int(direct_retries))):
            try:
                with path.open("w", encoding="utf-8", newline="\n") as f:
                    f.write(data)
                    f.flush()
                return True
            except OSError as exc:
                last_exc = exc
                time.sleep(min(0.01 * (attempt + 1), 0.10))
        return False
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def valid_ds_client_packet(wire):
    """Require both the AF DS crypto wrapper and UE3 packet grammar to parse."""
    try:
        plain = decrypt_wire(wire)
        packet_id, records = parse_packet(plain)
        return True, packet_id, len(records), None
    except Exception as exc:
        return False, None, None, str(exc)


def main():
    from af_login_names import LoginNameRewriter
    nickname_rewriter = LoginNameRewriter(Path(__file__).resolve().parents[2])
    os.environ["AF_DYNAMIC_LOGIN_NAMES"] = "1"
    print(f"[LOGIN-PATCH] Dynamic per-UIN nicknames enabled DB={nickname_rewriter.db_path}", flush=True)
    global ACTOR_DUMP_PATH
    ap = argparse.ArgumentParser(description="Assault Fire PH per-instance UDP bridge v9 multi-peer first-packet-latch AFDEV spawn")
    ap.add_argument("--listen-ip", default=LISTEN_IP)
    ap.add_argument("--listen-port", type=int, default=LISTEN_PORT)
    ap.add_argument("--target-host", default=TARGET[0])
    ap.add_argument("--target-port", type=int, default=TARGET[1])
    ap.add_argument("--actor-dump", default=str(ACTOR_DUMP_PATH))
    ap.add_argument(
        "--packet-diagnostics",
        action="store_true",
        default=os.environ.get("AF_DS_PACKET_DIAGNOSTICS", "").strip().lower()
        in ("1", "true", "yes", "on"),
        help="decrypt and log every relayed packet (off by default to reduce host-side jitter)",
    )

    ap.add_argument("--lazy-spawn", action="store_true")
    ap.add_argument("--python-exe", default=sys.executable)
    ap.add_argument("--loader-script", default="")
    ap.add_argument("--game-dir", default=os.environ.get("AF_GAME_DIR", ""))
    ap.add_argument("--map", dest="map_name", default=os.environ.get("AF_DS_DEFAULT_MAP", ""))
    ap.add_argument("--game", dest="game_class", default="PVEGame.TGSVGame")
    ap.add_argument("--max-players", type=int, default=4)
    ap.add_argument("--mode-id", type=lambda x: int(x, 0), default=0x00002001)
    ap.add_argument("--map-id", type=lambda x: int(x, 0), default=0x002F)
    ap.add_argument("--sub-mode-id", type=lambda x: int(x, 0), default=0x00001001)
    ap.add_argument("--room-flags", type=lambda x: int(x, 0), default=0x00003008)
    ap.add_argument("--instance-id", default="default")
    ap.add_argument("--pid-file", default="")
    ap.add_argument("--ready-file", default="")
    ap.add_argument("--loader-pid-file", default="")
    ap.add_argument("--loader-log", default="")
    ap.add_argument("--state-file", default="")
    ap.add_argument("--startup-timeout", type=float, default=120.0)
    ap.add_argument("--buffer-max-packets", type=int, default=256)
    ap.add_argument("--buffer-max-bytes", type=int, default=512 * 1024)
    ap.add_argument("--buffer-max-age", type=float, default=45.0)
    ap.add_argument(
        "--first-reply-retry", type=float,
        default=float(os.environ.get("AF_DS_FIRST_REPLY_RETRY", "1.25")),
    )
    ap.add_argument(
        "--first-reply-max-retries", type=int,
        default=int(os.environ.get("AF_DS_FIRST_REPLY_MAX_RETRIES", "3")),
    )
    ap.add_argument(
        "--first-reply-timeout", type=float,
        default=float(os.environ.get("AF_DS_FIRST_REPLY_TIMEOUT", "8.0")),
        help="seconds after first latched send before a peer is declared dead",
    )
    args = ap.parse_args()

    listen_ip = args.listen_ip
    listen_port = int(args.listen_port)
    target = (args.target_host, int(args.target_port))
    ACTOR_DUMP_PATH = Path(args.actor_dump).resolve()
    if args.packet_diagnostics:
        ACTOR_DUMP_PATH.parent.mkdir(parents=True, exist_ok=True)

    state_file = Path(args.state_file).resolve() if args.state_file else None
    ready_file = Path(args.ready_file).resolve() if args.ready_file else None
    pid_file = Path(args.pid_file).resolve() if args.pid_file else None
    loader_pid_file = Path(args.loader_pid_file).resolve() if args.loader_pid_file else None
    loader_log = Path(args.loader_log).resolve() if args.loader_log else None
    if loader_log:
        loader_log.parent.mkdir(parents=True, exist_ok=True)

    state = {
        "state": "BOOTING",
        "listen_ip": listen_ip,
        "listen_port": listen_port,
        "target_host": target[0],
        "target_port": target[1],
        "instance_id": args.instance_id,
        "lazy_spawn": bool(args.lazy_spawn),
        "loader_pid": None,
        "afdev_pid": None,
        "trigger_client": None,
        "buffered_packets": 0,
        "buffered_bytes": 0,
        "latched_packet_id": None,
        "suppressed_startup_packets": 0,
        "first_server_reply_at": None,
        "replay_attempts": 0,
        "error": None,
        "updated_at": time.time(),
    }

    state_write_failures = 0
    last_state_warning = 0.0

    def publish(**changes):
        nonlocal state_write_failures, last_state_warning
        state.update(changes)
        state["updated_at"] = time.time()
        if state_file:
            ok = atomic_write_json(state_file, state)
            if not ok:
                state_write_failures += 1
                now = time.time()
                if state_write_failures <= 3 or now - last_state_warning >= 5.0:
                    print(
                        f"[BRIDGE-v9.2] WARN state publication failed "
                        f"count={state_write_failures} path={state_file}; relay continues",
                        flush=True,
                    )
                    last_state_warning = now
            else:
                state_write_failures = 0

    if args.packet_diagnostics:
        append_actor_dump("\n=== NEW BRIDGE-v9 MULTI-PEER LATCH SESSION %.6f ===" % time.time())
    cs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cs.bind((listen_ip, listen_port))
    disable_udp_connreset(cs)

    publish(state="LISTENING")
    print(f"[BRIDGE-v9.2] lazy relay listening {listen_ip}:{listen_port} -> {target[0]}:{target[1]}", flush=True)
    print(f"[BRIDGE-v9.2] AFDEV=OFF; first valid AF DS/UE3 client UDP packet triggers v48", flush=True)
    print("[BRIDGE-v9] upstream sockets are allocated per client peer", flush=True)
    if args.packet_diagnostics:
        print(f"[BRIDGE-v9] diagnostic decode key = {DECODE_KEY.hex()}", flush=True)
        print(f"[BRIDGE-v9] actor payload log = {ACTOR_DUMP_PATH}", flush=True)
    else:
        print(
            "[BRIDGE-v9] packet diagnostics disabled; set AF_DS_PACKET_DIAGNOSTICS=1 "
            "or pass --packet-diagnostics to enable",
            flush=True,
        )

    # r10/v9 multiplayer bridge: one public room endpoint, but one distinct
    # upstream UDP socket per client peer.  AFDEV therefore sees separate source
    # ports and can create independent UE3 NetConnections for multiple players.
    peers = {}  # (client_ip,client_port) -> state dict
    sock_to_peer = {}
    trigger_client = None
    c2s = s2c = 0
    loader_proc = None
    loader_log_handle = None
    spawn_started = None
    afdev_ready = not args.lazy_spawn
    max_peers = max(1, int(args.max_players))

    def peer_public_state():
        rows = []
        for addr, pstate in sorted(peers.items(), key=lambda x: (x[0][0], x[0][1])):
            rows.append({
                "client": f"{addr[0]}:{addr[1]}",
                "upstream": f"{pstate['sock'].getsockname()[0]}:{pstate['sock'].getsockname()[1]}",
                "packet_id": pstate.get("packet_id"),
                "live": bool(pstate.get("live")),
                "suppressed": int(pstate.get("suppressed", 0)),
                "replay_attempts": int(pstate.get("replay_attempts", 0)),
            })
        return rows

    def publish_peers(**extra):
        live_count = sum(1 for p in peers.values() if p.get("live"))
        waiting_count = sum(1 for p in peers.values() if not p.get("live"))
        buffered_count = sum(1 for p in peers.values() if p.get("latched_wire") is not None and not p.get("live"))
        buffered_bytes = sum(len(p.get("latched_wire") or b"") for p in peers.values() if not p.get("live"))
        publish(
            peer_count=len(peers),
            live_peer_count=live_count,
            waiting_peer_count=waiting_count,
            buffered_packets=buffered_count,
            buffered_bytes=buffered_bytes,
            suppressed_startup_packets=sum(int(p.get("suppressed", 0)) for p in peers.values()),
            replay_attempts=sum(int(p.get("replay_attempts", 0)) for p in peers.values()),
            peers=peer_public_state(),
            **extra,
        )

    def make_peer(addr, wire, packet_id):
        if addr in peers:
            return peers[addr]
        if len(peers) >= max_peers:
            print(
                f"[BRIDGE-v9] drop peer {addr}: max_peers={max_peers} reached",
                flush=True,
            )
            return None
        us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        us.bind(("127.0.0.1", 0))
        disable_udp_connreset(us)
        pstate = {
            "sock": us,
            "latched_wire": bytes(wire),
            "packet_id": packet_id,
            "live": False,
            "first_send_at": None,
            "first_deadline": None,
            "replay_attempts": 0,
            "suppressed": 0,
        }
        peers[addr] = pstate
        sock_to_peer[us] = addr
        print(
            f"[BRIDGE-v9] peer registered client={addr[0]}:{addr[1]} "
            f"upstream={us.getsockname()[0]}:{us.getsockname()[1]} PacketId={packet_id}",
            flush=True,
        )
        publish_peers()
        return pstate

    def spawn_loader(addr, packet_id, records_count):
        nonlocal loader_proc, loader_log_handle, spawn_started, trigger_client
        if not args.lazy_spawn or loader_proc is not None:
            return
        loader_script = Path(args.loader_script).resolve()
        if not loader_script.is_file():
            raise RuntimeError(f"missing loader: {loader_script}")
        if not ready_file or not pid_file or not loader_pid_file:
            raise RuntimeError("lazy mode requires --ready-file --pid-file --loader-pid-file")
        game_dir = str(args.game_dir or "").strip()
        map_name = str(args.map_name or "").strip()
        if not game_dir:
            raise RuntimeError("missing --game-dir / AF_GAME_DIR (expected Assault Fire PH Binaries\\Win32)")
        if not map_name:
            raise RuntimeError("missing room-selected --map / AF_DS_DEFAULT_MAP")
        for p in (ready_file, pid_file, loader_pid_file):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

        child_env = os.environ.copy()
        child_env.setdefault("PYTHONUTF8", "1")
        child_env["PYTHONIOENCODING"] = "utf-8:backslashreplace"
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if DS_DIAGNOSTICS:
            stdout_target = subprocess.PIPE
        elif loader_log:
            loader_log_handle = loader_log.open("a", encoding="utf-8", buffering=1)
            stdout_target = loader_log_handle
        else:
            stdout_target = None

        cmd = [
            args.python_exe, str(loader_script),
            "--game-dir", game_dir,
            "--map", map_name,
            "--game", args.game_class,
            "--max-players", str(max(2, int(args.max_players))),
            "--mode-id", str(int(args.mode_id)),
            "--map-id", str(int(args.map_id)),
            "--sub-mode-id", str(int(args.sub_mode_id)),
            "--room-flags", str(int(args.room_flags)),
            "--port", str(target[1]),
            "--instance-id", args.instance_id,
            "--pid-file", str(pid_file),
            "--ready-file", str(ready_file),
            "--no-uac",
        ]
        loader_proc = subprocess.Popen(
            cmd,
            cwd=str(loader_script.parent),
            stdout=stdout_target,
            stderr=subprocess.STDOUT if stdout_target is not None else None,
            creationflags=creationflags,
            env=child_env,
        )
        if DS_DIAGNOSTICS:
            global LOADER_DIAGNOSTIC_THREAD
            LOADER_DIAGNOSTIC_TAIL.clear()
            LOADER_DIAGNOSTIC_THREAD = capture_pipe(loader_proc.stdout, DS_DIAGNOSTICS,
                *diagnostic_context(), "loader", LOADER_DIAGNOSTIC_TAIL)
        loader_pid_file.write_text(str(loader_proc.pid), encoding="utf-8")
        spawn_started = time.time()
        trigger_client = addr
        publish_peers(
            state="SPAWNING",
            loader_pid=loader_proc.pid,
            trigger_client=f"{addr[0]}:{addr[1]}",
            spawned_at=spawn_started,
            trigger_packet_id=packet_id,
            trigger_record_count=records_count,
        )
        print(
            f"[BRIDGE-v9] FIRST VALID DS UDP from {addr[0]}:{addr[1]} PacketId={packet_id} "
            f"records={records_count} -> spawning v48 loader pid={loader_proc.pid} target_udp={target[1]} "
            f"mode=0x{int(args.mode_id):08x} map=0x{int(args.map_id):04x} "
            f"submode=0x{int(args.sub_mode_id):08x} flags=0x{int(args.room_flags):08x}",
            flush=True,
        )

    def send_peer_latched(addr, pstate, reason):
        wire = pstate.get("latched_wire")
        if wire is None or pstate.get("live"):
            return False
        pstate["sock"].sendto(wire, target)
        pstate["replay_attempts"] += 1
        now = time.time()
        if pstate.get("first_send_at") is None:
            pstate["first_deadline"] = now + max(1.0, float(args.first_reply_timeout))
        pstate["first_send_at"] = now
        publish_peers()
        print(
            f"[BRIDGE-v9] peer {addr[0]}:{addr[1]} latched PacketId={pstate.get('packet_id')} "
            f"-> AFDEV via upstream={pstate['sock'].getsockname()[1]} "
            f"attempt={pstate['replay_attempts']} reason={reason}",
            flush=True,
        )
        return True

    def send_all_waiting(reason):
        for addr, pstate in list(peers.items()):
            if not pstate.get("live") and pstate.get("latched_wire") is not None:
                if pstate.get("replay_attempts", 0) == 0:
                    send_peer_latched(addr, pstate, reason)

    def poll_lazy_ready():
        nonlocal afdev_ready
        if not args.lazy_spawn or afdev_ready or loader_proc is None:
            return
        if loader_proc.poll() is not None:
            detail = f"loader exited rc={loader_proc.returncode}"
            if DS_DIAGNOSTICS:
                if LOADER_DIAGNOSTIC_THREAD:
                    LOADER_DIAGNOSTIC_THREAD.join(timeout=1)
                detail += "; loader_log_tail=" + " | ".join(LOADER_DIAGNOSTIC_TAIL)[-3500:]
            # r14 diagnostics: surface the loader's actual failure in the bridge
            # error instead of forcing the user to hunt a second file.
            try:
                lp = Path(args.loader_log) if args.loader_log else None
                if lp and lp.is_file():
                    lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
                    tail = " | ".join(x.strip() for x in lines[-14:] if x.strip())
                    if tail:
                        detail += "; loader_log_tail=" + tail[-3500:]
            except Exception:
                pass
            publish(state="FAILED", error=detail)
            raise RuntimeError(detail)
        if spawn_started is not None and time.time() - spawn_started > max(5.0, float(args.startup_timeout)):
            detail = f"SESSION_READY timeout after {args.startup_timeout:.1f}s"
            publish(state="FAILED", error=detail)
            raise RuntimeError(detail)
        if pid_file and pid_file.exists():
            try:
                publish(afdev_pid=int(pid_file.read_text(encoding="utf-8").strip()))
            except Exception:
                pass
        if ready_file and ready_file.exists():
            try:
                payload = json.loads(ready_file.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            if payload.get("ready") is True:
                if payload.get("zero_dskey") is not True:
                    detail = "loader SESSION_READY missing verified zero_dskey=true"
                    publish(state="FAILED", error=detail)
                    raise RuntimeError(detail)
                afpid = int(payload.get("pid") or state.get("afdev_pid") or 0) or None
                afdev_ready = True
                publish_peers(
                    state="AFDEV_READY",
                    afdev_pid=afpid,
                    afdev_ready_at=time.time(),
                    zero_dskey=True,
                    zero_dskey_socket=int(payload.get("zero_dskey_socket") or 0),
                    zero_dskey_rekeys=int(payload.get("zero_dskey_rekeys") or 0),
                    mode_id=int(payload.get("mode_id") or 0),
                    map_id=int(payload.get("map_id") or 0),
                    sub_mode_id=int(payload.get("sub_mode_id") or 0),
                    room_flags=int(payload.get("room_flags") or 0),
                    pve_difficulty=int(payload.get("pve_difficulty") or 0),
                    pve_difficulty_name=str(payload.get("pve_difficulty_name") or ""),
                    advanced_hero=bool(payload.get("advanced_hero")),
                )
                print(
                    f"[BRIDGE-v9.1] SESSION_READY AFDEV pid={afpid} udp={target[1]} "
                    f"zero_dskey=VERIFIED socket=0x{int(payload.get('zero_dskey_socket') or 0):08X}; "
                    f"difficulty={payload.get('pve_difficulty_name')} "
                    f"submode=0x{int(payload.get('sub_mode_id') or 0):08X} "
                    f"flags=0x{int(payload.get('room_flags') or 0):08X}; "
                    f"sending one latched handshake for each waiting peer ({len(peers)} peer(s))",
                    flush=True,
                )
                send_all_waiting("SESSION_READY")

    def maybe_retry_waiting():
        if not args.lazy_spawn or not afdev_ready:
            return
        retry_after = max(0.25, float(args.first_reply_retry))
        max_retries = max(1, int(args.first_reply_max_retries))
        now = time.time()
        timed_out = []
        for addr, pstate in list(peers.items()):
            if pstate.get("live") or pstate.get("latched_wire") is None:
                continue
            sent_at = pstate.get("first_send_at")
            if sent_at is None:
                send_peer_latched(addr, pstate, "AFDEV_READY")
                continue
            deadline = pstate.get("first_deadline")
            if deadline is not None and now >= float(deadline):
                timed_out.append((addr, pstate))
                continue
            if now - sent_at >= retry_after and pstate.get("replay_attempts", 0) < max_retries:
                send_peer_latched(addr, pstate, "no AFDEV reply yet")

        for addr, pstate in timed_out:
            attempts = int(pstate.get("replay_attempts", 0))
            print(
                f"[BRIDGE-v9.3] PEER FIRST-REPLY TIMEOUT client={addr[0]}:{addr[1]} "
                f"attempts={attempts}; removing dead peer",
                flush=True,
            )
            peers.pop(addr, None)
            sock = pstate.get("sock")
            if sock is not None:
                sock_to_peer.pop(sock, None)
                try:
                    sock.close()
                except Exception:
                    pass

        if timed_out:
            live_count = sum(1 for p in peers.values() if p.get("live"))
            waiting_count = sum(1 for p in peers.values() if not p.get("live"))
            if live_count:
                publish_peers(state="READY")
            elif waiting_count:
                publish_peers(state="AFDEV_READY")
            else:
                detail = (
                    "no AFDEV first reply from any peer before timeout; "
                    f"timed_out={len(timed_out)}"
                )
                publish(state="FAILED", error=detail)
                raise RuntimeError(detail)

    try:
        while True:
            poll_lazy_ready()
            maybe_retry_waiting()
            read_socks = [cs] + [p["sock"] for p in peers.values()]
            try:
                readable, _, _ = select.select(read_socks, [], [], 0.05)
            except OSError as exc:
                print(f"[select] {exc}", flush=True)
                time.sleep(0.02)
                continue

            for sock in readable:
                if sock is cs:
                    try:
                        wire, addr = cs.recvfrom(65535)
                    except ConnectionResetError as exc:
                        print(f"[C->S] UDP reset ignored: {exc}", flush=True)
                        continue
                    wire = nickname_rewriter.rewrite(wire, addr)
                    c2s += 1
                    if args.packet_diagnostics:
                        print(
                            f"[C->S #{c2s}] peer={addr[0]}:{addr[1]} "
                            f"{summarize('C->S', wire)}",
                            flush=True,
                        )

                    pstate = peers.get(addr)
                    if pstate is None:
                        ok, packet_id, records_count, why = valid_ds_client_packet(wire)
                        if not ok:
                            print(f"[BRIDGE-v9] pre-trigger/new-peer invalid UDP dropped from {addr}: {why}", flush=True)
                            continue
                        pstate = make_peer(addr, wire, packet_id)
                        if pstate is None:
                            continue
                        if args.lazy_spawn and loader_proc is None:
                            spawn_loader(addr, packet_id, records_count)
                            continue
                        if not args.lazy_spawn:
                            pstate["live"] = True
                            pstate["latched_wire"] = None
                            pstate["sock"].sendto(wire, target)
                            publish_peers(state="READY")
                            continue
                        if afdev_ready:
                            send_peer_latched(addr, pstate, "new peer after AFDEV ready")
                        continue

                    if not args.lazy_spawn:
                        pstate["sock"].sendto(wire, target)
                        continue

                    if not pstate.get("live"):
                        pstate["suppressed"] += 1
                        publish_peers()
                        n = pstate["suppressed"]
                        if n <= 4 or n % 10 == 0:
                            print(
                                f"[BRIDGE-v9] suppress startup retry #{n} from {addr}; "
                                "waiting for that peer's first AFDEV reply",
                                flush=True,
                            )
                        continue

                    pstate["sock"].sendto(wire, target)

                else:
                    addr = sock_to_peer.get(sock)
                    if addr is None:
                        continue
                    pstate = peers.get(addr)
                    if pstate is None:
                        continue
                    try:
                        wire, src = sock.recvfrom(65535)
                    except ConnectionResetError as exc:
                        print(f"[S->C] UDP reset ignored peer={addr}: {exc}", flush=True)
                        continue
                    if src != target:
                        continue
                    s2c += 1
                    if args.packet_diagnostics:
                        print(
                            f"[S->C #{s2c}] peer={addr[0]}:{addr[1]} "
                            f"{summarize('S->C', wire)}",
                            flush=True,
                        )
                    if args.lazy_spawn and not pstate.get("live"):
                        pstate["live"] = True
                        pstate["latched_wire"] = None
                        now = time.time()
                        if state.get("first_server_reply_at") is None:
                            state["first_server_reply_at"] = now
                        publish_peers(
                            state="READY",
                            ready_at=now,
                            first_server_reply_at=state.get("first_server_reply_at"),
                        )
                        print(
                            f"[BRIDGE-v9] PEER RELAY LIVE client={addr[0]}:{addr[1]} "
                            f"upstream={sock.getsockname()[1]} attempts={pstate['replay_attempts']} "
                            f"suppressed={pstate['suppressed']} live_peers="
                            f"{sum(1 for p in peers.values() if p.get('live'))}/{len(peers)}",
                            flush=True,
                        )
                    try:
                        cs.sendto(wire, addr)
                    except ConnectionResetError as exc:
                        print(f"[S->C #{s2c}] client reset ignored peer={addr}: {exc}", flush=True)
                    except OSError as exc:
                        print(f"[S->C #{s2c}] sendto client failed peer={addr}: {exc}", flush=True)
    except Exception as exc:
        if state.get("state") != "FAILED":
            publish(state="FAILED", error=f"{type(exc).__name__}: {exc}")
        print(f"[BRIDGE-v9] FATAL: {type(exc).__name__}: {exc}", flush=True)
        raise
    finally:
        # Best-effort orphan prevention. The parent spawner also kills these
        # PIDs on room release; this path covers bridge-side failures/Ctrl+C.
        try:
            afpid = None
            if pid_file and pid_file.exists():
                try:
                    afpid = int(pid_file.read_text(encoding="utf-8").strip())
                except Exception:
                    afpid = None
            if afpid:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(afpid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                    )
                else:
                    try:
                        os.kill(afpid, 15)
                    except OSError:
                        pass
        except Exception:
            pass
        try:
            if loader_proc is not None and loader_proc.poll() is None:
                loader_proc.terminate()
                try:
                    loader_proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    loader_proc.kill()
        except Exception:
            pass
        for _pstate in list(peers.values()):
            try:
                _pstate["sock"].close()
            except Exception:
                pass
        try:
            cs.close()
        except Exception:
            pass
        try:
            if loader_log_handle:
                loader_log_handle.flush()
                loader_log_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[BRIDGE-v9] stopped")
