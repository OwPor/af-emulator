# In-game translations

Curated English corrections for **Assault Fire PH 1.0.0.24**. This folder contains
translation edits and tools that apply them to your own client files.

The current pass includes 737 distinct localization keys across 10 `.int` files
(combining the earlier English cleanups) and 1,555 text strings across 120 UI
movies. Coverage includes storage, skills, menus, friends, rooms, settings,
quests, clans, player statistics, help screens and match results.

## Install

Close Assault Fire and run from the repository folder:

```powershell
py -3.12 -m pip install cryptography
py -3.12 .\translations\apply_english.py --client-root "D:\AssaultFirePH - Copy"
py -3.12 .\translations\apply_english.py --client-root "D:\AssaultFirePH - Copy" --apply
```

The first installer command previews all changes. The second applies them after
checking all inputs and backing up every affected file. Restart the client.

Localization edits work with stock uploaded PH text and the previously supplied
English Cleanup v1/v2/v3 files. They merge by section, key and occurrence, accept
known original/intermediate wording, and preserve unrelated custom text, extra
mall-name entries, encoding, placeholders and line endings. An unexpected value
at a targeted key is refused so a custom translation is not silently overwritten.

The movie patch accepts the exact reviewed `TGUI_Movies.upk`, the earlier
single-button Renew fix, or the complete UI English Cleanup v3 result. Its
equal-length UTF-8 edits preserve terminators, tag boundaries and package offsets.
The original package SHA-256 is
`f397ff383d7d6e56c5a78d99173d9756f70e8bd67f737051ff6643ae579591fa`.

For a different UI package, use `--localization-only` to apply only `.int` edits.
`--movies-only` selects just the reviewed movie patch. Both modes still default
to a preview; add `--apply` to write files. No emulator restart is required.

## Restore

The installer prints a folder under `AF_Translation_Backups`. Restore that exact
pre-install state with:

```powershell
py -3.12 .\translations\apply_english.py --client-root "D:\AssaultFirePH - Copy" --restore "<printed backup folder>" --apply
```

Restore validates backup hashes and refuses to overwrite client files edited
since installation. A failed installation rolls back files already written.
Backups made by the earlier standalone installers use their own restore tools.

## Report text problems

Choose **Incorrect or untranslated in-game text** on the repository's
[new issue page](https://github.com/armangido/af-emulator/issues/new/choose).
The form covers bad English, untranslated text, missing `?INT?` keys, clipped
labels and installer errors. Give the exact text, menu location, build/patch
version, reproduction steps, expected wording or meaning, and a screenshot.

## Files and remaining work

| File | Purpose |
| --- | --- |
| `apply_english.py` | Preview, installation, backups and exact restoration |
| `ph/en/localization.json` | Reviewed `.int` key edits and accepted prior wording |
| `ph/en/movies.json` | Verified movie hashes and exact text-byte replacements |
| `ph/en/labels.json` | Chinese-to-English label glossary for this pass |

This is a curated pass, not a complete translation of every client string.
Some remaining movie defaults contain sample names, placeholders or long
instructions; others need a larger byte budget. Many defaults are overridden
by `.int` localization at runtime. One compressed CFX movie is untouched.
Font names and assets remain intact. HTML markup is retained, with a translated
phrase placed in its first text span; live screenshots should be checked for
clipping, padding and styling.

All 199 inspected GFx movies retained identical headers and nested tag records
after patching. Installer/restore/idempotence and failure recovery were tested.
Live Windows rendering has not been verified here. Translations change no prices,
item IDs, skill unlocks, gameplay statistics, key bindings or server behavior.

Contributors should preserve placeholders and keep movie replacements at their
original encoded byte length. New `.int` edits should name a specific section,
key and occurrence and include known prior values. Do not add original game
packages to this folder; report screenshots and text are enough for triage.
