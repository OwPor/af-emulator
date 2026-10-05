import importlib.util
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('af_login_names',ROOT/'tools/bridge/af_login_names.py')
H=importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)

def url(uin=10001):
    return f':-528/UTFrontEnd?Name=Player1?Team=1?Uin={uin}?ClanName=Lmao?SpectatorOnly=0?bVIP=0?bIsRoomOwner=1'
def bunch(payload, ch=0, bits=None, op=0):
    return ('BUNCH',int(bool(op)),op,0,1,ch,23,1,len(payload)*8 if bits is None else bits,payload)
def login(uin=10001, packet=37, wide=False, trailing=False):
    payload = b'\x04'+struct.pack('<I',10000)+b'\x05'+H.write_string('response-kept')+H.write_string(url(uin),wide)
    bits = len(payload)*8
    if trailing:
        payload += b'\x7b\x15'
        bits += 13
    return H.encrypt_wire(H.build_packet(packet,[('ACK',13),bunch(payload,bits=bits),bunch(b'\x15',ch=2,bits=5)]))
def login_url(wire):
    _, records = H.parse_packet(H.decrypt_wire(wire))
    rec = next(r for r in records if r[0]=='BUNCH' and r[5]==0)
    _, pos, _ = H.read_string(rec[-1],6,rec[-2]//8)
    return H.read_string(rec[-1],pos,rec[-2]//8)[0]
def create_db(path):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE player_profiles (uin INTEGER PRIMARY KEY, nickname TEXT)')
        db.executemany('INSERT INTO player_profiles VALUES (?,?)',[(10001,'dadada'),(10002,'猫😀Friend')])

class NamesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root/'accounts.sqlite3'
        create_db(self.db)
        self.logs=[]
        self.rewriter=H.LoginNameRewriter(self.root,db_path=self.db,log=lambda s,**kw:self.logs.append(s))
    def tearDown(self): self.temp.cleanup()
    def test_per_account_all_login_packets_metadata_and_tail(self):
        original=login(trailing=True)
        result=self.rewriter.rewrite(original,('127.0.0.1',2000))
        self.assertEqual(login_url(result),url().replace('Name=Player1','Name=dadada'))
        a, ra=H.parse_packet(H.decrypt_wire(original)); b, rb=H.parse_packet(H.decrypt_wire(result))
        self.assertEqual(a,b)
        self.assertEqual(ra[0],rb[0]); self.assertEqual(ra[2],rb[2])
        self.assertEqual(ra[1][:8],rb[1][:8])
        self.assertEqual(ra[1][-2]-rb[1][-2],8*(len('Player1')-len('dadada')))
        self.assertEqual(H.bits_from_bytes(ra[1][-1])[:ra[1][-2]][-13:],H.bits_from_bytes(rb[1][-1])[:rb[1][-2]][-13:])
        self.assertEqual(ra[1][-1][:6+len(H.write_string('response-kept'))],rb[1][-1][:6+len(H.write_string('response-kept'))])
        self.assertEqual(self.rewriter.rewrite(original,('127.0.0.1',2000)),result)
        self.assertEqual(len(self.logs),1)
        late=self.rewriter.rewrite(login(10002),('127.0.0.1',2001))
        self.assertIn('Name=猫😀Friend?Team',login_url(late))
        self.assertNotIn('dadada',login_url(late))
    def test_wide_string_roundtrip(self):
        for s in ('abc','猫😀',''):
            encoded=H.write_string(s,True)
            self.assertEqual(H.read_string(encoded,0,len(encoded))[0],s)
        result=self.rewriter.rewrite(login(10002,wide=True),'wide')
        self.assertIn('Name=猫😀Friend',login_url(result))
    def test_url_options_and_ambiguous_identity(self):
        lookup=lambda u:'Account'+str(u)
        for source in (url()+'?Uin=10002',url()+'?Name=Second',url().replace('Uin=10001','Uin=10000'),url().replace('Uin=10001','NoUin=10001')):
            self.assertEqual(H.rewrite_url(source,lookup)[0],source)
        changed,_=H.rewrite_url('/Map?UIN=10002?Team=1',lookup)
        self.assertEqual(changed,'/Map?UIN=10002?Team=1?Name=Account10002')
        with self.assertRaises(ValueError): H.rewrite_url('/Map?Uin=abc',lookup)
    def test_missing_invalid_profiles_read_only_and_missing_database(self):
        before=self.db.read_bytes()
        original=login(77777)
        self.assertEqual(self.rewriter.rewrite(original,'missing'),original)
        self.assertEqual(self.db.read_bytes(),before)
        absent=self.root/'absent.sqlite3'
        self.assertIsNone(H.nickname_from_db(absent,10001));self.assertFalse(absent.exists())
        for bad in ('Bad?Uin=10002','X\x00Y','x'*32,''):
            with sqlite3.connect(self.db) as db: db.execute('UPDATE player_profiles SET nickname=? WHERE uin=10001',(bad,))
            self.assertEqual(self.rewriter.rewrite(login(packet=50+len(bad)),'bad'+bad),login(packet=50+len(bad)))
    def test_join_bypasses_gameplay_and_reconnect_refreshes_profile(self):
        first=login(); result=self.rewriter.rewrite(first,'peer')
        join=H.encrypt_wire(H.build_packet(38,[bunch(b'\x09')]))
        self.assertEqual(self.rewriter.rewrite(join,'peer'),join)
        with patch.object(H,'decrypt_wire',side_effect=AssertionError('gameplay decoded')):
            opaque=b'gameplay-datagram'
            self.assertEqual(self.rewriter.rewrite(opaque,'peer'),opaque)
            self.assertEqual(self.rewriter.rewrite(first,'peer'),result)
        with sqlite3.connect(self.db) as db:db.execute("UPDATE player_profiles SET nickname='NewNick' WHERE uin=10001")
        hello=H.encrypt_wire(H.build_packet(0,[('ACK',31),bunch(b'\0'+b'\0'*9,op=1)]))
        self.assertGreater(struct.unpack_from('<I',hello)[0],17)
        self.assertEqual(self.rewriter.rewrite(hello,'peer'),hello)
        self.assertIn('Name=NewNick',login_url(self.rewriter.rewrite(first,'peer')))
    def test_malformed_packets_and_rebuild_overflow_preserve_original(self):
        for i,wire in enumerate((b'',b'\0'*20,struct.pack('<I',9999)+b'\0'*16,H.encrypt_wire(b'\0'*17))):
            self.assertEqual(self.rewriter.rewrite(wire,('bad',i)),wire)
        bad=H.encrypt_wire(H.build_packet(2,[bunch(b'\x05'+struct.pack('<i',2147483647))]))
        self.assertEqual(self.rewriter.rewrite(bad,'bad-string'),bad)
        with sqlite3.connect(self.db) as db:db.execute('UPDATE player_profiles SET nickname=? WHERE uin=10001',('😀'*31,))
        data=b'\x05'+H.write_string('')+H.write_string(url()+ '?Padding='+'a'*310)
        oversized=H.encrypt_wire(H.build_packet(2,[bunch(data)]))
        self.assertEqual(self.rewriter.rewrite(oversized,'oversized'),oversized)
    def test_peer_and_retransmit_caches_bounded(self):
        for i in range(300):self.rewriter.rewrite(login(packet=i),('peer',i))
        self.assertEqual(len(self.rewriter.peers),256)
        for i in range(20):self.rewriter.rewrite(login(packet=i),'same')
        self.assertEqual(len(self.rewriter.peers['same']['packets']),16)
    def test_same_account_different_peers_refreshes_latest_database(self):
        self.assertIn('Name=dadada',login_url(self.rewriter.rewrite(login(),'first')))
        with sqlite3.connect(self.db) as db:db.execute("UPDATE player_profiles SET nickname='Renamed' WHERE uin=10001")
        self.assertIn('Name=Renamed',login_url(self.rewriter.rewrite(login(),'second')))

class BridgeTests(unittest.TestCase):
    def setUp(self):
        import shutil
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.bridge=self.root/'tools/bridge/af_ds_udp_bridge_v9_multi_peer_latch.py'
        self.bridge.parent.mkdir(parents=True)
        shutil.copyfile(ROOT/'tools/bridge'/self.bridge.name,self.bridge)
        shutil.copyfile(ROOT/'tools/bridge/af_login_names.py',self.bridge.with_name('af_login_names.py'))
    def tearDown(self):self.temp.cleanup()
    def test_actual_udp_bridge_two_accounts_late_join_and_reverse_packets(self):
        db=self.root/'server/assaultfire_accounts.sqlite3';db.parent.mkdir();create_db(db)
        target=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);target.bind(('127.0.0.1',0));target.settimeout(3)
        reserve=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1];reserve.close()
        state=self.root/'state.json';logfile=self.root/'bridge.log'
        env=os.environ.copy();env.pop('AF_ACCOUNT_DB',None);env['PYTHONUTF8']='1'
        clients=[]
        with logfile.open('wb') as out:
            proc=subprocess.Popen([sys.executable,str(self.bridge),'--listen-ip','127.0.0.1','--listen-port',str(port),'--target-port',str(target.getsockname()[1]),'--state-file',str(state)],stdout=out,stderr=subprocess.STDOUT,env=env)
        try:
            deadline=time.monotonic()+3
            while not state.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.02)
            self.assertIsNone(proc.poll(),logfile.read_text())
            self.assertTrue(state.exists(),logfile.read_text())
            upstreams=[]
            for uin in (10001,10002):
                client=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);client.bind(('127.0.0.1',0));client.settimeout(3);clients.append(client)
                hello=H.encrypt_wire(H.build_packet(0,[bunch(b'\0'+b'\0'*9,op=1)]))
                client.sendto(hello,('127.0.0.1',port));received,source=target.recvfrom(65535);self.assertEqual(received,hello)
                upstreams.append(source)
                reply=b'unmodified server reply';target.sendto(reply,source);self.assertEqual(client.recvfrom(65535)[0],reply)
                original=login(uin);client.sendto(original,('127.0.0.1',port));received,source2=target.recvfrom(65535)
                self.assertEqual(source2,source);self.assertIn('Name='+('dadada' if uin==10001 else '猫😀Friend'),login_url(received))
                client.sendto(original,('127.0.0.1',port));repeated,_=target.recvfrom(65535);self.assertEqual(repeated,received)
                join=H.encrypt_wire(H.build_packet(38,[bunch(b'\x09')]))
                client.sendto(join,('127.0.0.1',port));self.assertEqual(target.recvfrom(65535)[0],join)
                gameplay=b'unchanged gameplay datagram';client.sendto(gameplay,('127.0.0.1',port));self.assertEqual(target.recvfrom(65535)[0],gameplay)
            self.assertNotEqual(upstreams[0],upstreams[1])
            self.assertIn('uin=10001',logfile.read_text());self.assertIn('uin=10002',logfile.read_text())
        finally:
            proc.terminate();proc.wait(timeout=3);target.close()
            for client in clients:client.close()


if __name__=='__main__':unittest.main()
