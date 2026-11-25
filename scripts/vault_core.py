#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Multi-Vault 跨平台核心引擎
支持无限动态 N 仓注册表、系统凭据读写、Electron 会话快照原子切换与旧版四仓无损迁移。
零第三方依赖，仅使用 Python 标准库。
"""

import argparse
import base64
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CRED_TARGET = "gemini:antigravity"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
SESSION_REL_PATHS = [
    os.path.join("Local Storage", "leveldb"),
    "Network",
    "Session Storage",
]


def get_profiles_dir() -> Path:
    """获取多仓状态与快照目录（~/.gemini/antigravity/profiles）。"""
    p = Path.home() / ".gemini" / "antigravity" / "profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_antigravity_data_dir() -> Path:
    """获取 Antigravity Electron 运行态数据目录。"""
    sys_name = platform.system()
    if sys_name == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Antigravity"
    if sys_name == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Antigravity"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "Antigravity"


def extract_email_from_blob(blob: bytes) -> str:
    """从凭据二进制块（JWT id_token 或 JSON 文本）中提取邮箱。"""
    try:
        text = blob.decode("utf-8", errors="ignore")
        m_jwt = re.search(r'"id_token"\s*:\s*"([^"]+)"', text)
        if m_jwt:
            parts = m_jwt.group(1).split(".")
            if len(parts) >= 2:
                payload = parts[1]
                payload += "=" * ((4 - len(payload) % 4) % 4)
                raw = base64.urlsafe_b64decode(payload.encode("ascii"))
                payload_json = raw.decode("utf-8", errors="ignore")
                m_email = re.search(r'"email"\s*:\s*"([^"]+)"', payload_json)
                if m_email:
                    return m_email.group(1)
        m_fallback = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
        if m_fallback:
            return m_fallback.group(0)
    except Exception:
        pass
    return "未知账号"


def mask_email(email: str, enabled: bool) -> str:
    """隐私脱敏：开启后屏蔽真实邮箱。"""
    if not enabled or not email:
        return email
    return "••••••@••••.com (已屏蔽)"


# =============================================================================
# 跨平台系统凭据存储抽象
# =============================================================================

if platform.system() == "Windows":
    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    _advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))
    ]
    _advapi32.CredReadW.restype = wintypes.BOOL
    _advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _advapi32.CredWriteW.restype = wintypes.BOOL
    _advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi32.CredDeleteW.restype = wintypes.BOOL
    _advapi32.CredFree.argtypes = [ctypes.c_void_p]
    _advapi32.CredFree.restype = None


class CredentialStore:
    """跨平台系统凭据读写器（Windows Advapi32 / macOS Keychain / Linux secret-tool）。"""

    @staticmethod
    def read() -> Tuple[Optional[bytes], str]:
        sys_name = platform.system()
        if sys_name == "Windows":
            ptr = ctypes.POINTER(_CREDENTIAL)()
            if not _advapi32.CredReadW(CRED_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
                return None, ""
            try:
                cred = ptr.contents
                size = cred.CredentialBlobSize
                blob = bytes(ctypes.cast(cred.CredentialBlob, ctypes.POINTER(ctypes.c_ubyte * size)).contents)
                return blob, extract_email_from_blob(blob)
            finally:
                _advapi32.CredFree(ptr)

        if sys_name == "Darwin":
            res = subprocess.run(
                ["security", "find-generic-password", "-s", CRED_TARGET, "-a", "antigravity", "-w"],
                capture_output=True, text=True
            )
            if res.returncode != 0 or not res.stdout.strip():
                return None, ""
            raw_str = res.stdout.strip()
            try:
                blob = base64.b64decode(raw_str)
            except Exception:
                blob = raw_str.encode("utf-8")
            return blob, extract_email_from_blob(blob)

        # Linux: secret-tool
        res = subprocess.run(
            ["secret-tool", "lookup", "service", CRED_TARGET, "account", "antigravity"],
            capture_output=True, text=True
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None, ""
        raw_str = res.stdout.strip()
        try:
            blob = base64.b64decode(raw_str)
        except Exception:
            blob = raw_str.encode("utf-8")
        return blob, extract_email_from_blob(blob)

    @staticmethod
    def write(blob: bytes) -> bool:
        sys_name = platform.system()
        if sys_name == "Windows":
            buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
            cred = _CREDENTIAL()
            cred.Flags = 0
            cred.Type = CRED_TYPE_GENERIC
            cred.TargetName = CRED_TARGET
            cred.UserName = "antigravity"
            cred.Persist = CRED_PERSIST_LOCAL_MACHINE
            cred.CredentialBlobSize = len(blob)
            cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
            cred.Comment = "Antigravity Multi-Vault Managed"
            return bool(_advapi32.CredWriteW(ctypes.byref(cred), 0))

        encoded = base64.b64encode(blob).decode("ascii")
        if sys_name == "Darwin":
            res = subprocess.run(
                ["security", "add-generic-password", "-U", "-s", CRED_TARGET, "-a", "antigravity", "-w", encoded],
                capture_output=True, text=True
            )
            return res.returncode == 0

        res = subprocess.run(
            ["secret-tool", "store", "--label", "Antigravity Multi-Vault", "service", CRED_TARGET, "account", "antigravity"],
            input=encoded, capture_output=True, text=True
        )
        return res.returncode == 0

    @staticmethod
    def delete() -> bool:
        sys_name = platform.system()
        if sys_name == "Windows":
            return bool(_advapi32.CredDeleteW(CRED_TARGET, CRED_TYPE_GENERIC, 0))
        if sys_name == "Darwin":
            res = subprocess.run(
                ["security", "delete-generic-password", "-s", CRED_TARGET, "-a", "antigravity"],
                capture_output=True, text=True
            )
            return res.returncode == 0
        res = subprocess.run(
            ["secret-tool", "clear", "service", CRED_TARGET, "account", "antigravity"],
            capture_output=True, text=True
        )
        return res.returncode == 0


# =============================================================================
# 动态 N 仓注册表管理
# =============================================================================

def normalize_slot_id(slot_id: str) -> str:
    """规范化仓位 ID（小写字母/数字/短横线/下划线）。"""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", slot_id.strip()).lower()
    if not cleaned:
        raise ValueError(f"无效的仓位 ID: {slot_id}")
    return cleaned


def load_registry(profiles_dir: Path) -> Dict[str, Any]:
    reg_file = profiles_dir / "registry.json"
    if reg_file.exists():
        try:
            return json.loads(reg_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "primarySlot": "", "slots": []}


def save_registry(profiles_dir: Path, reg: Dict[str, Any]) -> None:
    reg_file = profiles_dir / "registry.json"
    reg_file.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def find_slot(reg: Dict[str, Any], slot_id: str) -> Optional[Dict[str, Any]]:
    for s in reg.get("slots", []):
        if s.get("id", "").lower() == slot_id.lower():
            return s
    return None


def upsert_slot(reg: Dict[str, Any], slot_id: str, email: str = "", label: str = "") -> Dict[str, Any]:
    existing = find_slot(reg, slot_id)
    now_ms = int(time.time() * 1000)
    if existing:
        if email:
            existing["email"] = email
        if label:
            existing["label"] = label
        existing["updatedAt"] = now_ms
        return existing

    entry = {
        "id": slot_id,
        "label": label or f"仓位 {slot_id.upper()}",
        "email": email,
        "createdAt": now_ms,
        "updatedAt": now_ms,
    }
    reg.setdefault("slots", []).append(entry)
    if not reg.get("primarySlot"):
        reg["primarySlot"] = slot_id
    return entry


def migrate_legacy_four_slots(profiles_dir: Path) -> Dict[str, Any]:
    """将旧版 A/B/C/D 四仓数据无损导入 registry.json（保留原文件以便兼容）。"""
    reg = load_registry(profiles_dir)
    legacy_ids = ["a", "b", "c", "d"]
    migrated = []

    cfg_file = profiles_dir / "vault_config.json"
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            prim = str(cfg.get("primarySlot", "")).strip().lower()
            if prim:
                reg["primarySlot"] = prim
        except Exception:
            pass

    for idx, sid in enumerate(legacy_ids):
        dat_file = profiles_dir / f"profile_{sid}.dat"
        email_file = profiles_dir / f"profile_{sid}.email"
        session_dir = profiles_dir / f"session_{sid}"
        if dat_file.exists() or email_file.exists() or session_dir.exists():
            email = email_file.read_text(encoding="utf-8", errors="ignore").strip() if email_file.exists() else ""
            default_label = "主力仓" if sid == reg.get("primarySlot") else f"仓位 {sid.upper()}"
            upsert_slot(reg, sid, email=email, label=default_label)
            migrated.append(sid)

    save_registry(profiles_dir, reg)
    return {"success": True, "migrated": migrated, "totalSlots": len(reg.get("slots", []))}


# =============================================================================
# Electron 会话快照与原子切换
# =============================================================================

def _copy_dir_skip_lock(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name.upper() == "LOCK":
            continue
        if item.is_file():
            try:
                shutil.copy2(item, dst / item.name)
            except Exception:
                pass


def backup_session(profiles_dir: Path, slot_id: str) -> None:
    """备份 Electron 会话（Local Storage/leveldb + Network/Cookies + Session Storage）。"""
    agy_dir = get_antigravity_data_dir()
    if not agy_dir.exists():
        return

    session_dir = profiles_dir / f"session_{slot_id}"
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
    session_dir.mkdir(parents=True, exist_ok=True)

    _copy_dir_skip_lock(agy_dir / "Local Storage" / "leveldb", session_dir / "Local Storage" / "leveldb")
    _copy_dir_skip_lock(agy_dir / "Session Storage", session_dir / "Session Storage")

    cookies_src = agy_dir / "Network" / "Cookies"
    if cookies_src.exists():
        net_dst = session_dir / "Network"
        net_dst.mkdir(parents=True, exist_ok=True)
        for fname in ("Cookies", "Cookies-journal"):
            f_src = agy_dir / "Network" / fname
            if f_src.exists():
                try:
                    shutil.copyfile(f_src, net_dst / fname)
                except Exception:
                    pass


def restore_session(profiles_dir: Path, slot_id: str) -> bool:
    """原子还原目标槽位的 Electron 会话，同时将当前会话移入 restore_backups/ 保留。"""
    session_dir = profiles_dir / f"session_{slot_id}"
    if not session_dir.exists():
        return False

    agy_dir = get_antigravity_data_dir()
    agy_dir.mkdir(parents=True, exist_ok=True)

    outgoing_dir = profiles_dir / "restore_backups" / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    for rel in SESSION_REL_PATHS:
        cur = agy_dir / rel
        if cur.exists():
            retained = outgoing_dir / rel
            retained.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cur), str(retained))

    _copy_dir_skip_lock(session_dir / "Local Storage" / "leveldb", agy_dir / "Local Storage" / "leveldb")
    _copy_dir_skip_lock(session_dir / "Session Storage", agy_dir / "Session Storage")

    net_src = session_dir / "Network"
    if net_src.exists():
        net_dst = agy_dir / "Network"
        net_dst.mkdir(parents=True, exist_ok=True)
        for fname in ("Cookies", "Cookies-journal"):
            f_src = net_src / fname
            if f_src.exists():
                shutil.copy2(f_src, net_dst / fname)

    return True


def _stop_antigravity_processes() -> None:
    """安全终止 Antigravity 前端及对应 language_server 进程以释放文件锁。"""
    sys_name = platform.system()
    if sys_name == "Windows":
        subprocess.run(["taskkill", "/F", "/IM", "Antigravity.exe"], capture_output=True)
        local_appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        owned_srv = str(Path(local_appdata) / "Programs" / "antigravity" / "resources" / "bin" / "language_server.exe")
        ps_cmd = (
            f"Get-CimInstance Win32_Process -Filter \"Name='language_server.exe'\" | "
            f"Where-Object {{ $_.ExecutablePath -eq '{owned_srv}' }} | "
            f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True)
    elif sys_name == "Darwin":
        subprocess.run(["pkill", "-x", "Antigravity"], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", "antigravity"], capture_output=True)
    time.sleep(0.8)


def _launch_antigravity() -> None:
    """脱离当前进程句柄独立启动 Antigravity。"""
    sys_name = platform.system()
    if sys_name == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        launcher = Path.home() / ".gemini" / "antigravity" / "bin" / "AntigravityLauncher.exe"
        app_exe = Path(local_appdata) / "Programs" / "antigravity" / "Antigravity.exe"
        target = launcher if launcher.exists() else app_exe
        if target.exists():
            DETACHED = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                [str(target), "--no-sandbox"],
                cwd=str(target.parent),
                creationflags=DETACHED | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    elif sys_name == "Darwin":
        subprocess.Popen(["open", "-a", "Antigravity"], start_new_session=True)
    else:
        subprocess.Popen(["antigravity", "--no-sandbox"], start_new_session=True)


# =============================================================================
# 业务命令实现
# =============================================================================

def cmd_status(mask: bool = False) -> Dict[str, Any]:
    profiles_dir = get_profiles_dir()
    reg = load_registry(profiles_dir)

    # 若注册表为空但存在旧版四仓数据，自动发现但不写盘，提示用户可迁移
    if not reg.get("slots"):
        for sid in ("a", "b", "c", "d"):
            if (profiles_dir / f"profile_{sid}.dat").exists():
                email_f = profiles_dir / f"profile_{sid}.email"
                mail = email_f.read_text(encoding="utf-8", errors="ignore").strip() if email_f.exists() else ""
                upsert_slot(reg, sid, email=mail, label=f"仓位 {sid.upper()}")

    _, cur_email = CredentialStore.read()
    slots_out: List[Dict[str, Any]] = []
    active_slot = ""

    for slot in reg.get("slots", []):
        sid = slot["id"]
        dat_exists = (profiles_dir / f"profile_{sid}.dat").exists()
        session_exists = (profiles_dir / f"session_{sid}").exists()
        email_f = profiles_dir / f"profile_{sid}.email"
        mail = email_f.read_text(encoding="utf-8", errors="ignore").strip() if email_f.exists() else slot.get("email", "")
        is_active = bool(cur_email and mail and cur_email.lower() == mail.lower())
        if is_active:
            active_slot = sid

        quota_file = profiles_dir / f"quota_{sid}.json"
        quota = {}
        if quota_file.exists():
            try:
                quota = json.loads(quota_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        slots_out.append({
            "id": sid,
            "label": slot.get("label", sid),
            "email": mask_email(mail, mask),
            "saved": dat_exists,
            "hasSession": session_exists,
            "isPrimary": sid == reg.get("primarySlot"),
            "isActive": is_active,
            "quota": quota,
        })

    return {
        "platform": platform.system(),
        "currentEmail": mask_email(cur_email, mask),
        "activeSlot": active_slot,
        "primarySlot": reg.get("primarySlot", ""),
        "privacyMasked": mask,
        "totalSlots": len(slots_out),
        "slots": slots_out,
    }


def cmd_save(slot_id: str, label: str = "", mask: bool = False) -> Dict[str, Any]:
    sid = normalize_slot_id(slot_id)
    profiles_dir = get_profiles_dir()
    blob, email = CredentialStore.read()
    if not blob:
        return {"success": False, "error": "无法读取当前系统登录凭据"}

    (profiles_dir / f"profile_{sid}.dat").write_bytes(blob)
    (profiles_dir / f"profile_{sid}.email").write_text(email, encoding="utf-8")
    backup_session(profiles_dir, sid)

    reg = load_registry(profiles_dir)
    upsert_slot(reg, sid, email=email, label=label)
    save_registry(profiles_dir, reg)

    return {"success": True, "slot": sid, "email": mask_email(email, mask)}


def cmd_switch_restart(slot_id: str, no_restart: bool = False, mask: bool = False) -> Dict[str, Any]:
    sid = normalize_slot_id(slot_id)
    profiles_dir = get_profiles_dir()
    dat_file = profiles_dir / f"profile_{sid}.dat"
    if not dat_file.exists():
        return {"success": False, "error": f"仓位 {sid} 快照不存在，请先执行 save"}
    if not (profiles_dir / f"session_{sid}").exists():
        return {"success": False, "error": f"仓位 {sid} 缺少 session 快照，请先重新存入"}

    if not no_restart:
        _stop_antigravity_processes()

    if not restore_session(profiles_dir, sid):
        return {"success": False, "error": f"还原仓位 {sid} 会话快照失败"}

    blob = dat_file.read_bytes()
    if not CredentialStore.write(blob):
        return {"success": False, "error": "写入系统凭据管理器失败"}

    q_cur = profiles_dir / "quota_current.json"
    q_cur.write_text('{"fiveHourLimit":"","weeklyLimit":"","updatedAt":0}', encoding="utf-8")

    if not no_restart:
        _launch_antigravity()

    email_f = profiles_dir / f"profile_{sid}.email"
    mail = email_f.read_text(encoding="utf-8", errors="ignore").strip() if email_f.exists() else ""
    return {"success": True, "slot": sid, "email": mask_email(mail, mask), "restarted": not no_restart}


def cmd_remove(slot_id: str, purge: bool = False) -> Dict[str, Any]:
    sid = normalize_slot_id(slot_id)
    profiles_dir = get_profiles_dir()
    reg = load_registry(profiles_dir)
    before = len(reg.get("slots", []))
    reg["slots"] = [s for s in reg.get("slots", []) if s.get("id") != sid]
    if reg.get("primarySlot") == sid:
        reg["primarySlot"] = reg["slots"][0]["id"] if reg["slots"] else ""
    save_registry(profiles_dir, reg)

    purged_files: List[str] = []
    if purge:
        for fname in (f"profile_{sid}.dat", f"profile_{sid}.email", f"quota_{sid}.json"):
            fpath = profiles_dir / fname
            if fpath.exists():
                fpath.unlink()
                purged_files.append(fname)
        sdir = profiles_dir / f"session_{sid}"
        if sdir.exists():
            shutil.rmtree(sdir, ignore_errors=True)
            purged_files.append(f"session_{sid}/")

    return {"success": len(reg["slots"]) < before or bool(purged_files), "removed": sid, "purged": purged_files}


def cmd_rename(slot_id: str, label: str) -> Dict[str, Any]:
    sid = normalize_slot_id(slot_id)
    profiles_dir = get_profiles_dir()
    reg = load_registry(profiles_dir)
    slot = find_slot(reg, sid)
    if not slot:
        return {"success": False, "error": f"仓位 {sid} 未注册"}
    slot["label"] = label
    slot["updatedAt"] = int(time.time() * 1000)
    save_registry(profiles_dir, reg)
    return {"success": True, "slot": sid, "label": label}


def cmd_prepare_new(no_restart: bool = False) -> Dict[str, Any]:
    """暂存当前运行态会话到 restore_backups/，清空登录态并拉起空白 Antigravity 以便登录新账号。"""
    profiles_dir = get_profiles_dir()
    agy_dir = get_antigravity_data_dir()

    if not no_restart:
        _stop_antigravity_processes()

    # 将当前活跃会话安全移出到 restore_backups/，绝不直接删除
    backed_up_to = ""
    if agy_dir.exists():
        outgoing_dir = profiles_dir / "restore_backups" / f"pre_new_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        moved = False
        for rel in SESSION_REL_PATHS:
            cur = agy_dir / rel
            if cur.exists():
                retained = outgoing_dir / rel
                retained.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(cur), str(retained))
                moved = True
        if moved:
            backed_up_to = str(outgoing_dir)

    CredentialStore.delete()
    (profiles_dir / "quota_current.json").write_text('{"fiveHourLimit":"","weeklyLimit":"","updatedAt":0}', encoding="utf-8")

    if not no_restart:
        _launch_antigravity()

    return {
        "success": True,
        "backupDir": backed_up_to,
        "nextStep": "请在打开的 Antigravity 中登录新账号，登录完成后运行 `python scripts/vault_core.py save <新仓位ID> --label <别名>` 即可入仓。",
    }


def cmd_set_primary(slot_id: str) -> Dict[str, Any]:
    sid = normalize_slot_id(slot_id)
    profiles_dir = get_profiles_dir()
    reg = load_registry(profiles_dir)
    if not find_slot(reg, sid):
        return {"success": False, "error": f"仓位 {sid} 未注册"}
    reg["primarySlot"] = sid
    save_registry(profiles_dir, reg)
    return {"success": True, "primarySlot": sid}


def main() -> None:
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Antigravity Multi-Vault 跨平台多仓管理内核")
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="查看全部仓位状态")
    p_status.add_argument("--mask", action="store_true", help="演示隐私模式：隐藏账号邮箱")

    p_save = sub.add_parser("save", help="将当前登录账号存入指定仓位")
    p_save.add_argument("slot", help="仓位 ID（如 slot-1, a, work）")
    p_save.add_argument("--label", default="", help="仓位自定义别名")
    p_save.add_argument("--mask", action="store_true", help="输出脱敏邮箱")

    p_sw = sub.add_parser("switch-restart", help="原子切换至指定仓位并重启应用")
    p_sw.add_argument("slot", help="目标仓位 ID")
    p_sw.add_argument("--no-restart", action="store_true", help="仅替换凭据与会话，不重启进程")
    p_sw.add_argument("--mask", action="store_true", help="输出脱敏邮箱")

    p_rm = sub.add_parser("remove", help="从注册表移除仓位")
    p_rm.add_argument("slot", help="仓位 ID")
    p_rm.add_argument("--purge", action="store_true", help="同时彻底删除该仓位的凭据与会话快照文件")

    p_ren = sub.add_parser("rename", help="修改仓位别名")
    p_ren.add_argument("slot", help="仓位 ID")
    p_ren.add_argument("label", help="新别名")

    p_prep = sub.add_parser("prepare-new", help="安全暂存当前会话并清空登录态，拉起空白 Antigravity 以便登录新账号")
    p_prep.add_argument("--no-restart", action="store_true", help="仅清理运行态，不自动重启进程")

    p_prim = sub.add_parser("set-primary", help="设置主力仓位")
    p_prim.add_argument("slot", help="仓位 ID")

    sub.add_parser("migrate-legacy", help="从旧版 A/B/C/D 四仓无损迁移至 registry.json")

    args = parser.parse_args()
    cmd = args.command or "status"

    if cmd == "status":
        res = cmd_status(mask=getattr(args, "mask", False))
    elif cmd == "save":
        res = cmd_save(args.slot, label=args.label, mask=args.mask)
    elif cmd == "switch-restart":
        res = cmd_switch_restart(args.slot, no_restart=args.no_restart, mask=args.mask)
    elif cmd == "remove":
        res = cmd_remove(args.slot, purge=args.purge)
    elif cmd == "rename":
        res = cmd_rename(args.slot, args.label)
    elif cmd == "prepare-new":
        res = cmd_prepare_new(no_restart=args.no_restart)
    elif cmd == "set-primary":
        res = cmd_set_primary(args.slot)
    elif cmd == "migrate-legacy":
        res = migrate_legacy_four_slots(get_profiles_dir())
    else:
        res = {"error": f"未知命令: {cmd}"}

    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
