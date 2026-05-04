#!/usr/bin/env python3
"""
量化因子评分系统 v1
替代 AI 看新闻选股，用可量化、可回溯的因子模型
六类因子：动量、价值、质量、波动率、流动性、技术面
输出：候选股因子排名
"""
import json, sys, subprocess, time, math
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))

# 导入增强因子模块
try:
    from akshare_data import (
        calc_all_enhanced_factors, score_enhanced_factors,
        get_index_kline as get_index_kline_enhanced
    )
    HAS_ENHANCED = True
except ImportError:
    HAS_ENHANCED = False

# === 因子配置 ===
FACTOR_WEIGHTS = {
    "momentum_20d": 15,     # 20日动量（近20日涨幅）
    "momentum_5d": 10,      # 5日动量（短期趋势）
    "volume_trend": 12,     # 量能趋势（5日均量/20日均量）
    "ma_alignment": 15,     # 均线多头排列强度
    "macd_hist": 10,        # MACD 柱状图方向
    "rsi_position": 8,      # RSI 位置（50-70最佳）
    "volatility": 10,       # 波动率（低波动加分）
    "turnover_rate": 8,     # 换手率（适中加分）
    "amount_rank": 7,       # 成交额排名
    "sector_momentum": 5,   # 所属板块动量
}

# === 数据获取 ===

def curl_get(url, timeout=12, retries=3):
    """带重试的 HTTP GET"""
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["curl", "-s", "--connect-timeout", "8", "-H",
                 "Referer: https://finance.sina.com.cn/", url],
                capture_output=True, timeout=timeout)
            text = r.stdout.decode("utf-8", errors="ignore").strip()
            if text and text != "null":
                return text
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(1 * (attempt + 1))
    return ""

def get_kline(code, days=60):
    """获取日K线数据（腾讯接口）"""
    market_code = f"sh{code}" if code.startswith("6") else f"sz{code}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market_code},day,,,{days},qfq"
    try:
        text = curl_get(url)
        data = json.loads(text)
        klines = data.get("data", {}).get(market_code, {})
        return klines.get("qfqday") or klines.get("day", [])
    except Exception:
        return []

def get_realtime_batch(codes):
    """批量获取实时行情（新浪接口）"""
    if not codes:
        return {}
    # 构造新浪代码格式
    sina_codes = []
    for c in codes:
        if c.startswith("6"):
            sina_codes.append(f"sh{c}")
        else:
            sina_codes.append(f"sz{c}")
    code_str = ",".join(sina_codes)
    url = f"https://hq.sinajs.cn/list={code_str}"
    text = curl_get(url, timeout=10)
    if not text:
        return {}
    result = {}
    for line in text.strip().split("\n"):
        if "=" not in line:
            continue
        var_part, data_part = line.split("=", 1)
        code_raw = var_part.split("_")[-1]
        pure_code = code_raw.lstrip("shsz")
        fields = data_part.strip('";\n').split(",")
        if len(fields) < 32:
            continue
        try:
            result[pure_code] = {
                "name": fields[0],
                "open": float(fields[1]) if fields[1] else 0,
                "pre_close": float(fields[2]) if fields[2] else 0,
                "price": float(fields[3]) if fields[3] else 0,
                "high": float(fields[4]) if fields[4] else 0,
                "low": float(fields[5]) if fields[5] else 0,
                "volume": float(fields[8]) if fields[8] else 0,  # 成交量（股）
                "amount": float(fields[9]) if fields[9] else 0,  # 成交额（元）
                "turnover_rate": float(fields[31]) if len(fields) > 31 and fields[31] else 0,
            }
        except (ValueError, IndexError):
            continue
    return result

def get_sector_ranking():
    """获取板块涨幅排名（通过 sector_rotation.py）"""
    try:
        r = subprocess.run(
            ["python3", str(Path(__file__).parent / "sector_rotation.py"), "--json"],
            capture_output=True, timeout=30)
        data = json.loads(r.stdout.decode("utf-8").strip())
        sectors = []
        for s in data.get("sector_ranking", []):
            sectors.append({"name": s["name"], "avg_change": s.get("change", 0)})
        return sectors
    except Exception:
        return []

def guess_sector(stock_name, stock_code=""):
    """根据名称猜测板块（匹配 sector_rotation.py 的板块名）"""
    kw_map = {
        "电子半导体": ["电子", "芯片", "半导体", "封测", "面板", "显示"],
        "计算机软件": ["软件", "信息", "计算", "数据", "云", "智能", "网络", "IT"],
        "通信5G": ["通信", "光纤", "5G", "基站", "天线"],
        "军工": ["军工", "航天", "航空", "兵器", "船舶", "导弹", "卫星"],
        "新能源": ["新能源", "锂", "光伏", "风电", "储能", "电池", "充电"],
        "医药生物": ["医药", "药业", "生物", "医疗", "疫苗", "诊断"],
        "汽车": ["汽车", "车", "整车", "零部件"],
        "化工材料": ["化工", "化学", "材料", "新材"],
        "钢铁有色": ["有色", "铜", "铝", "稀土", "钴", "钢铁", "钢"],
        "食品饮料": ["食品", "饮料", "白酒", "乳业", "调味"],
        "家电消费": ["家电", "空调", "冰箱", "美的", "格力"],
        "银行金融": ["银行", "证券", "保险", "金融"],
        "房地产": ["地产", "房产", "置业", "置地"],
        "机械设备": ["机械", "重工", "工程", "装备", "自动化", "机器人"],
    }
    for sector, keywords in kw_map.items():
        for kw in keywords:
            if kw in stock_name:
                return sector
    return "其他"

# === 因子计算 ===

def calc_kline_factors(klines):
    """从K线数据计算技术因子"""
    if not klines or len(klines) < 25:
        return None

    closes = [float(k[2]) for k in klines]
    highs = [float(k[3]) for k in klines]
    lows = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines if len(k) > 5]

    if len(closes) < 25 or len(volumes) < 20:
        return None

    factors = {}

    # 1. 20日动量
    if closes[-20] > 0:
        factors["momentum_20d"] = (closes[-1] - closes[-20]) / closes[-20] * 100

    # 2. 5日动量
    if closes[-5] > 0:
        factors["momentum_5d"] = (closes[-1] - closes[-5]) / closes[-5] * 100

    # 3. 量能趋势（5日均量 / 20日均量）
    avg_vol_5 = sum(volumes[-5:]) / 5
    avg_vol_20 = sum(volumes[-20:]) / 20
    if avg_vol_20 > 0:
        factors["volume_trend"] = avg_vol_5 / avg_vol_20

    # 4. 均线多头排列强度
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    ma_score = 0
    if ma5 > ma10:
        ma_score += 1
    if ma10 > ma20:
        ma_score += 1
    if closes[-1] > ma5:
        ma_score += 1
    if ma5 > ma10 > ma20:
        ma_score += 1  # 完美多头加分
    factors["ma_alignment"] = ma_score  # 0-4

    # 5. MACD 柱状图
    if len(closes) >= 26:
        ema12 = calc_ema(closes, 12)
        ema26 = calc_ema(closes, 26)
        dif = ema12 - ema26
        # 简化：用 DIF 方向代替完整 MACD
        if len(closes) >= 27:
            ema12_prev = calc_ema(closes[:-1], 12)
            ema26_prev = calc_ema(closes[:-1], 26)
            dif_prev = ema12_prev - ema26_prev
            factors["macd_hist"] = 1 if dif > dif_prev else -1
        else:
            factors["macd_hist"] = 0

    # 6. RSI（14日）
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(-14, 0):
            change = closes[i] - closes[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            factors["rsi_14"] = 100 - (100 / (1 + rs))
        else:
            factors["rsi_14"] = 100

    # 7. 波动率（20日日收益率标准差）
    if len(closes) >= 21:
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(-20, 0) if closes[i-1] > 0]
        if returns:
            mean_r = sum(returns) / len(returns)
            std_r = math.sqrt(sum((r - mean_r)**2 for r in returns) / len(returns))
            factors["volatility_20d"] = std_r * 100  # 百分比

    return factors

def calc_ema(prices, period):
    """计算 EMA"""
    if len(prices) < period:
        return prices[-1] if prices else 0
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def normalize_factor(value, factor_name, all_values):
    """将因子值归一化到 0-100 分"""
    if not all_values or value is None:
        return 50
    valid = [v for v in all_values if v is not None]
    if not valid:
        return 50
    min_v = min(valid)
    max_v = max(valid)
    if max_v == min_v:
        return 50

    # 某些因子越小越好（波动率）
    if factor_name == "volatility_20d":
        score = 100 - (value - min_v) / (max_v - min_v) * 100
    # RSI：50-70 最佳
    elif factor_name == "rsi_14":
        if 50 <= value <= 70:
            score = 80 + (value - 50) / 20 * 20
        elif value < 50:
            score = value / 50 * 60
        else:
            score = max(0, 100 - (value - 70) * 2)
    # 均线强度：0-4 映射到 0-100
    elif factor_name == "ma_alignment":
        score = value / 4 * 100
    # MACD：-1/0/1
    elif factor_name == "macd_hist":
        score = (value + 1) / 2 * 100
    # 量比：1.0-2.0 最佳
    elif factor_name == "volume_trend":
        if 1.0 <= value <= 2.0:
            score = 70 + (value - 1.0) * 30
        elif value < 1.0:
            score = value * 70
        else:
            score = max(0, 100 - (value - 2.0) * 20)
    else:
        score = (value - min_v) / (max_v - min_v) * 100

    return round(max(0, min(100, score)), 1)

# === 主评分函数 ===

def score_candidates(candidates, top_n=10):
    """
    对候选股进行因子评分（v2: 集成增强因子）
    candidates: list of dict, each has at least "code" and "name"
    """
    if not candidates:
        return []

    # 批量获取实时行情
    codes = [c["code"] for c in candidates]
    realtime = {}
    # 分批获取（新浪限制）
    batch_size = 30
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        rt = get_realtime_batch(batch)
        realtime.update(rt)
        if i + batch_size < len(codes):
            time.sleep(0.3)

    # 获取板块排名
    sector_ranking = get_sector_ranking()
    top_sectors = [s["name"] for s in sector_ranking[:5]]
    bottom_sectors = [s["name"] for s in sector_ranking[-5:]]

    # v2: 预获取上证指数K线（增强因子用）
    index_klines = None
    if HAS_ENHANCED:
        try:
            index_klines = get_index_kline_enhanced(120)
        except Exception:
            pass

    # 计算每只股票的因子
    scored = []
    all_factors = {}  # factor_name -> [values]

    for cand in candidates:
        code = cand["code"]
        name = cand.get("name", "")
        rt = realtime.get(code, {})

        # 获取K线（增强因子需要更多数据）
        klines = get_kline(code, 120 if HAS_ENHANCED else 60)
        time.sleep(0.1)

        kline_factors = calc_kline_factors(klines)
        if not kline_factors:
            continue

        # 补充实盘数据
        if rt:
            kline_factors["turnover_rate"] = rt.get("turnover_rate", 0)
            kline_factors["amount"] = rt.get("amount", 0)
            if rt.get("pre_close", 0) > 0 and rt.get("price", 0) > 0:
                kline_factors["intraday_change"] = (rt["price"] - rt["pre_close"]) / rt["pre_close"] * 100

        # 板块动量
        sector = guess_sector(name, code)
        if sector in [s.split("(")[0] for s in top_sectors]:
            kline_factors["sector_momentum"] = 2
        elif sector in [s.split("(")[0] for s in bottom_sectors]:
            kline_factors["sector_momentum"] = -1
        else:
            kline_factors["sector_momentum"] = 0

        # v2: 计算增强因子
        enhanced_bonus = 0
        enhanced_details = {}
        if HAS_ENHANCED and klines:
            try:
                enhanced_factors = calc_all_enhanced_factors(code, klines, index_klines)
                if enhanced_factors:
                    enhanced_bonus, enhanced_details = score_enhanced_factors(enhanced_factors)
                    # 合并到因子集合（用于归一化）
                    for fname, fval in enhanced_factors.items():
                        if isinstance(fval, (int, float)):
                            kline_factors[f"enh_{fname}"] = fval
            except Exception:
                pass

        # 收集因子值用于归一化
        for fname, fval in kline_factors.items():
            if fname not in all_factors:
                all_factors[fname] = []
            all_factors[fname].append(fval)

        scored.append({
            "code": code,
            "name": name,
            "sector": sector,
            "price": rt.get("price", 0),
            "change_pct": rt.get("intraday_change", 0),
            "factors": kline_factors,
            "enhanced_bonus": enhanced_bonus,
            "enhanced_details": enhanced_details,
        })

    # 归一化并计算综合分
    for stock in scored:
        total = 0
        factor_scores = {}
        for fname, weight in FACTOR_WEIGHTS.items():
            fval = stock["factors"].get(fname)
            all_vals = all_factors.get(fname, [])
            norm_score = normalize_factor(fval, fname, all_vals)
            factor_scores[fname] = norm_score
            total += norm_score * weight / 100
        # v2: 叠加增强因子加分（±25分范围，缩放到100分制）
        enh_bonus = stock.get("enhanced_bonus", 0)
        total += enh_bonus * 0.4  # 增强因子权重系数
        stock["factor_scores"] = factor_scores
        stock["total_score"] = round(total, 1)

    # 按总分排序
    scored.sort(key=lambda x: x["total_score"], reverse=True)

    # 输出排名
    result = []
    for i, s in enumerate(scored[:top_n]):
        entry = {
            "rank": i + 1,
            "code": s["code"],
            "name": s["name"],
            "sector": s["sector"],
            "price": s["price"],
            "change_pct": round(s.get("change_pct", 0), 2),
            "total_score": s["total_score"],
            "factor_scores": s["factor_scores"],
            "top_factors": get_top_factors(s["factor_scores"], 3),
        }
        # v2: 增强因子详情
        enh_details = s.get("enhanced_details", {})
        if enh_details:
            entry["enhanced_bonus"] = s.get("enhanced_bonus", 0)
            entry["enhanced_signals"] = enh_details
        result.append(entry)

    return result

def get_top_factors(scores, n=3):
    """获取得分最高的 N 个因子"""
    sorted_factors = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [{f[0]: f[1]} for f in sorted_factors[:n]]

# === CLI ===

def main():
    import argparse
    parser = argparse.ArgumentParser(description="量化因子评分")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    parser.add_argument("--top", type=int, default=10, help="输出前N名")
    parser.add_argument("--input", type=str, help="候选股JSON文件（stock_screener输出）")
    parser.add_argument("--codes", type=str, help="逗号分隔的股票代码")
    args = parser.parse_args()

    candidates = []

    if args.input:
        with open(args.input) as f:
            data = json.load(f)
            if isinstance(data, list):
                candidates = data
            elif isinstance(data, dict) and "candidates" in data:
                candidates = data["candidates"]
    elif args.codes:
        for c in args.codes.split(","):
            c = c.strip()
            candidates.append({"code": c, "name": ""})
    else:
        # 从 stdin 读取
        stdin_data = sys.stdin.read().strip()
        if stdin_data:
            try:
                data = json.loads(stdin_data)
                if isinstance(data, list):
                    candidates = data
                elif isinstance(data, dict) and "candidates" in data:
                    candidates = data["candidates"]
            except json.JSONDecodeError:
                print("Error: Invalid JSON input", file=sys.stderr)
                sys.exit(1)

    if not candidates:
        print("Error: No candidates provided", file=sys.stderr)
        sys.exit(1)

    results = score_candidates(candidates, top_n=args.top)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'='*70}")
        print(f"{'排名':>4} {'代码':>8} {'名称':<10} {'板块':<8} {'价格':>8} {'涨幅':>8} {'总分':>6}")
        print(f"{'='*70}")
        for r in results:
            print(f"{r['rank']:>4} {r['code']:>8} {r['name']:<10} {r['sector']:<8} "
                  f"{r['price']:>8.2f} {r['change_pct']:>+7.2f}% {r['total_score']:>6.1f}")
            top3 = ", ".join([f"{list(f.keys())[0]}={list(f.values())[0]:.0f}" for f in r["top_factors"]])
            print(f"     优势因子: {top3}")
            enh = r.get("enhanced_signals", {})
            if enh:
                enh_str = ", ".join(f"{k}:{v}" for k, v in enh.items())
                print(f"     增强信号: {enh_str}  加分: {r.get('enhanced_bonus', 0):+.0f}")
        print(f"{'='*70}")

if __name__ == "__main__":
    main()
