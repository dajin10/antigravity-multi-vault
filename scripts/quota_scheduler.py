#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Multi-Vault 智能接力调度器 (Best-Next Quota Scheduler)
综合 5 小时限额、每周限额与冷却恢复时间衰减推算，给出最优切号建议。
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from vault_core import cmd_status, mask_email


def parse_reset_minutes(reset_str: str) -> Optional[float]:
    """将中文/英文恢复倒计时文本（如 '4 分', '3 小时18 分', '6 天10 小时'）转为分钟。"""
    if not reset_str:
        return None
    total_min = 0.0
    matched = False
    for pattern, mult in [
        (r"(\d+)\s*(?:天|d)", 1440.0),
        (r"(\d+)\s*(?:小时|h)", 60.0),
        (r"(\d+)\s*(?:分|m)", 1.0),
    ]:
        m = re.search(pattern, reset_str, re.IGNORECASE)
        if m:
            total_min += float(m.group(1)) * mult
            matched = True
    return total_min if matched else None


def evaluate_slot(slot: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    """计算单个仓位的实时可用性评分与恢复推算。"""
    quota = slot.get("quota") or {}
    pct_5h = quota.get("fiveHourPct", -1)
    pct_wk = quota.get("weeklyPct", -1)
    reset_5h_str = quota.get("fiveHourReset", "")
    updated_at = quota.get("updatedAt", 0) or 0

    # 若无配额文件，默认视为满额/未知待刷新
    eff_5h = 100 if pct_5h < 0 else pct_5h
    eff_wk = 100 if pct_wk < 0 else pct_wk
    recovered_by_time = False

    # 时间衰减推算：若上次更新后已过 5h 恢复倒计时，推算 5h 额度已回满
    reset_min = parse_reset_minutes(reset_5h_str)
    if updated_at > 0 and reset_min is not None and eff_5h < 100:
        elapsed_min = (now_ms - updated_at) / 60000.0
        if elapsed_min >= reset_min:
            eff_5h = 100
            recovered_by_time = True

    # 综合健康分：5h 额度占 70%，周额度占 30%；若周额度耗尽则直接降为 0
    if eff_wk <= 0:
        score = 0.0
    else:
        score = round(eff_5h * 0.7 + eff_wk * 0.3, 2)

    if eff_5h <= 0 or eff_wk <= 0:
        tier = "已用尽"
    elif eff_5h <= 20:
        tier = "紧张"
    elif eff_5h <= 40:
        tier = "注意"
    elif eff_5h <= 70:
        tier = "良好"
    else:
        tier = "充裕"

    return {
        "id": slot["id"],
        "label": slot.get("label", slot["id"]),
        "email": slot.get("email", ""),
        "isActive": slot.get("isActive", False),
        "isPrimary": slot.get("isPrimary", False),
        "saved": slot.get("saved", False),
        "hasSession": slot.get("hasSession", False),
        "effective5hPct": eff_5h,
        "effectiveWeeklyPct": eff_wk,
        "recoveredByTimeEstimate": recovered_by_time,
        "fiveHourReset": reset_5h_str,
        "tier": tier,
        "score": score,
    }


def recommend_best_next(mask: bool = False) -> Dict[str, Any]:
    status = cmd_status(mask=mask)
    now_ms = int(time.time() * 1000)

    evaluated: List[Dict[str, Any]] = []
    for s in status.get("slots", []):
        if s.get("saved") and s.get("hasSession"):
            evaluated.append(evaluate_slot(s, now_ms))

    # 过滤掉当前正在使用的仓位，按综合分降序、主力优先排序
    candidates = [e for e in evaluated if not e["isActive"] and e["score"] > 0]
    candidates.sort(key=lambda x: (x["score"], 1 if x["isPrimary"] else 0), reverse=True)

    current = next((e for e in evaluated if e["isActive"]), None)
    best = candidates[0] if candidates else None

    reason = ""
    if current and current["effective5hPct"] <= 20 and best:
        reason = (
            f"当前仓 [{current['id']}] 5h 额度{current['tier']} ({current['effective5hPct']}%)，"
            f"推荐接力至 [{best['id']}] ({best['label']}，5h {best['effective5hPct']}% / 周 {best['effectiveWeeklyPct']}%)。"
        )
    elif best:
        reason = (
            f"当前仓 [{current['id'] if current else '未知'}] 状态正常；"
            f"最优备用接力仓为 [{best['id']}] ({best['label']}，综合分 {best['score']})。"
        )
    else:
        reason = "暂无可用的高额度备用仓位。"

    return {
        "activeSlot": status.get("activeSlot", ""),
        "currentEmail": status.get("currentEmail", ""),
        "privacyMasked": mask,
        "recommendedSlot": best["id"] if best else None,
        "reason": reason,
        "rankedSlots": sorted(evaluated, key=lambda x: x["score"], reverse=True),
    }


def main() -> None:
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Antigravity Multi-Vault 智能接力推荐")
    parser.add_argument("--recommend", action="store_true", help="输出 Best-Next 接力推荐")
    parser.add_argument("--mask", action="store_true", help="演示隐私模式：隐藏账号邮箱")
    parser.add_argument("--json", action="store_true", help="以纯 JSON 格式输出")
    args = parser.parse_args()

    result = recommend_best_next(mask=args.mask)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"=== Antigravity Multi-Vault 智能调度报告 ===")
    print(f"当前在席: {result['activeSlot'] or '未知'} ({result['currentEmail'] or '未登录'})")
    print(f"调度建议: {result['reason']}\n")
    for item in result["rankedSlots"]:
        tag = " [当前在席]" if item["isActive"] else (" [★主力]" if item["isPrimary"] else "")
        est = " (推算已恢复)" if item["recoveredByTimeEstimate"] else ""
        print(
            f"  - [{item['id']}] {item['label']}{tag}: "
            f"5h={item['effective5hPct']}%{est} | 周={item['effectiveWeeklyPct']}% | "
            f"等级={item['tier']} | 综合分={item['score']} | {item['email']}"
        )


if __name__ == "__main__":
    main()
