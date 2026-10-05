# Dynamic match nicknames

The PH client sends `Name=Player1` in its UE3 NMT_Login URL even when the lobby
already displays the account nickname. The DS UDP bridge now replaces only that
Name option with the persisted `player_profiles.nickname` for the same login UIN,
before AFDEV creates the player. This shared path applies across controller classes
and game modes, including late joins. Each player is looked up independently.

The database is opened read-only. `AF_ACCOUNT_DB` overrides the default
`server/assaultfire_accounts.sqlite3`. Missing profiles, malformed packets and
ambiguous login identities pass through unchanged. Reliable packet IDs, channel
metadata, ACKs, other URL options and opaque payload bits are preserved.
Retransmitted Login packets keep the same rewritten bytes. A reconnect refreshes
the nickname from SQLite. A profile rename during a match takes effect at the next
join/reconnect, rather than immediately updating an existing PRI.

The bridge sets `AF_DYNAMIC_LOGIN_NAMES=1` for its loader child. That disables the
legacy owner-only, exact-PvE-controller memory rename, which otherwise times out
in Mutation modes or risks assigning the owner's nickname to another player.
Direct loader runs without this environment flag retain the legacy fallback.

Restart the server and recreate/rejoin matches after pulling this change.
Look for `[LOGIN-PATCH] peer=... uin=... Name 'Player1' -> 'account nickname'`.

Validation covers separate UINs, Unicode names, late joins, retransmission,
reconnect refresh, packet metadata and bit preservation, read-only database access,
malformed input and a running two-peer UDP relay. Windows in-game validation of
this revision is still pending; packet tests do not prove every HUD rendering path.
