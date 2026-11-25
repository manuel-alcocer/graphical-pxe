"""SQLite storage: clients, groups (boot policies), events and transfers."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT NOT NULL DEFAULT '',
    mode          TEXT NOT NULL DEFAULT 'menu',   -- menu | local
    items         TEXT,                           -- JSON list of item ids, NULL = all
    tools         TEXT,                           -- JSON list of tool ids, NULL = all
    default_item  TEXT,                           -- item id or 'local', NULL = 'local'
    timeout       INTEGER                         -- seconds, NULL = global MENU_TIMEOUT
);

CREATE TABLE IF NOT EXISTS clients (
    mac           TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    group_id      INTEGER REFERENCES groups(id),
    ip            TEXT,
    hostname      TEXT,
    manufacturer  TEXT,
    product       TEXT,
    serial        TEXT,
    uuid          TEXT,
    platform      TEXT,
    arch          TEXT,
    first_seen    REAL,
    last_seen     REAL,
    next_boot     TEXT,
    notes         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS clients_ip ON clients(ip);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    mac     TEXT,
    ip      TEXT,
    kind    TEXT NOT NULL,     -- menu | choice | scheduled | local-policy | smb-connect | smb-disconnect
    item    TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_mac ON events(mac, ts);

CREATE TABLE IF NOT EXISTS transfers (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    mac      TEXT,
    ip       TEXT,
    proto    TEXT NOT NULL,    -- http | smb
    item     TEXT,
    path     TEXT NOT NULL,
    bytes    INTEGER NOT NULL, -- http: bytes sent; smb: size of the opened file
    status   INTEGER,
    seconds  REAL
);
CREATE INDEX IF NOT EXISTS transfers_ts ON transfers(ts);
CREATE INDEX IF NOT EXISTS transfers_mac ON transfers(mac, ts);
CREATE INDEX IF NOT EXISTS transfers_item ON transfers(item, ts);

CREATE TABLE IF NOT EXISTS state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            if not self.one("SELECT id FROM groups WHERE name = 'default'"):
                self.execute(
                    "INSERT INTO groups (name, description) VALUES ('default', ?)",
                    ("Computers that are not assigned to any other group",),
                )

    # ------------------------------------------------------------------ helpers
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM state WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.execute("INSERT INTO state (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # ------------------------------------------------------------------ groups
    def default_group_id(self) -> int:
        return self.one("SELECT id FROM groups WHERE name = 'default'")["id"]

    def group(self, group_id: int | None) -> dict:
        row = self.one("SELECT * FROM groups WHERE id = ?", (group_id,)) if group_id else None
        if not row:
            row = self.one("SELECT * FROM groups WHERE name = 'default'")
        return group_dict(row)

    def groups(self) -> list[dict]:
        rows = self.all("SELECT g.*, (SELECT COUNT(*) FROM clients c WHERE c.group_id = g.id) AS members "
                        "FROM groups g ORDER BY g.name = 'default' DESC, g.name")
        return [group_dict(r) for r in rows]

    # ------------------------------------------------------------------ clients
    def client(self, mac: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM clients WHERE mac = ?", (mac,))

    def touch_client(self, mac: str, info: dict[str, str]) -> sqlite3.Row:
        now = time.time()
        fields = {k: v for k, v in info.items() if v}
        with self.lock:
            if self.client(mac):
                sets = ", ".join(f"{k} = :{k}" for k in fields)
                self.execute(f"UPDATE clients SET last_seen = :now{', ' + sets if sets else ''} WHERE mac = :mac",
                             {**fields, "now": now, "mac": mac})
            else:
                cols = ["mac", "group_id", "first_seen", "last_seen", *fields]
                self.execute(f"INSERT INTO clients ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
                             {**fields, "mac": mac, "group_id": self.default_group_id(),
                              "first_seen": now, "last_seen": now})
            return self.client(mac)

    def mac_for_ip(self, ip: str, at: float) -> str | None:
        """The client that last announced this IP before `at` (same day)."""
        row = self.one("SELECT mac FROM events WHERE ip = ? AND ts <= ? AND ts > ? AND mac IS NOT NULL "
                       "ORDER BY ts DESC LIMIT 1", (ip, at + 5, at - 86400))
        if row:
            return row["mac"]
        row = self.one("SELECT mac FROM clients WHERE ip = ? AND last_seen > ? ORDER BY last_seen DESC LIMIT 1",
                       (ip, at - 86400))
        return row["mac"] if row else None

    def last_choice(self, mac: str, candidates: list[str], at: float) -> str | None:
        if not candidates:
            return None
        marks = ",".join("?" * len(candidates))
        row = self.one(f"SELECT item FROM events WHERE mac = ? AND kind IN ('choice', 'scheduled') "
                       f"AND item IN ({marks}) AND ts <= ? AND ts > ? ORDER BY ts DESC LIMIT 1",
                       (mac, *candidates, at + 5, at - 6 * 3600))
        return row["item"] if row else None

    # ------------------------------------------------------------------ events / transfers
    def add_event(self, kind: str, mac: str | None, ip: str | None, item: str | None = None,
                  detail: str | None = None, ts: float | None = None) -> None:
        self.execute("INSERT INTO events (ts, mac, ip, kind, item, detail) VALUES (?, ?, ?, ?, ?, ?)",
                     (ts or time.time(), mac, ip, kind, item, detail))

    def add_transfer(self, **row) -> None:
        self.execute("INSERT INTO transfers (ts, mac, ip, proto, item, path, bytes, status, seconds) "
                     "VALUES (:ts, :mac, :ip, :proto, :item, :path, :bytes, :status, :seconds)", row)

    def prune(self, keep_days: int) -> None:
        limit = time.time() - keep_days * 86400
        self.execute("DELETE FROM events WHERE ts < ?", (limit,))
        self.execute("DELETE FROM transfers WHERE ts < ?", (limit,))


def group_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["items"] = json.loads(data["items"]) if data.get("items") else None
    data["tools"] = json.loads(data["tools"]) if data.get("tools") else None
    return data
