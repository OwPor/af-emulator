"""Bounded SQLite DS diagnostics, correlated by session, room and game UIN."""
from collections import deque
from contextlib import contextmanager
import json
import os
import re
from pathlib import Path
import sqlite3
import threading
import time


def should_persist(source, message, level=None):
    """Production policy: warning/error severity or explicit match transitions."""
    text = str(message)
    severity = str(level or '').upper()
    if isinstance(level, int):
        if level >= 30:
            return True
        severity = 'DEBUG' if level < 20 else 'INFO'
    if severity in ('WARN', 'WARNING', 'ERROR', 'CRITICAL', 'FATAL'):
        return True
    tags = re.findall(r'\[(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL)\]', text, re.I)
    if any(tag.upper() in ('WARN','WARNING','ERROR','CRITICAL','FATAL') for tag in tags):
        return True
    if severity == 'DEBUG' or 'DEBUG' in [tag.upper() for tag in tags]:
        return False
    if source == 'actor':
        return False
    # Routine packet output may contain user text or protocol field names.
    if re.search(r'FOLLOW-UP (?:RX|FRAME)|TGame (?:APP:|post-CHGSKEY)|TX ZN2C_|RX CHUNK|HEARTBEAT', text, re.I):
        return False
    diagnostic = re.sub(r'\berror\s*[=:]\s*(?:None|0|false|"")\b', '', text, flags=re.I)
    if re.search(r'\b(?:warning|warn|error|fail|failed|failure|fatal|timeout|exception|traceback)\b|\b\w+(?:Error|Exception):', diagnostic, re.I):
        return True
    if source == 'spawner':
        return bool(re.search(r'^(?:reserved room=|settings updated room=|armed room=|re-armed ROUND_ENDED|SESSION_READY room=|RELAY LIVE room=|room-player (?:join|leave)|match-player (?:begin|in|quit)|round ended room=|release room=|teardown(?: complete)? room=|room owner transferred)|AFDEV STARTING', text))
    if source.startswith('server:'):
        label = source.split(':', 1)[1].upper()
        return label in ('DS-HANDOFF','DS-SETTINGS','ROOM','MATCH','LATEJOIN','REWARDS') and bool(re.search(
            r'match handoff prepared|bridge armed|A11A ->|StartMatch accepted|room (?:created|released|started)|leave finalized|owner transferred|round ended|match (?:started|ended)|rewards? (?:applied|granted)|late.?join (?:accepted|completed)', text, re.I))
    return source in ('bridge','loader') and bool(re.search(r'SESSION_READY|RELAY LIVE|AFDEV STARTING|\[BRIDGE-v9\] stopped|lazy.*(?:spawn|start)', text, re.I))


class DiagnosticsStore:
    def __init__(self, path, max_events=5000, per_session=500, max_chars=4096, max_sessions=1000, days=7):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_events, self.per_session = max_events, per_session
        self.max_chars, self.max_sessions, self.days = max_chars, max_sessions, days
        self._warned = False
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS ds_diagnostic_sessions (
                    session_id TEXT PRIMARY KEY, room_id INTEGER, owner_uin INTEGER,
                    updated_at REAL NOT NULL, state TEXT, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ds_diagnostic_users (
                    session_id TEXT NOT NULL, uin INTEGER NOT NULL,
                    PRIMARY KEY(session_id, uin));
                CREATE INDEX IF NOT EXISTS ds_diagnostic_users_uin ON ds_diagnostic_users(uin);
                CREATE TABLE IF NOT EXISTS ds_diagnostic_events (
                    id INTEGER PRIMARY KEY, created_at REAL NOT NULL, session_id TEXT,
                    room_id INTEGER, uin INTEGER, source TEXT NOT NULL,
                    message TEXT NOT NULL, truncated INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS ds_diagnostic_events_session ON ds_diagnostic_events(session_id,id);
                CREATE INDEX IF NOT EXISTS ds_diagnostic_events_uin ON ds_diagnostic_events(uin,id);
            ''')
            self.prune(db)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.25)
        try:
            with db:
                yield db
        finally:
            db.close()

    def prune(self, db):
        cutoff = time.time() - self.days * 86400
        db.execute('DELETE FROM ds_diagnostic_events WHERE created_at < ?', (cutoff,))
        db.execute('DELETE FROM ds_diagnostic_events WHERE id IN (SELECT id FROM ds_diagnostic_events ORDER BY id DESC LIMIT -1 OFFSET ?)', (self.max_events,))
        db.execute('DELETE FROM ds_diagnostic_sessions WHERE updated_at < ?', (cutoff,))
        db.execute('DELETE FROM ds_diagnostic_sessions WHERE session_id IN (SELECT session_id FROM ds_diagnostic_sessions ORDER BY updated_at DESC LIMIT -1 OFFSET ?)', (self.max_sessions,))
        db.execute('DELETE FROM ds_diagnostic_users WHERE session_id NOT IN (SELECT session_id FROM ds_diagnostic_sessions)')

    def append(self, session_id, room_id, uin, source, message, level=None):
        text = str(message)
        if not should_persist(source, text, level):
            return True
        try:
            with self.connect() as db:
                db.execute('INSERT INTO ds_diagnostic_events(created_at,session_id,room_id,uin,source,message,truncated) VALUES(?,?,?,?,?,?,?)',
                    (time.time(), session_id, room_id, uin, source[:32], text[:self.max_chars], int(len(text) > self.max_chars)))
                db.execute('DELETE FROM ds_diagnostic_events WHERE id IN (SELECT id FROM ds_diagnostic_events WHERE session_id IS ? ORDER BY id DESC LIMIT -1 OFFSET ?)', (session_id, self.per_session))
                self.prune(db)
            return True
        except sqlite3.Error as exc:
            if not self._warned:
                self._warned = True
                print(f'[DS-DIAGNOSTICS] database write failed: {exc}', flush=True)
            return False

    def snapshot(self, session_id, room, users):
        # Store selected bounded fields rather than arbitrary unbounded payloads.
        selected = {key: room.get(key) for key in ('room_id','owner_id','owner_nickname','instance_id','state','mode_id','map_id','map_name','game_class','round_generation','last_error')}
        selected = {key: value[:512] if isinstance(value, str) else value for key,value in selected.items()}
        with self.connect() as db:
            db.execute('INSERT INTO ds_diagnostic_sessions VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET owner_uin=excluded.owner_uin, updated_at=excluded.updated_at,state=excluded.state,snapshot=excluded.snapshot',
                (session_id,room['room_id'],room['owner_id'],time.time(),room['state'],json.dumps(selected)))
            db.executemany('INSERT OR IGNORE INTO ds_diagnostic_users VALUES(?,?)', ((session_id,int(uin)) for uin in sorted(set(users))[:32]))
            self.prune(db)

    def events_for_user(self, uin, limit=100):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('''
                SELECT e.* FROM ds_diagnostic_events e
                WHERE e.uin=? OR EXISTS (SELECT 1 FROM ds_diagnostic_users u
                    WHERE u.session_id=e.session_id AND u.uin=?)
                ORDER BY e.id DESC LIMIT ?''', (int(uin),int(uin),max(1,min(500,int(limit)))))]


def from_environment():
    path = os.environ.get('AF_DS_DIAGNOSTICS_DB')
    try:
        return DiagnosticsStore(path) if path else None
    except sqlite3.Error as exc:
        print(f'[DS-DIAGNOSTICS] child database capture unavailable: {exc}', flush=True)
        return None


def capture_pipe(pipe, store, session_id, room_id, uin, source, tail=None):
    """Continuously drain a binary child pipe; never buffer an unlimited line."""
    tail = tail if tail is not None else deque(maxlen=14)
    def drain():
        continuation_level = None
        traceback_active = False
        try:
            while True:
                chunk = pipe.readline(4097)
                if not chunk:
                    break
                line = chunk.decode('utf-8', errors='replace').rstrip('\r\n')
                tail.append(line[-4096:])
                if line.startswith('Traceback ('):
                    traceback_active = True
                level = 'ERROR' if traceback_active else continuation_level
                if level is None and re.search(r'\[(?:ERROR|FATAL|CRITICAL)\]', line, re.I):
                    level = 'ERROR'
                store.append(session_id, room_id, uin, source, line, level=level)
                continuation_level = level if not chunk.endswith(b'\n') else None
                if traceback_active and line and not line[0].isspace() and not line.startswith('Traceback ('):
                    traceback_active = False
        finally:
            pipe.close()
    thread = threading.Thread(target=drain, name=f'ds-diagnostics-{source}', daemon=True)
    thread.start()
    return thread
