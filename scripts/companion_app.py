#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Multi-Vault 桌面侧挂工作台 (PySide6 + WebEngine)
无边框透明圆角原生壳 + 本地 HTTP 28888 控制服务，共享 vault_core.py 内核。
"""

import ctypes
import json
import locale
import os
import platform
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow

SCRIPT_DIR = Path(__file__).resolve().parent
UI_HTML = SCRIPT_DIR.parent / "ui" / "index.html"
sys.path.insert(0, str(SCRIPT_DIR))

import vault_core  # noqa: E402

TOGGLE_EVENT_NAME = "Antigravity_MultiVault_Toggle_Event"
MUTEX_NAME = "Antigravity_MultiVault_Single_Instance_Mutex"


class AppState:
    def __init__(self) -> None:
        self.profiles_dir = vault_core.get_profiles_dir()
        self.dark_mode = self._load_theme()
        self.privacy_mode = self._load_privacy()
        self.lang = self._load_lang()
        self.auto_dock = True
        self.is_switching = False
        self.awaiting_slot = ""
        self.awaiting_label = ""
        self.last_toggle_ts = 0.0
        self.last_quota_mtime = 0.0
        self.lock = threading.Lock()

    def _load_theme(self) -> bool:
        f = self.profiles_dir / "theme.json"
        if f.exists() and '"light"' in f.read_text(encoding="utf-8", errors="ignore").lower():
            return False
        return True

    def save_theme(self, dark: bool) -> None:
        self.dark_mode = dark
        (self.profiles_dir / "theme.json").write_text(
            json.dumps({"theme": "dark" if dark else "light"}), encoding="utf-8"
        )

    def _load_privacy(self) -> bool:
        f = self.profiles_dir / "privacy.json"
        if f.exists() and "true" in f.read_text(encoding="utf-8", errors="ignore").lower():
            return True
        return False

    def save_privacy(self, priv: bool) -> None:
        self.privacy_mode = priv
        (self.profiles_dir / "privacy.json").write_text(
            json.dumps({"privacyMode": priv}), encoding="utf-8"
        )

    def _load_lang(self) -> str:
        f = self.profiles_dir / "lang.json"
        if f.exists():
            txt = f.read_text(encoding="utf-8", errors="ignore").lower()
            if '"en"' in txt:
                return "en"
            if '"zh"' in txt:
                return "zh"
        try:
            loc = locale.getlocale()[0] or ""
        except Exception:
            loc = ""
        sys_loc = (loc or os.environ.get("LANG", "")).lower()
        return "zh" if (sys_loc.startswith("zh") or "chinese" in sys_loc) else "en"

    def save_lang(self, lang: str) -> None:
        self.lang = "en" if lang == "en" else "zh"
        (self.profiles_dir / "lang.json").write_text(
            json.dumps({"lang": self.lang}), encoding="utf-8"
        )

    def sync_quota_if_updated(self, active_slot: str) -> None:
        if not active_slot:
            return
        try:
            cache_file = self.profiles_dir / "vault_status_cache.json"
            cache_payload = json.dumps({"activeSlot": active_slot}, ensure_ascii=False)
            if not cache_file.exists() or cache_file.read_text(encoding="utf-8", errors="ignore").strip() != cache_payload:
                cache_file.write_text(cache_payload, encoding="utf-8")
        except Exception:
            pass
        q_cur = self.profiles_dir / "quota_current.json"
        if not q_cur.exists():
            return
        try:
            mtime = q_cur.stat().st_mtime
            if mtime != self.last_quota_mtime:
                content = q_cur.read_text(encoding="utf-8", errors="ignore")
                data = json.loads(content)
                if isinstance(data, dict) and data.get("fiveHourLimit") and int(data.get("updatedAt") or 0) > 0:
                    self.last_quota_mtime = mtime
                    (self.profiles_dir / f"quota_{active_slot}.json").write_text(content, encoding="utf-8")
        except Exception:
            pass


STATE = AppState()


class UiBridge(QObject):
    sig_toggle = Signal(object, object, object, object, object)
    sig_hide = Signal()
    sig_quit = Signal()
    sig_drag = Signal()


BRIDGE = UiBridge()


class CompanionWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Antigravity 多仓工作台")
        self.setFixedSize(356, 868)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.web = QWebEngineView(self)
        self.web.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.setCentralWidget(self.web)
        self.web.load(QUrl("http://127.0.0.1:28888/"))

        BRIDGE.sig_toggle.connect(self.on_toggle)
        BRIDGE.sig_hide.connect(self.hide)
        BRIDGE.sig_quit.connect(QApplication.quit)
        BRIDGE.sig_drag.connect(self.on_start_drag)

        self.align_to_antigravity()

    def on_start_drag(self) -> None:
        win = self.windowHandle()
        if win is not None:
            win.startSystemMove()

    def align_to_antigravity(
        self, x: Optional[int] = None, y: Optional[int] = None, w: Optional[int] = None, h: Optional[int] = None
    ) -> None:
        if not STATE.auto_dock:
            return
        agy_top = y
        agy_right = (x + w) if (x is not None and w is not None) else None

        if agy_right is None:
            sync_file = STATE.profiles_dir / "companion_sync.json"
            if sync_file.exists():
                try:
                    d = json.loads(sync_file.read_text(encoding="utf-8"))
                    agy_top = int(d.get("top", 60))
                    agy_right = int(d.get("right", 0))
                except Exception:
                    pass

        screen = QGuiApplication.primaryScreen().availableGeometry()
        if agy_right and agy_right > 0:
            target_y = max(screen.top(), agy_top or 60)
            target_x = agy_right + 8
            if target_x + self.width() > screen.right():
                target_x = agy_right - self.width() - 12
            target_x = max(screen.left(), min(target_x, screen.right() - self.width()))
            target_y = max(screen.top(), min(target_y, screen.bottom() - self.height()))
            self.move(target_x, target_y)
        else:
            self.move(screen.right() - self.width() - 16, screen.top() + 60)

    def on_toggle(self, theme: Any, x: Any, y: Any, w: Any, h: Any) -> None:
        now = time.time()
        if theme in ("dark", "light"):
            STATE.save_theme(theme == "dark")

        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()
            self.align_to_antigravity(x, y, w, h)
            STATE.last_toggle_ts = now
        else:
            if now - STATE.last_toggle_ts < 0.6:
                self.raise_()
                self.activateWindow()
                return
            self.hide()
            STATE.last_toggle_ts = now


class ApiHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.lower()
        qs = parse_qs(parsed.query)

        if path == "/":
            html = UI_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)
            return

        if path == "/toggle":
            theme = qs.get("theme", [None])[0]
            def _int(k: str) -> Optional[int]:
                try:
                    v = int(qs.get(k, [""])[0])
                    return v if v > -5000 else None
                except Exception:
                    return None
            BRIDGE.sig_toggle.emit(theme, _int("x"), _int("y"), _int("w"), _int("h"))
            self._send_json({"ok": True, "status": "toggled"})
            return

        if path == "/status":
            st = vault_core.cmd_status(mask=STATE.privacy_mode)
            self._send_json({
                "ok": True,
                "darkMode": STATE.dark_mode,
                "privacyMode": STATE.privacy_mode,
                "primarySlot": st.get("primarySlot", ""),
                "slotCount": st.get("totalSlots", 0),
            })
            return

        if path == "/api/state":
            with STATE.lock:
                st = vault_core.cmd_status(mask=False)
                raw_cur = st.get("currentEmail", "")
                if STATE.awaiting_slot and raw_cur:
                    existing_emails = {s.get("email", "").lower() for s in st.get("slots", []) if s.get("email")}
                    if raw_cur.lower() not in existing_emails:
                        target_id, target_lbl = STATE.awaiting_slot, STATE.awaiting_label
                        STATE.awaiting_slot, STATE.awaiting_label = "", ""
                        vault_core.cmd_save(target_id, label=target_lbl)
                        st = vault_core.cmd_status(mask=False)

                STATE.sync_quota_if_updated(st.get("activeSlot", ""))
                st_masked = vault_core.cmd_status(mask=STATE.privacy_mode)
                st_masked["darkMode"] = STATE.dark_mode
                st_masked["privacyMode"] = STATE.privacy_mode
                st_masked["lang"] = STATE.lang
                st_masked["autoDock"] = STATE.auto_dock
                st_masked["isSwitching"] = STATE.is_switching
            self._send_json(st_masked)
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.lower()
        body = self._read_json_body()

        if path == "/api/start-drag":
            BRIDGE.sig_drag.emit()
            self._send_json({"ok": True})
        elif path == "/api/hide":
            BRIDGE.sig_hide.emit()
            self._send_json({"ok": True})
        elif path == "/api/quit":
            BRIDGE.sig_quit.emit()
            self._send_json({"ok": True})
        elif path == "/api/privacy":
            STATE.save_privacy(not STATE.privacy_mode)
            st = vault_core.cmd_status(mask=STATE.privacy_mode)
            st.update({"darkMode": STATE.dark_mode, "privacyMode": STATE.privacy_mode, "lang": STATE.lang, "autoDock": STATE.auto_dock, "isSwitching": STATE.is_switching})
            self._send_json(st)
        elif path == "/api/lang":
            STATE.save_lang("en" if STATE.lang == "zh" else "zh")
            st = vault_core.cmd_status(mask=STATE.privacy_mode)
            st.update({"darkMode": STATE.dark_mode, "privacyMode": STATE.privacy_mode, "lang": STATE.lang, "autoDock": STATE.auto_dock, "isSwitching": STATE.is_switching})
            self._send_json(st)
        elif path == "/api/theme":
            STATE.save_theme(not STATE.dark_mode)
            st = vault_core.cmd_status(mask=STATE.privacy_mode)
            st.update({"darkMode": STATE.dark_mode, "privacyMode": STATE.privacy_mode, "lang": STATE.lang, "autoDock": STATE.auto_dock, "isSwitching": STATE.is_switching})
            self._send_json(st)
        elif path == "/api/autodock":
            STATE.auto_dock = bool(body.get("autoDock", True))
            self._send_json({"ok": True})
        elif path == "/api/refresh-quota":
            trig = STATE.profiles_dir / "quota_trigger.json"
            trig.write_text(json.dumps({"action": "refresh", "timestamp": int(time.time() * 1000)}), encoding="utf-8")
            self._send_json({"ok": True})
        elif path == "/api/set-primary":
            self._send_json(vault_core.cmd_set_primary(str(body.get("slot", ""))))
        elif path == "/api/save":
            self._send_json(vault_core.cmd_save(str(body.get("slot", "")), label=str(body.get("label", "")), mask=STATE.privacy_mode))
        elif path == "/api/remove":
            self._send_json(vault_core.cmd_remove(str(body.get("slot", "")), purge=True))
        elif path == "/api/switch":
            slot = str(body.get("slot", ""))
            def _do_switch() -> None:
                STATE.is_switching = True
                try:
                    vault_core.cmd_switch_restart(slot)
                finally:
                    STATE.is_switching = False
            threading.Thread(target=_do_switch, daemon=True).start()
            self._send_json({"ok": True})
        elif path == "/api/prepare-new":
            STATE.awaiting_slot = str(body.get("slot", ""))
            STATE.awaiting_label = str(body.get("label", ""))
            def _do_prep() -> None:
                STATE.is_switching = True
                try:
                    vault_core.cmd_prepare_new()
                finally:
                    STATE.is_switching = False
            threading.Thread(target=_do_prep, daemon=True).start()
            self._send_json({"ok": True})
        elif path == "/api/launch":
            vault_core._launch_antigravity()
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)


def start_windows_toggle_listener() -> None:
    if platform.system() != "Windows":
        return
    k32 = ctypes.windll.kernel32
    evt = k32.CreateEventW(None, False, False, TOGGLE_EVENT_NAME)
    if not evt:
        return

    def _loop() -> None:
        while True:
            res = k32.WaitForSingleObject(evt, 2000)
            if res == 0:
                BRIDGE.sig_toggle.emit(None, None, None, None, None)

    threading.Thread(target=_loop, daemon=True).start()


def ensure_single_instance() -> bool:
    if platform.system() != "Windows":
        return True
    k32 = ctypes.windll.kernel32
    mutex = k32.CreateMutexW(None, True, MUTEX_NAME)
    err = k32.GetLastError()
    if err == 183:  # ERROR_ALREADY_EXISTS
        evt = k32.OpenEventW(0x0002, False, TOGGLE_EVENT_NAME)
        if evt:
            k32.SetEvent(evt)
            k32.CloseHandle(evt)
        return False
    globals()["_APP_MUTEX"] = mutex
    return True


def main() -> None:
    if not ensure_single_instance():
        return

    server = ThreadingHTTPServer(("127.0.0.1", 28888), ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    start_windows_toggle_listener()

    app = QApplication(sys.argv)
    win = CompanionWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
