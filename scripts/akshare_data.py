#!/usr/bin/env python3
"""
增强因子数据模块（基于腾讯+新浪 API）
从已有可靠数据源计算更多量化因子，提升选股质量

新增因子类别：
1. 多周期动量（5/10/20/60日，动量加速度）
2. 波动率结构（上行/下行波动率比、波动率锥）
3. 量价背离检测（价格新高但量能萎缩）
4. KDJ 随机指标（金叉/死叉/超买超卖）
5. 布林带位置（%b + 带宽）
6. OBV 能量潮趋势
7. 支撑/阻力位距离
8. 多周期 RSI（7日/14日/28日）
9. 价格相对位置（距60日高低点）
10. 成交额集中度（近5日 vs 近20日）
"""
import json, sys, subprocess, time, math
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# === 数据获取（复用已验证的接口） ===

def curl_get(url, timeout=12, retries=3, headers=None):
    """带重试的 HTTP GET"""
    cmd = ["curl", "-s", "--connect-timeout", "8"]
    if headers:
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(url)
    for attempt in range(retries):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            text = r.stdout.decode("utf-8", errors="ignore").strip()
            if text:
                return text
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(0.5 * (attempt + 1))
    return ""

def get_kline(code, days=120):
    """获取日K线（腾讯接口）"""
    market_code = f"sh{code}" if code.startswith("6") else f"sz{code}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market_code},day,,,{days},qfq"
    try:
        text = curl_get(url, timeout=12)
        data = json.loads(text)
        klines = data.get("data", {}).get(market_code, {})
        return klines.get("qfqday") or klines.get("day", [])
    except Exception:
        return []

def get_realtime_sina(code):
    """获取实时行情（新浪接口）"""
    prefix = "sh" if code.startswith("6") else "sz"
    url = f"https://hq.sinajs.cn/list={prefix}{code}"
    headers = {"Referer": "https://finance.sina.com.cn/"}
    text = curl_get(url, timeout=8, headers=headers)
    if not text or "=" not in text:
        return None
    try:
        data_part = text.split("=", 1)[1].strip('";\n')
        fields = data_part.split(",")
        if len(fields) < 32:
            return None
        return {
            "name": fields[0],
            "open": float(fields[1]) if fields[1] else 0,
            "pre_close": float(fields[2]) if fields[2] else 0,
            "price": float(fields[3]) if fields[3] else 0,
            "high": float(fields[4]) if fields[4] else 0,
            "low": float(fields[5]) if fields[5] else 0,
            "volume": float(fields[8]) if fields[8] else 0,
            "amount": float(fields[9]) if fields[9] else 0,
            "date": fields[30] if len(fields) > 30 else "",
            "time": fields[31] if len(fields) > 31 else "",
        }
    except (ValueError, IndexError):
        return None

def get_index_kline(days=120):
    """获取上证指数K线"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,{days},qfq"
    try:
        text = curl_get(url, timeout=12)
        data = json.loads(text)
        klines = data.get("data", {}).get("sh000001", {})
        return klines.get("qfqday") or klines.get("day", [])
    except Exception:
        return []


# ============================================================
# 技术指标计算
# ============================================================

def calc_ema(prices, period):
    """指数移动平均"""
    if len(prices) < period:
        return prices[-1] if prices else 0
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calc_rsi(closes, period=14):
    """RSI 指标"""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calc_kdj(klines, n=9, m1=3, m2=3):
    """KDJ 随机指标"""
    if not klines or len(klines) < n:
        return None, None, None
    closes = [float(k[2]) for k in klines]
    highs = [float(k[3]) for k in klines]
    lows = [float(k[4]) for k in klines]
    
    rsv_list = []
    for i in range(n - 1, len(closes)):
        high_n = max(highs[i-n+1:i+1])
        low_n = min(lows[i-n+1:i+1])
        rsv_list.append((closes[i] - low_n) / (high_n - low_n) * 100 if high_n != low_n else 50)
    
    k_val, d_val = 50, 50
    for rsv in rsv_list:
        k_val = (m1 - 1) / m1 * k_val + 1 / m1 * rsv
        d_val = (m2 - 1) / m2 * d_val + 1 / m2 * k_val
    j_val = 3 * k_val - 2 * d_val
    return round(k_val, 2), round(d_val, 2), round(j_val, 2)

def calc_bollinger(closes, period=20, num_std=2):
    """布林带"""
    if not closes or len(closes) < period:
        return None
    subset = closes[-period:]
    middle = sum(subset) / period
    std = math.sqrt(sum((x - middle)**2 for x in subset) / period)
    upper = middle + num_std * std
    lower = middle - num_std * std
    price = closes[-1]
    pct_b = (price - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    bandwidth = (upper - lower) / middle if middle > 0 else 0
    return {
        "upper": round(upper, 2), "middle": round(middle, 2), "lower": round(lower, 2),
        "pct_b": round(pct_b, 4), "bandwidth": round(bandwidth, 4),
    }

def calc_obv(klines):
    """OBV 能量潮"""
    if not klines or len(klines) < 10:
        return None
    closes = [float(k[2]) for k in klines]
    volumes = [float(k[5]) for k in klines if len(k) > 5]
    if len(volumes) < len(closes):
        return None
    
    obv = 0
    obv_list = [obv]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv += volumes[i]
        elif closes[i] < closes[i-1]:
            obv -= volumes[i]
        obv_list.append(obv)
    
    if len(obv_list) >= 10:
        recent = sum(obv_list[-5:]) / 5
        old = sum(obv_list[-10:-5]) / 5
        obv_trend = (recent - old) / abs(old) * 100 if old != 0 else 0
    else:
        obv_trend = 0
    
    return {
        "obv_latest": round(obv, 0),
        "obv_trend_pct": round(obv_trend, 2),
        "direction": "up" if obv_trend > 5 else "down" if obv_trend < -5 else "flat",
    }

def calc_williams_r(klines, period=14):
    """威廉指标 %R（-100 ~ 0，<-80 超卖，>-20 超买）"""
    if not klines or len(klines) < period:
        return None
    highs = [float(k[3]) for k in klines[-period:]]
    lows = [float(k[4]) for k in klines[-period:]]
    close = float(klines[-1][2])
    high_n = max(highs)
    low_n = min(lows)
    if high_n == low_n:
        return -50
    return round((high_n - close) / (high_n - low_n) * -100, 2)

def calc_cci(klines, period=20):
    """CCI 顺势指标（>100 超买，<-100 超卖）"""
    if not klines or len(klines) < period:
        return None
    tp_list = []
    for k in klines[-period:]:
        h, l, c = float(k[3]), float(k[4]), float(k[2])
        tp_list.append((h + l + c) / 3)
    mean_tp = sum(tp_list) / period
    mean_dev = sum(abs(tp - mean_tp) for tp in tp_list) / period
    if mean_dev == 0:
        return 0
    return round((tp_list[-1] - mean_tp) / (0.015 * mean_dev), 2)


# ============================================================
# 增强因子计算
# ============================================================

def calc_all_enhanced_factors(code, klines, index_klines=None, realtime=None):
    """
    计算全部增强因子
    
    返回 dict，包含：
    - 多周期动量 + 动量加速度
    - 波动率结构（上行/下行比）
    - 量价背离信号
    - KDJ 信号
    - 布林带位置
    - OBV 趋势
    - 多周期 RSI
    - 价格相对位置
    - 成交额集中度
    - 支撑/阻力距离
    - 威廉指标
    - CCI
    """
    if not klines or len(klines) < 20:
        return None
    
    closes = [float(k[2]) for k in klines]
    highs = [float(k[3]) for k in klines]
    lows = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines if len(k) > 5]
    
    factors = {}
    
    # === 1. 多周期动量 ===
    for period, name in [(5, "5d"), (10, "10d"), (20, "20d")]:
        if len(closes) >= period and closes[-period] > 0:
            factors[f"momentum_{name}"] = round((closes[-1] - closes[-period]) / closes[-period] * 100, 2)
    
    # 动量加速度（5日动量 - 前5日的5日动量）
    if len(closes) >= 15:
        m5_now = (closes[-1] - closes[-5]) / closes[-5] * 100
        m5_prev = (closes[-5] - closes[-10]) / closes[-10] * 100
        factors["momentum_acceleration"] = round(m5_now - m5_prev, 2)
    
    # 60日动量
    if len(closes) >= 60 and closes[-60] > 0:
        factors["momentum_60d"] = round((closes[-1] - closes[-60]) / closes[-60] * 100, 2)
    
    # === 2. 波动率结构 ===
    if len(closes) >= 21:
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(-20, 0) if closes[i-1] > 0]
        up_returns = [r for r in returns if r > 0]
        down_returns = [abs(r) for r in returns if r < 0]
        
        up_vol = (sum(r**2 for r in up_returns) / len(up_returns))**0.5 * 100 if up_returns else 0
        down_vol = (sum(r**2 for r in down_returns) / len(down_returns))**0.5 * 100 if down_returns else 0
        
        factors["upside_volatility"] = round(up_vol, 3)
        factors["downside_volatility"] = round(down_vol, 3)
        factors["vol_ratio_up_down"] = round(up_vol / down_vol, 2) if down_vol > 0 else 999
        
        # 总波动率
        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - mean_r)**2 for r in returns) / len(returns))
        factors["volatility_20d"] = round(std_r * 100, 3)
    
    # === 3. 量价背离检测 ===
    if len(closes) >= 20 and len(volumes) >= 20:
        # 价格创20日新高但量能萎缩 = 看空背离
        price_at_high = closes[-1] >= max(closes[-20:])
        vol_declining = sum(volumes[-5:]) / 5 < sum(volumes[-20:-5]) / 15 * 0.8
        
        # 价格创20日新低但量能放大 = 看多背离（恐慌出清）
        price_at_low = closes[-1] <= min(closes[-20:])
        vol_increasing = sum(volumes[-5:]) / 5 > sum(volumes[-20:-5]) / 15 * 1.3
        
        if price_at_high and vol_declining:
            factors["vol_price_divergence"] = -1  # 看空背离
        elif price_at_low and vol_increasing:
            factors["vol_price_divergence"] = 1   # 看多背离
        else:
            factors["vol_price_divergence"] = 0   # 无背离
    
    # === 4. KDJ ===
    k, d, j = calc_kdj(klines)
    if k is not None:
        factors["kdj_k"] = k
        factors["kdj_d"] = d
        factors["kdj_j"] = j
        if k > d and j > 0 and j < 80:
            factors["kdj_signal"] = 1   # 金叉
        elif k < d and j < 100 and j > 20:
            factors["kdj_signal"] = -1  # 死叉
        else:
            factors["kdj_signal"] = 0
        # 超买超卖
        if j > 100:
            factors["kdj_zone"] = "overbought"
        elif j < 0:
            factors["kdj_zone"] = "oversold"
        else:
            factors["kdj_zone"] = "neutral"
    
    # === 5. 布林带 ===
    boll = calc_bollinger(closes)
    if boll:
        factors["boll_pct_b"] = boll["pct_b"]
        factors["boll_bandwidth"] = boll["bandwidth"]
        if 0.3 <= boll["pct_b"] <= 0.7:
            factors["boll_position_score"] = 80
        elif 0.2 <= boll["pct_b"] <= 0.8:
            factors["boll_position_score"] = 60
        elif boll["pct_b"] < 0.05:
            factors["boll_position_score"] = 90  # 超跌反弹机会
        else:
            factors["boll_position_score"] = 30
    
    # === 6. OBV ===
    obv = calc_obv(klines)
    if obv:
        factors["obv_direction"] = obv["direction"]
        factors["obv_trend_pct"] = obv["obv_trend_pct"]
    
    # === 7. 多周期 RSI ===
    for period, name in [(7, "7d"), (14, "14d"), (28, "28d")]:
        if len(closes) >= period + 1:
            factors[f"rsi_{name}"] = round(calc_rsi(closes, period), 1)
    
    # RSI 组合信号
    rsi7 = factors.get("rsi_7d", 50)
    rsi14 = factors.get("rsi_14d", 50)
    if rsi7 > rsi14 and rsi7 > 50:
        factors["rsi_combo"] = 1   # 短期强于长期，看多
    elif rsi7 < rsi14 and rsi7 < 50:
        factors["rsi_combo"] = -1  # 短期弱于长期，看空
    else:
        factors["rsi_combo"] = 0
    
    # === 8. 价格相对位置 ===
    if len(closes) >= 60:
        high_60 = max(closes[-60:])
        low_60 = min(closes[-60:])
        if high_60 != low_60:
            factors["price_position_60d"] = round((closes[-1] - low_60) / (high_60 - low_60) * 100, 1)
            # 距离高点回撤
            factors["drawdown_from_high"] = round((closes[-1] - high_60) / high_60 * 100, 2)
            # 距离低点反弹
            factors["bounce_from_low"] = round((closes[-1] - low_60) / low_60 * 100, 2)
    
    # === 9. 成交额集中度 ===
    if len(volumes) >= 20:
        avg_vol_5 = sum(volumes[-5:]) / 5
        avg_vol_20 = sum(volumes[-20:]) / 20
        factors["volume_concentration"] = round(avg_vol_5 / avg_vol_20, 2) if avg_vol_20 > 0 else 1
        
        # 量能趋势（3日 vs 10日）
        if len(volumes) >= 10:
            avg_vol_3 = sum(volumes[-3:]) / 3
            avg_vol_10 = sum(volumes[-10:]) / 10
            factors["volume_trend_3d"] = round(avg_vol_3 / avg_vol_10, 2) if avg_vol_10 > 0 else 1
    
    # === 10. 支撑/阻力距离 ===
    if len(closes) >= 20:
        # 简单支撑位：近20日的低点聚集区
        recent_lows = sorted(lows[-20:])
        support = sum(recent_lows[:5]) / 5  # 最近5个低点均值
        resistance = sum(sorted(highs[-20:])[-5:]) / 5  # 最近5个高点均值
        
        if closes[-1] > 0:
            factors["support_distance_pct"] = round((closes[-1] - support) / closes[-1] * 100, 2)
            factors["resistance_distance_pct"] = round((resistance - closes[-1]) / closes[-1] * 100, 2)
    
    # === 11. 威廉指标 ===
    wr = calc_williams_r(klines)
    if wr is not None:
        factors["williams_r"] = wr
    
    # === 12. CCI ===
    cci = calc_cci(klines)
    if cci is not None:
        factors["cci"] = cci
    
    # === 13. 相对强弱（vs 上证指数） ===
    if index_klines and len(index_klines) >= 20 and len(closes) >= 20:
        idx_closes = [float(k[2]) for k in index_klines]
        if len(idx_closes) >= 20:
            stock_ret_20d = (closes[-1] - closes[-20]) / closes[-20]
            idx_ret_20d = (idx_closes[-1] - idx_closes[-20]) / idx_closes[-20]
            if abs(idx_ret_20d) > 0.001:
                factors["rs_vs_index_20d"] = round((1 + stock_ret_20d) / (1 + idx_ret_20d), 4)
            else:
                factors["rs_vs_index_20d"] = round(1.0 + (stock_ret_20d - idx_ret_20d) * 10, 4)
    
    # === 14. 均线距离 ===
    if len(closes) >= 60:
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        if ma20 > 0:
            factors["price_vs_ma20"] = round((closes[-1] - ma20) / ma20 * 100, 2)
        if ma60 > 0:
            factors["price_vs_ma60"] = round((closes[-1] - ma60) / ma60 * 100, 2)
        # 均线发散度
        if ma60 > 0:
            factors["ma_spread"] = round((ma5 - ma60) / ma60 * 100, 2)
    
    return factors


# ============================================================
# 增强评分函数（供 factor_scorer.py 调用）
# ============================================================

def score_enhanced_factors(factors):
    """
    将增强因子转化为评分加分（-25 ~ +25 范围）
    返回: (bonus, details_dict)
    """
    if not factors:
        return 0, {}
    
    bonus = 0
    details = {}
    
    # 1. 资金流向（暂无可靠数据源，跳过）
    
    # 2. KDJ 信号 (±5)
    kdj = factors.get("kdj_signal", 0)
    kdj_zone = factors.get("kdj_zone", "neutral")
    if kdj == 1:
        bonus += 5
        details["KDJ"] = "金叉↑"
    elif kdj == -1:
        bonus -= 3
        details["KDJ"] = "死叉↓"
    if kdj_zone == "oversold":
        bonus += 3
        details["KDJ_zone"] = "超卖区"
    elif kdj_zone == "overbought":
        bonus -= 3
        details["KDJ_zone"] = "超买区"
    
    # 3. 布林带位置 (±5)
    boll_score = factors.get("boll_position_score", 50)
    boll_b = factors.get("boll_pct_b", 0.5)
    if boll_score >= 80:
        bonus += 5
        details["BOLL"] = f"理想位置 %b={boll_b:.2f}"
    elif boll_score <= 30:
        if boll_b < 0.05:
            bonus += 3  # 超跌反弹机会
            details["BOLL"] = f"超跌 %b={boll_b:.2f}"
        else:
            bonus -= 5
            details["BOLL"] = f"极端 %b={boll_b:.2f}"
    
    # 4. OBV 趋势 (±5)
    obv_dir = factors.get("obv_direction", "flat")
    obv_trend = factors.get("obv_trend_pct", 0)
    if obv_dir == "up":
        bonus += 5
        details["OBV"] = f"放量↑ +{obv_trend:.1f}%"
    elif obv_dir == "down":
        bonus -= 5
        details["OBV"] = f"缩量↓ {obv_trend:.1f}%"
    
    # 5. 量价背离 (±5)
    divergence = factors.get("vol_price_divergence", 0)
    if divergence == -1:
        bonus -= 5
        details["量价背离"] = "价涨量缩⚠️"
    elif divergence == 1:
        bonus += 3
        details["量价背离"] = "放量探底"
    
    # 6. RSI 组合 (±3)
    rsi_combo = factors.get("rsi_combo", 0)
    rsi14 = factors.get("rsi_14d", 50)
    if rsi_combo == 1 and 40 <= rsi14 <= 70:
        bonus += 3
        details["RSI"] = f"多头排列 RSI14={rsi14:.0f}"
    elif rsi_combo == -1 and rsi14 < 30:
        bonus += 2  # 超跌可能反弹
        details["RSI"] = f"超跌 RSI14={rsi14:.0f}"
    elif rsi_combo == -1 and rsi14 > 70:
        bonus -= 3
        details["RSI"] = f"高位回落 RSI14={rsi14:.0f}"
    
    # 7. 动量加速度 (±3)
    accel = factors.get("momentum_acceleration", 0)
    if accel > 3:
        bonus += 3
        details["动量加速"] = f"+{accel:.1f}%"
    elif accel < -3:
        bonus -= 3
        details["动量加速"] = f"{accel:.1f}%"
    
    # 8. 波动率结构 (±2)
    vol_ratio = factors.get("vol_ratio_up_down", 1)
    if vol_ratio > 1.5:
        bonus += 2
        details["波动结构"] = "上行>下行"
    elif vol_ratio < 0.6:
        bonus -= 2
        details["波动结构"] = "下行>上行"
    
    # 9. 相对强弱 (±5)
    rs = factors.get("rs_vs_index_20d", 1)
    if rs and rs >= 1.15:
        bonus += 5
        details["RS"] = f"强于大盘 {rs:.2f}"
    elif rs and rs >= 1.05:
        bonus += 2
        details["RS"] = f"略强 {rs:.2f}"
    elif rs and rs <= 0.85:
        bonus -= 5
        details["RS"] = f"弱于大盘 {rs:.2f}"
    elif rs and rs <= 0.95:
        bonus -= 2
        details["RS"] = f"略弱 {rs:.2f}"
    
    # 10. 价格相对位置 (±2)
    pos_60d = factors.get("price_position_60d", 50)
    if 20 <= pos_60d <= 60:
        bonus += 2
        details["60d位置"] = f"中低位 {pos_60d:.0f}%"
    elif pos_60d > 90:
        bonus -= 2
        details["60d位置"] = f"高位 {pos_60d:.0f}%"
    elif pos_60d < 10:
        bonus += 1  # 超跌
        details["60d位置"] = f"超跌 {pos_60d:.0f}%"
    
    # 11. CCI (±2)
    cci = factors.get("cci", 0)
    if cci > 100:
        bonus += 2
        details["CCI"] = f"强势 {cci:.0f}"
    elif cci < -100:
        bonus += 1  # 超跌可能反弹
        details["CCI"] = f"超跌 {cci:.0f}"
    
    # 限制范围
    bonus = max(-25, min(25, bonus))
    return bonus, details


# ============================================================
# CLI
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="增强因子数据模块")
    parser.add_argument("--code", type=str, help="股票代码")
    parser.add_argument("--batch", type=str, help="逗号分隔的多个代码")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    args = parser.parse_args()
    
    if args.code:
        code = args.code
        print(f"获取 {code} 的增强因子...", file=sys.stderr)
        klines = get_kline(code, 120)
        if not klines:
            print(f"无法获取 {code} 的K线数据")
            return
        
        index_klines = get_index_kline(120)
        factors = calc_all_enhanced_factors(code, klines, index_klines)
        bonus, details = score_enhanced_factors(factors)
        
        if args.json:
            output = {
                "code": code,
                "enhanced_bonus": bonus,
                "details": details,
                "factors": factors,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            print(f"\n{'='*60}")
            print(f"  {code} 增强因子分析")
            print(f"{'='*60}")
            print(f"\n  📊 评分加减: {bonus:+d} 分")
            if details:
                for k, v in details.items():
                    print(f"    • {k}: {v}")
            print(f"\n  📈 因子明细:")
            for k, v in sorted(factors.items()):
                print(f"    {k}: {v}")
            print(f"{'='*60}")
    
    elif args.batch:
        codes = [c.strip() for c in args.batch.split(",")]
        index_klines = get_index_kline(120)
        results = []
        for code in codes:
            klines = get_kline(code, 120)
            if klines:
                factors = calc_all_enhanced_factors(code, klines, index_klines)
                bonus, details = score_enhanced_factors(factors)
                results.append({"code": code, "bonus": bonus, "details": details, "factors": factors})
            time.sleep(0.15)
        
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for r in results:
                print(f"{r['code']}: {r['bonus']:+d}分  {', '.join(f'{k}:{v}' for k,v in r['details'].items())}")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
