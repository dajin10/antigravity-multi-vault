---
name: antigravity-multi-vault
description: "跨平台 Antigravity 多账号工作台 Skill（Multi-Vault）。支持由 AI Agent 全自动安装依赖、挂载并启动 PySide6 桌面工作台，从 1 个账号起步自由增删扩展至 N 仓，实现原子会话切换与演示隐私脱敏。触发词：安装多仓、启动多仓工作台、多仓切换、新增账号、删除仓位、查看账号额度、切换账号。"
---

# Antigravity Multi-Vault（Agent 全自动部署与多仓调度协议）

本 Skill 面向 AI Agent 优先设计：**安装、依赖修复、工作台拉起、账号初始化与日常切换均由 Agent 闭环执行，不要求用户手动敲命令行。**

---

## 一、自动部署与自愈协议（Bootstrap & Health Check）

当用户说“安装/部署/启动多仓工作台”，或首次调用本 Skill 发现环境未就绪时，依次执行：

1. **检查桌面工作台健康状态**：
   - 请求 `http://127.0.0.1:28888/status`；
   - 若已返回 `{"ok": true}`，只需访问 `http://127.0.0.1:28888/toggle` 即可呼出窗口，无需重复启动。
2. **检查依赖并安装（若未运行）**：
   - 校验 `python -c "import PySide6, PySide6.QtWebEngineWidgets"`；缺失则自动执行 `pip install PySide6`。
3. **初始化仓位数据**：
   - 运行 `python scripts/vault_core.py status --mask`；
   - 若存在旧版 `a/b/c/d` 数据且无 `registry.json`，自动运行 `python scripts/vault_core.py migrate-legacy`；
   - 若 `totalSlots == 0` 且当前已登录账号，询问或直接运行 `python scripts/vault_core.py save slot-1 --label "主力仓"`。
4. **后台拉起工作台**：
   - Windows 使用 `pythonw scripts/companion_app.py`（`Start-Process -WindowStyle Hidden`），macOS/Linux 使用 `nohup python3 scripts/companion_app.py >/dev/null 2>&1 &`；
   - 等待 2~3 秒后再次探针 `http://127.0.0.1:28888/status` 确认 `"ok": true`。

---

## 二、日常调度路由（从 1 仓到 N 仓）

| 用户意图 | 执行命令 | 说明 |
| :--- | :--- | :--- |
| 呼出 / 隐藏桌面工作台 | 请求 `http://127.0.0.1:28888/toggle` | 秒级切换显示状态，未启动时先按“一、4”拉起 |
| 查看全部仓位状态 | `python scripts/vault_core.py status --mask` | 默认使用 `--mask` 脱敏输出，避免聊天记录暴露邮箱 |
| 归档当前账号为第 1/N 仓 | `python scripts/vault_core.py save <slot_id> --label "<别名>"` | 保存当前系统凭据与 Electron 会话快照 |
| 准备登录全新账号（空白沙盒） | `python scripts/vault_core.py prepare-new` | **需用户确认**：暂存当前会话至 `restore_backups/` 并拉起空白登录窗 |
| 切换到指定仓位并重启 | `python scripts/vault_core.py switch-restart <slot_id>` | **需用户确认**：原子替换凭据与会话并重启 Antigravity |
| 修改仓位别名 | `python scripts/vault_core.py rename <slot_id> "<新别名>"` | 更新 `registry.json` |
| 移除或彻底删除仓位 | `python scripts/vault_core.py remove <slot_id> [--purge]` | 加 `--purge` 时同步删除本地凭据与会话快照（至少保留 1 仓） |

---

## 三、安全铁律

1. **强重启前必须确认**：`prepare-new` 与 `switch-restart` 会终止正在运行的 `Antigravity` 与 `language_server` 进程。执行前先向用户确认，或在用户明确下达“立即切到 X / 帮我开启空白登录”时执行。
2. **禁止覆盖未归档账号**：切走或清空前检查当前在席账号是否已存入某个仓位；若未入仓，先提示或执行 `save`。
3. **原子回滚保护**：每次切换前，内核自动将当前 `Local Storage/leveldb`、`Network`、`Session Storage` 移入 `profiles/restore_backups/` 保留。
4. **隐私脱敏优先**：调用 `status` 汇报给用户或对外演示时优先附加 `--mask`。
