#!/usr/bin/env python3
"""
量化选股海选器 v2（新浪数据源）
四层漏斗：基础过滤 → Top80 → 技术验证 → 综合评分
新增：涨幅窗口 1-9.5%、基本面过滤、涨停板标记
输出 Top 10 候选
"""
import json, sys, subprocess, re, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))


_sector_ranking_cache = None

def get_sector_ranking():
    """获取行业板块涨幅排名（通过 sector_rotation.py，带缓存）"""
    global _sector_ranking_cache
    if _sector_ranking_cache is not None:
        return _sector_ranking_cache
    try:
        r = subprocess.run(
            ["python3", str(Path(__file__).parent / "sector_rotation.py"), "--json"],
            capture_output=True, timeout=30)
        data = json.loads(r.stdout.decode("utf-8").strip())
        sectors = []
        for s in data.get("sector_ranking", []):
            sectors.append({"name": s["name"], "avg_change": s.get("change", 0)})
        _sector_ranking_cache = sectors
        return sectors
    except Exception:
        return []

def guess_sector(stock_name):
    """根据股票名称猜测所属板块（匹配 sector_rotation.py 的板块名）"""
    kw_map = {
        "电子半导体": ["电子", "芯片", "半导体", "光电", "显示", "封测", "晶圆"],
        "计算机软件": ["软件", "信息", "数据", "计算", "网络", "智能", "云计算"],
        "通信5G": ["通信", "光纤", "5G", "基站", "光模块"],
        "医药生物": ["药业", "医药", "生物", "医疗", "疫苗", "诊断"],
        "食品饮料": ["食品", "饮料", "乳业", "白酒", "啤酒"],
        "汽车": ["汽车", "整车", "零部件"],
        "新能源": ["电气", "光伏", "风电", "储能", "电池", "锂电", "新能源"],
        "机械设备": ["机械", "设备", "自动化", "机器人", "数控"],
        "化工材料": ["化工", "化学", "新材料", "橡胶", "塑料"],
        "军工": ["军工", "航天", "国防", "导弹", "卫星"],
        "钢铁有色": ["钢铁", "特钢", "锂", "钴", "稀土", "铜", "铝", "锌"],
        "房地产": ["地产", "置业", "房产"],
        "银行金融": ["银行", "证券", "保险", "金融"],
        "家电消费": ["家电", "空调", "冰箱", "电视", "食品", "饮料", "白酒"],
    }
    for sector, kws in kw_map.items():
        for kw in kws:
            if kw in stock_name:
                return sector
    return None


def get_all_stocks_sina():
    """新浪获取全市场A股列表（分页）"""
    all_items = []
    page = 1
    while True:
        url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num=80&sort=changepercent&asc=0&node=hs_a")
        success = False
        for attempt in range(3):
            try:
                r = subprocess.run(
                    ["curl", "-s", "--connect-timeout", "10",
                     "-H", "Referer: https://finance.sina.com.cn/", url],
                    capture_output=True, timeout=15)
                text = r.stdout.decode("utf-8", errors="ignore").strip()
                if not text or text == "null":
                    break
                items = json.loads(text)
                if not items:
                    break
                all_items.extend(items)
                if len(items) < 80:
                    return all_items
                page += 1
                time.sleep(0.3)  # 请求间隔防封IP
                success = True
                break
            except Exception:
                if attempt < 2:
                    time.sleep(1)
        if not success:
            break
    return all_items

def get_kline(code, days=60, max_retries=3):
    """获取个股日K线（腾讯接口，含重试）"""
    market_code = f"sh{code}" if code.startswith("6") else f"sz{code}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market_code},day,,,{days},qfq"
    for attempt in range(max_retries):
        try:
            r = subprocess.run(["curl", "-s", "--connect-timeout", "8", url],
                               capture_output=True, timeout=12)
            data = json.loads(r.stdout.decode("utf-8"))
            klines = data.get("data", {}).get(market_code, {})
            rows = klines.get("qfqday") or klines.get("day", [])
            if rows:
                return rows
        except Exception:
            pass
        if attempt < max_retries - 1:
            time.sleep(1 * (attempt + 1))
    return []

# === v2 新增：上证指数K线缓存 ===
_index_klines_cache = None

def get_index_kline(days=25, max_retries=3):
    """获取上证指数日K线（腾讯接口，带缓存）"""
    global _index_klines_cache
    if _index_klines_cache is not None:
        return _index_klines_cache
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,{days},qfq"
    for attempt in range(max_retries):
        try:
            r = subprocess.run(["curl", "-s", "--connect-timeout", "8", url],
                               capture_output=True, timeout=12)
            data = json.loads(r.stdout.decode("utf-8"))
            klines = data.get("data", {}).get("sh000001", {})
            rows = klines.get("qfqday") or klines.get("day", [])
            if rows:
                _index_klines_cache = rows
                return rows
        except Exception:
            pass
        if attempt < max_retries - 1:
            time.sleep(1 * (attempt + 1))
    return []

def calc_relative_strength(code, period=20):
    """计算相对强弱（个股 vs 上证指数，近N日收益率之比）
    RS > 1.0 = 跑赢大盘，RS < 1.0 = 跑输大盘
    返回: (rs_ratio, stock_return_pct, index_return_pct)
    """
    stock_klines = get_kline(code, days=period + 5)
    index_klines = get_index_kline(days=period + 5)

    if not stock_klines or len(stock_klines) < period:
        return None, None, None
    if not index_klines or len(index_klines) < period:
        return None, None, None

    stock_close_now = float(stock_klines[-1][2])
    stock_close_ago = float(stock_klines[-period][2])
    index_close_now = float(index_klines[-1][2])
    index_close_ago = float(index_klines[-period][2])

    if stock_close_ago <= 0 or index_close_ago <= 0:
        return None, None, None

    stock_ret = (stock_close_now - stock_close_ago) / stock_close_ago
    index_ret = (index_close_now - index_close_ago) / index_close_ago

    if abs(index_ret) < 0.001:
        # 大盘几乎不动，用绝对收益差
        rs = 1.0 + (stock_ret - index_ret) * 10
    else:
        rs = (1 + stock_ret) / (1 + index_ret)

    return round(rs, 4), round(stock_ret * 100, 2), round(index_ret * 100, 2)

def calc_technical_score(klines):
    """技术指标评分（从60日K线）"""
    if not klines or len(klines) < 20:
        return 0, {}
    closes = [float(k[2]) for k in klines]
    volumes = [float(k[5]) for k in klines if len(k) > 5]

    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20

    ema12 = sum(closes[-12:]) / 12
    ema26 = sum(closes[-26:]) / 26 if len(closes) >= 26 else closes[-1]
    dif = ema12 - ema26

    avg_vol_5 = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 1
    avg_vol_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
    vol_ratio = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 1

    score = 0
    detail = {}

    ma_aligned = ma5 > ma10 > ma20
    detail["ma_aligned"] = ma_aligned
    if ma_aligned:
        score += 2

    macd_positive = dif > 0
    detail["macd_positive"] = macd_positive
    if macd_positive:
        score += 1

    detail["vol_ratio"] = round(vol_ratio, 2)
    if vol_ratio > 1.5:
        score += 1

    if len(volumes) >= 5:
        recent_vol = sum(volumes[-5:]) / 5
        old_vol = sum(volumes[-20:-5]) / 15 if len(volumes) >= 20 else recent_vol
        if old_vol > 0 and recent_vol / old_vol > 1.5:
            score += 1
            detail["vol_breakout"] = True

    return score, detail

def screen_stocks(top_n=10, exclude_codes=None, json_output=True, max_price=20.0):
    """主筛选函数"""
    if exclude_codes is None:
        exclude_codes = set()

    stocks = get_all_stocks_sina()
    if not stocks:
        msg = {"error": "无法获取股票列表"}
        if json_output:
            print(json.dumps(msg, ensure_ascii=False))
        return []

    # ① 基础过滤
    candidates = []
    limit_up_stocks = []  # 记录涨停板股票（不买但标记）
    for s in stocks:
        symbol = s.get("symbol", "")
        code = s.get("code", "")
        name = s.get("name", "")
        price = float(s.get("trade", 0) or 0)
        change = float(s.get("changepercent", 0) or 0)
        turnover = float(s.get("turnoverratio", 0) or 0)
        amount = float(s.get("amount", 0) or 0)
        mktcap = float(s.get("nmc", 0) or 0)
        pe = float(s.get("pe", 0) or 0)

        if not all([code, name, price > 0]):
            continue

        # 排除 ST
        if "ST" in name or "st" in name:
            continue
        # 排除北交所和科创板
        if symbol.startswith("bj") or code.startswith("688"):
            continue
        # 排除已持有
        if code in exclude_codes:
            continue

        # 涨停板标记但不入选
        if change >= 9.9:
            limit_up_stocks.append({
                "code": code, "name": name, "change": change, "price": price,
            })
            continue

        # 涨幅 1-9.5%（放宽窗口）
        if change < 1.0 or change > 9.5:
            continue
        # 换手 > 1.5%（稍放宽）
        if turnover < 1.5:
            continue
        # 价格 3-20 元
        if price < 3.0 or price > max_price:
            continue
        # 成交额 > 3000万
        if amount < 30000000:
            continue
        # 流通市值 > 30亿
        if mktcap < 300000:
            continue

        # 基本面过滤：PE 为负（亏损）排除
        if pe < 0:
            continue

        market_code = symbol if symbol.startswith(("sh", "sz")) else f"sz{code}"

        candidates.append({
            "code": code,
            "market_code": market_code,
            "name": name,
            "price": price,
            "change": change,
            "turnover": turnover,
            "amount": round(amount / 1e8, 2),
            "mktcap": round(mktcap / 10000, 2),
            "pe": round(pe, 2),
        })

    # ②b 板块轮动排名（自上而下：先看板块再选个股）
    sector_ranking = get_sector_ranking()
    top3_sectors = {s["name"] for s in sector_ranking[:3]} if sector_ranking else set()

    # ② 预排序（多因子轻量预评分，避免纯涨幅主导）
    for s in candidates:
        # 流通市值越大越好（流动性）
        mkt_score = min(s.get("mktcap", 0) / 1000, 5)  # 0-5
        # 成交额越大越好
        amt_score = min(s.get("amount", 0) / 10, 3)    # 0-3
        # 涨幅温和区间加分（3-6% 最佳，避免追极端）
        chg = s.get("change", 0)
        chg_score = 2 if 3.0 <= chg <= 6.0 else 1 if 2.0 <= chg <= 8.0 else 0
        # ②b 板块轮动加分（自上而下：优先从 Top 3 板块选）
        stock_sector = guess_sector(s["name"])
        sector_bonus = 0
        if stock_sector in top3_sectors:
            sector_bonus = 3  # Top 3 板块大加分
        elif sector_ranking:
            # Top 4-6 板块小加分
            mid_sectors = {s2["name"] for s2 in sector_ranking[3:6]}
            if stock_sector in mid_sectors:
                sector_bonus = 1
        s["pre_score"] = mkt_score + amt_score + chg_score + sector_bonus
        s["sector"] = stock_sector
    candidates.sort(key=lambda x: x["pre_score"], reverse=True)
    top_pool = candidates[:50]

    # ③ 技术验证 + 相对强弱（纯过滤，不做复杂评分）
    passed = []
    for i, stock in enumerate(top_pool):
        klines = get_kline(stock["code"], 60)
        tech_score, tech_detail = calc_technical_score(klines)

        tech_count = sum([
            tech_detail.get("ma_aligned", False),
            tech_detail.get("macd_positive", False),
            tech_detail.get("vol_ratio", 1) > 1.5,
            tech_detail.get("vol_breakout", False),
        ])
        if tech_count < 2:
            continue

        # 相对强弱计算
        rs_ratio, stock_ret, index_ret = calc_relative_strength(stock["code"], period=20)
        tech_detail["rs_ratio"] = rs_ratio
        tech_detail["rs_stock_ret"] = stock_ret
        tech_detail["rs_index_ret"] = index_ret

        # 流动性检查（滑点不可过大）
        if stock["price"] > 0:
            tick = 0.01 if stock["price"] < 10 else 0.05 if stock["price"] < 50 else 0.1
            slip_pct = (tick * 2) / stock["price"] * 100
            if "300" in stock["code"] or "301" in stock["code"]:
                slip_pct *= 1.5
            if slip_pct > 0.5:
                continue

        # 板块标签
        stock["sector"] = guess_sector(stock["name"])

        stock["tech_score"] = tech_score
        stock["tech_detail"] = tech_detail
        # 技术预评分（轻量，用于排序）
        stock["total_score"] = tech_score + (2 if rs_ratio and rs_ratio >= 1.1 else 0)
        passed.append(stock)

        if (i + 1) % 5 == 0:
            time.sleep(0.05)

    # 排序取 Top N（交给 factor_scorer 做精细评分）
    passed.sort(key=lambda x: x["total_score"], reverse=True)
    result = passed[:top_n]

    # 清理输出字段
    for s in result:
        s.pop("market_code", None)
        s.pop("mktcap", None)

    output = {
        "candidates": result,
        "limit_up_today": limit_up_stocks[:20],
        "total_screened": len(candidates),
        "passed_tech": len(passed),
        "sector_ranking": sector_ranking[:10] if sector_ranking else [],
        "top3_sectors": list(top3_sectors),
    }

    if json_output:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return result

if __name__ == "__main__":
    top = 10
    exclude = set()
    json_out = True
    max_price = 20.0
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--top" and i + 1 < len(args):
            top = int(args[i + 1])
            i += 2
        elif args[i] == "--max-price" and i + 1 < len(args):
            max_price = float(args[i + 1])
            i += 2
        elif args[i] == "--exclude" and i + 1 < len(args):
            exclude = set(args[i + 1].split(","))
            i += 2
        elif args[i] == "--no-json":
            json_out = False
            i += 1
        else:
            i += 1
    screen_stocks(top_n=top, exclude_codes=exclude, json_output=json_out, max_price=max_price)
