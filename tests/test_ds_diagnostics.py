import io
from pathlib import Path
import tempfile
import time
import unittest
import os
from unittest.mock import patch
from server.assaultfire_ds_spawner import SpawnerConfig, DedicatedServerSpawner
from server.assaultfire_ds_diagnostics import DiagnosticsStore, capture_pipe, should_persist
from server.assaultfire_database_logging import DatabaseLogger

class DiagnosticTests(unittest.TestCase):
    def test_server_packet_noise_does_not_enter_logging_queue(self):
        with tempfile.TemporaryDirectory() as td:
            logger=DatabaseLogger(Path(td)/'db.sqlite3')
            logger.console_level_name='ERROR'
            with patch.object(logger._queue,'put_nowait',side_effect=AssertionError('packet log enqueued')):
                for _ in range(1000):
                    logger.emit('ZONE','TGame APP: cmd=0xa004 body=00')
            logger.emit('ZONE','malformed request',level='WARNING')
            logger._queue.join()
            logger.close()
            with logger.store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM ds_diagnostic_events').fetchone()[0],1)

    def test_production_filter_avoids_database_writes_for_packet_noise(self):
        with tempfile.TemporaryDirectory() as td:
            store=DiagnosticsStore(Path(td)/'db.sqlite3')
            with patch.object(store, 'connect', side_effect=AssertionError('filtered log reached SQLite')):
                for source,message in (
                    ('server:ZONE','[INFO] TGame APP: cmd=0xa004 body=00'),
                    ('server:ZONE','[INFO] TX ZN2C_RES_HEARTBEAT wire=abcd'),
                    ('loader','[AFDEV] scanning UObject 123 error=None'),
                    ('actor','channel=2 packet=123 bytes=abcd'),
                    ('bridge','[C->S #100] forwarded 120 bytes'),
                    ('server:ROOM','[DEBUG] room created'),
                ):
                    store.append('run',1,10001,source,message)
            for source,message,level in (
                ('server:ZONE','[WARNING] malformed packet',None),
                ('server:ZONE','write refused','ERROR'),
                ('spawner','reserved room=1 owner=10001',None),
                ('spawner','match-player begin room=1 starter=10001',None),
                ('spawner','SESSION_READY room=1',None),
                ('spawner','round ended room=1',None),
                ('bridge','[BRIDGE-v9] FATAL: RuntimeError: loader failed',None),
            ):
                store.append('run',1,10001,source,message,level=level)
            self.assertEqual(len(store.events_for_user(10001)),7)

    def test_live_ipc_is_temporary_and_snapshots_persist_in_account_database(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'AF_ACCOUNT_DB':str(Path(td)/'accounts.sqlite3')}):
            os.environ.pop('AF_DS_RUNTIME_DIR',None)
            config=SpawnerConfig.from_env()
            ipc=config.runtime_dir
            with patch.object(DedicatedServerSpawner,'_udp_port_available',return_value=True):
                spawner=DedicatedServerSpawner(config)
                room=spawner.reserve_lobby(owner_id=10001,mode_id=0x1001,map_id=59)
                self.assertFalse((ipc/'spawner_state.json').exists())
                spawner.shutdown_all()
            self.assertFalse(ipc.exists())
            with spawner._diagnostics.connect() as db:
                self.assertEqual(db.execute('SELECT owner_uin,state FROM ds_diagnostic_sessions').fetchone(),(10001,'RELEASED'))

    def test_server_logger_uses_authoritative_caller_uin_and_room(self):
        with tempfile.TemporaryDirectory() as td:
            logger=DatabaseLogger(Path(td)/'db.sqlite3')
            def handler():
                role_state={'uin':10002,'v143b_ds_room_id':123}
                logger.emit('ZONE','request failed')
            handler()
            logger._queue.join()
            logger.close()
            rows=logger.store.events_for_user(10002)
            self.assertEqual(rows[0]['room_id'],123)
            self.assertIn('request failed',rows[0]['message'])

    def test_retention_truncation_and_shared_match_user_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            store=DiagnosticsStore(Path(td)/'accounts.sqlite3',max_events=7,per_session=3,max_chars=32,max_sessions=2)
            for session in ('match-a','match-b','match-c'):
                store.snapshot(session,dict(room_id=1,owner_id=10001,state='READY'),[10001,10002])
                for i in range(5):
                    store.append(session,1,10001,'loader',str(i)+'X'*100,level='ERROR')
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM ds_diagnostic_events').fetchone()[0],7)
                self.assertLessEqual(db.execute('SELECT max(n) FROM (SELECT count(*) n FROM ds_diagnostic_events GROUP BY session_id)').fetchone()[0],3)
                self.assertEqual(db.execute('SELECT count(*) FROM ds_diagnostic_sessions').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT max(length(message)),min(truncated) FROM ds_diagnostic_events').fetchone(),(32,1))
                self.assertEqual(db.execute('SELECT count(*) FROM ds_diagnostic_users WHERE session_id="match-a"').fetchone()[0],0)
            self.assertTrue(store.events_for_user(10002))
            self.assertEqual(store.events_for_user(99999),[])

    def test_old_events_expire_and_ids_do_not_cross_server_runs(self):
        with tempfile.TemporaryDirectory() as td:
            store=DiagnosticsStore(Path(td)/'db.sqlite3')
            store.append('run-a:room-1:0',1,10001,'bridge','old',level='ERROR')
            with store.connect() as db:
                db.execute('UPDATE ds_diagnostic_events SET created_at=?',(time.time()-8*86400,))
            store.append('run-b:room-1:0',1,10002,'bridge','new',level='ERROR')
            self.assertEqual(store.events_for_user(10001),[])
            self.assertEqual(store.events_for_user(10002)[0]['message'],'new')

    def test_child_output_is_drained_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            store=DiagnosticsStore(Path(td)/'db.sqlite3')
            thread=capture_pipe(io.BytesIO(b'[ERROR] '+b'X'*20000+b'\nERROR: map load\n'),store,'run',1,10001,'loader')
            thread.join(5)
            self.assertFalse(thread.is_alive())
            rows=store.events_for_user(10001)
            self.assertEqual(rows[0]['message'],'ERROR: map load')
            self.assertTrue(all(len(r['message'])<=4096 for r in rows))
            self.assertTrue(any(r['truncated'] for r in rows))

if __name__=='__main__': unittest.main()
