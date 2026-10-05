"""SQLite-backed clan persistence for Assault Fire PH.

This module deliberately contains no wire/protocol code.  It shares the same
SQLite file as PlayerDatabase and keeps clan state authoritative on the server.

Membership, captain permissions, announcements, capacity and permanent badge
ownership use transactions in the account database. SafeCode uses salted
scrypt. Wire encoding lives in clan_wire/clan_service.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ClanDBError(RuntimeError):
    def __init__(self, message, code=0x1008):
        super().__init__(message)
        self.code = code


CLAN_NAME_MAX_BYTES = 16  # PH UI restricts names to 4..16 characters.
SAFE_CODE_MAX_BYTES = 10
CERTIFIED_MAIL_MAX_BYTES = 50
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_single_byte(value: str, *, field: str, max_bytes: int) -> str:
    value = str(value or '')
    if value != value.strip():
        raise ClanDBError(f'{field} may not start or end with whitespace')
    try:
        raw = value.encode('latin1', 'strict')
    except UnicodeEncodeError as exc:
        raise ClanDBError(f'{field} is not encodable in the PH single-byte field') from exc
    if not (1 <= len(raw) <= int(max_bytes)):
        raise ClanDBError(f'{field} length must be 1..{max_bytes} bytes')
    if any(b < 0x20 or b == 0x7F for b in raw):
        raise ClanDBError(f'{field} contains control characters')
    return value


def _validate_optional_single_byte(value: str, *, field: str, max_bytes: int) -> str:
    value = str(value or '')
    if not value:
        return ''
    try:
        raw = value.encode('latin1', 'strict')
    except UnicodeEncodeError as exc:
        raise ClanDBError(f'{field} is not encodable in the PH single-byte field') from exc
    if len(raw) > int(max_bytes):
        raise ClanDBError(f'{field} exceeds {max_bytes} bytes')
    if any(b < 0x20 or b == 0x7F for b in raw):
        raise ClanDBError(f'{field} contains control characters')
    return value


def normalize_clan_name(name: str) -> str:
    value = _normalize_single_byte(name, field='clan name', max_bytes=CLAN_NAME_MAX_BYTES)
    if len(value) < 4:
        raise ClanDBError('clan name must be at least 4 characters')
    return value


def _hash_safe_code(safe_code: str) -> tuple[bytes | None, bytes | None]:
    safe_code = str(safe_code or '')
    if not safe_code:
        return None, None
    _validate_optional_single_byte(
        safe_code,
        field='SafeCode',
        max_bytes=SAFE_CODE_MAX_BYTES,
    )
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        safe_code.encode('latin1'),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return digest, salt


class ClanDatabase:
    MAX_MEMBERS = 20
    MAX_EXPANDED_MEMBERS = 1000  # PH DefaultTeamPostRight.ini.

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA busy_timeout = 5000')
        conn.execute('PRAGMA journal_mode = WAL')
        return conn

    def init_schema(self) -> Path:
        with self._lock:
            conn = self._connect()
            try:
                # Refuse to silently reinterpret an incompatible legacy clan
                # table.  The current account DB normally has no clan tables.
                existing = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clans'"
                ).fetchone()
                if existing:
                    cols = {
                        str(r['name'])
                        for r in conn.execute('PRAGMA table_info(clans)').fetchall()
                    }
                    required = {
                        'clan_id', 'name', 'name_norm', 'owner_uin',
                        'safe_code_hash', 'safe_code_salt', 'certified_mail',
                        'notice', 'introduction', 'created_at', 'updated_at',
                    }
                    if not required.issubset(cols):
                        raise ClanDBError(
                            'existing clans table has an incompatible legacy schema; '
                            'back up/migrate it instead of overwriting it'
                        )

                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS clans (
                        clan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        name_norm TEXT NOT NULL UNIQUE,
                        owner_uin INTEGER NOT NULL,
                        safe_code_hash BLOB,
                        safe_code_salt BLOB,
                        certified_mail TEXT NOT NULL DEFAULT '',
                        notice TEXT NOT NULL DEFAULT '',
                        introduction TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS clan_members (
                        clan_id INTEGER NOT NULL,
                        uin INTEGER NOT NULL UNIQUE,
                        role TEXT NOT NULL CHECK (
                            role IN (
                                'captain', 'vicecaptain', 'instructor',
                                'player', 'reserveplayer', 'honorarycaptain'
                            )
                        ),
                        joined_at TEXT NOT NULL,
                        PRIMARY KEY (clan_id, uin),
                        FOREIGN KEY (clan_id) REFERENCES clans(clan_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_clan_members_clan
                        ON clan_members(clan_id);

                    CREATE TABLE IF NOT EXISTS clan_applications (
                        application_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        clan_id INTEGER NOT NULL,
                        uin INTEGER NOT NULL,
                        message TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','accepted','rejected','cancelled')),
                        created_at TEXT NOT NULL,
                        resolved_at TEXT,
                        UNIQUE (clan_id, uin, status),
                        FOREIGN KEY (clan_id) REFERENCES clans(clan_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS clan_audit (
                        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        clan_id INTEGER,
                        actor_uin INTEGER,
                        target_uin INTEGER,
                        action TEXT NOT NULL,
                        detail TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    """
                )
                # Additive migration keeps existing v2 clans and membership.
                conn.execute('BEGIN IMMEDIATE')
                cols = {r['name'] for r in conn.execute('PRAGMA table_info(clans)')}
                for name, default in [('max_members', 20), ('packed_icn', -1),
                                      ('packed_frm', -1), ('packed_bkg', -1)]:
                    if name not in cols:
                        conn.execute(f'ALTER TABLE clans ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}')
                # The metalib defines -1 as an unequipped badge slot. Older
                # overlays wrote 0; it is not a catalog item and can be repaired
                # without changing any real equipped component or owned badge.
                for name in ('packed_icn', 'packed_frm', 'packed_bkg'):
                    conn.execute(f'UPDATE clans SET {name}=-1 WHERE {name}=0')
                conn.execute('CREATE TABLE IF NOT EXISTS clan_badges ('
                             'gid INTEGER PRIMARY KEY AUTOINCREMENT, clan_id INTEGER NOT NULL, '
                             'item_id INTEGER NOT NULL, badge_type INTEGER NOT NULL, '
                             'obtained_at INTEGER NOT NULL, avail_hours INTEGER NOT NULL, '
                             'UNIQUE(clan_id,item_id), '
                             'FOREIGN KEY(clan_id) REFERENCES clans(clan_id) ON DELETE CASCADE)')
                conn.execute('CREATE TABLE IF NOT EXISTS clan_invitations ('
                             'invite_id INTEGER PRIMARY KEY AUTOINCREMENT, '
                             'clan_id INTEGER NOT NULL, manager_uin INTEGER NOT NULL, '
                             'uin INTEGER NOT NULL, status TEXT NOT NULL DEFAULT \'pending\' '
                             'CHECK(status IN (\'pending\',\'accepted\',\'rejected\',\'cancelled\')), '
                             'created_at TEXT NOT NULL, resolved_at TEXT, '
                             'FOREIGN KEY(clan_id) REFERENCES clans(clan_id) ON DELETE CASCADE)')
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_clan_invitation_pending '
                             'ON clan_invitations(clan_id,uin) WHERE status=\'pending\'')
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('clan_schema_version','2')")
                conn.commit()
            finally:
                conn.close()
        return self.db_path

    def _player_exists(self, conn: sqlite3.Connection, uin: int) -> bool:
        return bool(conn.execute(
            'SELECT 1 FROM player_profiles WHERE uin=?',
            (int(uin),),
        ).fetchone())

    def clan_name_available(self, name: str) -> bool:
        name = normalize_clan_name(name)
        conn = self._connect()
        try:
            row = conn.execute(
                'SELECT 1 FROM clans WHERE name_norm=?',
                (name.casefold(),),
            ).fetchone()
            return row is None
        finally:
            conn.close()

    def get_clan(self, clan_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                'SELECT * FROM clans WHERE clan_id=?',
                (int(clan_id),),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_player_clan(self, uin: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT c.*, m.role AS member_role, m.joined_at AS member_joined_at
                FROM clan_members m
                JOIN clans c ON c.clan_id=m.clan_id
                WHERE m.uin=?
                """,
                (int(uin),),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_clan_members(self, clan_id: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT m.uin, p.nickname, m.role, m.joined_at
                FROM clan_members m
                LEFT JOIN player_profiles p ON p.uin=m.uin
                WHERE m.clan_id=?
                ORDER BY
                    CASE m.role
                        WHEN 'captain' THEN 0
                        WHEN 'vicecaptain' THEN 1
                        WHEN 'instructor' THEN 2
                        WHEN 'honorarycaptain' THEN 3
                        WHEN 'player' THEN 4
                        ELSE 5
                    END,
                    lower(COALESCE(p.nickname,'')), m.uin
                """,
                (int(clan_id),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def create_clan(
        self,
        owner_uin: int,
        name: str,
        *,
        safe_code: str = '',
        certified_mail: str = '',
        badge_catalog: dict | None = None,
    ) -> dict[str, Any]:
        owner_uin = int(owner_uin)
        name = normalize_clan_name(name)
        certified_mail = _validate_optional_single_byte(
            certified_mail,
            field='CertifiedMail',
            max_bytes=CERTIFIED_MAIL_MAX_BYTES,
        )
        # Validate before opening the write transaction. Hash only when the
        # transaction actually needs a new clan, keeping retransmits cheap.
        _validate_optional_single_byte(
            safe_code,
            field='SafeCode',
            max_bytes=SAFE_CODE_MAX_BYTES,
        )
        if len(safe_code) < 4:
            raise ClanDBError('SafeCode must be 4..10 characters')
        now = _utc_now()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute('BEGIN IMMEDIATE')
                if not self._player_exists(conn, owner_uin):
                    raise ClanDBError(f'player profile missing for uin={owner_uin}')

                current = conn.execute(
                    """
                    SELECT c.* FROM clan_members m
                    JOIN clans c ON c.clan_id=m.clan_id
                    WHERE m.uin=?
                    """,
                    (owner_uin,),
                ).fetchone()
                if current:
                    # Network retransmit safety: the exact same owner/name
                    # creation is idempotent; attempting another clan is not.
                    if (
                        int(current['owner_uin']) == owner_uin
                        and str(current['name']).casefold() == name.casefold()
                    ):
                        conn.commit()
                        return dict(current)
                    raise ClanDBError(f'player {owner_uin} is already in a clan', 0x1010)

                if conn.execute(
                    'SELECT 1 FROM clans WHERE name_norm=?',
                    (name.casefold(),),
                ).fetchone():
                    raise ClanDBError(f'clan name unavailable: {name!r}', 0x1005)

                # Derive one permanent starter component per slot from the
                # authoritative catalog. No commodity/item IDs are assumed.
                catalog = badge_catalog if badge_catalog is not None else json.loads(
                    Path(__file__).with_name('clan_badge_catalog.json').read_text())
                starter = []
                for kind in (0, 1, 2):
                    choices = [r for r in catalog['commodities'].values()
                               if r['type'] == kind and -1 in r['hours']]
                    if not choices:
                        raise ClanDBError('badge catalog has no permanent starter for slot ' + str(kind))
                    starter.append(min(choices, key=lambda r: r['item_id']))

                safe_hash, safe_salt = _hash_safe_code(safe_code)
                cur = conn.execute(
                    """
                    INSERT INTO clans(
                        name,name_norm,owner_uin,
                        safe_code_hash,safe_code_salt,certified_mail,
                        notice,introduction,created_at,updated_at,
                        packed_icn,packed_frm,packed_bkg
                    ) VALUES(?,?,?,?,?,?, '', '', ?, ?, ?, ?, ?)
                    """,
                    (
                        name, name.casefold(), owner_uin,
                        safe_hash, safe_salt, certified_mail,
                        now, now,
                        *(row['item_id'] for row in starter),
                    ),
                )
                clan_id = int(cur.lastrowid)
                conn.executemany(
                    'INSERT INTO clan_badges(clan_id,item_id,badge_type,obtained_at,avail_hours) '
                    'VALUES(?,?,?,?,?)',
                    [(clan_id, row['item_id'], row['type'], int(time.time()), -1)
                     for row in starter],
                )
                conn.execute(
                    """
                    INSERT INTO clan_members(clan_id,uin,role,joined_at)
                    VALUES(?,?,'captain',?)
                    """,
                    (clan_id, owner_uin, now),
                )
                conn.execute(
                    """
                    INSERT INTO clan_audit(
                        clan_id,actor_uin,target_uin,action,detail,created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (clan_id, owner_uin, owner_uin, 'create', name, now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        result = self.get_clan(clan_id)
        if result is None:
            raise ClanDBError('created clan disappeared after commit')
        return result

    def verify_safe_code(self, clan_id: int, safe_code: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                'SELECT safe_code_hash,safe_code_salt FROM clans WHERE clan_id=?',
                (int(clan_id),),
            ).fetchone()
            if not row:
                return False
            digest = row['safe_code_hash']
            salt = row['safe_code_salt']
            if digest is None or salt is None:
                return str(safe_code or '') == ''
            try:
                candidate = hashlib.scrypt(
                    str(safe_code or '').encode('latin1', 'strict'),
                    salt=bytes(salt), n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                    dklen=32,
                )
            except (UnicodeEncodeError, ValueError):
                return False
            return hmac.compare_digest(candidate, bytes(digest))
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute('BEGIN IMMEDIATE')
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _membership(self, conn, uin, *, captain=False):
        row = conn.execute(
            'SELECT m.*, c.owner_uin FROM clan_members m '
            'JOIN clans c ON c.clan_id=m.clan_id WHERE m.uin=?', (uin,)
        ).fetchone()
        if row is None:
            raise ClanDBError('player has no clan', 0x1002)
        if captain and (row['role'] != 'captain' or row['owner_uin'] != uin):
            raise ClanDBError('captain permission required', 0x1004)
        return row

    def _audit(self, conn, clan_id, actor, target, action):
        conn.execute(
            'INSERT INTO clan_audit(clan_id,actor_uin,target_uin,action,created_at) '
            'VALUES(?,?,?,?,?)', (clan_id, actor, target, action, _utc_now())
        )

    def apply(self, uin, clan_id, message=''):
        message = _validate_optional_single_byte(message, field='application', max_bytes=80)
        with self.transaction() as c:
            if not self._player_exists(c, uin):
                raise ClanDBError('player profile missing')
            if c.execute('SELECT 1 FROM clan_members WHERE uin=?', (uin,)).fetchone():
                raise ClanDBError('already in a clan', 0x1010)
            if not c.execute('SELECT 1 FROM clans WHERE clan_id=?', (clan_id,)).fetchone():
                raise ClanDBError('clan does not exist', 0x1002)
            row = c.execute(
                'SELECT * FROM clan_applications WHERE clan_id=? AND uin=? '
                "AND status='pending'", (clan_id, uin)
            ).fetchone()
            if row:
                return dict(row)
            # A player can reapply after leaving; keep one application record
            # per clan/player and avoid the legacy status uniqueness trap.
            row = c.execute('SELECT * FROM clan_applications WHERE clan_id=? AND uin=?',
                            (clan_id, uin)).fetchone()
            if row:
                application_id = row['application_id']
                c.execute("UPDATE clan_applications SET status='pending', message=?, "
                          'created_at=?, resolved_at=NULL WHERE application_id=?',
                          (message, _utc_now(), application_id))
            else:
                cur = c.execute('INSERT INTO clan_applications(clan_id,uin,message,created_at) '
                                'VALUES(?,?,?,?)', (clan_id, uin, message, _utc_now()))
                application_id = cur.lastrowid
            self._audit(c, clan_id, uin, uin, 'apply')
            return dict(c.execute('SELECT * FROM clan_applications WHERE application_id=?',
                                  (application_id,)).fetchone())

    def applications(self, actor):
        c = self._connect()
        try:
            member = self._membership(c, actor, captain=True)
            return [dict(r) for r in c.execute(
                "SELECT a.*, p.nickname FROM clan_applications a JOIN player_profiles p "
                "ON p.uin=a.uin WHERE a.clan_id=? AND a.status='pending' "
                'ORDER BY a.application_id LIMIT 100', (member['clan_id'],))]
        finally:
            c.close()

    def approve(self, actor, target, application_id, agree):
        with self.transaction() as c:
            member = self._membership(c, actor, captain=True)
            row = c.execute('SELECT * FROM clan_applications WHERE application_id=? '
                            'AND uin=? AND clan_id=?',
                            (application_id, target, member['clan_id'])).fetchone()
            desired = 'accepted' if agree else 'rejected'
            if row is None:
                raise ClanDBError('application missing')
            if row['status'] == desired:
                return member['clan_id']
            if row['status'] != 'pending':
                raise ClanDBError('application already resolved')
            if agree:
                if c.execute('SELECT 1 FROM clan_members WHERE uin=?', (target,)).fetchone():
                    raise ClanDBError('target already in a clan', 0x1010)
                count = c.execute('SELECT count(*) FROM clan_members WHERE clan_id=?',
                                  (member['clan_id'],)).fetchone()[0]
                maximum = c.execute('SELECT max_members FROM clans WHERE clan_id=?',
                                    (member['clan_id'],)).fetchone()[0]
                if count >= maximum:
                    raise ClanDBError('clan is full', 0x100D)
                c.execute("INSERT INTO clan_members(clan_id,uin,role,joined_at) "
                          "VALUES(?,?,'player',?)", (member['clan_id'], target, _utc_now()))
            c.execute('UPDATE clan_applications SET status=?, resolved_at=? WHERE application_id=?',
                      (desired, _utc_now(), application_id))
            self._audit(c, member['clan_id'], actor, target, desired)
            return member['clan_id']

    def invite(self, actor, target):
        with self.transaction() as c:
            member = self._membership(c, actor, captain=True)
            if target == actor or not self._player_exists(c, target):
                raise ClanDBError('invalid invitation target')
            if c.execute('SELECT 1 FROM clan_members WHERE uin=?', (target,)).fetchone():
                raise ClanDBError('target already in a clan', 0x1010)
            clan = c.execute('SELECT * FROM clans WHERE clan_id=?', (member['clan_id'],)).fetchone()
            count = c.execute('SELECT count(*) FROM clan_members WHERE clan_id=?',
                              (member['clan_id'],)).fetchone()[0]
            if count >= clan['max_members']:
                raise ClanDBError('clan is full', 0x100D)
            invitation = c.execute('SELECT * FROM clan_invitations WHERE clan_id=? AND uin=? '
                                   "AND status='pending'", (member['clan_id'],target)).fetchone()
            if invitation is None:
                cur = c.execute('INSERT INTO clan_invitations(clan_id,manager_uin,uin,created_at) '
                                'VALUES(?,?,?,?)', (member['clan_id'],actor,target,_utc_now()))
                invitation = c.execute('SELECT * FROM clan_invitations WHERE invite_id=?',
                                       (cur.lastrowid,)).fetchone()
                self._audit(c,member['clan_id'],actor,target,'invite')
            return dict(invitation), dict(clan)

    def confirm_invitation(self, actor, clan_id, invite_id, agree):
        with self.transaction() as c:
            invitation = c.execute('SELECT * FROM clan_invitations WHERE invite_id=? AND clan_id=? '
                                   'AND uin=?', (invite_id,clan_id,actor)).fetchone()
            if invitation is None:
                raise ClanDBError('invitation missing or belongs to another player')
            clan = c.execute('SELECT * FROM clans WHERE clan_id=?', (clan_id,)).fetchone()
            if clan is None:
                raise ClanDBError('clan missing',0x1002)
            desired = 'accepted' if agree else 'rejected'
            if invitation['status'] == desired:
                if agree and not c.execute('SELECT 1 FROM clan_members WHERE clan_id=? AND uin=?',
                                           (clan_id,actor)).fetchone():
                    raise ClanDBError('accepted invitation no longer matches membership')
                return dict(clan), False
            if invitation['status'] != 'pending':
                raise ClanDBError('invitation already resolved')
            if agree:
                manager = self._membership(c,invitation['manager_uin'],captain=True)
                if manager['clan_id'] != clan_id:
                    raise ClanDBError('invitation manager changed clans',0x1004)
                if c.execute('SELECT 1 FROM clan_members WHERE uin=?',(actor,)).fetchone():
                    raise ClanDBError('already in a clan',0x1010)
                count = c.execute('SELECT count(*) FROM clan_members WHERE clan_id=?',(clan_id,)).fetchone()[0]
                if count >= clan['max_members']:
                    raise ClanDBError('clan is full',0x100D)
                c.execute("INSERT INTO clan_members(clan_id,uin,role,joined_at) VALUES(?,?,'player',?)",
                          (clan_id,actor,_utc_now()))
                c.execute("UPDATE clan_invitations SET status='cancelled',resolved_at=? WHERE uin=? "
                          "AND status='pending' AND invite_id!=?",(_utc_now(),actor,invite_id))
            c.execute('UPDATE clan_invitations SET status=?,resolved_at=? WHERE invite_id=?',
                      (desired,_utc_now(),invite_id))
            self._audit(c,clan_id,actor,actor,'invite_'+desired)
            return dict(clan), bool(agree)

    def leave(self, actor):
        with self.transaction() as c:
            member = self._membership(c, actor)
            if member['role'] == 'captain':
                raise ClanDBError('captain cannot leave before ownership transfer', 0x1004)
            clan = dict(c.execute('SELECT * FROM clans WHERE clan_id=?',
                                 (member['clan_id'],)).fetchone())
            c.execute('DELETE FROM clan_members WHERE uin=?', (actor,))
            self._audit(c, member['clan_id'], actor, actor, 'leave')
            return clan

    def kick(self, actor, target, safe_code):
        with self.transaction() as c:
            member = self._membership(c, actor, captain=True)
            if not self.verify_safe_code(member['clan_id'], safe_code):
                raise ClanDBError('invalid SafeCode', 0x101A)
            other = self._membership(c, target)
            if actor == target or member['clan_id'] != other['clan_id']:
                raise ClanDBError('invalid kick target', 0x1004)
            c.execute('DELETE FROM clan_members WHERE uin=?', (target,))
            self._audit(c, member['clan_id'], actor, target, 'kick')
            return member['clan_id']

    def disband(self, actor, safe_code):
        """Immediately dissolve the actor's clan and return its former members."""
        with self.transaction() as c:
            manager = self._membership(c, actor, captain=True)
            cid = manager['clan_id']
            if not self.verify_safe_code(cid, safe_code):
                raise ClanDBError('invalid SafeCode', 0x101A)
            members = [r['uin'] for r in c.execute(
                'SELECT uin FROM clan_members WHERE clan_id=?', (cid,))]
            self._audit(c, cid, actor, None, 'disband')
            # All clan-owned tables have ON DELETE CASCADE; account profiles,
            # wallets and the independent audit history remain intact.
            c.execute('DELETE FROM clans WHERE clan_id=?', (cid,))
            return members

    def set_member_role(self, actor, target, role):
        """Persist an appointment for any non-captain member of this clan."""
        if role not in ('vicecaptain', 'instructor', 'player',
                        'reserveplayer', 'honorarycaptain'):
            raise ClanDBError('invalid member role')
        with self.transaction() as c:
            manager = self._membership(c, actor, captain=True)
            member = self._membership(c, target)
            if (actor == target or manager['clan_id'] != member['clan_id']
                    or member['role'] == 'captain'):
                raise ClanDBError('invalid appointment target', 0x1004)
            if member['role'] == role:
                return manager['clan_id']
            # The stock appointment panel allows up to four honorary captains.
            if role == 'honorarycaptain':
                count = c.execute(
                    "SELECT COUNT(*) FROM clan_members WHERE clan_id=? "
                    "AND role='honorarycaptain'", (manager['clan_id'],)
                ).fetchone()[0]
                if count >= 4:
                    raise ClanDBError('honorary captain positions are full')
            c.execute('UPDATE clan_members SET role=? WHERE clan_id=? AND uin=?',
                      (role, manager['clan_id'], target))
            c.execute('UPDATE clans SET updated_at=? WHERE clan_id=?',
                      (_utc_now(), manager['clan_id']))
            self._audit(c, manager['clan_id'], actor, target,
                        'set_role:' + member['role'] + '->' + role)
            return manager['clan_id']

    def update_introduction(self, actor, text):
        text = _validate_optional_single_byte(text, field='introduction', max_bytes=127)
        with self.transaction() as c:
            member = self._membership(c, actor, captain=True)
            c.execute('UPDATE clans SET introduction=?, updated_at=? WHERE clan_id=?',
                      (text, _utc_now(), member['clan_id']))
            self._audit(c, member['clan_id'], actor, actor, 'introduction')
            return member['clan_id']

    def update_notice(self, actor, index, text):
        # This metalib has capacity for one bulletin. Its index is zero based.
        if index != 0:
            raise ClanDBError('invalid bulletin index')
        text = _validate_optional_single_byte(text, field='announcement', max_bytes=100)
        with self.transaction() as c:
            member = self._membership(c, actor, captain=True)
            c.execute('UPDATE clans SET notice=?, updated_at=? WHERE clan_id=?',
                      (text, _utc_now(), member['clan_id']))
            self._audit(c, member['clan_id'], actor, actor, 'announcement')
            return member['clan_id']

    def expand(self, actor, maximum):
        with self.transaction() as c:
            member = self._membership(c, actor, captain=True)
            old = c.execute('SELECT max_members FROM clans WHERE clan_id=?',
                            (member['clan_id'],)).fetchone()[0]
            if not old <= maximum <= self.MAX_EXPANDED_MEMBERS:
                raise ClanDBError('invalid expansion size')
            if maximum == old:
                return member['clan_id']
            # Stock UI: 471 -> 764 quotes 2930 AP, i.e. 10 AP per new seat.
            cost = (maximum - old) * 10
            wallet = c.execute('SELECT ap FROM player_wallets WHERE uin=?',
                               (actor,)).fetchone()
            if wallet is None or wallet['ap'] < cost:
                raise ClanDBError('insufficient expansion AP', 0x820B)
            now = _utc_now()
            c.execute('UPDATE player_wallets SET ap=ap-?,updated_at=? WHERE uin=?',
                      (cost, now, actor))
            c.execute('INSERT INTO player_moneyflow(uin,occurred_at,money_type,number,current_balance,'
                      'reason,details,commodity_ids,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                      (actor, int(time.time()), 1, -cost, wallet['ap']-cost, 8,
                       f'clan expansion {old}->{maximum}', '[]', now))
            c.execute('UPDATE clans SET max_members=?, updated_at=? WHERE clan_id=?',
                      (maximum, _utc_now(), member['clan_id']))
            if maximum != old:
                self._audit(c, member['clan_id'], actor, actor, 'expand')
            return member['clan_id']

    def list_badges(self, clan_id):
        c = self._connect()
        try:
            return [dict(r) for r in c.execute('SELECT * FROM clan_badges WHERE clan_id=? ORDER BY gid',
                                              (clan_id,))]
        finally:
            c.close()

    def buy_badges(self, actor, pay_type, commodities, catalog):
        if not 1 <= len(commodities) <= 20:
            raise ClanDBError('empty or oversized badge cart')
        selected = []
        for entry in commodities:
            row = catalog['commodities'].get(str(entry['CommodityId']))
            if row is None:
                raise ClanDBError('unknown badge commodity')
            index = entry['PriceIndex'] - catalog['price_index_base']
            if not 0 <= index < len(row['prices']) or entry['VoucherId']:
                raise ClanDBError('invalid badge option or voucher')
            if pay_type != {'GP': 1, 'TP': 2, 'MP': 3}[row['currency']]:
                raise ClanDBError('wrong badge currency')
            # Clan EXP/level progression is not implemented. An ordinal cap
            # of seven per category permanently blocked legitimate catalog
            # entries (including the captured commodity 200230). Purchases use
            # the complete configured catalog until progression is supported.
            if row['item_id'] in [r['item_id'] for r, _ in selected]:
                raise ClanDBError('duplicate badge in cart')
            selected.append((row, index))
        with self.transaction() as c:
            member = self._membership(c, actor, captain=True)
            owned = {r[0] for r in c.execute('SELECT item_id FROM clan_badges WHERE clan_id=?',
                                           (member['clan_id'],))}
            fresh = [(r, i) for r, i in selected if r['item_id'] not in owned]
            if len(owned) + len(fresh) > 100:
                raise ClanDBError('badge inventory is full')
            totals = {'ap': 0, 'gp': 0, 'mp': 0}
            for row, index in fresh:
                totals[{'TP': 'ap', 'GP': 'gp', 'MP': 'mp'}[row['currency']]] += row['prices'][index]
            wallet = c.execute('SELECT * FROM player_wallets WHERE uin=?', (actor,)).fetchone()
            if wallet is None or any(wallet[k] < v for k, v in totals.items()):
                raise ClanDBError('insufficient badge funds', 0x820B)
            # Wallet debit, grants and audit share one transaction. Client Price
            # is informational (the captured PH client sent zero), never trusted.
            c.execute('UPDATE player_wallets SET ap=ap-?,gp=gp-?,mp=mp-?,updated_at=? WHERE uin=?',
                      (totals['ap'], totals['gp'], totals['mp'], _utc_now(), actor))
            now = int(time.time())
            for row, index in fresh:
                c.execute('INSERT INTO clan_badges(clan_id,item_id,badge_type,obtained_at,avail_hours) '
                          'VALUES(?,?,?,?,?)', (member['clan_id'], row['item_id'], row['type'], now, row['hours'][index]))
            for currency, amount in totals.items():
                if amount:
                    c.execute('INSERT INTO player_moneyflow(uin,occurred_at,money_type,number,current_balance,'
                              'reason,details,commodity_ids,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                              (actor, now, {'ap': 1, 'gp': 2, 'mp': 3}[currency], -amount,
                               wallet[currency]-amount, 8, 'clan badge purchase',
                               json.dumps([e['CommodityId'] for e in commodities]), _utc_now()))
            if fresh:
                self._audit(c, member['clan_id'], actor, actor, 'buy_badges')
            return member['clan_id']

    def set_badge(self, actor, icon, frame, background):
        # Accept the earlier emulator's zero sentinel as a compatibility input,
        # but persist and project the stock client's -1 value.
        icon, frame, background = (-1 if item == 0 else item
                                   for item in (icon, frame, background))
        with self.transaction() as c:
            member = self._membership(c, actor, captain=True)
            owned = {r['item_id']: r['badge_type'] for r in c.execute(
                'SELECT item_id,badge_type FROM clan_badges WHERE clan_id=?', (member['clan_id'],))}
            for item, kind in [(icon, 0), (frame, 1), (background, 2)]:
                if item != -1 and owned.get(item) != kind:
                    raise ClanDBError('badge component not owned or wrong type')
            c.execute('UPDATE clans SET packed_icn=?,packed_frm=?,packed_bkg=?,updated_at=? WHERE clan_id=?',
                      (icon, frame, background, _utc_now(), member['clan_id']))
            self._audit(c, member['clan_id'], actor, actor, 'set_badge')
            return member['clan_id']

    def search(self, name):
        c = self._connect()
        try:
            # Literal substring search: percent/underscore are not SQL wildcards.
            return [dict(r) for r in c.execute(
                'SELECT * FROM clans WHERE instr(name_norm,?)>0 ORDER BY clan_id LIMIT 36',
                (str(name).casefold(),))]
        finally:
            c.close()
