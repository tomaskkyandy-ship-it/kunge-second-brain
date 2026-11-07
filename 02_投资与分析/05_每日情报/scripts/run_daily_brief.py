#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
坤哥的第二大脑 · 云端每日投资情报与买卖扳机监控引擎 (v2.0 免付费配额满血版)
运行环境：GitHub Actions (Ubuntu) 或 本地 Python 3.10+
核心特性：
1. 动态扫描 `02_投资与分析/04_交易记录/` 下所有个股与加密资产笔记（加减股票只需改 Obsidian 笔记，零代码维护）
2. 通过 yfinance + Google News RSS 直接免费拉取美股/BTC实时行情、10Y美债、DXY、VIX 以及过去24小时全球权威财经新闻（无需付费 Google Search Grounding 配额！）
3. 自动多模型智能容灾回退：优先调用 `gemini-3.5-flash` -> `gemini-3-flash-preview` -> `gemini-3.1-flash-lite` -> `gemini-3.8-flash`
4. 自动写入 `02_投资与分析/05_每日情报/YYYY-MM-DD_每日投资情报.md`（绝不自动篡改个股底稿）
5. 通过 Bark 推送高可读性摘要与分级预警到坤哥的 iPhone 锁屏
"""

import os
import re
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta

import yaml
import requests
import yfinance as yf

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
            "body_excerpt": body[:2000],
        })
    return targets


def fetch_google_news_rss(query: str, max_items: int = 6):
    """通过 Google News RSS 免费抓取最新英文/中文权威财经新闻"""
    encoded_q = urllib.parse.quote(f"{query} when:2d")
    rss_url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en-US&gl=US&ceid=US:en"
    items = []
    try:
        resp = requests.get(rss_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for item in root.findall("./channel/item")[:max_items]:
                title = item.findtext("title", "")
                pub_date = item.findtext("pubDate", "")
                source = item.findtext("source", "")
                items.append({
                    "title": title,
                    "source": source,
                    "published": pub_date
                })
    except Exception as e:
        items.append({"note": f"RSS fetch warning: {e}"})
    return items


def fetch_realtime_market_and_news(targets):
    """使用 yfinance + Google News RSS 获取宏观路标（含黄金、原油、金油比）与股票池标的的实时精确行情及最新新闻"""
    macro_symbols = {
        "10Y美债收益率 (^TNX)": "^TNX",
        "美元指数 (DX-Y.NYB)": "DX-Y.NYB",
        "WTI原油期货 (CL=F)": "CL=F",
        "COMEX黄金期货 (GC=F)": "GC=F",
        "VIX恐慌指数 (^VIX)": "^VIX",
        "标普500 (^GSPC)": "^GSPC",
        "纳斯达克100 (^NDX)": "^NDX",
    }

    snapshot = {
        "timestamp_bjt": NOW_BJT.strftime("%Y-%m-%d %H:%M:%S"),
        "macro_prices": {},
        "cross_asset_ratios": {},
        "macro_news": fetch_google_news_rss("US economy Federal Reserve CPI ISM PMI Treasury yield oil gold", max_items=8),
        "targets_data": {}
    }

    # 1. 获取宏观高频指标
    for label, sym in macro_symbols.items():
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period="5d")
            if not hist.empty:
                latest = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else latest
                chg_pct = ((latest - prev) / prev * 100) if prev else 0.0
                snapshot["macro_prices"][label] = {
                    "latest": round(latest, 2),
                    "change_pct": f"{chg_pct:+.2f}%"
                }
        except Exception as e:
            snapshot["macro_prices"][label] = {"error": str(e)}

    # 2. 获取股票池标的实时价格与专属新闻
    crypto_map = {"BTC": "BTC-USD", "ZEC": "ZEC-USD"}
    for t in targets:
        raw_ticker = t["ticker"].upper()
        yf_sym = crypto_map.get(raw_ticker, raw_ticker)
        t_info = {
            "company": t["company"],
            "status": t["status"],
            "target_buy_price": t["target_buy_price"],
            "latest_price": t["recorded_price"],
            "change_pct_1d": "0.00%",
            "latest_news": []
        }
        try:
            tk = yf.Ticker(yf_sym)
            hist = tk.history(period="5d")
            if not hist.empty:
                latest = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else latest
                chg_pct = ((latest - prev) / prev * 100) if prev else 0.0
                t_info["latest_price"] = round(latest, 2)
                t_info["change_pct_1d"] = f"{chg_pct:+.2f}%"

            # 尝试拉取 yfinance 内置新闻
            yf_news = getattr(tk, "news", []) or []
            for n in yf_news[:4]:
                content = n.get("content", {}) if isinstance(n, dict) else {}
                title = content.get("title") or n.get("title")
                if title:
                    t_info["latest_news"].append({"title": title, "source": "Yahoo Finance"})
        except Exception:
            pass

        # 补充 Google News RSS 定向抓取
        rss_news = fetch_google_news_rss(f"{t['company']} {raw_ticker} stock crypto", max_items=5)
        t_info["latest_news"].extend(rss_news)
        snapshot["targets_data"][raw_ticker] = t_info

    # 3. 自动计算华尔街核心跨资产比值：金油比 (Gold/Oil Ratio) 与 BTC/黄金比 (BTC/Gold Ratio)
    gold_price = snapshot["macro_prices"].get("COMEX黄金期货 (GC=F)", {}).get("latest")
    oil_price = snapshot["macro_prices"].get("WTI原油期货 (CL=F)", {}).get("latest")
    btc_price = snapshot["targets_data"].get("BTC", {}).get("latest_price")

    if isinstance(gold_price, (int, float)) and isinstance(oil_price, (int, float)) and oil_price > 0:
        go_ratio = round(gold_price / oil_price, 2)
        if go_ratio > 30:
            go_signal = "🚨 >30 衰退/避险与潜在救市放水预警区（金强油弱）"
        elif go_ratio < 15:
            go_signal = "🔴 <15 恶性通胀与加息压制预警区（油强金弱）"
        else:
            go_signal = "🟢 15–30 正常周期波动区间"
        snapshot["cross_asset_ratios"]["金油比 (Gold/WTI Ratio)"] = {
            "value": go_ratio,
            "signal": go_signal,
            "formula": f"${gold_price} / ${oil_price}"
        }

    if isinstance(btc_price, (int, float)) and isinstance(gold_price, (int, float)) and gold_price > 0:
        bg_ratio = round(btc_price / gold_price, 2)
        snapshot["cross_asset_ratios"]["数字黄金/实物黄金比 (BTC/Gold Ratio)"] = {
            "value": f"1 BTC = {bg_ratio} 盎司黄金",
            "note": "用于评估 BTC 相对于全球央行黄金买盘的滞后补涨空间或过热程度"
        }

    return snapshot


def call_gemini_with_fallback(api_key: str, prompt: str) -> str:
    """使用 REST API 调用 Gemini 3.x 模型群，具备自动降级与重试机制（完全适配免费层 Key）"""
    candidate_models = [
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-3.8-flash",
        "gemini-3.1-flash-lite",
        "gemini-flash-lite-latest",
    ]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2}
    }

    last_err = ""
    for model_name in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        for attempt in range(2):
            try:
                print(f"🧠 正在请求大模型 [{model_name}] (尝试 {attempt + 1}/2)...")
                resp = requests.post(url, json=payload, timeout=90)
                if resp.status_code == 200:
                    data = resp.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    print(f"✅ 模型 [{model_name}] 响应成功！")
                    return text
                else:
                    last_err = f"{model_name} HTTP {resp.status_code}: {resp.text[:200]}"
                    print(f"⚠️ {last_err}")
                    time.sleep(2)
            except Exception as e:
                last_err = f"{model_name} Exception: {e}"
                print(f"⚠️ {last_err}")
                time.sleep(2)

    raise RuntimeError(f"所有候选 Gemini 模型均调用失败，最后错误: {last_err}")


def generate_intelligence_with_gemini(targets, market_snapshot):
    """构建 Prompt 并调用 Gemini 生成每日投资情报"""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未检测到环境变量 GEMINI_API_KEY！请在 GitHub Secrets 中配置。")

    news_sop_path = METHOD_DIR / "新闻过滤与贝叶斯动态更新系统.md"
    news_sop_text = news_sop_path.read_text(encoding="utf-8")[:2500] if news_sop_path.exists() else ""

    prompt = f"""你现在是「坤哥的第二大脑」专属机构级首席投资官与风控官。
今天是北京时间 **{TODAY_STR}**，当前执行批次为：**{SESSION_LABEL}**。

### 一、今日实时行情与过去 24–48 小时全球权威财经新闻（已由系统实时抓取）
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

### 四、你的执行任务
1. **严格执行半衰期三级漏斗**：
   - 从上方抓取到的实时新闻中，剔除半衰期 < 1 周的一级噪音（放置在文末第 6 节简短划掉）。
   - 重点深潜二级中观信号（1–4季度）与三级微观突变（>3年或黑天鹅），每条核心情报必须回答 **「预期差三问」**（① News vs. Price Action 预期差在哪？② 二阶传导是谁？③ 属于软证据还是硬证据？）。
2. **「学徒买、师傅卖」双扳机实时比对**：
   - 将上方 JSON 中的实时最新价（`latest_price`）与每只标的预设的 `target_buy_price` 精确对比，计算还差多少百分比，判断是否触发🟢建仓扳机或🔴卖出/排雷预警。
3. **输出格式要求（严格输出两部分，用 `===BARK_SUMMARY_SPLIT===` 分隔）**：

#### 第一部分：完整的 Obsidian Markdown 报告（直接以 `---` YAML Frontmatter 开头，不要包裹在代码块里）
- 注意：正文中所有的美元符号必须转义为 `\\$`（如 `\\$120`、`\\$75,500`），防止 Obsidian KaTeX 公式渲染错乱！
- 必须使用 Obsidian 双链语法引用笔记（如 `[[Alphabet GOOGL]]`、`[[Bitcoin BTC]]`、`[[Circle CRCL]]`、`[[Coinbase COIN]]`、`[[SpaceX SPCX]]`、`[[Recursion RXRX]]`、`[[股票池]]`）。
- 章节结构必须包含：
  - YAML Frontmatter (`title`, `date: {TODAY_STR}`, `tags: [投资系统/每日情报]`, `macro_quadrant`, `triggered_alerts`)
  - `# 📡 {TODAY_STR} 每日投资情报与买卖扳机监控（{SESSION_LABEL}）`
  - `## 一、🚨 买卖扳机与红线预警仪表盘（学徒买 · 师傅卖）`（Markdown 表格）
  - `## 二、🧭 宏观 7 大路标与金油双锚快照`（必须包含 WTI 原油价格与三档警戒线对比、黄金价格、实时【金油比】与【BTC/黄金比】信号解读）
  - `## 三、🔬 核心标的二级/三级情报深潜（含预期差三问）`
  - `## 四、🌊 五大赛道二阶传导扫描`
  - `## 五、⚖️ 贝叶斯动态调分建议表（待坤哥人工确认）`
  - `## 六、🗑️ 今日已过滤的一级噪音（半衰期 < 1 周）`

#### 分隔符（独占一行）：
`===BARK_SUMMARY_SPLIT===`

#### 第二部分：用于推送到坤哥 iPhone Bark 的精简 JSON（严禁包裹代码块，直接输出合法 JSON 对象）
格式如下：
{{
  "alert_level": "active 或 timeSensitive（若有标的跌入狙击买点 15% 以内、或触发卖出红线/排雷日历 <7 天、或 WTI 原油 >$90 / 金油比 >30，设为 timeSensitive）",
  "title": "📡 {TODAY_STR} 投资情报：一句话核心结论（不超过22个字）",
  "body": "用 4 行极简要点列出：\\n1. 🟢 买点监控（如 COIN 现价$xx 距买点差x%）\\n2. ⚠️ 排雷预警\\n3. 🧭 宏观与金油双锚（10Y美债/WTI油价/黄金/金油比）\\n4. ⚖️ 贝叶斯调分建议摘要"
}}
"""

    full_text = call_gemini_with_fallback(api_key, prompt)

    if "===BARK_SUMMARY_SPLIT===" in full_text:
        md_part, bark_part = full_text.split("===BARK_SUMMARY_SPLIT===", 1)
    else:
        md_part = full_text
        bark_part = "{}"

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
            "body": "今日股票池买卖扳机巡检与半衰期过滤已完成，请点击通知或打开 Obsidian 查看完整报告。"
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

    if bark_key_or_url.startswith("http"):
        base_url = bark_key_or_url.rstrip("/")
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

    market_snapshot = fetch_realtime_market_and_news(targets)
    print("📈 已拉取实时行情与全球权威财经 RSS 快照。")

    md_content, bark_data = generate_intelligence_with_gemini(targets, market_snapshot)
    report_path = save_report_to_obsidian(md_content)
    print(f"📝 每日情报已写入: {report_path}")

    send_bark_notification(bark_data)
    print("🎉 全部流程执行完毕！")


if __name__ == "__main__":
    main()
