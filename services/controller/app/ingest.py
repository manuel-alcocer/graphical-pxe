"""Follow the nginx JSON access log and the samba audit log, and store what each client used."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from .catalog import HTTP_ROOT, Catalog
from .db import Database

log = logging.getLogger("pxe.ingest")

LOG_DIR = Path(os.environ.get("PXE_LOG_DIR", "/var/log/pxe"))
SMB_ROOT = os.environ.get("SMB_SHARE_PATH", "/srv/images")  # path of the share inside the samba container
MAX_LOG_BYTES = int(os.environ.get("PXE_MAX_LOG_BYTES", str(64 * 1024 * 1024)))
KEEP_DAYS = int(os.environ.get("PXE_KEEP_DAYS", "90"))
SMB_DEDUP_SECONDS = 600
IGNORED_HTTP = ("/ipxe/", "/assets/")


class LogFollower:
    """Read appended lines of a log file, remembering the offset across restarts."""

    def __init__(self, db: Database, path: Path):
        self.db, self.path = db, path
        self.key = f"offset:{path.name}"

    def read(self) -> list[str]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return []
        saved = self.db.get_state(self.key, "0:0").split(":")
        inode, offset = int(saved[0]), int(saved[1])
        if st.st_ino != inode or st.st_size < offset:
            offset = 0  # rotated or truncated
        if st.st_size == offset:
            return []
        with self.path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1  # leave a partial last line for next time
        lines = chunk[:end].decode("utf-8", "replace").splitlines()
        offset += end
        # Writers append (O_APPEND), so truncating in place is safe once everything is consumed
        if offset >= MAX_LOG_BYTES and offset == st.st_size:
            os.truncate(self.path, 0)
            offset = 0
        self.db.set_state(self.key, f"{st.st_ino}:{offset}")
        return lines


class Ingestor:
    def __init__(self, db: Database, catalog: Catalog):
        self.db, self.catalog = db, catalog
        self.http = LogFollower(db, LOG_DIR / "http.json")
        self.smb = LogFollower(db, LOG_DIR / "smb.log")
        self._recent_smb: dict[tuple[str, str], float] = {}
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="ingest", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        last_prune = 0.0
        while not self._stop.is_set():
            try:
                for line in self.http.read():
                    self.http_line(line)
                for line in self.smb.read():
                    self.smb_line(line)
                if time.time() - last_prune > 3600:
                    self.db.prune(KEEP_DAYS)
                    last_prune = time.time()
            except Exception:  # keep following whatever happens
                log.exception("ingest failed")
            self._stop.wait(2)

    # ------------------------------------------------------------------ helpers
    def _item(self, mac: str | None, path: str, ts: float) -> str | None:
        candidates = self.catalog.items_for_path(path)
        if len(candidates) == 1:
            return candidates[0]
        if mac and candidates:
            chosen = self.db.last_choice(mac, candidates, ts)
            if chosen:
                return chosen
        return candidates[0] if candidates else None

    # ------------------------------------------------------------------ nginx
    def http_line(self, line: str) -> None:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return
        uri = rec.get("uri", "")
        if uri == "/boot.ipxe" or uri.startswith(IGNORED_HTTP):
            return
        try:
            ts = datetime.fromisoformat(rec["t"]).timestamp()
        except (KeyError, ValueError):
            ts = time.time()
        ip = rec.get("ip")
        mac = self.db.mac_for_ip(ip, ts) if ip else None
        self.db.add_transfer(ts=ts, mac=mac, ip=ip, proto="http", item=self._item(mac, uri, ts), path=uri,
                             bytes=int(rec.get("bytes") or 0), status=int(rec.get("status") or 0),
                             seconds=float(rec.get("seconds") or 0))

    # ------------------------------------------------------------------ samba full_audit
    def smb_line(self, line: str) -> None:
        # <ip>|<machine>|<user>|<op>|<ok|fail>|<args...>
        parts = line.strip().split("|")
        if len(parts) < 6 or parts[3] not in ("connect", "disconnect", "openat"):
            return
        ip, machine, user, op, result = parts[:5]
        ts = time.time()
        mac = self.db.mac_for_ip(ip, ts)
        if op in ("connect", "disconnect"):
            kind = f"smb-{op}" if result == "ok" else "smb-denied"
            self.db.add_event(kind, mac, ip, detail=f"user {user} from {machine}, share {parts[5]}", ts=ts)
            return
        if result != "ok":
            return
        full = parts[-1]
        if not full.startswith(SMB_ROOT):
            return
        rel = "images" + full[len(SMB_ROOT):]
        local = HTTP_ROOT / rel
        if not local.is_file():
            return  # directories are opened all the time while browsing
        key = (ip, rel)
        if ts - self._recent_smb.get(key, 0) < SMB_DEDUP_SECONDS:
            return  # Windows reopens the same file many times
        self._recent_smb[key] = ts
        self.db.add_transfer(ts=ts, mac=mac, ip=ip, proto="smb", item=self._item(mac, "/" + rel, ts),
                             path="/" + rel, bytes=local.stat().st_size, status=None, seconds=None)
