"""PXE control center: per-client iPXE menus (boot policies by MAC/group) and usage tracking."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .catalog import Catalog, pxecli
from .db import Database
from .ingest import LOG_DIR, Ingestor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pxe.controller")

DB_PATH = os.environ.get("PXE_DB", "/var/lib/pxe-controller/pxe.db")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
ACTIVE_SECONDS = 15 * 60
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
APP_DIR = Path(__file__).parent

db = Database(DB_PATH)
catalog = Catalog()
ingestor = Ingestor(db, catalog)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ingestor.start()
    yield
    ingestor.stop()


app = FastAPI(title="PXE control center", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


# ---------------------------------------------------------------------- formatting
def human_bytes(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if delta >= size:
            return f"{int(delta // size)} {unit} ago"
    return ""


def local_time(ts: float | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.fromtimestamp(ts, TZ).strftime(fmt) if ts else ""


templates.env.filters.update(bytes=human_bytes, ago=ago, localtime=local_time)
templates.env.globals.update(label=catalog.label)


def normalize_mac(value: str) -> str:
    mac = value.strip().lower().replace("-", ":")
    if re.fullmatch(r"[0-9a-f]{12}", mac):
        mac = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
    return mac


# ---------------------------------------------------------------------- auth for the web UI
@app.middleware("http")
async def admin_auth(request: Request, call_next):
    path = request.url.path
    if ADMIN_PASSWORD and not (path.startswith("/ipxe/") or path == "/healthz"):
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, ADMIN_USER) and secrets.compare_digest(password, ADMIN_PASSWORD)
            except ValueError:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="PXE control center"'})
    return await call_next(request)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "items": len(catalog.items)}


# ---------------------------------------------------------------------- iPXE endpoints
IPXE = "text/plain; charset=utf-8"


def client_ip(request: Request, announced: str = "") -> str:
    if announced and announced != "0.0.0.0":
        return announced
    real = request.headers.get("x-real-ip")
    host = request.client.host if request.client else ""
    return real if real and host in ("127.0.0.1", "::1") else host


@app.get("/ipxe/boot.ipxe", response_class=PlainTextResponse)
def ipxe_entry() -> Response:
    if not catalog.items:
        # No menu.json yet ('./pxe build' not run/pushed): nginx falls back to the static menu
        raise HTTPException(503, "menu.json not found")
    # Second stage: hand the client's identity to /ipxe/menu (relative URLs resolve against this script)
    params = "&".join([
        "mac=${netX/mac}", "ip=${netX/ip}", "hostname=${hostname:uristring}", "uuid=${uuid}",
        "serial=${serial:uristring}", "manufacturer=${manufacturer:uristring}",
        "product=${product:uristring}", "platform=${platform}", "arch=${buildarch}",
    ])
    return Response(f"#!ipxe\nchain --autofree /ipxe/menu?{params} || exit 1\n", media_type=IPXE)


@app.get("/ipxe/menu")
def ipxe_menu(request: Request, mac: str, ip: str = "", hostname: str = "", uuid: str = "", serial: str = "",
              manufacturer: str = "", product: str = "", platform: str = "", arch: str = "") -> Response:
    mac = normalize_mac(mac)
    if not MAC_RE.match(mac):
        raise HTTPException(400, "bad mac")
    ip = client_ip(request, ip)
    info = {"ip": ip, "hostname": hostname, "uuid": uuid, "serial": serial, "manufacturer": manufacturer,
            "product": product, "platform": platform, "arch": arch}
    client = db.touch_client(mac, {k: v.strip()[:200] for k, v in info.items()})
    group = db.group(client["group_id"])
    scheduled = client["next_boot"] if client["next_boot"] and catalog.item(client["next_boot"]) else None

    if group["mode"] == "local" and not scheduled:
        db.add_event("local-policy", mac, ip, "local", f"group {group['name']}")
        script = ["#!ipxe", f"echo PXE policy (group {group['name']}): booting from the local disk...",
                  "sleep 3", *pxecli.LOCAL_BOOT, ":failed", "exit 1", ""]
        return Response("\n".join(script), media_type=IPXE)

    allowed = None if group["items"] is None else set(group["items"])
    if scheduled and allowed is not None:
        allowed.add(scheduled)
    timeout = group["timeout"] if group["timeout"] is not None else catalog.menu_timeout
    who = client["name"] or client["hostname"] or mac
    db.add_event("scheduled" if scheduled else "menu", mac, ip, scheduled, f"group {group['name']}")
    script = pxecli.render_menu(
        catalog.boot_items(), {}, allowed=allowed, tools=None if group["tools"] is None else set(group["tools"]),
        default=group["default_item"], timeout=timeout, banner=f"{who}  |  group {group['name']}",
        event_url=f"/ipxe/event?mac={quote(mac)}&item=", autoboot=scheduled,
    )
    return Response(script, media_type=IPXE)


@app.get("/ipxe/event")
def ipxe_event(request: Request, mac: str, item: str) -> Response:
    mac = normalize_mac(mac)
    client = db.client(mac)
    db.add_event("choice", mac, client_ip(request, client["ip"] if client else ""), item[:100])
    if client and client["next_boot"] == item:
        db.execute("UPDATE clients SET next_boot = NULL WHERE mac = ?", (mac,))
    return Response("#!ipxe\n", media_type=IPXE)


# ---------------------------------------------------------------------- queries for the UI
def smb_sessions() -> list[dict]:
    try:
        data = json.loads((LOG_DIR / "smbstatus.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if time.time() - (LOG_DIR / "smbstatus.json").stat().st_mtime > 120:
        return []  # stale snapshot: samba is not running
    files: dict[str, list[str]] = {}
    for entry in data.get("open_files", {}).values():
        for opener in entry.get("opens", {}).values():
            pid = str(opener.get("server_id", {}).get("pid", ""))
            files.setdefault(pid, []).append(entry.get("filename", ""))
    sessions = []
    for sess in data.get("sessions", {}).values():
        ip = sess.get("remote_machine", "")
        pid = str(sess.get("server_id", {}).get("pid", ""))
        sessions.append({"ip": ip, "user": sess.get("username", ""), "mac": db.mac_for_ip(ip, time.time()),
                         "files": sorted({f for f in files.get(pid, []) if f and f != "."})})
    return sessions


def activity(limit: int = 50, mac: str | None = None, item: str | None = None, since: float = 0) -> list[dict]:
    where_e, where_t, params = ["ts > :since"], ["ts > :since"], {"since": since, "limit": limit}
    if mac:
        where_e.append("mac = :mac")
        where_t.append("mac = :mac")
        params["mac"] = mac
    if item:
        where_e.append("item = :item")
        where_t.append("item = :item")
        params["item"] = item
    rows = db.all(
        f"""SELECT ts, mac, ip, kind, item, detail, NULL AS path, NULL AS bytes, NULL AS status FROM events
            WHERE {' AND '.join(where_e)} AND kind != 'menu'
            UNION ALL
            SELECT ts, mac, ip, proto AS kind, item, NULL, path, bytes, status FROM transfers
            WHERE {' AND '.join(where_t)}
            ORDER BY ts DESC LIMIT :limit""", params)
    names = {r["mac"]: r["name"] or r["hostname"] for r in db.all("SELECT mac, name, hostname FROM clients")}
    return [{**dict(r), "who": names.get(r["mac"]) or r["mac"] or r["ip"]} for r in rows]


def client_rows() -> list[dict]:
    week = time.time() - 7 * 86400
    rows = db.all(
        """SELECT c.*, g.name AS group_name,
                  (SELECT item FROM events e WHERE e.mac = c.mac AND e.kind IN ('choice', 'scheduled')
                   ORDER BY ts DESC LIMIT 1) AS last_item,
                  (SELECT COALESCE(SUM(bytes), 0) FROM transfers t
                   WHERE t.mac = c.mac AND t.proto = 'http' AND t.ts > :week) AS bytes_week,
                  (SELECT MAX(ts) FROM transfers t WHERE t.mac = c.mac) AS last_transfer
           FROM clients c LEFT JOIN groups g ON g.id = c.group_id
           ORDER BY c.last_seen DESC""", {"week": week})
    out = []
    for r in rows:
        d = dict(r)
        d["last_active"] = max(d["last_seen"] or 0, d["last_transfer"] or 0)
        out.append(d)
    return out


def image_stats() -> list[dict]:
    week = time.time() - 7 * 86400
    stats = {r["item"]: dict(r) for r in db.all(
        """SELECT item,
                  COALESCE(SUM(CASE WHEN proto = 'http' THEN bytes END), 0) AS http_bytes,
                  COALESCE(SUM(CASE WHEN proto = 'http' AND ts > :week THEN bytes END), 0) AS http_week,
                  COUNT(CASE WHEN proto = 'smb' THEN 1 END) AS smb_opens,
                  COUNT(DISTINCT mac) AS clients, MAX(ts) AS last_transfer
           FROM transfers WHERE item IS NOT NULL GROUP BY item""", {"week": week})}
    boots = {r["item"]: dict(r) for r in db.all(
        """SELECT item, COUNT(*) AS boots, COUNT(CASE WHEN ts > :week THEN 1 END) AS boots_week,
                  COUNT(DISTINCT mac) AS boot_clients, MAX(ts) AS last_boot
           FROM events WHERE kind IN ('choice', 'scheduled') AND item IS NOT NULL GROUP BY item""",
        {"week": week})}
    out = []
    ids = [i["id"] for i in catalog.items] + [t for t in catalog.tools] + ["local"]
    for item_id in ids + sorted(set(stats) | set(boots) - set(ids)):
        s, b = stats.get(item_id, {}), boots.get(item_id, {})
        if item_id not in [i["id"] for i in catalog.items] and not b and not s:
            continue
        found = catalog.item(item_id)
        out.append({
            "id": item_id, "label": catalog.label(item_id),
            "category": found["category"] if found else ("tools" if item_id != "local" else "local"),
            "in_catalog": bool(found) or item_id in catalog.tools or item_id == "local",
            "boots": b.get("boots", 0), "boots_week": b.get("boots_week", 0),
            "clients": max(b.get("boot_clients", 0), s.get("clients", 0)),
            "http_bytes": s.get("http_bytes", 0), "http_week": s.get("http_week", 0),
            "smb_opens": s.get("smb_opens", 0),
            "last_used": max(b.get("last_boot") or 0, s.get("last_transfer") or 0) or None,
        })
    return out


def page(request: Request, name: str, **context) -> HTMLResponse:
    user = request.headers.get("x-authentik-username") or (ADMIN_USER if ADMIN_PASSWORD else "")
    return templates.TemplateResponse(request, name, {"user": user, "nav": name.split(".")[0], **context})


# ---------------------------------------------------------------------- web UI
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    now = time.time()
    day = now - 86400
    clients = client_rows()
    active = [c for c in clients if c["last_active"] > now - ACTIVE_SECONDS]
    for c in active:
        last = db.one("SELECT path, proto, bytes, ts FROM transfers WHERE mac = ? ORDER BY ts DESC LIMIT 1",
                      (c["mac"],))
        c["last_path"] = dict(last) if last else None
        c["bytes_recent"] = db.one("SELECT COALESCE(SUM(bytes), 0) AS b FROM transfers "
                                   "WHERE mac = ? AND proto = 'http' AND ts > ?", (c["mac"], now - 3600))["b"]
    stats = {
        "clients_day": db.one("SELECT COUNT(DISTINCT mac) AS n FROM events WHERE ts > ?", (day,))["n"],
        "boots_day": db.one("SELECT COUNT(*) AS n FROM events WHERE kind IN ('choice', 'scheduled') "
                            "AND item NOT IN ('local', 'reboot', 'poweroff') AND ts > ?", (day,))["n"],
        "bytes_day": db.one("SELECT COALESCE(SUM(bytes), 0) AS n FROM transfers "
                            "WHERE proto = 'http' AND ts > ?", (day,))["n"],
        "clients_total": len(clients),
    }
    images = sorted((i for i in image_stats() if i["http_week"] or i["boots_week"]),
                    key=lambda i: (i["http_week"], i["boots_week"]), reverse=True)[:8]
    top = max([i["http_week"] for i in images] or [1]) or 1
    return page(request, "dashboard.html", stats=stats, active=active, smb=smb_sessions(),
                recent=activity(30), images=images, top=top)


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request):
    return page(request, "clients.html", clients=client_rows(), groups=db.groups(), now=time.time(),
                active_seconds=ACTIVE_SECONDS)


@app.post("/clients")
def client_add(mac: str = Form(...), name: str = Form(""), group_id: int = Form(...)):
    mac = normalize_mac(mac)
    if not MAC_RE.match(mac):
        raise HTTPException(400, f"invalid MAC address: {mac}")
    if not db.client(mac):
        db.execute("INSERT INTO clients (mac, name, group_id, first_seen) VALUES (?, ?, ?, NULL)",
                   (mac, name.strip(), group_id))
    return RedirectResponse(f"/clients/{mac}", status_code=303)


@app.get("/clients/{mac}", response_class=HTMLResponse)
def client_page(request: Request, mac: str):
    client = db.client(normalize_mac(mac))
    if not client:
        raise HTTPException(404, "unknown client")
    totals = db.one("SELECT COALESCE(SUM(CASE WHEN proto = 'http' THEN bytes END), 0) AS http, "
                    "COUNT(CASE WHEN proto = 'smb' THEN 1 END) AS smb FROM transfers WHERE mac = ?",
                    (client["mac"],))
    return page(request, "client.html", client=client, groups=db.groups(), group=db.group(client["group_id"]),
                items=catalog.items, categories=catalog.categories, totals=totals,
                history=activity(200, mac=client["mac"]))


@app.post("/clients/{mac}")
def client_update(mac: str, name: str = Form(""), group_id: int = Form(...), notes: str = Form("")):
    db.execute("UPDATE clients SET name = ?, group_id = ?, notes = ? WHERE mac = ?",
               (name.strip(), group_id, notes.strip(), normalize_mac(mac)))
    return RedirectResponse(f"/clients/{normalize_mac(mac)}", status_code=303)


@app.post("/clients/{mac}/group")
def client_set_group(mac: str, group_id: int = Form(...)):
    db.execute("UPDATE clients SET group_id = ? WHERE mac = ?", (group_id, normalize_mac(mac)))
    return RedirectResponse("/clients", status_code=303)


@app.post("/clients/{mac}/next-boot")
def client_next_boot(mac: str, item: str = Form("")):
    db.execute("UPDATE clients SET next_boot = ? WHERE mac = ?", (item or None, normalize_mac(mac)))
    return RedirectResponse(f"/clients/{normalize_mac(mac)}", status_code=303)


@app.post("/clients/{mac}/delete")
def client_delete(mac: str):
    db.execute("DELETE FROM clients WHERE mac = ?", (normalize_mac(mac),))
    return RedirectResponse("/clients", status_code=303)


@app.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request, edit: int | None = None):
    groups = db.groups()
    current = next((g for g in groups if g["id"] == edit), None) if edit else None
    return page(request, "groups.html", groups=groups, current=current, new=edit == 0,
                items=catalog.items, categories=catalog.categories, tools=catalog.tools)


@app.post("/groups")
async def group_save(request: Request):
    form = await request.form()
    group_id = int(form.get("id") or 0)
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "name is required")
    items = None if form.get("all_items") else json.dumps(form.getlist("items"))
    tools = json.dumps(form.getlist("tools"))
    timeout = str(form.get("timeout", "")).strip()
    values = {
        "name": name, "description": str(form.get("description", "")).strip(),
        "mode": "local" if form.get("mode") == "local" else "menu", "items": items, "tools": tools,
        "default_item": str(form.get("default_item") or "") or None,
        "timeout": int(timeout) if timeout.isdigit() else None,
    }
    if group_id:
        if db.group(group_id)["name"] == "default":
            values["name"] = "default"
        db.execute("UPDATE groups SET name = :name, description = :description, mode = :mode, items = :items, "
                   "tools = :tools, default_item = :default_item, timeout = :timeout WHERE id = :id",
                   {**values, "id": group_id})
    else:
        try:
            db.execute("INSERT INTO groups (name, description, mode, items, tools, default_item, timeout) "
                       "VALUES (:name, :description, :mode, :items, :tools, :default_item, :timeout)", values)
        except Exception as exc:
            raise HTTPException(400, f"could not create group: {exc}") from exc
    return RedirectResponse("/groups", status_code=303)


@app.post("/groups/{group_id}/delete")
def group_delete(group_id: int):
    if db.group(group_id)["name"] != "default":
        db.execute("UPDATE clients SET group_id = ? WHERE group_id = ?", (db.default_group_id(), group_id))
        db.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    return RedirectResponse("/groups", status_code=303)


@app.get("/images", response_class=HTMLResponse)
def images_page(request: Request):
    return page(request, "images.html", images=image_stats(), categories=catalog.categories)


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, mac: str = "", item: str = ""):
    return page(request, "activity.html", rows=activity(500, mac=normalize_mac(mac) if mac else None,
                                                         item=item or None), mac=mac, item=item)
