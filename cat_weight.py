#!/usr/bin/env python3
"""cat_weight.py - keep Mr Meow's weigh-ins from the Wyze Scale X on the Pi.

    cat_weight.py               fetch new weigh-ins from every Wyze account with a saved session
    cat_weight.py --print       also print the stored history and the trend
    cat_weight.py --login NAME  log in to one account (mine / owner) and save its session

Whoever weighs him, the reading lands on the Wyze account signed in on that phone, so
this reads every account it has a session for, takes the profiles the scale marks as a
cat, and stores each weigh-in once in riko_state/cat_weight.db. The database is the
history: it keeps readings even if Wyze stops returning old ones. daily_report.py reads it.

Wyze rate-limits logins hard (429 for hours), so this never logs in on its own: it
reuses the saved session and only refreshes its token. A session is made once with
--login, from the environment (.env):
    WYZE_EMAIL, WYZE_PASSWORD, WYZE_KEY_ID, WYZE_API_KEY [, WYZE_TOTP_KEY]          account "mine"
    WYZE_OWNER_EMAIL, ..._PASSWORD, ..._KEY_ID, ..._API_KEY [, ..._TOTP_KEY]        account "owner"
Read-only towards Wyze. Needs wyze-sdk (in .venv-wyze); the storage and trend functions
below do not, so the report and the tests import this module without it.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "riko_state"
DB = STATE / "cat_weight.db"
ACCOUNTS = {"mine": "WYZE_", "owner": "WYZE_OWNER_"}
KG_TO_LB = 2.20462
BACKFILL_DAYS = 3 * 365          # first fetch for a profile reaches back this far
CHUNK_DAYS = 90                  # per request
log = logging.getLogger("cat_weight")


def session_file(account: str) -> Path:
    return STATE / ("wyze_session.json" if account == "mine" else f"wyze_session_{account}.json")


# ------------------------------------------------------------------ pure parts
def cat_profiles(family: list[dict]) -> dict[str, str]:
    """{profile id: nickname} for the profiles the scale marks as a cat."""
    return {m["id"]: m.get("nick_name") or "" for m in family
            if isinstance(m, dict) and m.get("id")
            and (str(m.get("member_property", "")).lower() == "cat")}


def rows_from_records(records: list[dict], account: str, nick: str) -> list[tuple]:
    out = []
    for r in records:
        if r.get("is_delete") or not r.get("weight") or not r.get("measure_ts"):
            continue
        out.append((str(r.get("data_id") or f"{r['family_member_id']}:{r['measure_ts']}"),
                    r["measure_ts"] / 1000, float(r["weight"]), account, r.get("family_member_id") or "", nick))
    return out


def open_db(path: Path = DB, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    path.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS weigh_ins (
        data_id TEXT PRIMARY KEY, ts REAL NOT NULL, kg REAL NOT NULL,
        account TEXT, profile_id TEXT, nickname TEXT, stored REAL)""")
    return db


def store(db: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert new weigh-ins; skip ones already stored (same id, or the same reading
    within a minute, as both accounts could hold the same weigh-in). Returns how many."""
    new = 0
    for data_id, ts, kg, account, pid, nick in rows:
        dup = db.execute("SELECT 1 FROM weigh_ins WHERE data_id = ? OR (abs(ts - ?) < 60 AND abs(kg - ?) < 0.05)",
                         (data_id, ts, kg)).fetchone()
        if dup:
            continue
        db.execute("INSERT INTO weigh_ins VALUES (?,?,?,?,?,?,?)", (data_id, ts, kg, account, pid, nick, time.time()))
        new += 1
    db.commit()
    return new


def lb(kg: float) -> float:
    return round(kg * KG_TO_LB, 1)


def trend(db: sqlite3.Connection, now: float | None = None) -> dict | None:
    """Latest weight and the change over ~2 and ~4 weeks (against the reading nearest
    that far back, if one is within 5 days of it)."""
    now = now or time.time()
    rows = db.execute("SELECT ts, kg FROM weigh_ins WHERE ts <= ? ORDER BY ts", (now,)).fetchall()
    if not rows:
        return None
    ts, kg = rows[-1]
    def change(days: int):
        target = ts - days * 86400
        near = min(rows, key=lambda r: abs(r[0] - target))
        return round(lb(kg) - lb(near[1]), 1) if abs(near[0] - target) <= 5 * 86400 and near[0] < ts else None
    return {"latest_lb": lb(kg), "latest_date": time.strftime("%Y-%m-%d", time.localtime(ts)),
            "days_since": int((now - ts) // 86400),
            "change_2w_lb": change(14), "change_4w_lb": change(28),
            "weigh_ins_30d": sum(1 for r in rows if r[0] > now - 30 * 86400),
            "weigh_ins_total": len(rows),
            "first_date": time.strftime("%Y-%m-%d", time.localtime(rows[0][0]))}


def report_section(path: Path = DB) -> dict | None:
    """What daily_report shows; None if there is no history yet."""
    if not path.exists():
        return None
    db = open_db(path, readonly=True)
    try:
        return trend(db)
    except sqlite3.Error as exc:
        log.warning("cat_weight.db: %r", exc)
        return None
    finally:
        db.close()


# ------------------------------------------------------------------ Wyze
def _save(account: str, c) -> None:
    f = session_file(account)
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps({"access_token": c._token, "refresh_token": c._refresh_token,
                             "user_id": getattr(c, "_user_id", None)}))
    f.chmod(0o600)


def login(account: str) -> None:
    from wyze_sdk import Client
    p = ACCOUNTS[account]
    env = lambda k: os.environ.get(p + k) or None
    if not (env("EMAIL") and env("PASSWORD") and env("KEY_ID") and env("API_KEY")):
        raise SystemExit(f"{p}EMAIL / PASSWORD / KEY_ID / API_KEY are not all set in .env")
    c = Client(email=env("EMAIL"), password=env("PASSWORD"), key_id=env("KEY_ID"),
               api_key=env("API_KEY"), totp_key=env("TOTP_KEY"))
    _save(account, c)
    print(f"{account}: logged in, session saved")


def _client(account: str):
    from wyze_sdk import Client
    t = json.loads(session_file(account).read_text())
    c = Client(token=t["access_token"], refresh_token=t["refresh_token"])
    c._user_id = t.get("user_id")
    return c


def fetch(account: str, db: sqlite3.Connection) -> int:
    """New weigh-ins of the cat profiles on one account; returns how many were stored."""
    from datetime import datetime, timedelta
    from wyze_sdk.service.scale_service import ScaleServiceClient
    c = _client(account)
    try:
        scales = c.scales.list()
    except Exception:                       # expired access token: rotate it once
        c.refresh_token()
        scales = c.scales.list()
    _save(account, c)                       # the refresh token may have rotated
    svc = ScaleServiceClient(token=c._token)
    new = 0
    for s in scales:
        if not s.product.model.startswith("WL_SC"):
            continue
        family = c.scales._scale_client(s.product.model).get_family_member(did=s.mac).data.get("data") or []
        for pid, nick in cat_profiles(family).items():
            last = db.execute("SELECT max(ts) FROM weigh_ins WHERE profile_id = ?", (pid,)).fetchone()[0]
            now = datetime.now()                # fixed, or the last chunk never ends
            start = datetime.fromtimestamp(last) - timedelta(days=2) if last else now - timedelta(days=BACKFILL_DAYS)
            while start < now:
                end = min(start + timedelta(days=CHUNK_DAYS), now)
                recs = svc.get_records(user_id=pid, start_time=start, end_time=end).data.get("data") or []
                new += store(db, rows_from_records(recs, account, nick))
                start = end
    return new


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--login", choices=sorted(ACCOUNTS), help="log in to this account and save its session")
    ap.add_argument("--print", action="store_true", help="print the stored history and trend")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if a.login:
        login(a.login)
    db = open_db()
    failed = 0
    for account in ACCOUNTS:
        if not session_file(account).exists():
            continue
        try:
            n = fetch(account, db)
            print(f"{time.strftime('%Y-%m-%d %H:%M')} {account}: {n} new weigh-in(s)")
        except Exception as exc:
            failed += 1
            print(f"{time.strftime('%Y-%m-%d %H:%M')} {account}: failed: {type(exc).__name__}: {str(exc)[:200]}")
    if a.print:
        for ts, kg, account in db.execute("SELECT ts, kg, account FROM weigh_ins ORDER BY ts"):
            print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}  {lb(kg):5.1f} lb  ({account})")
        print(json.dumps(trend(db)))
    db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
