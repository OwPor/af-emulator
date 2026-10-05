"""SQLite-backed thread-safe match-room registry for the stable v143b backend.

Room membership and settings are stored in the account database. Network
connections, online sessions, and dedicated-server processes remain transient.
"""
from __future__ import annotations

import base64
import copy
import functools
import json
import os
import sqlite3
import threading
import time
from pathlib import Path


_BYTES_MARKER = "__af_lobby_bytes_v1__"


def _json_default(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_BYTES_MARKER: base64.b64encode(bytes(value)).decode("ascii")}
    raise TypeError(f"unsupported lobby value: {type(value).__name__}")


def _json_object_hook(value):
    if set(value) == {_BYTES_MARKER}:
        try:
            return base64.b64decode(value[_BYTES_MARKER], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid byte value in persisted lobby state") from exc
    return value


def _database_operation(*, write=False):
    """Reload under a SQLite transaction so separate instances share state."""
    def decorate(method):
        @functools.wraps(method)
        def wrapped(self, *args, **kwargs):
            with self._lock:
                connection = self._connection
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    self._load_state_locked()
                    previous_rooms = copy.deepcopy(self._rooms) if write else None
                    result = method(self, *args, **kwargs)
                    if write:
                        self._save_state_locked(previous_rooms)
                    connection.commit()
                    return result
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    self._rooms = {}
                    self._player_room = {}
                    try:
                        self._load_state_locked()
                    except Exception:
                        self._rooms = {}
                        self._player_room = {}
                    raise
        return wrapped
    return decorate

PLAYER_STATE_UNREADY = 8
PLAYER_STATE_READY = 9
PLAYER_FLAG_ROOM_OWNER = 4


class RoomRegistryError(ValueError):
    """Raised when a room operation cannot be completed safely."""


class RoomRegistry:
    def __init__(self, db_path: str | os.PathLike[str] | None = None):
        self._lock = threading.RLock()
        self._rooms: dict[int, dict] = {}
        self._player_room: dict[int, int] = {}
        self.db_path = str(db_path) if db_path is not None else ":memory:"
        if self.db_path != ":memory:" and not self.db_path.startswith("file:"):
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            check_same_thread=False,
            uri=self.db_path.startswith("file:"),
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.db_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self):
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS af_lobby_rooms (
                room_id INTEGER PRIMARY KEY,
                room_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS af_lobby_members (
                room_id INTEGER NOT NULL
                    REFERENCES af_lobby_rooms(room_id) ON DELETE CASCADE,
                uin INTEGER NOT NULL UNIQUE,
                seat_index INTEGER NOT NULL,
                member_json TEXT NOT NULL,
                PRIMARY KEY (room_id, uin),
                UNIQUE (room_id, seat_index)
            );
            """
        )
        self._connection.commit()

    def _load_state_locked(self):
        self._rooms = {}
        self._player_room = {}
        for row in self._connection.execute(
            "SELECT room_id, room_json FROM af_lobby_rooms ORDER BY room_id"
        ):
            room_id = int(row["room_id"])
            room = json.loads(row["room_json"], object_hook=_json_object_hook)
            room["room_id"] = room_id
            room["members"] = {}
            self._rooms[room_id] = room

        for row in self._connection.execute(
            """SELECT room_id, uin, seat_index, member_json
               FROM af_lobby_members ORDER BY room_id, uin"""
        ):
            room_id, uin = int(row["room_id"]), int(row["uin"])
            room = self._rooms.get(room_id)
            if room is None:
                raise RoomRegistryError(f"lobby-member-room-missing:{room_id}")
            member = json.loads(row["member_json"], object_hook=_json_object_hook)
            if int(member.get("seat_index", -1)) != int(row["seat_index"]):
                raise RoomRegistryError(f"lobby-member-seat-mismatch:{room_id}:{uin}")
            room["members"][uin] = member
            self._player_room[uin] = room_id

    def _save_state_locked(self, previous_rooms):
        connection = self._connection
        for room_id in previous_rooms.keys() - self._rooms.keys():
            connection.execute(
                "DELETE FROM af_lobby_rooms WHERE room_id = ?",
                (int(room_id),),
            )
        for room_id, room in self._rooms.items():
            if previous_rooms.get(room_id) == room:
                continue
            room_data = {key: value for key, value in room.items() if key != "members"}
            room_json = json.dumps(
                room_data,
                ensure_ascii=False,
                separators=(",", ":"),
                default=_json_default,
            )
            connection.execute(
                """INSERT INTO af_lobby_rooms(room_id, room_json) VALUES(?, ?)
                   ON CONFLICT(room_id) DO UPDATE SET room_json=excluded.room_json""",
                (int(room_id), room_json),
            )
            connection.execute(
                "DELETE FROM af_lobby_members WHERE room_id = ?",
                (int(room_id),),
            )
            for uin, member in room.get("members", {}).items():
                member_json = json.dumps(
                    member,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=_json_default,
                )
                connection.execute(
                    """INSERT INTO af_lobby_members(
                           room_id, uin, seat_index, member_json
                       ) VALUES(?, ?, ?, ?)""",
                    (
                        int(room_id),
                        int(uin),
                        int(member["seat_index"]),
                        member_json,
                    ),
                )

    def close(self):
        """Close this registry's SQLite connection."""
        with self._lock:
            self._connection.close()

    @staticmethod
    def _member(uin: int, nickname: str, seat_index: int, *, owner=False, observer=False) -> dict:
        return {
            "uin": int(uin),
            "nickname": str(nickname or f"Player{int(uin)}")[:31],
            "seat_index": int(seat_index),
            "state": PLAYER_STATE_UNREADY,
            "ready": False,
            "flags": PLAYER_FLAG_ROOM_OWNER if owner else 0,
            "spec_equip_flags": 0,
            "camp": 1,
            "observer": bool(observer),
            "joined_at": time.time(),
        }

    @staticmethod
    def _snapshot(room: dict | None) -> dict | None:
        if room is None:
            return None
        members = [copy.deepcopy(m) for m in room["members"].values()]
        members.sort(key=lambda m: (int(m["seat_index"]), int(m["uin"])))
        fighter_count = sum(1 for m in members if not m.get("observer"))
        observer_count = sum(1 for m in members if m.get("observer"))
        owner = room["members"].get(int(room["owner_uin"]))
        out = {k: copy.deepcopy(v) for k, v in room.items() if k != "members"}
        out.update(
            {
                "room_id": int(room["room_id"]),
                "display_id": int(room.get("display_id", int(room["room_id"]) & 0xFFFF or 1)),
                "qqtalk_room_id": int(room.get("qqtalk_room_id", 0)),
                "sub_channel_id": int(room.get("sub_channel_id", 1)),
                "name": str(room.get("name") or f"Room {int(room['room_id'])}"),
                "owner_uin": int(room["owner_uin"]),
                "owner_name": str(owner["nickname"] if owner else room.get("owner_name", "")),
                "match_settings_wire": bytes(room.get("match_settings_wire", b"")),
                "mode_id": int(room.get("mode_id", 0)),
                "map_id": int(room.get("map_id", 0)),
                "sub_mode_id": int(room.get("sub_mode_id", 0)),
                "flags": int(room.get("flags", 0)),
                "fighter_capacity": int(room.get("fighter_capacity", 4)),
                "observer_capacity": int(room.get("observer_capacity", 0)),
                "fighter_count": fighter_count,
                "observer_count": observer_count,
                "password": str(room.get("password", "")),
                "members": members,
                "started": bool(room.get("started", False)),
                "no_late_join": bool(room.get("no_late_join", False)),
                "created_at": float(room.get("created_at", 0.0)),
            }
        )
        return out

    @staticmethod
    def _seat_ranges(room: dict, *, observer: bool):
        fighters = max(1, int(room.get("fighter_capacity", 4) or 4))
        observers = max(0, int(room.get("observer_capacity", 0) or 0))
        return range(fighters, fighters + observers) if observer else range(0, fighters)

    def _next_free_seat_locked(self, room: dict, *, observer: bool) -> int | None:
        used = {int(m["seat_index"]) for m in room["members"].values()}
        for seat in self._seat_ranges(room, observer=observer):
            if seat not in used:
                return seat
        return None

    @_database_operation()
    def get_room(self, room_id: int) -> dict | None:
        with self._lock:
            return self._snapshot(self._rooms.get(int(room_id)))

    @_database_operation()
    def room_for_player(self, uin: int) -> dict | None:
        with self._lock:
            room_id = self._player_room.get(int(uin))
            return self._snapshot(self._rooms.get(room_id)) if room_id is not None else None

    @_database_operation()
    def require_owner(self, room_id: int, uin: int) -> dict:
        room_id, uin = int(room_id), int(uin)
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                raise RoomRegistryError("room-not-found")
            if uin not in room["members"]:
                raise RoomRegistryError("not-in-room")
            if int(room["owner_uin"]) != uin:
                raise RoomRegistryError(
                    f"not-room-owner:owner={int(room['owner_uin'])}:requester={uin}"
                )
            return self._snapshot(room)

    @_database_operation()
    def list_rooms(self, include_started: bool = False) -> list[dict]:
        with self._lock:
            rooms = [
                self._snapshot(room)
                for room in self._rooms.values()
                if include_started or not room.get("started")
            ]
        rooms.sort(key=lambda r: (int(r.get("display_id", 0)), int(r["room_id"])))
        return rooms

    @_database_operation(write=True)
    def create_room(self, room_data: dict, *, owner_uin: int, owner_name: str) -> dict:
        owner_uin = int(owner_uin)
        room_id = int(room_data["room_id"])
        with self._lock:
            if room_id in self._rooms:
                raise RoomRegistryError(f"room-already-exists:{room_id}")
            if owner_uin in self._player_room:
                raise RoomRegistryError(f"player-already-in-room:{owner_uin}")
            room = copy.deepcopy(dict(room_data))
            room["room_id"] = room_id
            room["display_id"] = int(room.get("display_id", room_id & 0xFFFF or 1))
            room["owner_uin"] = owner_uin
            room["owner_name"] = str(owner_name or f"Player{owner_uin}")[:31]
            room["fighter_capacity"] = max(1, int(room.get("fighter_capacity", 4) or 4))
            room["observer_capacity"] = max(0, int(room.get("observer_capacity", 0) or 0))
            room["password"] = str(room.get("password") or "")[:7]
            room["started"] = False
            room["no_late_join"] = bool(room.get("no_late_join", False))
            room["created_at"] = float(room.get("created_at") or time.time())
            room["members"] = {}
            room["join_seq"] = 1
            owner_member = self._member(
                owner_uin, owner_name, 0, owner=True, observer=False
            )
            owner_member["join_seq"] = 1
            room["members"][owner_uin] = owner_member
            self._rooms[room_id] = room
            self._player_room[owner_uin] = room_id
            return self._snapshot(room)

    @_database_operation(write=True)
    def join_room(self, *, uin: int, nickname: str, room_id: int, password="", observer=False):
        uin, room_id = int(uin), int(room_id)
        observer = bool(observer)
        with self._lock:
            current = self._player_room.get(uin)
            if current == room_id:
                room = self._rooms.get(room_id)
                if room is None or uin not in room["members"]:
                    raise RoomRegistryError("room-membership-corrupt")
                member = copy.deepcopy(room["members"][uin])
                existing = sorted(int(x) for x in room["members"] if int(x) != uin)
                return self._snapshot(room), member, existing
            if current is not None:
                raise RoomRegistryError(f"player-already-in-room:{current}")
            room = self._rooms.get(room_id)
            if room is None:
                raise RoomRegistryError("room-not-found")

            if room.get("started") and room.get("no_late_join"):
                raise RoomRegistryError("room-no-late-join")
            expected = str(room.get("password") or "")
            if expected and str(password or "") != expected:
                raise RoomRegistryError("bad-room-password")
            seat = self._next_free_seat_locked(room, observer=observer)
            if seat is None:
                raise RoomRegistryError("room-full")
            existing = sorted(int(x) for x in room["members"])
            member = self._member(uin, nickname, seat, owner=False, observer=observer)
            if "join_seq" in room and isinstance(room["join_seq"], int):
                room["join_seq"] = int(room["join_seq"]) + 1
            else:
                room["join_seq"] = max(
                    (
                        int(m.get("join_seq", 0) or 0)
                        for m in room["members"].values()
                    ),
                    default=0,
                ) or len(room["members"])
                room["join_seq"] += 1
            member["join_seq"] = int(room["join_seq"])
            room["members"][uin] = member
            self._player_room[uin] = room_id
            return self._snapshot(room), copy.deepcopy(member), existing

    @_database_operation(write=True)
    def rollback_join(self, uin: int, room_id: int) -> bool:
        """Undo only a newly inserted non-owner join after downstream failure."""
        uin, room_id = int(uin), int(room_id)
        with self._lock:
            if self._player_room.get(uin) != room_id:
                return False
            room = self._rooms.get(room_id)
            if room is None:
                self._player_room.pop(uin, None)
                return False
            if int(room.get("owner_uin", 0)) == uin:
                raise RoomRegistryError("refusing-to-rollback-room-owner")
            if uin not in room["members"]:
                self._player_room.pop(uin, None)
                return False
            room["members"].pop(uin, None)
            self._player_room.pop(uin, None)
            return True

    @_database_operation(write=True)
    def leave_room(self, uin: int) -> dict | None:
        uin = int(uin)
        with self._lock:
            room_id = self._player_room.pop(uin, None)
            if room_id is None:
                return None
            room = self._rooms.get(room_id)
            if room is None:
                return None
            removed = room["members"].pop(uin, None)
            if removed is None:
                return None
            old_owner = int(room["owner_uin"])
            deleted = not room["members"]
            new_owner = None
            snapshot = None
            if deleted:
                self._rooms.pop(room_id, None)
            else:
                if old_owner == uin:
                    new_owner_member = min(
                        room["members"].values(),
                        key=lambda m: (
                            int(m.get("join_seq", 10**12) or 10**12),
                            float(m.get("joined_at", 0.0) or 0.0),
                            int(m["seat_index"]),
                            int(m["uin"]),
                        ),
                    )
                    new_owner = int(new_owner_member["uin"])
                    room["owner_uin"] = new_owner
                    for member in room["members"].values():
                        member["flags"] = int(member.get("flags", 0)) & ~PLAYER_FLAG_ROOM_OWNER
                    new_owner_member["flags"] |= PLAYER_FLAG_ROOM_OWNER
                snapshot = self._snapshot(room)
            return {
                "room_id": int(room_id),
                "removed": copy.deepcopy(removed),
                "old_owner_uin": old_owner,
                "new_owner_uin": new_owner,
                "deleted": deleted,
                "room": snapshot,
            }

    @_database_operation(write=True)
    def set_ready(self, uin: int, ready: bool) -> dict:
        uin = int(uin)
        with self._lock:
            room_id = self._player_room.get(uin)
            room = self._rooms.get(room_id) if room_id is not None else None
            if room is None or uin not in room["members"]:
                raise RoomRegistryError("not-in-room")
            member = room["members"][uin]
            member["ready"] = bool(ready)
            member["state"] = PLAYER_STATE_READY if ready else PLAYER_STATE_UNREADY
            return self._snapshot(room)

    @_database_operation(write=True)
    def set_player_state(self, uin: int, state: int, *, ready=None) -> dict:
        """Set one member's authoritative MatchRoomPlayerInfo.State."""
        uin = int(uin)
        with self._lock:
            room_id = self._player_room.get(uin)
            room = self._rooms.get(room_id) if room_id is not None else None
            if room is None or uin not in room["members"]:
                raise RoomRegistryError("not-in-room")
            member = room["members"][uin]
            member["state"] = int(state)
            if ready is not None:
                member["ready"] = bool(ready)
            return self._snapshot(room)

    @_database_operation(write=True)
    def reset_round_state(self, room_id: int) -> dict:
        """Return a surviving logical room to the stock pre-round state.

        A player can leave UE3 gameplay and return to the same room without
        leaving/rejoining the room itself. In that path the previous round's
        started/ready flags must not leak into the next Ready/Start cycle.
        """
        room_id = int(room_id)
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                raise RoomRegistryError("room-not-found")
            room["started"] = False
            for member in room["members"].values():
                member["ready"] = False
                member["state"] = PLAYER_STATE_UNREADY
            return self._snapshot(room)

    @_database_operation(write=True)
    def move_member(self, uin: int, new_seat: int, camp: int):
        uin, new_seat = int(uin), int(new_seat)
        with self._lock:
            room_id = self._player_room.get(uin)
            room = self._rooms.get(room_id) if room_id is not None else None
            if room is None or uin not in room["members"]:
                raise RoomRegistryError("not-in-room")
            member = room["members"][uin]
            if member.get("observer"):
                allowed = set(self._seat_ranges(room, observer=True))
                if new_seat not in allowed:
                    raise RoomRegistryError(f"seat-out-of-range:{new_seat}")
            elif new_seat < 0 or new_seat >= 32:
                # r20: stock PH fighter seats use sparse 32-entry IDs.
                # FighterCapacity limits population, not numeric seat-id range.
                raise RoomRegistryError(f"seat-out-of-range:{new_seat}")
            for other_uin, other in room["members"].items():
                if int(other_uin) != uin and int(other["seat_index"]) == new_seat:
                    raise RoomRegistryError(f"seat-occupied:{new_seat}")
            old_seat = int(member["seat_index"])
            member["seat_index"] = new_seat
            member["camp"] = int(camp) & 0xFF
            return self._snapshot(room), copy.deepcopy(member), old_seat

    @_database_operation(write=True)
    def set_started(self, room_id: int, started: bool) -> dict:
        with self._lock:
            room = self._rooms.get(int(room_id))
            if room is None:
                raise RoomRegistryError("room-not-found")
            room["started"] = bool(started)
            return self._snapshot(room)

    @_database_operation(write=True)
    def update_settings(
        self,
        room_id: int,
        *,
        match_settings_wire: bytes,
        mode_id: int,
        map_id: int,
        map_string: str,
        sub_mode_id: int,
        flags: int,
        setting_type: int = 0,
        value: int = 0,
        respawn_time: int = 0,
        recode_type: int = 0,
        live_delay_sec: int = 0,
        no_late_join=None,
    ) -> dict:
        """Replace the room's authoritative MatchSettings after A11E."""
        with self._lock:
            room = self._rooms.get(int(room_id))
            if room is None:
                raise RoomRegistryError("room-not-found")
            room.update(
                {
                    "match_settings_wire": bytes(match_settings_wire),
                    "mode_id": int(mode_id) & 0xFFFFFFFF,
                    "map_id": int(map_id) & 0xFFFF,
                    "map_string": str(map_string or ""),
                    "sub_mode_id": int(sub_mode_id) & 0xFFFFFFFF,
                    "flags": int(flags) & 0xFFFFFFFF,
                    "setting_type": int(setting_type) & 0xFFFF,
                    "value": int(value) & 0xFFFF,
                    "respawn_time": int(respawn_time) & 0xFFFF,
                    "recode_type": int(recode_type) & 0xFFFF,
                    "live_delay_sec": int(live_delay_sec) & 0xFFFF,
                }
            )
            if no_late_join is not None:
                room["no_late_join"] = bool(no_late_join)
            return self._snapshot(room)

    def clear_all(self) -> int:
        """Remove persisted live rooms before a fresh server session starts."""
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                room_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM af_lobby_rooms"
                    ).fetchone()["count"]
                )
                connection.execute("DELETE FROM af_lobby_rooms")
                connection.commit()
                self._rooms.clear()
                self._player_room.clear()
                return room_count
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    @_database_operation(write=True)
    def recover_after_restart(
        self,
        *,
        retain_uins: set[int] | None = None,
    ) -> dict[str, int]:
        """Keep resumable waiting rooms and discard rooms that cannot resume.

        Active matches depend on transient UE3 dedicated-server processes, so
        they are removed. Waiting rooms survive only for players whose
        persisted transport session has not expired. Ready state and DS
        allocation metadata are cleared because those are process-local.
        """
        retained = (
            None if retain_uins is None
            else {int(uin) for uin in retain_uins if int(uin) > 0}
        )
        recovered_rooms = 0
        removed_rooms = 0
        removed_members = 0
        with self._lock:
            for room_id, room in list(self._rooms.items()):
                # A configured DS spawner does not make a waiting room an
                # active match. Its process-local reservation is cleared below;
                # the room itself can still be restored for the player to retry.
                if bool(room.get("started")):
                    for uin in list((room.get("members") or {}).keys()):
                        self._player_room.pop(int(uin), None)
                    self._rooms.pop(room_id, None)
                    removed_rooms += 1
                    continue

                members = room.get("members") or {}
                for raw_uin in list(members):
                    uin = int(raw_uin)
                    if retained is not None and uin not in retained:
                        members.pop(raw_uin, None)
                        self._player_room.pop(uin, None)
                        removed_members += 1

                if not members:
                    self._rooms.pop(room_id, None)
                    removed_rooms += 1
                    continue

                owner_uin = int(room.get("owner_uin", 0))
                if owner_uin not in members:
                    new_owner = min(
                        members.values(),
                        key=lambda member: (
                            int(member.get("join_seq", 10**12) or 10**12),
                            float(member.get("joined_at", 0.0) or 0.0),
                            int(member.get("seat_index", 0)),
                            int(member.get("uin", 0)),
                        ),
                    )
                    owner_uin = int(new_owner["uin"])
                    room["owner_uin"] = owner_uin
                for uin, member in members.items():
                    member["ready"] = False
                    member["state"] = PLAYER_STATE_UNREADY
                    flags = int(member.get("flags", 0)) & ~PLAYER_FLAG_ROOM_OWNER
                    if int(uin) == owner_uin:
                        flags |= PLAYER_FLAG_ROOM_OWNER
                    member["flags"] = flags

                room["started"] = False
                room.pop("ds_slot", None)
                room.pop("ds_public_port", None)
                room.pop("ds_endpoint", None)
                recovered_rooms += 1

        return {
            "recovered_rooms": recovered_rooms,
            "removed_rooms": removed_rooms,
            "removed_members": removed_members,
        }
