# Dedicated server mode and map routing

The spawner accepts every native mode ID in the stock `DefaultGame.ini` catalog.
It bundles the recovered stock map and prefix catalog, and reads readable
`GameModeSettings`, `GameMapSettings`, and `DefaultMapPrefixes` overrides from the
installed game, so the lobby's `ModeId` and `MapId` select the matching UE3
GameInfo class and cooked world. A known mode with an unknown map ID fails closed
when the client also supplies no map name.

The bundled catalog is required because retail configuration files can be opaque
containers. SND ModeId 4097 / MapId 59 resolves to `BM-Maya_4_Main` with
`UTGame.TGBombMatch` even when the installed config cannot be parsed as text.
Catalog routing does not establish that every mode's gameplay has been verified.

| Mode family | Mode ID(s) | Startup settings |
| --- | --- | --- |
| Standard PvP: Bomb, Team, Annihilation, Individual, Relay, Capture Point, Escape, Super Team | 4097–4104 | Native |
| Survival, Defense, Learning, Story, Tower Defense | 8193–8197 | PvE settings for Survival, Defense, and Tower Defense; native for Learning and Story |
| Mech War, Mecha Team, Mecha Doom, Crazy Team | 513, 514, 517, 519 | Native |
| Tank TDM and Tank Siege | 515, 518; lobby alias 523 | Native |
| Power Mode Team, Bomb, Annihilation, Individual | 2049–2052 | Native |
| Clan Team, Bomb, Annihilation, Capture Point | 1025–1028 | Native |
| Mutation and Hero Mutation / Queen | 516, 521 | Native |
| Zombie Scavenge, Zombie Mild, Survival 3 | 257–259 | Native |

The stock config repeats ModeId 8194 for Defense and IF2. The IF2 map prefix
selects its alternate `TGIFGame.TGIF2Game` class. Mech War's map ID 25 is named
`Canyon_Main` in the map table but launches the installed `MHM-Canyon_Main`
world. Existing verified overrides preserve the selected Mutation, Hero
Mutation, Tower Defense, Tank, Survival, and Defense worlds.

The server loader applies difficulty fields only to Survival, Defense, and Tower
Defense. Other catalog modes retain their native GameInfo settings. The mode
catalog routing and loader dispatch are covered by automated tests. Live launch
verification remains limited to the modes previously exercised on the Windows
game client and AFDEV build; each additional family should be smoke-tested there.
Client lobby visibility remains controlled separately by the private client
catalog patch.
