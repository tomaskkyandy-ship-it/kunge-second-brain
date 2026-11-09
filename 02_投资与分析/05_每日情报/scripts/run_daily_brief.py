#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
坤哥的第二大脑 · 云端每日投资情报与买卖双扳机监控引擎 (v3.0 机构量化终极版)
运行环境：GitHub Actions (Ubuntu) 或 本地 Python 3.10+
v3.0 六大核心进化特性：
1. 【双池杠铃架构】：自动识别 `💎皇冠明珠复利池 (85%)` 与 `🚀新物种观察池 (15% 第一性原理记分卡)`，自动屏蔽 `已放弃` 标的。
2. 【🟢学徒买 + 🔴师傅卖 双扳机监控】：同时提取 `target_buy_price` 与 `target_sell_price`（泡沫止盈 + 逻辑证伪 + 移动护利），自动计算双向百分比距离。
3. 【一手量化盘面与链上硬数据】：
   - 通过 yfinance 1年日线数据自动计算每只标的的 `RSI(14) 超买超卖指标`、`MA200 牛熊均线偏离度`、`距 52 周高点回撤率`、`量比 (Volume Ratio)`；
   - 通过 CoinGecko 免费接口实时抓取 `USDC 真实流通总市值（亿美元）`（直接验证 Circle 30% 增速扳机！）；
   - 通过 Alternative.me 免费接口实时抓取 `加密恐慌与贪婪指数 (Crypto Fear & Greed Index)`；
   - 自动计算 `金油比 (Gold/WTI)`、`美债期限利差 (10Y-13W)`、`BTC/黄金比`。
4. 【早报 vs 晚报 彻底差异化分工】：
   - ☀️ 08:30 早报：《美股收盘量价复盘 & 预期差三问 & 贝叶斯调分早报》（深度基本面与调分审计）；
   - 🌙 21:30 晚报：《美股盘前宏观/金油异动 & 买卖双扳机挂单作战快报》（聚焦今晚挂单指令與新猎物异动）。
5. 【🔭 五大赛道新物种与错杀猎物雷达】：实时扫描核心赛道代表性龙头（NVDA, TSLA, LLY, RKLB, PLTR, META, AMZN）是否出现单日暴跌 >5% 或 RSI < 32 恐慌错杀，主动发现新研究目标！
6. 【iPhone Bark 智能分级告警】：一旦触发买点/卖点共振（或 RSI 极值），自动以 `timeSensitive` 突破勿扰模式直达锁屏。
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
IS_MORNING = NOW_BJT.hour < 15
SESSION_LABEL = "早报 (08:30 美股收盘深度复盘 & 贝叶斯审计)" if IS_MORNING else "晚报 (21:30 美股盘前异动 & 挂单狙击快报)"

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


def extract_first_price(price_str: str):
    """从 '$230 或单季FCF转正' 或 '$55000-$66000' 中提取第一个基准美元价格数字"""
    if not price_str:
        return None
    m = re.search(r"\$\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)", str(price_str))
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except Exception:
            return None
    return None


def compute_technical_indicators(hist_df):
    """基于 1 年历史日线数据计算机构级量价指标：RSI(14)、MA200偏离度、52周回撤、量比"""
    if hist_df is None or hist_df.empty or len(hist_df) < 15:
        return {}
    closes = hist_df["Close"].dropna()
    if len(closes) < 15:
        return {}

    latest = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else latest
    chg_1d = ((latest - prev) / prev * 100) if prev else 0.0

    # 1. 计算 14 日 RSI (Wilder 平滑近似)
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14, min_periods=14).mean().iloc[-1]
    avg_loss = loss.rolling(window=14, min_periods=14).mean().iloc[-1]
    if avg_loss == 0:
        rsi_14 = 100.0
    else:
        rs = float(avg_gain) / float(avg_loss)
        rsi_14 = round(100.0 - (100.0 / (1.0 + rs)), 1)

    if rsi_14 <= 30:
        rsi_signal = f"🟢极度超卖({rsi_14}≤30 恐慌击球信号)"
    elif rsi_14 >= 75:
        rsi_signal = f"🔴严重超买({rsi_14}≥75 泡沫止盈信号)"
    else:
        rsi_signal = f"⚪中性({rsi_14})"

    # 2. 计算 MA200 牛熊分界线与偏离度
    ma200_val = float(closes.tail(200).mean())
    ma200_dist = ((latest - ma200_val) / ma200_val * 100) if ma200_val else 0.0

    # 3. 计算 52 周最高/最低点与回撤幅度
    high_52w = float(closes.max())
    low_52w = float(closes.min())
    drawdown_52w = ((latest - high_52w) / high_52w * 100) if high_52w else 0.0

    # 4. 计算成交量 20 日量比 (Volume Ratio)
    vol_ratio_str = "N/A"
    if "Volume" in hist_df.columns:
        vols = hist_df["Volume"].dropna()
        if len(vols) >= 20 and float(vols.tail(20).mean()) > 0:
            v_ratio = float(vols.iloc[-1]) / float(vols.tail(20).mean())
            vol_ratio_str = f"{v_ratio:.2f}x"

    return {
        "latest_price": round(latest, 2),
        "change_pct_1d": f"{chg_1d:+.2f}%",
        "rsi_14": rsi_14,
        "rsi_signal": rsi_signal,
        "ma200": round(ma200_val, 2),
        "ma200_distance_pct": f"{ma200_dist:+.1f}%",
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "drawdown_from_52w_high": f"{drawdown_52w:.1f}%",
        "volume_ratio_20d": vol_ratio_str,
    }


def load_portfolio_targets():
    """从 04_交易记录 文件夹读取所有活跃标的笔记（排除索引、雷达池、交易流水与已放弃标的）"""
    targets = []
    skip_files = {"股票池.md", "投资决策日志.md", "新物种与候选猎物雷达池.md"}
    for md_file in sorted(RECORDS_DIR.glob("*.md")):
        if md_file.name in skip_files:
            continue
        meta, body = parse_markdown_frontmatter(md_file)
        if "ticker" not in meta or "action" in meta:
            continue
        status_str = str(meta.get("status", "观察中")).strip()
        if "放弃" in status_str:
            continue
        targets.append({
            "file_name": md_file.stem,
            "ticker": str(meta.get("ticker", "")),
            "company": str(meta.get("company", md_file.stem)),
            "asset_type": str(meta.get("asset_type", "股票")),
            "pool_type": str(meta.get("pool_type", "💎皇冠明珠复利池")),
            "macro_bucket": str(meta.get("macro_bucket", "")),
            "sector": str(meta.get("sector", "")),
            "status": status_str,
            "recorded_price": meta.get("price", ""),
            "target_buy_price": str(meta.get("target_buy_price", "")),
            "target_sell_price": str(meta.get("target_sell_price", "")),
            "macro_score": meta.get("macro_score", ""),
            "quality_score": meta.get("quality_score", ""),
            "vitality_score": meta.get("vitality_score", ""),
            "score_rate": meta.get("score_rate", ""),
            "position_pct": meta.get("position_pct", 0),
            "verdict": str(meta.get("verdict", "")),
            "body_excerpt": body[:2000],
        })
    return targets


def fetch_google_news_rss(query: str, max_items: int = 5):
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


def fetch_onchain_and_sentiment_metrics():
    """免费拉取一手加密与稳定币链上硬数据：USDC 实时流通市值 + 加密恐慌贪婪指数"""
    metrics = {}
    # 1. CoinGecko 免费接口获取 USDC 实时总市值（验证 Circle 30% 增速核心扳机）
    try:
        cg_resp = requests.get(
            "https://api.coingecko.com/api/v3/coins/usd-coin?localization=false&tickers=false&community_data=false&developer_data=false",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        if cg_resp.status_code == 200:
            cg_data = cg_resp.json()
            mcap = cg_data.get("market_data", {}).get("market_cap", {}).get("usd", 0)
            if mcap:
                metrics["USDC实时流通总市值 (CoinGecko)"] = f"${mcap / 1e8:.2f} 亿美元 (${mcap / 1e9:.2f}B)"
    except Exception as e:
        metrics["USDC实时流通总市值"] = f"获取略过 ({e})"

    # 2. Alternative.me 免费接口获取加密市场恐慌与贪婪指数
    try:
        fng_resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if fng_resp.status_code == 200:
            fng_item = fng_resp.json().get("data", [{}])[0]
            val = fng_item.get("value", "")
            cls = fng_item.get("value_classification", "")
            metrics["加密恐慌与贪婪指数 (Fear & Greed)"] = f"{val} / 100 ({cls})"
    except Exception as e:
        metrics["加密恐慌与贪婪指数"] = f"获取略过 ({e})"

    return metrics


def scan_sector_radar_candidates(existing_tickers: set):
    """🔭 五大赛道新物种与错杀猎物雷达：自动扫描候选猎物并附带【第一性原理推荐理由】与【核心卡点】"""
    radar_watchlist = [
        {
            "ticker": "PLTR",
            "name": "Palantir (AI操作系统新物种)",
            "pool": "🚀新物种观察池",
            "recommend_reason": "本体论(Ontology)架构深嵌军工与巨头血管，转换成本极高；AIP引爆商业S曲线，Rule of 40突破80%+的罕见高FCF应用层真龙",
            "risk_and_trigger": "平时PE/PS估值极贵且有SBC减持；重点盯防随纳指回调至 $130-$140 区间或日线 RSI≤30 恐慌错杀时转正建档"
        },
        {
            "ticker": "NVDA",
            "name": "NVIDIA (AI算力全栈底座)",
            "pool": "💎皇冠/🚀新物种",
            "recommend_reason": "黄仁勋极客掌舵，CUDA生态+NVLink+1年1代极限迭代垄断全球AI训练>80%利润池",
            "risk_and_trigger": "下游云巨头CapEx/折旧比达4-6倍高位且自研ASIC分流；等回调至 $150-$170 或 RSI≤30 时狙击"
        },
        {
            "ticker": "TSLA",
            "name": "Tesla (FSD与具身机器人新物种)",
            "pool": "🚀新物种观察池",
            "recommend_reason": "端到端物理AI大脑+百万车队真实数据闭环+一体化压铸莱特定律；Megapack储能现金牛喂养Robotaxi与Optimus人形机器人",
            "risk_and_trigger": "汽车价格战挤压毛利、FSD监管审批时滞；等回调至 $200-$220 或无监督FSD跨州获批拐点"
        },
        {
            "ticker": "RKLB",
            "name": "Rocket Lab (商业航天第二极)",
            "pool": "🚀新物种观察池",
            "recommend_reason": "全球除SpaceX外唯一高频发射+卫星零部件垂直整合龙头(空间系统占营收>70%提供造血底盘)，中型可回收火箭Neutron直指星座组网",
            "risk_and_trigger": "Neutron首飞与回收工程验证期FCF微负；等回调至 $18-$22 或 Neutron 成功入轨回收验证"
        },
        {
            "ticker": "META",
            "name": "Meta Platforms (社交垄断与AI皇冠)",
            "pool": "💎皇冠明珠复利池",
            "recommend_reason": "33亿日活社交网络效应不可复制，AI推荐直接拉升广告ARPU(高ROIIC)，扎克伯格随时可砍开支恢复巨额FCF",
            "risk_and_trigger": "Reality Labs年亏超$150亿且处AI CapEx军备竞赛期；等 $480-$520 或 CapEx 增速见顶"
        },
        {
            "ticker": "AMZN",
            "name": "Amazon (AWS云与物流机器人飞轮)",
            "pool": "💎皇冠明珠复利池",
            "recommend_reason": "AWS云+高毛利广告驱动营业利润率创新高，百万台仓储机器人按莱特定律持续压降履约成本",
            "risk_and_trigger": "面临AWS AI基建与Kuiper卫星高CapEx折旧期；等 $175-$190 区间或FCF加速扩张拐点"
        },
        {
            "ticker": "LLY",
            "name": "Eli Lilly (GLP-1代谢医药皇冠)",
            "pool": "💎皇冠明珠复利池",
            "recommend_reason": "替尔泊肽(Mounjaro/Zepbound)十年一遇超级大单品，适应症向心血管/NASH扩展，强定价权且无近期专利悬崖",
            "risk_and_trigger": "表观PE常年40-50x偏贵、口服竞品与医保控费扰动；等错杀回调至 $680-$730 区间"
        },
    ]
    radar_results = []
    for item in radar_watchlist:
        sym = item["ticker"]
        if sym in existing_tickers:
            continue
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period="6mo")
            tech = compute_technical_indicators(hist)
            if tech:
                radar_results.append({
                    "ticker": sym,
                    "name": item["name"],
                    "recommended_pool": item["pool"],
                    "latest_price": tech["latest_price"],
                    "change_1d": tech["change_pct_1d"],
                    "rsi_14": tech["rsi_14"],
                    "rsi_signal": tech["rsi_signal"],
                    "drawdown_52w": tech["drawdown_from_52w_high"],
                    "recommend_reason": item["recommend_reason"],
                    "risk_and_trigger": item["risk_and_trigger"],
                })
        except Exception:
            pass
    return radar_results


def fetch_realtime_market_and_news(targets):
    """汇总宏观路标、金油双锚、一手链上数据、股票池量价双扳机、以及新物种雷达"""
    macro_symbols = {
        "10Y美债收益率 (^TNX)": "^TNX",
        "13周短债收益率 (^IRX)": "^IRX",
        "美元指数 (DX-Y.NYB)": "DX-Y.NYB",
        "WTI原油期货 (CL=F)": "CL=F",
        "COMEX黄金期货 (GC=F)": "GC=F",
        "VIX恐慌指数 (^VIX)": "^VIX",
        "标普500 (^GSPC)": "^GSPC",
        "纳斯达克100 (^NDX)": "^NDX",
    }

    snapshot = {
        "timestamp_bjt": NOW_BJT.strftime("%Y-%m-%d %H:%M:%S"),
        "session_mode": SESSION_LABEL,
        "macro_prices": {},
        "cross_asset_ratios": {},
        "onchain_and_sentiment": fetch_onchain_and_sentiment_metrics(),
        "macro_news": fetch_google_news_rss("US economy Federal Reserve CPI ISM PMI Treasury yield oil gold", max_items=6),
        "targets_data": {},
        "new_hunter_radar": [],
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

    # 2. 获取股票池标的实时量价技术指标、双向扳机距离与专属新闻
    crypto_map = {"BTC": "BTC-USD", "ZEC": "ZEC-USD"}
    existing_set = set()
    for t in targets:
        raw_ticker = t["ticker"].upper()
        existing_set.add(raw_ticker)
        yf_sym = crypto_map.get(raw_ticker, raw_ticker)
        t_info = {
            "company": t["company"],
            "pool_type": t["pool_type"],
            "status": t["status"],
            "quality_score": t["quality_score"],
            "vitality_score": t["vitality_score"],
            "score_rate": t["score_rate"],
            "target_buy_price": t["target_buy_price"],
            "target_sell_price": t["target_sell_price"],
            "latest_price": t["recorded_price"],
            "distance_to_buy_trigger": "待计算",
            "distance_to_sell_trigger": "待计算",
            "technicals": {},
            "latest_news": []
        }
        try:
            tk = yf.Ticker(yf_sym)
            hist = tk.history(period="1y")
            tech = compute_technical_indicators(hist)
            if tech:
                t_info["latest_price"] = tech["latest_price"]
                t_info["technicals"] = tech

                # 自动计算距离【学徒买点】与【师傅卖点】的精确百分比
                buy_num = extract_first_price(t["target_buy_price"])
                sell_num = extract_first_price(t["target_sell_price"])
                cur_p = tech["latest_price"]
                if buy_num and cur_p > 0:
                    diff_buy = (cur_p - buy_num) / cur_p * 100
                    t_info["distance_to_buy_trigger"] = f"距买点(${buy_num:g})还需跌 {-diff_buy:.1f}%" if diff_buy > 0 else f"🟢已跌破买点(${buy_num:g}) {abs(diff_buy):.1f}%！"
                if sell_num and cur_p > 0:
                    diff_sell = (sell_num - cur_p) / cur_p * 100
                    t_info["distance_to_sell_trigger"] = f"距止盈卖点(${sell_num:g})还有 +{diff_sell:.1f}% 空间" if diff_sell > 0 else f"🔴已突破止盈卖点(${sell_num:g})！"

            # 拉取 yfinance 内置新闻
            yf_news = getattr(tk, "news", []) or []
            for n in yf_news[:3]:
                content = n.get("content", {}) if isinstance(n, dict) else {}
                title = content.get("title") or n.get("title")
                if title:
                    t_info["latest_news"].append({"title": title, "source": "Yahoo Finance"})
        except Exception:
            pass

        rss_news = fetch_google_news_rss(f"{t['company']} {raw_ticker} stock crypto", max_items=4)
        t_info["latest_news"].extend(rss_news)
        snapshot["targets_data"][raw_ticker] = t_info

    # 3. 计算金油比、BTC/黄金比、美债期限利差
    gold_price = snapshot["macro_prices"].get("COMEX黄金期货 (GC=F)", {}).get("latest")
    oil_price = snapshot["macro_prices"].get("WTI原油期货 (CL=F)", {}).get("latest")
    tnx_10y = snapshot["macro_prices"].get("10Y美债收益率 (^TNX)", {}).get("latest")
    irx_3m = snapshot["macro_prices"].get("13周短债收益率 (^IRX)", {}).get("latest")
    btc_price = snapshot["targets_data"].get("BTC", {}).get("latest_price")

    if isinstance(gold_price, (int, float)) and isinstance(oil_price, (int, float)) and oil_price > 0:
        go_ratio = round(gold_price / oil_price, 2)
        if go_ratio > 30:
            go_signal = "🚨 >30 衰退避险与潜在救市放水预警区"
        elif go_ratio < 15:
            go_signal = "🔴 <15 恶性石油通胀与暴力加息预警区"
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
            "value": f"1 BTC = {bg_ratio} 盎司黄金"
        }

    if isinstance(tnx_10y, (int, float)) and isinstance(irx_3m, (int, float)):
        spread = round(tnx_10y - irx_3m, 2)
        snapshot["cross_asset_ratios"]["美债期限利差 (10Y - 3M Yield Curve)"] = {
            "value": f"{spread:+.2f}%",
            "note": "正值代表收益率曲线陡峭化，负值代表倒挂衰退预警"
        }

    # 4. 扫描五大赛道新猎物雷达
    snapshot["new_hunter_radar"] = scan_sector_radar_candidates(existing_set)

    return snapshot


def call_gemini_with_fallback(api_key: str, prompt: str) -> str:
    """使用 REST API 调用 Gemini 3.x 模型群，具备自动降级与重试机制"""
    candidate_models = [
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-3.8-flash",
        "gemini-3.1-flash-lite",
        "gemini-flash-lite-latest",
    ]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192}
    }

    last_err = ""
    for model_name in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        for attempt in range(2):
            try:
                print(f"🧠 正在请求大模型 [{model_name}] (尝试 {attempt + 1}/2)...")
                resp = requests.post(url, json=payload, timeout=95)
                if resp.status_code == 200:
                    data = resp.json()
                    parts = data["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts if not p.get("thought", False))
                    if not text.strip():
                        text = "".join(p.get("text", "") for p in parts)
                    print(f"✅ 模型 [{model_name}] 响应成功！(长度: {len(text)} 字符)")
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
    """根据早报/晚报不同职能构建差异化 Prompt，生成 v3.0 投资情报"""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未检测到环境变量 GEMINI_API_KEY！请在 GitHub Secrets 中配置。")

    news_sop_path = METHOD_DIR / "新闻过滤与贝叶斯动态更新系统.md"
    news_sop_text = news_sop_path.read_text(encoding="utf-8")[:2000] if news_sop_path.exists() else ""

    if IS_MORNING:
        mode_instructions = """
### ☀️ 当前为【08:30 早报模式：美股隔夜收盘深度复盘 & 贝叶斯调分审计】
请按以下 6 大章节撰写完整 Markdown 报告（直接以 `---` YAML 开头）：
1. `## 一、🚨 买卖双扳机与量化量价仪表盘（🟢学徒买 · 🔴师傅卖）`
   - 表格列必须包含：`标的(双链)` | `所属池子(💎皇冠/🚀新物种)` | `收盘价(涨跌)` | `RSI(14) & MA200偏离` | `🟢学徒买点(距买点%)` | `🔴师傅卖点(泡沫止盈/证伪线)` | `排雷日历与操盘指令`
2. `## 二、🧭 宏观 7 大路标、金油双锚与链上硬数据快照`
   - 解读 10Y美债、WTI原油三档警戒线、COMEX黄金、实时【金油比】、【BTC/黄金比】、**【USDC 实时流通市值】**（对比 Circle 30% 增速目标）与【加密恐慌贪婪指数】。
3. `## 三、🔬 核心精锐二级/三级情报深潜（含预期差三问）`
   - 仅分析当前股票池中的 5 只活跃精锐（`GOOGL`、`BTC`、`CRCL`、`COIN`、`SPCX`），严禁出现已删除的 `RXRX / CT / ZEC`！
   - 对 `[[SpaceX SPCX]]` 必须结合其 **【🚀新物种生命力 85.5% vs 12/08 解禁倒计时】** 双重属性分析。
4. `## 四、🔭 五大赛道二阶传导与「新物种/错杀猎物雷达」（详见 [[新物种与候选猎物雷达池]]）`
   - 结合 `new_hunter_radar` 中的数据，必须输出一张 **【🔭 候选猎物雷达追踪表】**，表格列必须包含：`候选标的` | `归属池` | `最新价(涨跌) & RSI(14)` | `💡核心推荐理由(第一性原理/护城河)` | `⚠️当前卡点与转正观察价`，绝不允许只给代码不写推荐理由！
5. `## 五、⚖️ 贝叶斯动态调分建议表（待坤哥人工确认）`
6. `## 六、🗑️ 今日已过滤的一级噪音（半衰期 < 1 周）`
"""
    else:
        mode_instructions = """
### 🌙 当前为【21:30 晚报模式：美股盘前宏观/金油异动 & 今晚挂单狙击快报】
⚠️ **晚报铁律：拒绝重复早报的长篇大论！必须极度精炼、实战导向，专为美股开盘前 1 小时盯盘与挂单服务！**
请按以下 4 大精简实战章节输出：
1. `## 一、🎯 今晚开盘「学徒买 · 师傅卖」挂单作战表（仅列主池 5 大精锐）`
   - 表格列：`标的(双链)` | `盘前/最新价` | `RSI(14)` | `🟢今晚买点挂单线(差距%)` | `🔴今晚止盈/止损红线` | `今晚开盘一句话动作指令`
2. `## 二、⚡ 欧美盘宏观、金油比与链上资金异动速递`
   - 聚焦过去 12 小时内 10Y美债、WTI原油、黄金、USDC 流通市值、加密恐慌贪婪指数的最新边际变化。
3. `## 三、🔭 「新物种与错杀猎物雷达池」盘前异动与推荐逻辑追踪（详见 [[新物种与候选猎物雷达池]]）`
   - 必须输出一张 **【🔭 预备队猎物雷达追踪表】**，列包含：`候选标的(PLTR/NVDA/TSLA/RKLB等)` | `最新价 & RSI(14)` | `💡为什么值得盯(核心推荐理由)` | `🎯转正观察区与今晚策略`。
4. `## 四、⚖️ 盘前紧急贝叶斯调分或排雷提醒（若无则写“今日晚盘维持早报评分不变”）`
"""

    prompt = f"""你现在是「坤哥的第二大脑」专属机构级首席投资官与量化风控官。
今天是北京时间 **{TODAY_STR}**，当前执行批次为：**{SESSION_LABEL}**。

### 一、系统实时拉取的量化盘面、链上硬数据与全球财经资讯
```json
{json.dumps(market_snapshot, ensure_ascii=False, indent=2)}
```

### 二、坤哥当前活跃股票池档案（85% 皇冠复利池 + 15% 新物种观察池，共 {len(targets)} 只精锐）
```json
{json.dumps(targets, ensure_ascii=False, indent=2)}
```

---

{mode_instructions}

### 四、通用格式硬性要求（严格输出两部分，用 `[BARK_PUSH_START]` 分隔）
- **严禁输出任何已删除标的（RXRX、CT、ZEC）！**
- 正文中所有的美元符号必须转义为 `\\$`（如 `\\$120`、`\\$75,500`），防止 Obsidian KaTeX 公式渲染错乱！
- 必须使用 Obsidian 双链语法（如 `[[Alphabet GOOGL]]`、`[[Bitcoin BTC]]`、`[[Circle CRCL]]`、`[[Coinbase COIN]]`、`[[SpaceX SPCX]]`、`[[股票池]]`、`[[颠覆性新物种与生命力评估模型]]`、`[[新物种与候选猎物雷达池]]`）。

#### 分隔符（在 Markdown 报告全部结束后，独占一行写以下标记）：
`[BARK_PUSH_START]`

#### 第二部分：用于推送到坤哥 iPhone Bark 的精简 JSON（严禁包裹代码块，直接输出合法 JSON 对象）
{{
  "alert_level": "active 或 timeSensitive（若有标的距买点/卖点 <15%、或 RSI≤32 / RSI≥75、或排雷日历 <10天、或 WTI原油 >$90，设为 timeSensitive）",
  "title": "📡 {TODAY_STR} {'早报' if IS_MORNING else '晚报'}：一句话核心结论（不超过22个字）",
  "body": "用 4 行极简要点列出：\\n1. 🟢🔴 双扳机监控（最近买点/卖点标的与RSI）\\n2. 🚀 新物种与排雷预警（SPCX解禁/猎物雷达）\\n3. 🧭 宏观与链上硬数据（10Y美债/油价/USDC市值）\\n4. ⚖️ 操盘与调分指令"
}}
"""

    full_text = call_gemini_with_fallback(api_key, prompt)

    if "[BARK_PUSH_START]" in full_text:
        md_part, bark_part = full_text.split("[BARK_PUSH_START]", 1)
    elif "===BARK_SUMMARY_SPLIT===" in full_text:
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
            "title": f"📡 {TODAY_STR} {'早报' if IS_MORNING else '晚报'}已送达",
            "body": "今日买卖双扳机、RSI量价指标与新物种雷达巡检已完成，请点击查看。"
        }

    return md_part, bark_data


def save_report_to_obsidian(md_content: str):
    """将生成的日报写入 Obsidian 05_每日情报 目录，自动清理已废弃标的残留，并更新总览索引"""
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    report_path = DAILY_DIR / f"{TODAY_STR}_每日投资情报.md"

    def purge_deprecated_lines(text: str) -> str:
        """过滤掉旧报告表格中可能残留的 RXRX / Concrete CT / Zcash ZEC 行"""
        deprecated_patterns = ("RXRX", "Recursion", "Concrete CT", "Zcash ZEC", "| **CT** |", "| **ZEC** |")
        cleaned_lines = [
            line for line in text.splitlines()
            if not (line.lstrip().startswith("|") and any(p in line for p in deprecated_patterns))
        ]
        return "\n".join(cleaned_lines)

    md_content = purge_deprecated_lines(md_content)

    if report_path.exists() and not IS_MORNING:
        existing = purge_deprecated_lines(report_path.read_text(encoding="utf-8"))
        # 清理晚报可能自带的重复 YAML Frontmatter
        clean_evening = re.sub(r"^---\s*\n.*?\n---\s*\n", "", md_content, flags=re.DOTALL).strip()
        if "## 🌙 晚盘" not in existing:
            combined = existing.rstrip() + f"\n\n---\n\n## 🌙 晚盘开盘作战快报（{NOW_BJT.strftime('%H:%M')} 更新）\n\n" + clean_evening
            report_path.write_text(combined, encoding="utf-8")
        else:
            head_part = existing.split("## 🌙 晚盘")[0].rstrip()
            combined = head_part + f"\n\n---\n\n## 🌙 晚盘开盘作战快报（{NOW_BJT.strftime('%H:%M')} 更新）\n\n" + clean_evening
            report_path.write_text(combined, encoding="utf-8")
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
    print(f"🚀 开始执行【坤哥的第二大脑 · 每日投资情报 v3.0】({TODAY_STR} {SESSION_LABEL})...")
    targets = load_portfolio_targets()
    print(f"📦 已加载活跃精锐标的 {len(targets)} 只: {[t['ticker'] for t in targets]}")

    market_snapshot = fetch_realtime_market_and_news(targets)
    print("📈 已拉取实时量价指标 (RSI/MA200)、链上 USDC 市值、金油双锚与新物种雷达数据。")

    md_content, bark_data = generate_intelligence_with_gemini(targets, market_snapshot)
    report_path = save_report_to_obsidian(md_content)
    print(f"📝 每日情报已写入: {report_path}")

    send_bark_notification(bark_data)
    print("🎉 v3.0 全部流程执行完毕！")


if __name__ == "__main__":
    main()
