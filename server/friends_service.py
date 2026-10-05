"""Persistent friends/social protocol support for Assault Fire PH 1.0.0.24.

Package v3 integration is based on public main commit
2ed58d886da18f6d682a6274ca8487dca9c60660. v3 keeps the live A30F
request parser and adds the same nickname fallback used by web/app.py: prefer
player_profiles, then fall back to profiles for registered accounts whose game
profile has not yet been projected.

This module deliberately keeps the social SQLite state separate from the large
ZONE server file while using the same assaultfire_accounts.sqlite3 database.

Verified/recovered wire families:
  A303/A304 friends presence
  A305/A306 friend request
  A307/A308 accept/reject result
  A309/A30A delete friend

Live capture added 2026-10-01:
  A30F Find Player request
    u16 QueryType | u64 QueryUin | TDR LP string QueryContent

A310 v165 keeps the response wire form that reached the stock UI callback and
uses EPTE_AddByAll for a resolved player. A405/A406 private friend chat and
offline persistence are also included in this restored module.
"""

from __future__ import annotations

import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TGAME_ZN_MAGIC = 0x3243
SNS_ERR_SUCC = 0x8300
# PH add-friend completion/agree code (33537 decimal).
SNS_ADDFRID_AGREE = 0x8301
# Find Player / A310 stock-UI success code.
QUERY_FRIEND_ERR_SUCC = 0x8100
SNS_ERR_NOT_FOUND = 0x8301  # controlled local candidate; success path is the target test

CMD_REQ_FRIEND_STATUS = 0xA303
CMD_RES_FRIEND_STATUS = 0xA304
CMD_REQ_ADD_FRIEND = 0xA305
CMD_NTF_ADD_FRIEND = 0xA306
CMD_C2S_RES_ADD_FRIEND = 0xA307
CMD_S2C_RES_ADD_FRIEND = 0xA308
CMD_REQ_DEL_FRIEND = 0xA309
CMD_RES_DEL_FRIEND = 0xA30A
CMD_REQ_QUERY_FRIEND = 0xA30F
CMD_RES_QUERY_FRIEND = 0xA310
# v170 static PH friend enrichment/presence recovery.
# Compiled proto_c2zn.tdr:
#   A326 ZN2C_NtfFriendLoginOut = u64 FriendUin | u32 Type
#   A33A C2ZN_ReqPlayerExp      = u16 Type | u16 Count | u64 Uins[Count]
#   A33B ZN2C_ResPlayerExp      = u16 Type | u16 Count | u64 Uins[Count] | u32 Exps[Count]
# FriendStatusTypeEnum: EFST_Login=1, EFST_Logout=2.
CMD_NTF_FRIEND_LOGINOUT = 0xA326
CMD_REQ_PLAYER_EXP = 0xA33A
CMD_RES_PLAYER_EXP = 0xA33B
EFST_LOGIN = 1
EFST_LOGOUT = 2
# A326 ZN2C_NtfFriendLoginOut uses a different byte/ordinal domain.
# Live stock PH: Type=1 renders 'Friend ... Offline'.
FRIEND_ONLINE_TYPE = 0
FRIEND_OFFLINE_TYPE = 1
CMD_REQ_CHAT_P2P = 0xA405
CMD_NTF_CHAT_P2P = 0xA406

EPTE_ADD_BY_NONE = 0x0000
EPTE_ADD_BY_ALL = 0x0001
EPTE_ADD_BY_VERIFY = 0x0002
EPTE_ADD_STATUS = 0x0003
EPTE_FIND_BY_QQ = 0x0004

A310_WIRE_RECOVERY = "v166-find-player-ui-success-8100"
FRIEND_ENRICHMENT_PATCH = "v170b-static-a326-a33b-friend-presence-exp"
FRIEND_PRESENCE_ENUM_PATCH = "v173-friend-presence-enum-1-2"
FRIEND_PRESENCE_SPLIT_PATCH = "v174-split-a304-status-a326-online-type"
FRIEND_PRESENCE_INBOX_PATCH = "v171-presence-01-no-login-a308-seed"


class FriendsError(RuntimeError):
    pass


class FriendsProtocolError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _u8(v: int) -> bytes:
    return struct.pack(">B", int(v) & 0xFF)


def _u16(v: int) -> bytes:
    return struct.pack(">H", int(v) & 0xFFFF)


def _u32(v: int) -> bytes:
    return struct.pack(">I", int(v) & 0xFFFFFFFF)


def _u64(v: int) -> bytes:
    return struct.pack(">Q", int(v) & 0xFFFFFFFFFFFFFFFF)


def _wire_bytes(value: str, *, max_bytes: int) -> bytes:
    # PH's legacy nickname/social strings are byte strings. latin-1 preserves
    # all single-byte values and is compatible with the already-used ASCII set.
    raw = str(value or "").encode("latin-1", errors="replace")
    if len(raw) + 1 > int(max_bytes):
        raw = raw[: max(0, int(max_bytes) - 1)]
    return raw


def pack_lp_string(value: str, max_bytes: int = 256) -> bytes:
    raw = _wire_bytes(value, max_bytes=max_bytes) + b"\x00"
    return _u32(len(raw)) + raw


def read_lp_string(body: bytes, off: int, max_bytes: int = 256) -> tuple[str, int]:
    if off < 0 or off + 4 > len(body):
        raise FriendsProtocolError("truncated TDR string length")
    size = struct.unpack_from(">I", body, off)[0]
    off += 4
    if size < 1 or size > int(max_bytes):
        raise FriendsProtocolError(f"invalid TDR string size={size} max={max_bytes}")
    if off + size > len(body):
        raise FriendsProtocolError(
            f"truncated TDR string size={size} remaining={len(body)-off}"
        )
    raw = bytes(body[off : off + size])
    off += size
    if not raw.endswith(b"\x00"):
        raise FriendsProtocolError("TDR string is not NUL terminated")
    return raw[:-1].decode("latin-1", errors="replace"), off


def build_server_app(cmd: int, body: bytes) -> bytes:
    body = bytes(body or b"")
    return struct.pack(">HHHH", TGAME_ZN_MAGIC, int(cmd) & 0xFFFF, 8, len(body)) + body




def parse_read_offline_message_notice(body: bytes) -> dict[str, Any]:
    """AB03: u16 Count | u64 MsgIdArray[Count]."""
    body = bytes(body)
    if len(body) < 2:
        raise FriendsProtocolError(f"AB03 too short: {len(body)}B")
    count = struct.unpack_from(">H", body, 0)[0]
    if count > 256:
        raise FriendsProtocolError(f"AB03 unreasonable count={count}")
    expected = 2 + count * 8
    if len(body) != expected:
        raise FriendsProtocolError(
            f"AB03 malformed count={count} expected={expected} got={len(body)}"
        )
    ids = [
        struct.unpack_from(">Q", body, 2 + i * 8)[0]
        for i in range(count)
    ]
    return {"count": count, "msg_ids": ids}


def parse_player_exp_request(body: bytes) -> dict[str, Any]:
    # A33A: u16 Type | u16 Count | u64 Uins[Count].
    body = bytes(body)
    if len(body) < 4:
        raise FriendsProtocolError(f"A33A too short: {len(body)}B")
    system_type, count = struct.unpack_from(">HH", body, 0)
    if count > 100:
        raise FriendsProtocolError(f"A33A unreasonable count={count}")
    expected = 4 + count * 8
    if len(body) != expected:
        raise FriendsProtocolError(
            f"A33A malformed count={count} expected={expected} got={len(body)}"
        )
    uins = [
        struct.unpack_from(">Q", body, 4 + i * 8)[0]
        for i in range(count)
    ]
    return {"type": system_type, "count": count, "uins": uins}


def build_player_exp_response(
    system_type: int,
    uins: list[int] | tuple[int, ...],
    exps: list[int] | tuple[int, ...],
) -> bytes:
    # A33B: u16 Type | u16 Count | u64 Uins[] | u32 Exps[].
    uins = [int(v) for v in (uins or [])]
    exps = [int(v) for v in (exps or [])]
    if len(uins) != len(exps):
        raise FriendsProtocolError(
            f"A33B UIN/EXP length mismatch: {len(uins)} != {len(exps)}"
        )
    if len(uins) > 100:
        raise FriendsProtocolError(f"A33B unreasonable count={len(uins)}")
    body = (
        _u16(system_type)
        + _u16(len(uins))
        + b"".join(_u64(v) for v in uins)
        + b"".join(_u32(v) for v in exps)
    )
    return build_server_app(CMD_RES_PLAYER_EXP, body)


def build_friend_loginout(friend_uin: int, online: bool) -> bytes:
    # A326: u64 FriendUin | u32 OnlineType (0=online, 1=offline).
    # A326.Type uses FriendStatusTypeEnum: Login=1, Logout=2.
    return build_server_app(0xA326, _u64(friend_uin) + _u32(1 if bool(online) else 2))


def parse_friend_status_request(body: bytes) -> dict[str, Any]:
    """A303: u16 Count | u16 Type | u64 FriendUinLst[Count]."""
    body = bytes(body)
    if len(body) < 4:
        raise FriendsProtocolError(f"A303 too short: {len(body)}B")
    count, query_type = struct.unpack_from(">HH", body, 0)
    if count > 256:
        raise FriendsProtocolError(f"A303 unreasonable count={count}")
    expected = 4 + count * 8
    if len(body) != expected:
        raise FriendsProtocolError(
            f"A303 malformed count={count} expected={expected} got={len(body)}"
        )
    uins = [struct.unpack_from(">Q", body, 4 + i * 8)[0] for i in range(count)]
    return {"count": count, "type": query_type, "uins": uins}


def _pack_player_address(
    main_channel_id: int = 1,
    zone_id: int = 1,
    sub_channel_id: int = 1,
    room_id: int = 0,
    display_id: int = 0,
    mode_id: int = 0,
    map_id: int = 0,
) -> bytes:
    # PH TDR PlayerAddress: 4 + 4 + 8 + 2 + 4 + 2 = 24 bytes.
    return (_u32(main_channel_id) + _u32(sub_channel_id) + _u64(room_id)
            + _u16(display_id) + _u32(mode_id) + _u16(map_id))


def _pack_friend_status(uin: int, online: bool) -> bytes:
    # Compiled proto_c2zn.tdr enum: EFST_Login=1, EFST_Logout=2.
    # Status is PlayerStateEnums, not FriendStatusTypeEnum.
    address = _pack_player_address() if bool(online) else bytes(24)
    return _u64(uin) + address + _u16(7 if bool(online) else 1)


def build_friend_status_response(
    query_type: int,
    requested_uins: list[int] | tuple[int, ...],
    friends: list[dict[str, Any]],
    online_uins: set[int] | list[int] | tuple[int, ...],
) -> bytes:
    requested = {int(x) for x in (requested_uins or [])}
    online = {int(x) for x in (online_uins or [])}
    rows: list[bytes] = []
    for friend in friends or []:
        friend_uin = int(friend["uin"])
        if requested and friend_uin not in requested:
            continue
        rows.append(_pack_friend_status(friend_uin, friend_uin in online))
    body = _u16(len(rows)) + _u16(query_type) + b"".join(rows)
    return build_server_app(CMD_RES_FRIEND_STATUS, body)


def build_friend_invite(
    *, request_id: int, proposer_uin: int, proposer_name: str,
    remark: str = "", msg_id: int | None = None,
) -> bytes:
    if msg_id is None:
        msg_id = int(request_id)
    body = (
        _u32(request_id)
        + _u64(proposer_uin)
        + pack_lp_string(proposer_name, 32)
        + pack_lp_string(remark, 256)
        + (b"\x00" * 8)
        + _u64(msg_id)
    )
    return build_server_app(CMD_NTF_ADD_FRIEND, body)


def parse_add_friend_request(body: bytes) -> dict[str, Any]:
    """A305: u64 RespondentUin | LP NickName | LP Remark."""
    body = bytes(body)
    if len(body) < 8:
        raise FriendsProtocolError(f"A305 too short: {len(body)}B")
    respondent_uin = struct.unpack_from(">Q", body, 0)[0]
    off = 8
    respondent_name, off = read_lp_string(body, off, 32)
    remark, off = read_lp_string(body, off, 256)
    return {
        "respondent_uin": respondent_uin,
        "respondent_name": respondent_name,
        "remark": remark,
        "tail": body[off:],
    }


def parse_add_friend_client_response(body: bytes) -> dict[str, Any]:
    """A307: u32 RequestId | u16 Result | u64 ProposerUin | optional LP name."""
    body = bytes(body)
    if len(body) < 14:
        raise FriendsProtocolError(f"A307 too short: {len(body)}B")
    request_id = struct.unpack_from(">I", body, 0)[0]
    result = struct.unpack_from(">H", body, 4)[0]
    proposer_uin = struct.unpack_from(">Q", body, 6)[0]
    off = 14
    proposer_name = ""
    if off < len(body):
        try:
            proposer_name, off = read_lp_string(body, off, 32)
        except FriendsProtocolError:
            proposer_name = ""
    return {
        "request_id": request_id,
        "result": result,
        "proposer_uin": proposer_uin,
        "proposer_name": proposer_name,
        "tail": body[off:],
    }


def build_add_friend_result(
    *, friend_uin: int, friend_name: str,
    result: int = SNS_ADDFRID_AGREE, msg_id: int = 1,
) -> bytes:
    """A308: u16 Result | u64 FriendUin | LP name | DT(8) | u64 MsgId.

    Successful friend materialization uses SNS_ADDFRID_AGREE (0x8301),
    not generic SNS_ERR_SUCC (0x8300).
    """
    body = (
        _u16(result)
        + _u64(friend_uin)
        + pack_lp_string(friend_name, 32)
        + (b"\x00" * 8)
        + _u64(msg_id)
    )
    return build_server_app(CMD_S2C_RES_ADD_FRIEND, body)


def parse_delete_friend_request(body: bytes) -> int:
    body = bytes(body)
    if len(body) != 8:
        raise FriendsProtocolError(f"A309 must be exactly 8B, got {len(body)}B")
    return struct.unpack(">Q", body)[0]


def build_delete_friend_response(friend_uin: int, result: int = SNS_ERR_SUCC) -> bytes:
    # Recovered TDR order: FriendUin | Result.
    return build_server_app(CMD_RES_DEL_FRIEND, _u64(friend_uin) + _u16(result))


def parse_query_friend_request(body: bytes) -> dict[str, Any]:
    """Parse the live A30F Find Player request.

    Captured 2026-10-01 for nickname "mynigga":
      0002 0000000000000000 00000008 6d796e6967676100
      ^type ^query UIN         ^len     ^nickname + NUL
    """
    body = bytes(body)
    if len(body) < 15:  # 2 + 8 + 4 + at least one NUL
        raise FriendsProtocolError(f"A30F too short: {len(body)}B")
    query_type = struct.unpack_from(">H", body, 0)[0]
    query_uin = struct.unpack_from(">Q", body, 2)[0]
    query_content, off = read_lp_string(body, 10, 64)
    return {
        "query_type": query_type,
        "query_uin": query_uin,
        "query_content": query_content,
        "tail": body[off:],
    }


def build_query_friend_response(
    player: dict[str, Any] | None,
    *,
    result: int | None = None,
    privacy_flags: int = EPTE_ADD_BY_ALL,
) -> bytes:
    """Build A310 in the v165 wire form.

    Successful layout:
      u16 Result | u64 FriendUin | LP FriendNickName |
      u32 FriendExp | u16 FriendPrivacyFlags

    v165 keeps the response form that reached the stock QueryFriend UI
    callback and changes only a resolved player's privacy policy to
    EPTE_AddByAll (0x0001).
    """
    if player is None:
        result = SNS_ERR_NOT_FOUND if result is None else int(result)
        uin = 0
        nickname = ""
        experience = 0
        privacy_flags = EPTE_ADD_BY_NONE
    else:
        result = QUERY_FRIEND_ERR_SUCC if result is None else int(result)
        uin = int(player.get("uin") or 0)
        nickname = str(player.get("nickname") or "")
        experience = max(0, int(player.get("experience") or 0))

    body = (
        _u16(result)
        + _u64(uin)
        + pack_lp_string(nickname, 32)
        + _u32(experience)
        + _u16(privacy_flags)
    )
    return build_server_app(CMD_RES_QUERY_FRIEND, body)


def parse_chat_p2p_request(body: bytes) -> dict[str, Any]:
    body = bytes(body)
    if len(body) < 10:
        raise FriendsProtocolError('truncated A405')
    chat_type, recipient_uin = struct.unpack_from('>HQ', body)
    if chat_type != 0x0100:
        raise FriendsProtocolError('unsupported private chat type')
    recipient_name, off = read_lp_string(body, 10, 32)
    message, off = read_lp_string(body, off, 128)
    if off + 1 != len(body) or body[off] != 0:
        raise FriendsProtocolError('only ordinary text chat is supported')
    return dict(chat_type=chat_type, recipient_uin=recipient_uin,
                uin_to=str(recipient_uin), recipient_name=recipient_name,
                message=message, pkg_count=0, tail=b'')


def build_chat_p2p_notify(chat_type: int, message: str, *,
                          sender_uin: int, sender_name: str,
                          sender_in_match: bool = False) -> bytes:
    # PH TDR: type:u16, sender:u64, two strings, count:i8, ReqInMatch:u8.
    body = (_u16(chat_type) + _u64(sender_uin)
            + pack_lp_string(sender_name, 32) + pack_lp_string(message, 128)
            + _u8(0) + _u8(int(bool(sender_in_match))))
    return build_server_app(CMD_NTF_CHAT_P2P, body)


class FriendsService:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS friends (
                    player_uin INTEGER NOT NULL,
                    friend_uin INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (player_uin, friend_uin),
                    CHECK (player_uin <> friend_uin)
                );

                CREATE INDEX IF NOT EXISTS idx_friends_friend
                    ON friends(friend_uin, player_uin);

                CREATE TABLE IF NOT EXISTS friend_requests (
                    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_uin INTEGER NOT NULL,
                    to_uin INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    remark TEXT NOT NULL DEFAULT '',
                    msg_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    CHECK (from_uin <> to_uin)
                );

                CREATE INDEX IF NOT EXISTS idx_friend_requests_to_status
                    ON friend_requests(to_uin, status, request_id);
                CREATE INDEX IF NOT EXISTS idx_friend_requests_pair_status
                    ON friend_requests(from_uin, to_uin, status, request_id);

                CREATE TABLE IF NOT EXISTS private_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_uin INTEGER NOT NULL,
                    recipient_uin INTEGER NOT NULL,
                    chat_type INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    delivered_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_private_messages_pending
                    ON private_messages(recipient_uin, delivered_at, message_id);
                """
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_identity(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        nickname = str(row["nickname"] or "").strip()
        if not nickname:
            return None
        keys = set(row.keys())
        experience = int(row["experience"] or 0) if "experience" in keys else 0
        return {
            "uin": int(row["uin"]),
            "nickname": nickname,
            "experience": experience,
        }

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(table),),
        ).fetchone() is not None

    def _identity_for_uin_conn(
        self, conn: sqlite3.Connection, uin: int
    ) -> dict[str, Any] | None:
        uin = int(uin)
        if self._table_exists(conn, "player_profiles"):
            row = conn.execute(
                "SELECT uin, nickname, experience FROM player_profiles WHERE uin = ?",
                (uin,),
            ).fetchone()
            found = self._row_to_identity(row)
            if found is not None:
                found["source"] = "player_profiles"
                return found
        if self._table_exists(conn, "profiles"):
            row = conn.execute(
                "SELECT uin, nickname FROM profiles WHERE uin = ?",
                (uin,),
            ).fetchone()
            found = self._row_to_identity(row)
            if found is not None:
                found["source"] = "profiles"
                return found
        return None

    def get_player_identity(self, uin: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            return self._identity_for_uin_conn(conn, int(uin))
        finally:
            conn.close()

    def find_player(self, *, uin: int = 0, nickname: str = "") -> dict[str, Any] | None:
        uin = int(uin or 0)
        nickname = str(nickname or "").strip()
        conn = self._connect()
        try:
            if uin:
                found = self._identity_for_uin_conn(conn, uin)
                if found is not None:
                    return found

            if nickname:
                key = nickname.casefold()
                seen_uins: set[int] = set()

                # Match the website's authority order: projected game profile first.
                if self._table_exists(conn, "player_profiles"):
                    rows = conn.execute(
                        "SELECT uin, nickname, experience FROM player_profiles "
                        "WHERE nickname IS NOT NULL AND nickname <> ''"
                    ).fetchall()
                    for row in rows:
                        row_uin = int(row["uin"])
                        seen_uins.add(row_uin)
                        if str(row["nickname"] or "").casefold() == key:
                            found = self._row_to_identity(row)
                            if found is not None:
                                found["source"] = "player_profiles"
                                return found

                # Registered accounts may have a nickname in profiles before a
                # player_profiles projection exists. Do not let an empty projected
                # row hide the authoritative registration/profile nickname.
                if self._table_exists(conn, "profiles"):
                    rows = conn.execute(
                        "SELECT uin, nickname FROM profiles "
                        "WHERE nickname IS NOT NULL AND nickname <> ''"
                    ).fetchall()
                    for row in rows:
                        if str(row["nickname"] or "").casefold() != key:
                            continue
                        found = self._row_to_identity(row)
                        if found is None:
                            continue
                        projected = self._identity_for_uin_conn(conn, int(found["uin"]))
                        return projected or found
            return None
        finally:
            conn.close()

    def list_friends(self, player_uin: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT friend_uin, created_at FROM friends WHERE player_uin = ?",
                (int(player_uin),),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                identity = self._identity_for_uin_conn(conn, int(row["friend_uin"]))
                if identity is None:
                    continue
                item = dict(identity)
                item["created_at"] = str(row["created_at"])
                out.append(item)
            out.sort(key=lambda item: (str(item["nickname"]).casefold(), int(item["uin"])))
            return out
        finally:
            conn.close()

    def are_friends(self, a: int, b: int) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM friends WHERE player_uin=? AND friend_uin=?",
                (int(a), int(b)),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def create_friend_request(self, from_uin: int, to_uin: int, remark: str = "") -> dict[str, Any]:
        from_uin = int(from_uin)
        to_uin = int(to_uin)
        if from_uin == to_uin:
            raise FriendsError("cannot add self as friend")
        now = _utc_now()
        remark = str(remark or "")[:255]
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM friends WHERE player_uin=? AND friend_uin=?",
                (from_uin, to_uin),
            ).fetchone():
                conn.commit()
                return {
                    "already_friends": True,
                    "duplicate": False,
                    "opposite_pending": False,
                    "from_uin": from_uin,
                    "to_uin": to_uin,
                    "request_id": 0,
                    "msg_id": 0,
                    "remark": remark,
                }

            same = conn.execute(
                """SELECT * FROM friend_requests
                   WHERE from_uin=? AND to_uin=? AND status='pending'
                   ORDER BY request_id DESC LIMIT 1""",
                (from_uin, to_uin),
            ).fetchone()
            if same is not None:
                conn.commit()
                out = dict(same)
                out.update({"already_friends": False, "duplicate": True, "opposite_pending": False})
                return out

            opposite = conn.execute(
                """SELECT * FROM friend_requests
                   WHERE from_uin=? AND to_uin=? AND status='pending'
                   ORDER BY request_id DESC LIMIT 1""",
                (to_uin, from_uin),
            ).fetchone()
            if opposite is not None:
                conn.commit()
                out = dict(opposite)
                out.update({"already_friends": False, "duplicate": False, "opposite_pending": True})
                return out

            cur = conn.execute(
                """INSERT INTO friend_requests(
                       from_uin,to_uin,status,remark,msg_id,created_at,resolved_at
                   ) VALUES(?,?,'pending',?,0,?,NULL)""",
                (from_uin, to_uin, remark, now),
            )
            request_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE friend_requests SET msg_id=? WHERE request_id=?",
                (request_id, request_id),
            )
            conn.commit()
            return {
                "request_id": request_id,
                "from_uin": from_uin,
                "to_uin": to_uin,
                "status": "pending",
                "remark": remark,
                "msg_id": request_id,
                "created_at": now,
                "resolved_at": None,
                "already_friends": False,
                "duplicate": False,
                "opposite_pending": False,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_pending_friend_requests(self, to_uin: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM friend_requests
                WHERE to_uin=? AND status='pending'
                ORDER BY request_id ASC
                """,
                (int(to_uin),),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                ident = self._identity_for_uin_conn(conn, int(item["from_uin"]))
                item["from_nickname"] = str(ident["nickname"]) if ident else ""
                out.append(item)
            return out
        finally:
            conn.close()

    def resolve_friend_request(self, request_id: int, to_uin: int, accepted: bool) -> dict[str, Any]:
        request_id = int(request_id)
        to_uin = int(to_uin)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM friend_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise FriendsError(f"friend request not found: {request_id}")
            if int(row["to_uin"]) != to_uin:
                raise FriendsError(
                    f"friend request {request_id} is not addressed to uin={to_uin}"
                )
            duplicate = str(row["status"]) != "pending"
            if not duplicate:
                status = "accepted" if accepted else "rejected"
                now = _utc_now()
                conn.execute(
                    "UPDATE friend_requests SET status=?, resolved_at=? WHERE request_id=?",
                    (status, now, request_id),
                )
                if accepted:
                    a = int(row["from_uin"])
                    b = int(row["to_uin"])
                    conn.execute(
                        "INSERT OR IGNORE INTO friends(player_uin,friend_uin,created_at) VALUES(?,?,?)",
                        (a, b, now),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO friends(player_uin,friend_uin,created_at) VALUES(?,?,?)",
                        (b, a, now),
                    )
            conn.commit()
            out = dict(row)
            out["status"] = "accepted" if accepted else "rejected"
            out["duplicate"] = duplicate
            return out
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete_friendship(self, a: int, b: int) -> bool:
        a = int(a)
        b = int(b)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            before = conn.total_changes
            conn.execute(
                "DELETE FROM friends WHERE (player_uin=? AND friend_uin=?) "
                "OR (player_uin=? AND friend_uin=?)",
                (a, b, b, a),
            )
            existed = conn.total_changes > before
            conn.commit()
            return bool(existed)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def store_private_message(
        self,
        sender_uin: int,
        recipient_uin: int,
        chat_type: int,
        message: str,
    ) -> dict[str, Any]:
        sender_uin = int(sender_uin)
        recipient_uin = int(recipient_uin)
        if not self.are_friends(sender_uin, recipient_uin):
            raise FriendsError(
                "private messages are only allowed between persisted friends"
            )
        message = str(message or "")[:127]
        now = _utc_now()
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO private_messages(
                    sender_uin,recipient_uin,chat_type,message,sent_at,delivered_at
                ) VALUES(?,?,?,?,?,NULL)
                """,
                (sender_uin, recipient_uin, int(chat_type) & 0xFFFF, message, now),
            )
            conn.commit()
            return {
                "message_id": int(cur.lastrowid),
                "sender_uin": sender_uin,
                "recipient_uin": recipient_uin,
                "chat_type": int(chat_type) & 0xFFFF,
                "message": message,
                "sent_at": now,
                "delivered_at": None,
            }
        finally:
            conn.close()

    def list_pending_private_messages(
        self,
        recipient_uin: int,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT message_id, sender_uin, recipient_uin, chat_type,
                       message, sent_at, delivered_at
                FROM private_messages
                WHERE recipient_uin=? AND delivered_at IS NULL
                ORDER BY message_id ASC
                LIMIT ?
                """,
                (int(recipient_uin), limit),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                ident = self._identity_for_uin_conn(conn, int(item["sender_uin"]))
                item["sender_nickname"] = str(ident["nickname"]) if ident else ""
                out.append(item)
            return out
        finally:
            conn.close()

    def mark_private_message_delivered(self, message_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE private_messages SET delivered_at=COALESCE(delivered_at, ?) "
                "WHERE message_id=?",
                (_utc_now(), int(message_id)),
            )
            conn.commit()
        finally:
            conn.close()

