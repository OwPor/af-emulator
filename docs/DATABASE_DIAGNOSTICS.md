# Bounded database diagnostics

Server, DS spawner, bridge, loader and actor diagnostic output is captured in the
account database selected by `AF_ACCOUNT_DB` (normally
`server/assaultfire_accounts.sqlite3`). Console logging remains available.

Production database capture keeps warnings, errors and explicit match lifecycle
events (room reserve/settings, startup, readiness, relay live, player join/leave,
round end and teardown). Routine packet dumps, heartbeats, actor payloads and
loader scanning/progress are filtered before SQL writes. The main server also
filters before queueing. Unstructured child errors are recognized from severity
tags/failure text; Python traceback continuations are retained. Lifecycle snapshots
only refresh when selected state or membership fields change.


Defaults: 5,000 events total, 500 per diagnostic session, 4,096 characters per
message, 1,000 match snapshots, and seven days of retention. Pruning runs on
writes and initialization. Deleted SQLite pages can be reused; these limits bound
retained diagnostic data, not the size of accounts, inventories or other tables.
A bounded server logging queue prevents diagnostic output from blocking packet
handling; overflow messages are counted. Failed database writes are reported.

`ds_diagnostic_events` records UTC epoch time, source, room, session and UIN;
`truncated=1` marks shortened messages. `ds_diagnostic_sessions` holds bounded
mode/map/state snapshots. `ds_diagnostic_users` links owners and all observed
room/match participants to each match, including previous members. UIN is the
same stable identity used by `game_identities` and `player_profiles`. Logs with
no known user retain NULL UIN; no user is guessed. Session IDs include a server
run UUID and round generation, so room numbers reused after restart cannot
cross-link different users.

View recent diagnostics for a user from the repo root:

```powershell
py -3.12 .\tools\diagnostics\ds_logs.py --uin 10001 --limit 100
```

PID, readiness and bridge state files still coordinate live processes. The default
DS IPC directory is now an OS temporary directory; match directories are removed
on teardown and the root is removed on orderly shutdown. Explicit
`AF_DS_RUNTIME_DIR` overrides are retained. Unexpected process termination can
leave temporary IPC files. Existing historical runtime folders are not deleted
by the installer. Other runtime files used by preflight/web/launch status are
separate from persistent diagnostics.

Database capture replaces the server logger import and merges bridge capture
changes. It does not replace the whole server or loader source. Backups are made
before applying. Native Windows AFDEV execution still requires live validation.
