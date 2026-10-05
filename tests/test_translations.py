"""Exercise translation merging and reversible installation without client assets."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('af_translations', ROOT/'translations/apply_english.py')
patch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patch)

class TranslationTests(unittest.TestCase):
    def test_all_reviewed_localization_entries(self):
        groups, _ = patch.load_data()
        for name, entries in groups.items():
            with self.subTest(file=name):
                chunks=['; Preserve this comment\r\n']
                sections={}
                for entry in entries:sections.setdefault(entry['section'],[]).append(entry)
                for section, rows in sections.items():
                    chunks.append('['+section+']\r\n')
                    keys={}
                    for row in rows:keys.setdefault(row['key'],[]).append(row)
                    for key, rows_for_key in keys.items():
                        selected={r['occurrence']:r for r in rows_for_key}
                        for occurrence in range(1,max(selected)+1):
                            row=selected.get(occurrence)
                            value=row['accepted_values'][0] if row else 'Untouched duplicate'
                            chunks.append(key+' = '+value+' \r\n')
                    chunks.append('CustomUnrelatedKey="Keep my translation"\r\n')
                raw=b'\xff\xfe'+''.join(chunks).encode('utf-16-le')
                original=patch.encode(raw,True)
                result,count=patch.patch_localization(original,entries)
                self.assertEqual(count,len(entries))
                decoded,encrypted=patch.decode(result)
                self.assertTrue(encrypted)
                self.assertIn('CustomUnrelatedKey="Keep my translation"\r\n',decoded[2:].decode('utf-16-le'))
                self.assertEqual(patch.patch_localization(result,entries),(result,0))

    def test_endianness_placeholders_and_custom_values(self):
        entries=[dict(section='UI',key='Greeting',occurrence=1,
                      accepted_values=['"Hi {Name}"'],value='"Hello {Name}"')]
        raw=b'\xfe\xff'+'[UI]\r\nGreeting = "Hi {Name}"\r\nOther=unchanged\r\n'.encode('utf-16-be')
        result,count=patch.patch_localization(raw,entries)
        self.assertEqual(count,1)
        self.assertTrue(result.startswith(b'\xfe\xff'))
        self.assertIn('Other=unchanged\r\n',result[2:].decode('utf-16-be'))
        with self.assertRaisesRegex(ValueError,'Custom or unexpected'):
            patch.patch_localization(b'\xff\xfe'+'[UI]\r\nGreeting="Custom wording"\r\n'.encode('utf-16-le'),entries)
        with self.assertRaisesRegex(ValueError,'Placeholder mismatch'):
            patch.patch_localization(raw,[{**entries[0],'value':'"Hello"'}])

    def test_movie_ranges_hashes_and_already_patched_input(self):
        before=b'header-original-text-footer'
        after=b'header-replaced-text-footer'
        start=7;old=b'original';new=b'replaced'
        spec=dict(size=len(before),accepted_sha256=[patch.sha(before)],patched_sha256=patch.sha(after),
                  sites=[dict(offset=start,before_hex=old.hex(),after_hex=new.hex())])
        self.assertEqual(patch.patch_movie(before,spec),(after,1))
        self.assertEqual(patch.patch_movie(after,spec),(after,0))
        with self.assertRaisesRegex(ValueError,'Unexpected UI movie'):
            patch.patch_movie(before+b'x',spec)
        with self.assertRaisesRegex(ValueError,'overlapping'):
            patch.patch_movie(before,{**spec,'sites':spec['sites']*2})

    def test_install_restore_preflight_and_rollback(self):
        entry=dict(section='UI',key='Label',occurrence=1,accepted_values=['Cancel old'],value='Cancel')
        fake_movie={'path':'TGame/CookedPC/UI/TGUI_Movies.upk'}
        original_loader=patch.load_data
        real_writer=patch.atomic_write
        patch.load_data=lambda:({'One.int':[entry],'Two.int':[entry]},fake_movie)
        try:
            with tempfile.TemporaryDirectory() as directory,contextlib.redirect_stdout(io.StringIO()):
                client=Path(directory);folder=client/'TGame/Localization/INT';folder.mkdir(parents=True)
                raw=b'\xff\xfe'+'[UI]\r\nLabel=Cancel old\r\nCustom=preserved\r\n'.encode('utf-16-le')
                for name in ('One.int','Two.int'):(folder/name).write_bytes(raw)
                self.assertIsNone(patch.install(client,localization_only=True))
                self.assertFalse((client/'AF_Translation_Backups').exists())
                backup=patch.install(client,True,localization_only=True)
                self.assertIsNotNone(backup)
                self.assertIsNone(patch.install(client,True,localization_only=True))
                patch.restore(client,backup,True);patch.restore(client,backup,True)
                self.assertEqual((folder/'One.int').read_bytes(),raw)
                self.assertEqual((folder/'Two.int').read_bytes(),raw)
                custom=b'\xff\xfe'+'[UI]\r\nLabel=Custom\r\n'.encode('utf-16-le')
                (folder/'Two.int').write_bytes(custom)
                with self.assertRaisesRegex(ValueError,'No client files changed'):
                    patch.install(client,True,localization_only=True)
                self.assertEqual((folder/'One.int').read_bytes(),raw)
                (folder/'Two.int').write_bytes(raw)
                calls=[0]
                def fail_second(path,data):
                    calls[0]+=1
                    if calls[0]==2:raise OSError('injected write failure')
                    real_writer(path,data)
                patch.atomic_write=fail_second
                with self.assertRaises(OSError):patch.install(client,True,localization_only=True)
                self.assertEqual((folder/'One.int').read_bytes(),raw)
                self.assertEqual((folder/'Two.int').read_bytes(),raw)
        finally:
            patch.load_data=original_loader;patch.atomic_write=real_writer

if __name__=='__main__':unittest.main()
