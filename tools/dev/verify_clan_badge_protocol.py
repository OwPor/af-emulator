"""Read-only verification of badge enum and empty-slot facts in the PH metalib.

Only the exact metadata layout recovered for PH is accepted. No game files are
modified or redistributed. Run with Python 3.12 and a path to proto_c2zn.tdr.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

METALIB_SHA256 = '712d4eebfeeafe5c977530a59c7ce05d81cf203e2fb0cab1029653dca65758d2'
POINTER_BASE = 0x114
STRUCT_HEADER = 0xB8
FIELD_SIZE = 0xB4


def inspect(path):
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != METALIB_SHA256:
        raise ValueError('different metalib; these offsets are not verified for SHA-256 ' + digest)

    def text_at(pointer):
        offset = POINTER_BASE + pointer
        if not POINTER_BASE <= offset < len(data):
            raise ValueError('metadata string pointer out of bounds')
        return data[offset:data.index(b'\0', offset)].decode('gbk')

    def field(meta_pointer, index):
        offset = POINTER_BASE + meta_pointer + STRUCT_HEADER + index * FIELD_SIZE
        return struct.unpack_from('<45I', data, offset)

    descriptor = field(0x6B8B0, 4)
    if text_at(descriptor[3]) != 'Type':
        raise ValueError('unexpected ClanPropInfo field')
    enum_pointer = descriptor[39]
    enum_offset = POINTER_BASE + enum_pointer
    count = struct.unpack_from('<I', data, enum_offset)[0]
    enum_name = data[enum_offset+20:enum_offset+148].split(b'\0')[0].decode('ascii')
    if enum_name != 'CommodityDisplayTypeEnums' or count != 14:
        raise ValueError('unexpected badge enum reference')
    entries = {}
    for index in struct.unpack_from('<14I', data, enum_offset+148):
        name_pointer, value, _, _ = struct.unpack_from('<4I', data, POINTER_BASE+index*16)
        name = text_at(name_pointer)
        if 'BADGE' in name:
            entries[name] = value
    expected = {'CommodityDisplayType_BADGEICN':8,
                'CommodityDisplayType_BADGEBKG':9,
                'CommodityDisplayType_BADGEFRM':10}
    if entries != expected:
        raise ValueError('unexpected badge display types')

    slots = {}
    for index in (2,3,4):
        descriptor = field(0xD0C78, index)
        name, comment = text_at(descriptor[3]), text_at(descriptor[36])
        if '-1' not in comment:
            raise ValueError('missing unequipped-slot evidence')
        slots[name] = {'unequipped':-1, 'metadata_comment':comment}
    return {'sha256':digest, 'Type_enum':enum_name,
            'Type_enum_pointer':hex(enum_pointer), 'badge_types':entries,
            'set_badge_slots':slots}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('metalib', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(args.metalib), ensure_ascii=True, indent=2))
    except (OSError,ValueError,struct.error) as exc:
        parser.exit(2, str(exc)+'\n')


if __name__ == '__main__':
    main()
