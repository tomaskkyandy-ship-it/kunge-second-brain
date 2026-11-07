#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
坤哥的第二大脑 · 云端每日投资情报与买卖扳机监控引擎
运行环境：GitHub Actions (Ubuntu) 或 本地 Python 3.10+
功能：
1. 动态扫描 `02_投资与分析/04_交易记录/` 下所有个股与加密资产笔记（提取 YAML 参数与核心扳机）
2. 通过 yfinance 实时拉取美股价格、BTC 价格、10Y 美债收益率、美元指数 DXY、VIX 恐慌指数
3. 调用 Google Gemini API (开启原生 Google Search 实时联网搜索)，结合半衰期漏斗与贝叶斯模型生成早/晚报
4. 自动写入 `02_投资与分析/05_每日情报/YYYY-MM-DD_每日投资情报.md`（绝不篡改个股底稿）
5. 通过 Bark 推送高可读性摘要与分级预警到坤哥的 iPhone 锁屏
"""

import os
import re
import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

import yaml
import requests
import yfinance as yf
from google import genai
from google.genai import types

# 设定北京时间 (UTC+8)
BJT = timezone(timedelta(hours=8))
NOW_BJT = datetime.now(BJT)
TODAY_STR = NOW_BJT.strftime("%Y-%m-%d")
SESSION_LABEL = "早报 (08:30 美股盘后复盘)" if NOW_BJT.hour < 15 else "晚报 (21:30 美股盘前与宏观预警)"

# 仓库根目录定位
SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = SCRIPT_DIR.parents[2]  # E:\obsidian仓库\坤哥的第二大脑
RECORDS_DIR = VAULT_ROOT / "02_投资与分析" / "04_交易记录"
METHOD_DIR = VAULT_ROOT / "02_投资与分析" / "01_方法论"
DAILY_DIR = VAULT_ROOT / "02_投资与分析" / "05_每日情报"


def parse_markdown_frontmatter(file_path: Path):
    """解析 Obsidian Markdown 文件的 YAML Frontmatter 和正文摘要"""
    text = file_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except Exception:
        meta = {}
    return meta, parts[2].strip()


def load_portfolio_targets():
    """从 04_交易记录 文件夹读取所有标的笔记（排除索引与交易流水）"""
    targets = []
    skip_files = {"股票池.md", "投资决策日志.md"}
    for md_file in sorted(RECORDS_DIR.glob("*.md")):
        if md_file.name in skip_files:
            continue
        meta, body = parse_markdown_frontmatter(md_file)
        if "ticker" not in meta or "action" in meta:
            continue
        targets.append({
            "file_name": md_file.stem,
            "ticker": str(meta.get("ticker", "")),
            "company": str(meta.get("company", md_file.stem)),
            "asset_type": str(meta.get("asset_type", "股票")),
            "macro_bucket": str(meta.get("macro_bucket", "")),
            "sector": str(meta.get("sector", "")),
            "status": str(meta.get("status", "观察中")),
            "recorded_price": meta.get("price", ""),
            "target_buy_price": str(meta.get("target_buy_price", "")),
            "macro_score": meta.get("macro_score", ""),
            "quality_score": meta.get("quality_score", ""),
            "score_rate": meta.get("score_rate", ""),
            "position_pct": meta.get("position_pct", 0),
            "verdict": str(meta.get("verdict", "")),
            "body_excerpt": body[:2500],  # 截取核心结论、排雷日历与买卖扳机供 AI 对照
        })
    return targets


def fetch_realtime_market_data(targets):
    """使用 yfinance 获取宏观路标与股票池标的的实时精确行情"""
    macro_symbols = {
        "10Y美债收益率 (^TNX)": "^TNX",
        "美元指数 (DX-Y.NYB)": "DX-Y.NYB",
        "VIX恐慌指数 (^VIX)": "^VIX",
        "标普500 (^GSPC)": "^GSPC",
        "纳斯达克100 (^NDX)": "^NDX",
    }

    market_snapshot = {"macro": {}, "targets": {}}

    # 1. 获取宏观高频指标
    for label, sym in macro_symbols.items():
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period="5d")
            if not hist.empty:
                latest = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else latest
                chg_pct = ((latest - prev) / prev * 100) if prev else 0.0
                market_snapshot["macro"][label] = {
                    "latest": round(latest, 2),
                    "change_pct": f"{chg_pct:+.2f}%"
                }
        except Exception as e:
            market_snapshot["macro"][label] = {"error": str(e)}

    # 2. 获取股票池标的实时价格
    crypto_map = {"BTC": "BTC-USD", "ZEC": "ZEC-USD"}
    for t in targets:
        raw_ticker = t["ticker"].upper()
        yf_sym = crypto_map.get(raw_ticker, raw_ticker)
        try:
            tk = yf.Ticker(yf_sym)
            hist = tk.history(period="5d")
            if not hist.empty:
                latest = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else latest
                chg_pct = ((latest - prev) / prev * 100) if prev else 0.0
                market_snapshot["targets"][raw_ticker] = {
                    "latest_price": round(latest, 2),
                    "change_pct_1d": f"{chg_pct:+.2f}%"
                }
            else:
                market_snapshot["targets"][raw_ticker] = {
                    "latest_price": t["recorded_price"],
                    "note": "非公开交易或暂无yfinance行情，沿用档案价"
                }
        except Exception:
            market_snapshot["targets"][raw_ticker] = {
                "latest_price": t["recorded_price"],
                "note": "拉取异常，沿用档案价"
            }

    return market_snapshot


def generate_intelligence_with_gemini(targets, market_snapshot):
    """调用 Gemini 2.5 (带 Google Search Grounding) 生成每日投资情报"""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未检测到环境变量 GEMINI_API_KEY！请在 GitHub Secrets 中配置。")

    client = genai.Client(api_key=api_key)

    # 读取方法论核心摘要作为标尺
    news_sop_path = METHOD_DIR / "新闻过滤与贝叶斯动态更新系统.md"
    news_sop_text = news_sop_path.read_text(encoding="utf-8")[:3000] if news_sop_path.exists() else ""

    prompt = f"""你现在是「坤哥的第二大脑」专属机构级首席投资官与风控官。
今天是北京时间 **{TODAY_STR}**，当前执行批次为：**{SESSION_LABEL}**。

### 一、今日实时行情快照（已由系统通过交易所接口精确拉取）
```json
{json.dumps(market_snapshot, ensure_ascii=False, indent=2)}
```

### 二、坤哥当前 Obsidian 股票池标的档案
```json
{json.dumps(targets, ensure_ascii=False, indent=2)}
```

### 三、必须遵守的方法论与过滤铁律（摘自《新闻过滤与贝叶斯动态更新系统》）
{news_sop_text}

---

### 四、你的执行任务（请务必调用 Google Search 工具搜索过去 24 小时最新英文/中文权威财经动态）
1. **定向联网搜索**：
   - 搜索美国宏观最新动态（ISM PMI、CPI/PCE、美联储官员最新表态、2Y/10Y美债与高收益债利差 HY OAS）。
   - 搜索股票池活跃标的（`GOOGL`、`BTC`、`CRCL`、`COIN`、`SPCX`、`RXRX`）过去 24–48 小时的重要财报、SEC 文件（10-Q/8-K/13F/Form 4）、监管进展、ETF 净流入/流出、锁定期解禁与临床数据。
2. **严格执行半衰期三级漏斗**：
   - 剔除 90% 半衰期 < 1 周的一级噪音（放置在文末第 6 节简短划掉）。
   - 重点深潜二级中观信号（1–4季度）与三级微观突变（>3年或黑天鹅），每条核心情报必须回答 **「预期差三问」**（① News vs. Price Action 预期差在哪？② 二阶传导是谁？③ 属于软证据还是硬证据？）。
3. **「学徒买、师傅卖」双扳机实时比对**：
   - 将上方 JSON 中的实时最新价（`latest_price`）与每只标的预设的 `target_buy_price` 精确对比，计算还差多少百分比，判断是否触发🟢建仓扳机或🔴卖出/排雷预警。
4. **输出格式要求（严格输出两部分，用 `===BARK_SUMMARY_SPLIT===` 分隔）**：

#### 第一部分：完整的 Obsidian Markdown 报告（直接以 `---` YAML Frontmatter 开头，不要包裹在代码块里）
- 注意：正文中所有的美元符号必须转义为 `\\$`（如 `\\$120`、`\\$75,500`），防止 Obsidian KaTeX 公式渲染错乱！
- 必须使用 Obsidian 双链语法引用笔记（如 `[[Alphabet GOOGL]]`、`[[Bitcoin BTC]]`、`[[Circle CRCL]]`、`[[Coinbase COIN]]`、`[[SpaceX SPCX]]`、`[[Recursion RXRX]]`、`[[股票池]]`）。
- 章节结构必须包含：
  - YAML Frontmatter (`title`, `date: {TODAY_STR}`, `tags: [投资系统/每日情报]`, `macro_quadrant`, `triggered_alerts`)
  - `# 📡 {TODAY_STR} 每日投资情报与买卖扳机监控（{SESSION_LABEL}）`
  - `## 一、🚨 买卖扳机与红线预警仪表盘（学徒买 · 师傅卖）`（Markdown 表格）
  - `## 二、🧭 宏观 7 大路标与四象限快照`
  - `## 三、🔬 核心标的二级/三级情报深潜（含预期差三问）`
  - `## 四、🌊 五大赛道二阶传导扫描`
  - `## 五、⚖️ 贝叶斯动态调分建议表（待坤哥人工确认）`
  - `## 六、🗑️ 今日已过滤的一级噪音（半衰期 < 1 周）`

#### 分隔符（独占一行）：
`===BARK_SUMMARY_SPLIT===`

#### 第二部分：用于推送到坤哥 iPhone Bark 的精简 JSON（严禁包裹代码块，直接输出合法 JSON 对象）
格式如下：
{{
  "alert_level": "active 或 timeSensitive（若有标的跌入狙击买点 5% 以内、或触发卖出红线/排雷日历 <7 天，设为 timeSensitive）",
  "title": "📡 {TODAY_STR} 投资早/晚报：一句话核心结论（不超过22个字）",
  "body": "用 4-6 行极简要点列出：\\n1. 🚨 扳机状态（如 COIN 现价$xx 距买点差x%）\\n2. ⚠️ 排雷预警\\n3. 🧭 宏观温度（10Y美债/VIX）\\n4. ⚖️ 是否有贝叶斯调分建议"
}}
"""

    # 调用 Gemini 2.5 Flash 并启用 Google Search Grounding
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.2,
        ),
    )

    full_text = response.text or ""
    if "===BARK_SUMMARY_SPLIT===" in full_text:
        md_part, bark_part = full_text.split("===BARK_SUMMARY_SPLIT===", 1)
    else:
        md_part = full_text
        bark_part = "{}"

    # 清理可能残留的首尾代码块标记
    md_part = re.sub(r"^```(?:markdown)?\s*", "", md_part.strip())
    md_part = re.sub(r"\s*```$", "", md_part.strip())

    bark_part = re.sub(r"^```(?:json)?\s*", "", bark_part.strip())
    bark_part = re.sub(r"\s*```$", "", bark_part.strip())
    try:
        bark_data = json.loads(bark_part)
    except Exception:
        bark_data = {
            "alert_level": "timeSensitive",
            "title": f"📡 {TODAY_STR} 每日投资情报已送达",
            "body": "今日股票池买卖扳机巡检与半衰期过滤已完成，请打开 Obsidian 查看完整报告。"
        }

    return md_part, bark_data


def save_report_to_obsidian(md_content: str):
    """将生成的日报写入 Obsidian 05_每日情报 目录，并更新总览索引"""
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    report_path = DAILY_DIR / f"{TODAY_STR}_每日投资情报.md"

    if report_path.exists() and "晚报" in SESSION_LABEL:
        existing = report_path.read_text(encoding="utf-8")
        if "## 🌙 晚盘更新" not in existing:
            combined = existing.rstrip() + f"\n\n---\n\n## 🌙 晚盘更新（{NOW_BJT.strftime('%H:%M')}）\n\n" + md_content
            report_path.write_text(combined, encoding="utf-8")
        else:
            report_path.write_text(md_content, encoding="utf-8")
    else:
        report_path.write_text(md_content, encoding="utf-8")

    # 自动把当天日报链接加入 每日投资情报总览.md
    index_path = DAILY_DIR / "每日投资情报总览.md"
    if index_path.exists():
        idx_text = index_path.read_text(encoding="utf-8")
        link_str = f"[[{TODAY_STR}_每日投资情报]]"
        if link_str not in idx_text:
            idx_text = idx_text.rstrip() + f"\n- {link_str} — 自动生成于 {NOW_BJT.strftime('%Y-%m-%d %H:%M')}\n"
            index_path.write_text(idx_text, encoding="utf-8")

    return report_path


def send_bark_notification(bark_data: dict):
    """向坤哥的 iPhone Bark 发送原生推送通知"""
    bark_key_or_url = os.environ.get("BARK_KEY", "").strip()
    if not bark_key_or_url:
        print("⚠️ 未配置 BARK_KEY 环境变量，跳过 iPhone Bark 推送。")
        return

    # 兼容用户直接填 Key 或填完整 https://api.day.app/xxx/ URL
    if bark_key_or_url.startswith("http"):
        base_url = bark_key_or_url.rstrip("/")
        # 如果 URL 是 https://api.day.app/<key>，提取 endpoint
        parts = base_url.split("/")
        bark_server = "/".join(parts[:3])
        device_key = parts[3] if len(parts) > 3 else ""
    else:
        bark_server = "https://api.day.app"
        device_key = bark_key_or_url.strip("/")

    payload = {
        "device_key": device_key,
        "title": bark_data.get("title", f"📡 {TODAY_STR} 每日投资情报"),
        "body": bark_data.get("body", "今日投资简报已生成，请查收。"),
        "group": "坤哥投资情报",
        "icon": "https://cdn-icons-png.flaticon.com/512/3310/3310624.png",
        "level": bark_data.get("alert_level", "timeSensitive"),
        "sound": "bell",
    }

    # 如果配置了 GITHUB_REPOSITORY，点击 Bark 通知可直接跳转到 GitHub 手机网页查看完整 Markdown 报告
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if gh_repo:
        payload["url"] = f"https://github.com/{gh_repo}/blob/main/02_%E6%8A%95%E8%B5%84%E4%B8%8E%E5%88%86%E6%9E%90/05_%E6%AF%8F%E6%97%A5%E6%83%85%E6%8A%A5/{TODAY_STR}_%E6%AF%8F%E6%97%A5%E6%8A%95%E8%B5%84%E6%83%85%E6%8A%A5.md"

    resp = requests.post(f"{bark_server}/push", json=payload, timeout=15)
    resp.raise_for_status()
    print(f"✅ iPhone Bark 推送成功！响应: {resp.text}")


def main():
    print(f"🚀 开始执行【坤哥的第二大脑 · 每日投资情报】({TODAY_STR} {SESSION_LABEL})...")
    targets = load_portfolio_targets()
    print(f"📦 已加载股票池标的 {len(targets)} 只: {[t['ticker'] for t in targets]}")

    market_snapshot = fetch_realtime_market_data(targets)
    print("📈 已拉取实时行情快照。")

    md_content, bark_data = generate_intelligence_with_gemini(targets, market_snapshot)
    report_path = save_report_to_obsidian(md_content)
    print(f"📝 每日情报已写入: {report_path}")

    send_bark_notification(bark_data)
    print("🎉 全部流程执行完毕！")


if __name__ == "__main__":
    main()
