"""Boot items published by './pxe build' (menu.json) and the shared iPXE menu renderer."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path

HTTP_ROOT = Path(os.environ.get("PXE_HTTP_ROOT", "/srv/http"))


def _load_cli():
    # The ./pxe generator is the single source of truth for how menus look.
    path = os.environ.get("PXE_CLI") or str(Path(__file__).resolve().parents[3] / "pxe")
    loader = importlib.machinery.SourceFileLoader("pxecli", path)
    spec = importlib.util.spec_from_loader("pxecli", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pxecli"] = module  # dataclasses look the module up while it loads
    loader.exec_module(module)
    return module


pxecli = _load_cli()


class Catalog:
    def __init__(self, root: Path = HTTP_ROOT):
        self.path = root / "menu.json"
        self._mtime = 0.0
        self._lock = threading.Lock()
        self.data: dict = {"items": [], "categories": pxecli.CATEGORIES, "tools": pxecli.TOOLS, "menu_timeout": 0}

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            with self._lock:
                self.data = json.loads(self.path.read_text())
                self._mtime = mtime

    @property
    def items(self) -> list[dict]:
        self._refresh()
        return self.data["items"]

    @property
    def tools(self) -> dict[str, str]:
        self._refresh()
        return self.data.get("tools", pxecli.TOOLS)

    @property
    def categories(self) -> dict[str, str]:
        self._refresh()
        return self.data.get("categories", pxecli.CATEGORIES)

    @property
    def menu_timeout(self) -> int:
        self._refresh()
        return int(self.data.get("menu_timeout", 0))

    def item(self, item_id: str) -> dict | None:
        return next((i for i in self.items if i["id"] == item_id), None)

    def label(self, item_id: str | None) -> str:
        if not item_id:
            return ""
        if item_id == "local":
            return "Local disk"
        if item_id in self.tools:
            return self.tools[item_id].split(" (")[0]
        found = self.item(item_id)
        return found["label"] if found else item_id

    def boot_items(self) -> list:
        return [pxecli.BootItem(i["id"], i["label"], i["category"], i["script"], i.get("src", ""))
                for i in self.items]

    def items_for_path(self, path: str) -> list[str]:
        """Item ids a downloaded/opened file belongs to (several entries may share files)."""
        parts = path.lstrip("/").split("/")
        if len(parts) >= 2 and parts[0] == "images":
            return [i["id"] for i in self.items if i.get("src") == parts[1]]
        if len(parts) >= 2 and parts[0] == "iso":
            src = parts[1].removesuffix(".iso")
            return [i["id"] for i in self.items if i.get("src") == src]
        if len(parts) >= 2 and parts[0] == "windows":
            return [parts[1]] if self.item(parts[1]) else []
        if parts[0] == "wimboot":
            return [i["id"] for i in self.items if i["category"].startswith("windows")]
        return []
