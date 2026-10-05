#!/usr/bin/env python3
"""View bounded database logs for one authoritative game UIN."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'server'))
from assaultfire_ds_diagnostics import DiagnosticsStore
p=argparse.ArgumentParser()
p.add_argument('--uin',required=True,type=int)
p.add_argument('--limit',default=100,type=int)
p.add_argument('--db',default=os.environ.get('AF_ACCOUNT_DB',str(Path(__file__).resolve().parents[2]/'server'/'assaultfire_accounts.sqlite3')))
a=p.parse_args()
path=Path(a.db)
if not path.is_file(): p.error(f'database does not exist: {path}')
# Read-only inspection must not initialize/prune diagnostics or create a database.
with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
    db.row_factory=sqlite3.Row
    rows=db.execute('''SELECT e.* FROM ds_diagnostic_events e WHERE e.uin=? OR EXISTS
        (SELECT 1 FROM ds_diagnostic_users u WHERE u.session_id=e.session_id AND u.uin=?)
        ORDER BY e.id DESC LIMIT ?''',(a.uin,a.uin,max(1,min(500,a.limit)))).fetchall()
    for row in reversed(rows):print(json.dumps(dict(row),ensure_ascii=False))
