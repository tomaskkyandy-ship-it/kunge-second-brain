#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
坤哥的第二大脑 · 周度 CIO 战略复盘与贝叶斯审计引擎 (v4.0)
运行时间：每周日北京时间 20:00（GitHub Actions UTC 12:00）或手动触发
功能：
1. 读取 05_每日情报/data/market_history.jsonl 最近 7 天结构化时序数据
2. 汇总本周宏观象限迁移、BTC 6 维流动性雷达轨迹、全池 6 大精锐周涨跌与 RSI 演变
3. 梳理未来 30 天排雷日历与本周触发过买点/超卖区的标的复盘
4. 调用 Gemini 生成《周度 CIO 战略复盘》（若 AI 失败则自动降级输出纯数据保底周报）
5. 写入 05_每日情报/周报/YYYY-Www_周度CIO战略复盘.md，更新总览索引并推送 iPhone Bark
"""

import os
import re
import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import yaml
import requests

BJT = timezone(timedelta(hours=8))
NOW_BJT = datetime.now(BJT)
ISO_YEAR, ISO_WEEK, _ = NOW_BJT.isocalendar()
WEEK_TAG = f"{ISO_YEAR}-W{ISO_WEEK:02d}"
TODAY_STR = NOW_BJT.strftime("%Y-%m-%d")

SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = SCRIPT_DIR.parents[2]
RESEARCH_DIR = VAULT_ROOT / "02_投资与分析" / "03_个股研究与股票池"
DAILY_DIR = VAULT_ROOT / "02_投资与分析" / "05_每日情报"
WEEKLY_DIR = DAILY_DIR / "周报"
HISTORY_FILE = DAILY_DIR / "data" / "market_history.jsonl"


def load_weekly_history(days: int = 7) -> list[dict]:
    """加载最近 N 天的结构化时序记录"""
    records = []
    if not HISTORY_FILE.exists():
        return records
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("date"):
                records.append(obj)
        except Exception:
            continue
    records.sort(key=lambda x: x["date"])
    return records[-days:]


def load_active_targets() -> list[dict]:
    """加载当前股票池所有活跃标的"""
    targets = []
    skip = {"股票池.md", "新物种与候选猎物雷达池.md", "_选股分析模板.md"}
    if not RESEARCH_DIR.exists():
        return targets
    for f in sorted(RESEARCH_DIR.glob("*.md")):
        if f.name in skip:
            continue
        text = f.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except Exception:
            meta = {}
        ticker = str(meta.get("代码") or meta.get("ticker") or "").strip()
        status_str = str(meta.get("状态") or meta.get("status") or "")
        if not ticker or "放弃" in status_str:
            continue
        targets.append({
            "file_name": f.stem,
            "ticker": ticker,
            "pool_type": str(meta.get("所属池子") or meta.get("pool_type") or ""),
            "status": status_str,
            "score_rate": meta.get("综合分%") if meta.get("综合分%") is not None else meta.get("score_rate", ""),
            "target_buy_price": str(meta.get("🟢学徒买点") or meta.get("target_buy_price") or ""),
            "target_sell_price": str(meta.get("🔴师傅卖点") or meta.get("target_sell_price") or ""),
            "next_risk_date": str(meta.get("💣排雷日历") or meta.get("next_risk_date") or ""),
            "verdict": str(meta.get("结论") or meta.get("verdict") or ""),
        })
    return targets


def build_weekly_quant_summary(history: list[dict], targets: list[dict]) -> dict:
    """用程序精确计算本周量化对比表（杜绝大模型算错涨跌幅与天数）"""
    summary = {
        "week_tag": WEEK_TAG,
        "date_range": f"{history[0]['date']} 至 {history[-1]['date']}" if history else TODAY_STR,
        "days_covered": len(history),
        "macro_weekly_shift": {},
        "btc_radar_weekly_trajectory": [],
        "targets_weekly_performance": [],
        "upcoming_risks_30d": [],
    }

    if history:
        first_m = history[0].get("macro", {})
        last_m = history[-1].get("macro", {})
        for k, label, unit in [
            ("tnx_10y", "10Y美债收益率", "%"),
            ("dxy", "美元指数DXY", ""),
            ("wti", "WTI原油", "$"),
            ("gold", "COMEX黄金", "$"),
            ("gold_oil_ratio", "金油比", ""),
            ("usdc_mcap_b", "USDC市值", "B"),
            ("fear_greed", "加密恐慌贪婪指数", ""),
        ]:
            v1, v2 = first_m.get(k), last_m.get(k)
            if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                diff = round(v2 - v1, 2)
                summary["macro_weekly_shift"][label] = f"{v1}{unit} → {v2}{unit} (周变动 {diff:+.2f}{unit})"

        for rec in history:
            r = rec.get("btc_radar", {})
            summary["btc_radar_weekly_trajectory"].append(
                f"{rec['date']}: {r.get('green_lights', '?')}/6 绿灯 ({r.get('multiplier', 'N/A')})"
            )

    # 个股周度表现与 RSI 演变
    for t in targets:
        tk = t["ticker"]
        prices = [rec.get("targets", {}).get(tk, {}).get("price") for rec in history]
        prices = [p for p in prices if isinstance(p, (int, float))]
        rsis = [rec.get("targets", {}).get(tk, {}).get("rsi_14") for rec in history]
        rsis = [r for r in rsis if isinstance(r, (int, float))]
        oversold_days = sum(1 for r in rsis if r <= 30)

        wk_chg = round((prices[-1] - prices[0]) / prices[0] * 100, 2) if len(prices) >= 2 and prices[0] > 0 else 0.0
        summary["targets_weekly_performance"].append({
            "file_name": t["file_name"],
            "ticker": tk,
            "pool_type": t["pool_type"],
            "status": t["status"],
            "score_rate": t["score_rate"],
            "week_open_price": prices[0] if prices else None,
            "week_close_price": prices[-1] if prices else None,
            "week_change_pct": f"{wk_chg:+.2f}%",
            "latest_rsi": rsis[-1] if rsis else None,
            "oversold_days_this_week": oversold_days,
            "target_buy_price": t["target_buy_price"],
        })

        # 未来 30 天排雷日历
        m_d = re.search(r"(\d{4}-\d{2}-\d{2})", t["next_risk_date"])
        if m_d:
            try:
                rd = datetime.strptime(m_d.group(1), "%Y-%m-%d").date()
                days_left = (rd - NOW_BJT.date()).days
                if days_left <= 30:
                    summary["upcoming_risks_30d"].append({
                        "file_name": t["file_name"],
                        "risk_date": m_d.group(1),
                        "days_left": days_left,
                    })
            except Exception:
                pass

    return summary


def call_gemini(api_key: str, prompt: str) -> str:
    models = [
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-3.8-flash",
        "gemini-3.1-flash-lite",
        "gemini-flash-lite-latest",
    ]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.25, "maxOutputTokens": 12288},
    }
    last_err = ""
    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        try:
            resp = requests.post(url, json=payload, headers={"x-goog-api-key": api_key}, timeout=90)
            if resp.status_code == 200:
                parts = resp.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought", False))
                if text.strip():
                    return text
            last_err = f"{m} HTTP {resp.status_code}"
        except Exception as e:
            last_err = f"{m}: {str(e).replace(api_key, '***')}"
        time.sleep(2)
    raise RuntimeError(f"周报大模型调用失败: {last_err}")


def build_fallback_weekly_md(summary: dict) -> str:
    rows = []
    for item in summary["targets_weekly_performance"]:
        p1 = f"\\${item['week_open_price']:,.2f}" if item["week_open_price"] else "N/A"
        p2 = f"\\${item['week_close_price']:,.2f}" if item["week_close_price"] else "N/A"
        rows.append(
            f"| [[{item['file_name']}]] | {item['pool_type']} | {item['status']} | {p1} → {p2} ({item['week_change_pct']}) | RSI {item['latest_rsi']}（本周超卖 {item['oversold_days_this_week']} 天） | {item['target_buy_price']} |"
        )
    macro_lines = "\n".join(f"- **{k}**：{v}" for k, v in summary["macro_weekly_shift"].items())
    return (
        f"## 一、📊 本周全池相对强弱与量价复盘（{summary['date_range']}）\n\n"
        f"| 标的 | 所属池子 | 状态 | 周初 → 周末 (周涨跌) | 最新 RSI & 超卖天数 | 🟢学徒买点 |\n"
        f"| :--- | :--- | :---: | :--- | :--- | :--- |\n"
        + "\n".join(rows)
        + f"\n\n## 二、🧭 本周宏观象限与流动性迁移\n\n{macro_lines}\n\n"
        f"**BTC 流动性雷达轨迹**：" + " → ".join(summary["btc_radar_weekly_trajectory"]) + "\n"
    )


def send_bark(title: str, body: str):
    bark_key = os.environ.get("BARK_KEY", "").strip()
    if not bark_key:
        return
    if bark_key.startswith("http"):
        parts = bark_key.rstrip("/").split("/")
        server, key = "/".join(parts[:3]), (parts[3] if len(parts) > 3 else "")
    else:
        server, key = "https://api.day.app", bark_key.strip("/")
    payload = {
        "device_key": key,
        "title": title,
        "body": body,
        "group": "坤哥投资情报",
        "level": "active",
    }
    try:
        requests.post(f"{server}/push", json=payload, timeout=15)
    except Exception:
        pass


def main():
    print(f"📅 开始生成【{WEEK_TAG} 周度 CIO 战略复盘】...")
    history = load_weekly_history(7)
    targets = load_active_targets()
    summary = build_weekly_quant_summary(history, targets)

    prompt = f"""你是「坤哥的第二大脑」首席投资官（CIO）。今天是 {TODAY_STR}（{WEEK_TAG} 周日战略复盘）。
请基于以下程序精确核算的本周 7 日量化时序数据，撰写一份极高含金量、去噪音的《周度 CIO 战略复盘》：

```json
{json.dumps(summary, ensure_ascii=False, indent=2)}
```

⚠️ 严格格式要求：
1. 正文直接从 `## 一、📊 本周全池相对强弱与量价复盘` 开始，**严禁自行输出顶部 `---` YAML 前言**！
2. 所有美元符号必须转义为 `\\$`，双链必须是干净的 `[[Bitcoin BTC]]`，严禁包裹反引号！
3. 必须包含以下 4 大章节：
   - `## 一、📊 本周全池相对强弱与量价复盘`（完整 6 列表格：标的 | 所属池子 | 状态 | 周初→周末(周涨跌) | RSI与本周超卖天数 | 🟢下周挂单与动作定调）
   - `## 二、🧭 本周宏观象限迁移与 BTC 流动性雷达复盘`（解读 10Y美债、DXY、WTI、金油比、USDC市值、恐慌指数的周变动，以及 6 维绿灯轨迹对应的仓位乘数纪律）
   - `## 三、💣 未来 30 天排雷日历与仓位纪律审计`（聚焦 `upcoming_risks_30d`，尤其是 12/08 SpaceX 解禁与 12/16 FOMC 等硬节点，给出防御清单）
   - `## 四、🧠 下周 CIO 三条最高优先级行动纲领`（简短犀利，每条必须可执行、可证伪）
"""

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            body_md = call_gemini(api_key, prompt)
            body_md = re.sub(r"^```(?:markdown)?\s*", "", body_md.strip())
            body_md = re.sub(r"\s*```$", "", body_md.strip())
            body_md = re.sub(r"`(\[\[.+?\]\])`", r"\1", body_md)
            status = "Complete"
        except Exception as e:
            print(f"⚠️ AI 周报生成失败，启用数据保底版: {e}")
            body_md = build_fallback_weekly_md(summary)
            status = "DataOnly"
    else:
        body_md = build_fallback_weekly_md(summary)
        status = "DataOnly"

    header = (
        f"---\n"
        f"title: {WEEK_TAG} 周度CIO战略复盘\n"
        f"date: {TODAY_STR}\n"
        f"week: {WEEK_TAG}\n"
        f"tags:\n  - 投资/周报\n"
        f"status: {status}\n"
        f"---\n\n"
    )

    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = WEEKLY_DIR / f"{WEEK_TAG}_周度CIO战略复盘.md"
    out_path.write_text(header + body_md.strip() + "\n", encoding="utf-8")
    print(f"📝 周度 CIO 战略复盘已写入: {out_path}")

    index_path = DAILY_DIR / "每日投资情报总览.md"
    if index_path.exists():
        idx_text = index_path.read_text(encoding="utf-8")
        link_str = f"[[{WEEK_TAG}_周度CIO战略复盘]]"
        if link_str not in idx_text:
            idx_text = idx_text.rstrip() + f"\n- 📅 {link_str} — 周度战略复盘（{summary['date_range']}）\n"
            index_path.write_text(idx_text, encoding="utf-8")

    send_bark(
        f"📅 {WEEK_TAG} 周度 CIO 战略复盘已生成",
        f"覆盖 {summary['date_range']} 宏观迁移、6大精锐周强弱排名与未来30天排雷日历，请在 Obsidian 查收。",
    )


if __name__ == "__main__":
    main()
