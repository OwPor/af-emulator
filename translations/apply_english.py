"""Preview, install, or restore the curated PH 1.0.0.24 English translations."""
import argparse
import collections
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import zlib

DATA = Path(__file__).resolve().parent / 'ph/en'
MAGIC = bytes.fromhex('f3f3f3f3')
KEY = bytes.fromhex('5447414D451FE92C9A971A0CD1F610FB')
TOKENS = re.compile(r'\{[^{}\r\n]+\}|\$[A-Za-z_][A-Za-z_0-9]*\$|%[A-Za-z_][A-Za-z_0-9]*%|RePlaceName\d+|`[A-Za-z]|\\[nrt]')

def sha(data):
    return hashlib.sha256(data).hexdigest()

def decode(blob):
    if not blob.startswith(MAGIC):
        return blob, False
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if len(blob) <= 4 or (len(blob)-4) % 16:
        raise ValueError('Encrypted localization is not AES aligned')
    decryptor = Cipher(algorithms.AES(KEY), modes.ECB()).decryptor()
    payload = decryptor.update(blob[4:]) + decryptor.finalize()
    raw = zlib.decompress(payload[4:])
    if len(raw) != struct.unpack_from('<I', payload)[0]:
        raise ValueError('Decompressed localization size mismatch')
    return raw, True

def encode(raw, encrypted):
    if not encrypted:
        return raw
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    payload = struct.pack('<I',len(raw)) + zlib.compress(raw)
    payload += b'\0' * (-len(payload) % 16)
    encryptor = Cipher(algorithms.AES(KEY),modes.ECB()).encryptor()
    return MAGIC + encryptor.update(payload) + encryptor.finalize()

def patch_localization(blob, entries):
    raw, encrypted = decode(blob)
    if raw.startswith(b'\xff\xfe'):
        bom, encoding = raw[:2], 'utf-16-le'
    elif raw.startswith(b'\xfe\xff'):
        bom, encoding = raw[:2], 'utf-16-be'
    else:
        raise ValueError('Expected UTF-16 localization with a BOM')
    lines = raw[2:].decode(encoding).splitlines(keepends=True)
    edits = {(e['section'],e['key'],e['occurrence']):e for e in entries}
    if len(edits) != len(entries):
        raise ValueError('Duplicate translation target in patch data')
    section = '';counts=collections.Counter();seen=set();changed=0
    for index,line in enumerate(lines):
        stripped=line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section=stripped[1:-1]
        if not stripped or stripped.startswith((';','#')) or '=' not in line:
            continue
        prefix,rest=line.split('=',1);key=prefix.strip()
        counts[(section,key)]+=1
        target=(section,key,counts[(section,key)])
        if target not in edits:
            continue
        seen.add(target);entry=edits[target]
        body=rest.rstrip('\r\n');old=body.strip();new=entry['value']
        if old==new:
            continue
        if old not in entry['accepted_values']:
            raise ValueError('Custom or unexpected value at ['+section+'] '+key+'; refused to overwrite')
        if collections.Counter(TOKENS.findall(old)) != collections.Counter(TOKENS.findall(new)):
            raise ValueError('Placeholder mismatch at '+key)
        leading=body[:len(body)-len(body.lstrip())];trailing=body[len(body.rstrip()):]
        lines[index]=prefix+'='+leading+new+trailing+rest[len(body):]
        changed+=1
    missing=set(edits)-seen
    if missing:
        raise ValueError('Missing translation keys: '+', '.join(t[1] for t in sorted(missing)[:5]))
    if not changed:
        return blob,0
    patched_raw=bom+''.join(lines).encode(encoding)
    result=encode(patched_raw,encrypted)
    if decode(result)!=(patched_raw,encrypted):
        raise ValueError('Localization encryption round-trip failed')
    return result,changed

def patch_movie(blob, spec):
    digest=sha(blob)
    if digest==spec['patched_sha256']:
        return blob,0
    if digest not in spec['accepted_sha256'] or len(blob)!=spec['size']:
        raise ValueError('Unexpected UI movie version or local edit; refused to overwrite')
    result=bytearray(blob);previous_end=0;changed=0
    for site in sorted(spec['sites'],key=lambda s:s['offset']):
        before=bytes.fromhex(site['before_hex']);after=bytes.fromhex(site['after_hex'])
        start=site['offset'];end=start+len(before)
        if start<previous_end or start<0 or end>len(blob) or len(before)!=len(after):
            raise ValueError('Invalid or overlapping movie patch range')
        if result[start:end] not in (before,after):
            raise ValueError('UI text does not match the expected original')
        if result[start:end]!=after:changed+=1
        result[start:end]=after;previous_end=end
    if sha(result)!=spec['patched_sha256']:
        raise ValueError('Patched movie verification failed')
    return bytes(result),changed

def atomic_write(path,data):
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix=path.name+'.',suffix='.tmp',delete=False) as f:
            temporary=Path(f.name);f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
    finally:
        if temporary is not None and temporary.exists():temporary.unlink()

def load_data():
    entries=json.loads((DATA/'localization.json').read_text(encoding='utf-8'))['entries']
    groups=collections.defaultdict(list)
    for entry in entries:
        name=entry['file']
        if Path(name).name!=name or not name.endswith('.int'):
            raise ValueError('Invalid localization filename')
        groups[name].append(entry)
    movie=json.loads((DATA/'movies.json').read_text(encoding='utf-8'))
    if movie['path']!='TGame/CookedPC/UI/TGUI_Movies.upk':
        raise ValueError('Unexpected movie package path')
    return groups,movie

def install(client,apply=False,localization_only=False,movies_only=False):
    groups,movie=load_data();pending=[];errors=[]
    specs=[]
    if not movies_only:
        specs.extend((Path('TGame/Localization/INT')/name,'localization',edits) for name,edits in groups.items())
    if not localization_only:specs.append((Path(movie['path']),'movie',movie))
    for relative,kind,edits in specs:
        path=client/relative
        try:
            original=path.read_bytes()
            patched,count=patch_movie(original,edits) if kind=='movie' else patch_localization(original,edits)
        except (OSError,ValueError,zlib.error,UnicodeError,struct.error) as exc:
            errors.append(str(relative)+': '+str(exc));continue
        if count:
            pending.append((relative,path,original,patched));print('Ready: '+str(relative)+' ('+str(count)+' corrections)')
        else:print('Already installed: '+str(relative))
    if errors:raise ValueError('\n'.join(errors)+'\nNo client files changed.')
    if not pending:
        print('All selected English translations are already installed.');return None
    if not apply:
        print('Preview only. Add --apply to install.');return None
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    backup=client/'AF_Translation_Backups'/('Public_English_'+stamp)
    backup.mkdir(parents=True,exist_ok=False)
    record={'version':1,'client_root':str(client.resolve()),'files':[]}
    for relative,path,original,patched in pending:
        saved=backup/relative;saved.parent.mkdir(parents=True,exist_ok=True);saved.write_bytes(original)
        if sha(saved.read_bytes())!=sha(original):raise ValueError('Backup verification failed; client unchanged')
        record['files'].append({'path':relative.as_posix(),'before_sha256':sha(original),'after_sha256':sha(patched)})
    (backup/'backup.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    written=[]
    try:
        for relative,path,original,patched in pending:
            if sha(path.read_bytes())!=sha(original):raise ValueError(str(relative)+': changed after preflight')
            atomic_write(path,patched);written.append((path,original))
            if sha(path.read_bytes())!=sha(patched):raise ValueError(str(relative)+': installed verification failed')
    except Exception:
        for path,original in reversed(written):atomic_write(path,original)
        raise
    print('Installed. Restart Assault Fire.');print('Backup folder: '+str(backup));return backup

def restore(client,backup,apply=False):
    groups,movie=load_data()
    allowed={('TGame/Localization/INT/'+name) for name in groups}|{movie['path']}
    record=json.loads((backup/'backup.json').read_text(encoding='utf-8'))
    if record.get('version')!=1 or Path(record['client_root']).resolve()!=client.resolve():
        raise ValueError('Backup belongs to a different client or installer')
    pending=[];errors=[];seen=set()
    for entry in record['files']:
        value=entry['path']
        if value not in allowed or value in seen:raise ValueError('Unexpected backup file')
        seen.add(value);path=client/Path(value);original=(backup/Path(value)).read_bytes()
        if sha(original)!=entry['before_sha256']:raise ValueError('Backup integrity failed: '+value)
        current=path.read_bytes();digest=sha(current)
        if digest==entry['before_sha256']:print('Already restored: '+value)
        elif digest==entry['after_sha256']:pending.append((path,current,original));print('Ready to restore: '+value)
        else:errors.append(value+': changed since installation; refused to overwrite')
    if errors:raise ValueError('\n'.join(errors)+'\nNo client files changed.')
    if not apply:print('Restore preview only. Add --apply to restore.');return
    written=[]
    try:
        for path,current,original in pending:
            if sha(path.read_bytes())!=sha(current):raise ValueError('Client file changed after preflight')
            atomic_write(path,original);written.append((path,current))
            if sha(path.read_bytes())!=sha(original):raise ValueError('Restore verification failed')
    except Exception:
        for path,current in reversed(written):atomic_write(path,current)
        raise
    print('Restored exact pre-install files. Restart Assault Fire.')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-root',type=Path,required=True,help='Folder containing TGame')
    parser.add_argument('--apply',action='store_true',help='Write changes; otherwise preview only')
    parser.add_argument('--restore',type=Path,help='Backup folder printed by this installer')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--localization-only',action='store_true',help='Only update .int localization text')
    group.add_argument('--movies-only',action='store_true',help='Only update hardcoded UI movie text')
    args=parser.parse_args()
    if args.restore and (args.localization_only or args.movies_only):parser.error('--restore cannot use partial-install flags')
    try:
        client=args.client_root.resolve()
        if args.restore:restore(client,args.restore.resolve(),args.apply)
        else:install(client,args.apply,args.localization_only,args.movies_only)
    except ImportError:
        print('Install the localization dependency: py -3.12 -m pip install cryptography',file=sys.stderr);return 1
    except (OSError,ValueError,KeyError,zlib.error,UnicodeError,struct.error) as exc:
        print('ERROR: '+str(exc),file=sys.stderr);return 1
    return 0

if __name__=='__main__':sys.exit(main())
