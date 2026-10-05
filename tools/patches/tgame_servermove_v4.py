#!/usr/bin/env python3
"""Apply the verified Assault Fire PH v1.0.0.24 ServerMove-v4 patch to a local AFDEV copy.

This tool never distributes a game executable. It operates only on the user's
own TGame_AFDEV.exe and validates build-specific native anchors before writing.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import struct
from pathlib import Path

IMAGE_BASE = 0x00400000
SERVERMOVE_STUB = 0x013A88D0
CORRECTION_IMPL = 0x008F2620
MOVEAUTO_IMPL = 0x008F24B0
PZ_MOVE_SELECTOR = 0x0162AA20
PZ_PWSM_IMPL = 0x0162ABB0
PZ_DIRECT_BASE_CALL = 0x0162ACC4
SECTION_NAME = b".afm4\0\0\0"
SECTION_CHARS = 0x60000020
BRIDGE_LEN = 639
STOCK_STUB = bytes.fromhex("C2 28 00 CC CC")

# Exact 639-byte v4 body produced by the validated reconstruction at VA 0x025BE000.
# Internal branches are position-independent. The six external CALL rel32 operands
# are regenerated for the actual .afm4 VA before installation.
_BRIDGE_TEMPLATE = base64.b64decode(
    "VYvsU1ZXg+wwi/GLntgBAACF2w+EVgIAAPMPEEUI8w8QjvgDAAAPL8gPg0ACAACLhpADAAA7ww+EQgAAAA9XwPMPEUXw8w8RRezzDxFF6Ild0GoAjUXQUGoA/zU8PwYC/zU4PwYCi87ok8Dy/VCLBouAIAEAAIvO/9DpNgAAAPMPEEUM8w9ZBXviWwLzDxFF8PMPEEUQ8w9ZBXviWwLzDxFF7PMPEEUU8w9ZBXviWwLzDxFF6PMPEEUI8w9chvgDAADzD12GiAMAAPMPEUXk/3Xki87ohw8z/oXAD4UNAAAAD1fA8w8RReTpEgAAAPMPEEXk8w9Zg5AAAADzDxFF5ItFCImG+AMAAIsN+GsGAoXJD4Q+AAAA6GRYfv7ZXeCLReCJhgAEAACLRfAl////f4tN7IHh////fwvBi03ogeH///9/C8EPhAkAAACLReCJhhAEAACLRSyL0CX//wAAweoQagBSUIvO6HOhYv6LhpADAAA7ww+F9AAAAIuGYAAAAA+2i5QAAACD+QEPhBEAAACD+QIPhAgAAACJRdzpBQAAADPAiUXci4ZkAAAAiUXYi0UoJf8AAADB4AiJRdT/deT/ddT/ddj/ddyLy+j/KAL/8w8QReQPV9IPL8IPhmMAAACLDfhrBgKFyQ+EVQAAAGoA6Knfff6FwA+ERgAAAIO4rAMAAAAPhTkAAACLlmgAAAArVdRSi45kAAAAK03YUYuGYAAAACtF3FD/dej/dez/dfD/dST/deSLBou40AQAAIvO/9eLPou/zAQAAP91LP91KP91JP91IP91HP91GP91FP91EP91DP91CIvO/9eDxDBfXluL5V3CKADNzMw9"
)

_EXTERNAL_CALLS = {
    0x068: 0x004EA100,  # UFunction resolver (GivePawn)
    0x0D4: 0x008EF060,  # CheckSpeedHack
    0x117: 0x00DA3980,  # World TimeSeconds
    0x168: 0x00BE82E0,  # AActor::SetRotation
    0x1CC: 0x015E0AD0,  # FaceRotation thunk
    0x1F2: 0x00D9C1A0,  # UWorld::GetWorldInfo
}

KNOWN_VTABLES = {
    "TGPlayerController": (0x01D64720, MOVEAUTO_IMPL, SERVERMOVE_STUB),
    "TGPVPPlayerController": (0x01D651D8, MOVEAUTO_IMPL, SERVERMOVE_STUB),
    "TGTeamPlayerController": (0x01D65728, MOVEAUTO_IMPL, SERVERMOVE_STUB),
    "PVEPlayerController": (0x01E24C28, MOVEAUTO_IMPL, SERVERMOVE_STUB),
    "TGMechaPlayerController": (0x01E25180, MOVEAUTO_IMPL, SERVERMOVE_STUB),
    "TGAIGamePlayerController": (0x01E256D8, MOVEAUTO_IMPL, SERVERMOVE_STUB),
    "PZPlayerControllerBase": (0x01E25C28, PZ_MOVE_SELECTOR, PZ_PWSM_IMPL),
    "TGBioPlayerController": (0x01E42FE0, MOVEAUTO_IMPL, SERVERMOVE_STUB),
}


def _align(value: int, alignment: int) -> int:
    if alignment <= 0 or alignment & (alignment - 1):
        raise RuntimeError(f"invalid alignment 0x{alignment:X}")
    return (value + alignment - 1) & ~(alignment - 1)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PE32:
    def __init__(self, data: bytes):
        self.data = data
        if len(data) < 0x400 or data[:2] != b"MZ":
            raise RuntimeError("not an MZ executable")
        self.pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[self.pe:self.pe + 4] != b"PE\0\0":
            raise RuntimeError("missing PE signature")
        self.coff = self.pe + 4
        self.machine = struct.unpack_from("<H", data, self.coff)[0]
        self.nsec = struct.unpack_from("<H", data, self.coff + 2)[0]
        self.opt_size = struct.unpack_from("<H", data, self.coff + 16)[0]
        self.opt = self.coff + 20
        self.magic = struct.unpack_from("<H", data, self.opt)[0]
        if self.machine != 0x14C or self.magic != 0x10B:
            raise RuntimeError("expected PE32/i386")
        self.image_base = struct.unpack_from("<I", data, self.opt + 28)[0]
        self.section_alignment = struct.unpack_from("<I", data, self.opt + 32)[0]
        self.file_alignment = struct.unpack_from("<I", data, self.opt + 36)[0]
        self.size_of_code = struct.unpack_from("<I", data, self.opt + 4)[0]
        self.size_of_image = struct.unpack_from("<I", data, self.opt + 56)[0]
        self.size_of_headers = struct.unpack_from("<I", data, self.opt + 60)[0]
        self.sec_table = self.opt + self.opt_size
        self.sections = []
        for i in range(self.nsec):
            offset = self.sec_table + i * 40
            name = data[offset:offset + 8].rstrip(b"\0").decode("latin1", "replace")
            vsize, rva, raw_size, raw = struct.unpack_from("<IIII", data, offset + 8)
            chars = struct.unpack_from("<I", data, offset + 36)[0]
            self.sections.append({
                "name": name,
                "header": offset,
                "vsize": vsize,
                "rva": rva,
                "raw_size": raw_size,
                "raw": raw,
                "chars": chars,
            })

    def va_to_raw(self, va: int) -> int:
        rva = va - self.image_base
        for section in self.sections:
            span = max(section["vsize"], section["raw_size"])
            if section["rva"] <= rva < section["rva"] + span:
                delta = rva - section["rva"]
                if delta >= section["raw_size"]:
                    raise RuntimeError(f"VA 0x{va:08X} is virtual-only")
                return section["raw"] + delta
        raise RuntimeError(f"VA 0x{va:08X} is not file-backed")

    def u32(self, va: int) -> int:
        return struct.unpack_from("<I", self.data, self.va_to_raw(va))[0]


def _bridge_for(code_va: int) -> bytes:
    code = bytearray(_BRIDGE_TEMPLATE)
    if len(code) != BRIDGE_LEN:
        raise RuntimeError("embedded ServerMove-v4 bridge length mismatch")
    for insn_off, target in _EXTERNAL_CALLS.items():
        if code[insn_off] != 0xE8:
            raise RuntimeError(f"embedded CALL opcode mismatch at +0x{insn_off:X}")
        disp = target - (code_va + insn_off + 5)
        struct.pack_into("<i", code, insn_off + 1, disp)
    return bytes(code)


def _validate_build(pe: PE32, data: bytes, *, require_stock_stub: bool) -> None:
    if pe.image_base != IMAGE_BASE:
        raise RuntimeError(f"unexpected ImageBase 0x{pe.image_base:08X}")
    if any(section["name"] == ".reloc" for section in pe.sections):
        raise RuntimeError("unexpected relocation section")

    raw = pe.va_to_raw(SERVERMOVE_STUB)
    stub = data[raw:raw + 5]
    if require_stock_stub and stub != STOCK_STUB:
        raise RuntimeError(f"shared ServerMove stub mismatch: {stub.hex(' ')}")

    checks = {
        0x008EF060: bytes.fromhex("83 EC 14 83 3D A4 E9 05"),
        0x00DA3980: bytes.fromhex("6A 00"),
        0x00D9C1A0: bytes.fromhex("6A FF 68"),
        0x015E0AD0: bytes.fromhex("83 EC 10 8B 44 24"),
        0x00934DA0: bytes.fromhex("83 EC 0C 56"),
        0x00BE82E0: bytes.fromhex("83 EC 5C"),
        0x004EA100: bytes.fromhex("6A FF 68 10 CE 8B 01"),
        0x009564ED: bytes.fromhex("89 0D 38 3F 06 02 89 15 3C 3F 06 02"),
    }
    for va, expected in checks.items():
        offset = pe.va_to_raw(va)
        if data[offset:offset + len(expected)] != expected:
            raise RuntimeError(f"native anchor mismatch at 0x{va:08X}")

    # Exact WorldInfo.Pauser proof.
    proof = pe.va_to_raw(0x00A7C918)
    if data[proof] != 0xE8:
        raise RuntimeError("WorldInfo proof site is not CALL")
    disp = struct.unpack_from("<i", data, proof + 1)[0]
    if 0x00A7C918 + 5 + disp != 0x00D9C1A0:
        raise RuntimeError("WorldInfo proof CALL target changed")
    if data[proof + 5:proof + 12] != bytes.fromhex("83 B8 AC 03 00 00 00"):
        raise RuntimeError("WorldInfo.Pauser +0x3AC proof changed")

    # SetRotation wrapper proof.
    proof = pe.va_to_raw(0x00934DFD)
    if data[proof] != 0xE8:
        raise RuntimeError("SetRotation proof site is not CALL")
    disp = struct.unpack_from("<i", data, proof + 1)[0]
    if 0x00934DFD + 5 + disp != 0x00BE82E0:
        raise RuntimeError("SetRotation proof CALL target changed")

    for label, (vtable, move_impl, pwsm_impl) in KNOWN_VTABLES.items():
        if pe.u32(vtable + 0x4C8) != SERVERMOVE_STUB:
            raise RuntimeError(f"{label}: ServerMove slot changed")
        if pe.u32(vtable + 0x4CC) != CORRECTION_IMPL:
            raise RuntimeError(f"{label}: correction slot changed")
        if pe.u32(vtable + 0x4D0) != move_impl:
            raise RuntimeError(f"{label}: MoveAutonomous slot changed")
        if pe.u32(vtable + 0x528) != pwsm_impl:
            raise RuntimeError(f"{label}: PlayerWalkingServerMove slot changed")

    # PZ wrapper must still call the shared base stub.
    offset = pe.va_to_raw(PZ_DIRECT_BASE_CALL)
    if data[offset] != 0xE8:
        raise RuntimeError("PZ direct base call opcode changed")
    disp = struct.unpack_from("<i", data, offset + 1)[0]
    if PZ_DIRECT_BASE_CALL + 5 + disp != SERVERMOVE_STUB:
        raise RuntimeError("PZ direct base call target changed")

    # The supported image has 33 literal data references to the shared stripped
    # movement stub. The datetime patch does not change those dispatch tables.
    refs = data.count(struct.pack("<I", SERVERMOVE_STUB))
    if refs != 33:
        raise RuntimeError(
            f"shared ServerMove literal reference count changed: {refs} != 33"
        )


def classify(path: Path) -> dict:
    try:
        data = path.read_bytes()
        pe = PE32(data)
        if pe.image_base != IMAGE_BASE:
            raise RuntimeError(f"unexpected ImageBase 0x{pe.image_base:08X}")
        afm4 = [section for section in pe.sections if section["name"] == ".afm4"]
        stub_offset = pe.va_to_raw(SERVERMOVE_STUB)
        stub = data[stub_offset:stub_offset + 5]

        if afm4:
            if len(afm4) != 1:
                raise RuntimeError("multiple .afm4 sections")
            section = afm4[0]
            code_va = pe.image_base + section["rva"]
            if len(stub) != 5 or stub[0] != 0xE9:
                raise RuntimeError(".afm4 exists but shared stub is not JMP")
            disp = struct.unpack_from("<i", stub, 1)[0]
            if SERVERMOVE_STUB + 5 + disp != code_va:
                raise RuntimeError("shared stub does not target .afm4")
            expected = _bridge_for(code_va)
            actual = data[section["raw"]:section["raw"] + len(expected)]
            if actual != expected:
                raise RuntimeError(
                    ".afm4 body does not match verified ServerMove-v4"
                )
            _validate_build(pe, data, require_stock_stub=False)
            return {
                "status": "already-patched",
                "path": str(path),
                "sha256": _sha(data),
                "servermove_va": f"0x{code_va:08X}",
                "message": "verified ServerMove-v4 .afm4 section is installed",
            }

        _validate_build(pe, data, require_stock_stub=True)
        return {
            "status": "unpatched-compatible",
            "path": str(path),
            "sha256": _sha(data),
            "message": "exact ServerMove-v4 native anchors and stock stub verified",
        }
    except Exception as exc:
        return {
            "status": "unsupported",
            "path": str(path),
            "message": str(exc),
        }


def apply_patch(path: Path) -> dict:
    before = classify(path)
    if before["status"] == "already-patched":
        return before
    if before["status"] != "unpatched-compatible":
        raise RuntimeError(before["message"])

    original = path.read_bytes()
    pe = PE32(original)
    new_header = pe.sec_table + pe.nsec * 40
    first_raw = min(
        (section["raw"] for section in pe.sections if section["raw"]),
        default=len(original),
    )
    if new_header + 40 > pe.size_of_headers or new_header + 40 > first_raw:
        raise RuntimeError("no free 40-byte PE section-header slot for .afm4")
    if any(original[new_header:new_header + 40]):
        raise RuntimeError("next PE section-header slot is not empty")

    last_end = max(
        section["rva"] + max(section["vsize"], section["raw_size"])
        for section in pe.sections
    )
    new_rva = _align(last_end, pe.section_alignment)
    new_raw = _align(len(original), pe.file_alignment)
    code_va = pe.image_base + new_rva
    bridge = _bridge_for(code_va)
    raw_size = _align(len(bridge), pe.file_alignment)
    new_size_image = _align(
        new_rva + len(bridge),
        pe.section_alignment,
    )

    out = bytearray(original)
    if len(out) < new_raw:
        out.extend(b"\0" * (new_raw - len(out)))
    out.extend(b"\0" * raw_size)
    out[new_raw:new_raw + len(bridge)] = bridge

    section_header = bytearray(40)
    section_header[:8] = SECTION_NAME
    struct.pack_into("<I", section_header, 8, len(bridge))
    struct.pack_into("<I", section_header, 12, new_rva)
    struct.pack_into("<I", section_header, 16, raw_size)
    struct.pack_into("<I", section_header, 20, new_raw)
    struct.pack_into("<I", section_header, 36, SECTION_CHARS)
    out[new_header:new_header + 40] = section_header

    struct.pack_into("<H", out, pe.coff + 2, pe.nsec + 1)
    struct.pack_into("<I", out, pe.opt + 4, pe.size_of_code + raw_size)
    struct.pack_into("<I", out, pe.opt + 56, new_size_image)
    struct.pack_into("<I", out, pe.opt + 64, 0)

    stub_offset = pe.va_to_raw(SERVERMOVE_STUB)
    disp = code_va - (SERVERMOVE_STUB + 5)
    out[stub_offset:stub_offset + 5] = b"\xE9" + struct.pack("<i", disp)

    temporary = path.with_name(path.name + ".servermove-v4.tmp")
    backup = path.with_name(path.name + ".servermove-v4.bak")
    temporary.write_bytes(out)
    try:
        checked = classify(temporary)
        if checked["status"] != "already-patched":
            raise RuntimeError(
                "temporary ServerMove-v4 image failed read-back validation: "
                + checked["message"]
            )

        if backup.exists():
            if backup.read_bytes() != original:
                raise RuntimeError(
                    f"existing backup does not match current input: {backup}"
                )
        else:
            backup.write_bytes(original)

        os.replace(temporary, path)
        installed = classify(path)
        if installed["status"] != "already-patched":
            shutil.copy2(backup, path)
            raise RuntimeError(
                "installed ServerMove-v4 failed validation; backup restored"
            )

        installed["status"] = "patched"
        installed["backup"] = str(backup)
        return installed
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, help="local TGame_AFDEV.exe")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = apply_patch(args.path) if args.apply else classify(args.path)
        code = 0 if result["status"] != "unsupported" else 1
    except Exception as exc:
        result = {
            "status": "unsupported",
            "path": str(args.path),
            "message": str(exc),
        }
        code = 1

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"[{result['status'].upper()}] {result.get('message', '')}")
        for key in ("path", "sha256", "servermove_va", "backup"):
            if result.get(key):
                print(f"{key}: {result[key]}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
