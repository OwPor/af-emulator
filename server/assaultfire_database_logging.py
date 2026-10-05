"""Console logging plus bounded database capture, with caller UIN correlation."""
import atexit
from datetime import datetime
import inspect
import os
from pathlib import Path
import queue
import re
import threading
import uuid
try:
    from .assaultfire_ds_diagnostics import DiagnosticsStore, should_persist
except ImportError:
    from assaultfire_ds_diagnostics import DiagnosticsStore, should_persist


class DatabaseLogger:
    def __init__(self, db_path=None):
        self.path = Path(db_path or os.environ.get('AF_ACCOUNT_DB', str(Path(__file__).with_name('assaultfire_accounts.sqlite3')))).resolve()
        self.store = DiagnosticsStore(self.path)
        self.console_level_name = os.environ.get('AF_LOG_LEVEL', 'INFO').upper()
        self._run = 'server:'+uuid.uuid4().hex
        self._queue = queue.Queue(maxsize=1024)
        self._stop = threading.Event()
        self._dropped = 0
        self._context = threading.local()
        self._thread = threading.Thread(target=self._consume,daemon=True,name='server-db-logs')
        self._thread.start()
        atexit.register(self.close)

    def _consume(self):
        while not self._stop.is_set() or not self._queue.empty():
            try: record = self._queue.get(timeout=.1)
            except queue.Empty: continue
            try: self.store.append(*record)
            finally: self._queue.task_done()

    def emit(self, label, message, level=None):
        levels={'DEBUG':10,'INFO':20,'WARNING':30,'WARN':30,'ERROR':40,'CRITICAL':50}
        name=str(level or 'INFO').upper()
        if isinstance(level,int): name=next((n for n,v in levels.items() if v==level),'INFO')
        if levels.get(name,20)>=levels.get(self.console_level_name,20):
            print(f'[{datetime.now():%H:%M:%S}] [{name}] [{label}] {message}',flush=True)
        # log() forwards to emit(); use its caller's authoritative role_state.
        frame=inspect.currentframe()
        uin=room_id=None
        try:
            caller=frame.f_back
            if caller and caller.f_code.co_name=='log': caller=caller.f_back
            role=caller.f_locals.get('role_state') if caller else None
            if isinstance(role,dict):
                self._context.role = role
            else:
                role = getattr(self._context, 'role', None)
            if isinstance(role,dict):
                uin=role.get('uin')
                room=role.get('v79_created_match_room') or {}
                room_id=role.get('v143b_ds_room_id') or room.get('room_id')
        finally:
            del frame
            del caller
        if uin is None:
            match=re.search(r'\buin[=:]\s*(\d+)\b',str(message),re.I)
            if match: uin=int(match.group(1))
        if room_id is None:
            match=re.search(r'\broom[=:]\s*(\d+)\b',str(message))
            if match: room_id=int(match.group(1))
        body=f'[{name}] {message}'
        if not should_persist('server:'+str(label), body, level=name):
            return
        # Preserve the truncation marker while bounding queued memory as well.
        record=(self._run+':'+str(uin or 'global'),room_id,uin,'server:'+str(label),body[:4097])
        try: self._queue.put_nowait(record)
        except queue.Full:
            self._dropped+=1
            if self._dropped==1: print('[DB-LOGS] capture queue full; dropping diagnostics to keep the server responsive',flush=True)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=3)
        if self._dropped:
            self.store.append(self._run,None,None,'server:logging',f'[WARNING] dropped {self._dropped} messages because capture queue was full')


def build_logger():
    return DatabaseLogger()
