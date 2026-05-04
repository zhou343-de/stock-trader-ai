#!/usr/bin/env python3
"""
Market Regime 分类器 v2
双维度判定：技术面（120根日K线）+ 情绪面
输出：bull / range / bear + 置信度 + 参数库
新增：ATR 动态止损参数、涨幅窗口 1-9.5%
"""
import json, sys, subprocess, re, math
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# === 数据获取 ===
def get_kline(code="sh000001", days=120, max_retries=3):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
    for attempt in range(max_retries):
        try:
            r = subprocess.run(["curl", "-s", "--connect-timeout", "10", url],
                               capture_output=True, timeout=15)
            data = json.loads(r.stdout.decode("utf-8"))
            klines = data.get("data", {}).get(code, {})
            rows = klines.get("qfqday") or klines.get("day", [])
            if rows:
                return rows
        except Exception:
            pass
        if attempt < max_retries - 1:
            import time
            time.sleep(1 * (attempt + 1))
    raise RuntimeError(f"No kline data for {code} after {max_retries} retries")

def curl_with_retry(args, max_retries=3, timeout=15):
    """带重试的 curl 封装"""
    import time
    for attempt in range(max_retries):
        try:
            r = subprocess.run(["curl", "-s"] + args,
                               capture_output=True, timeout=timeout)
            return r.stdout.decode("utf-8", errors="ignore").strip()
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
    return ""

def get_emotion_data():
    try:
        total_text = curl_with_retry(["--connect-timeout", "5",
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount?node=hs_a"], timeout=10)
        if not total_text:
            return None
        total = int(json.loads(total_text))

        top_text = curl_with_retry(["--connect-timeout", "10",
            "-H", "Referer: https://finance.sina.com.cn/",
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page=1&num=80&sort=changepercent&asc=0&node=hs_a"], timeout=15)
        if not top_text:
            return None
        top_items = json.loads(top_text)

        bottom_text = curl_with_retry(["--connect-timeout", "10",
            "-H", "Referer: https://finance.sina.com.cn/",
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page=1&num=80&sort=changepercent&asc=1&node=hs_a"], timeout=15)
        if not bottom_text:
            return None
        bottom_items = json.loads(bottom_text)

        up_in_top = sum(1 for s in top_items if float(s.get("changepercent", 0)) > 0)
        down_in_bottom = sum(1 for s in bottom_items if float(s.get("changepercent", 0)) < 0)
        limit_up = sum(1 for s in top_items if float(s.get("changepercent", 0)) >= 9.9 and float(s.get("buy", 0)) > 0)
        limit_down = sum(1 for s in bottom_items if float(s.get("changepercent", 0)) <= -9.9 and float(s.get("sell", 0)) == 0)

        if up_in_top == 80:
            up = int(total * 0.8)
        elif up_in_top > 60:
            up = int(total * up_in_top / 80 * 0.7)
        else:
            up = int(total * up_in_top / 80 * 0.5)

        if down_in_bottom == 80:
            down = int(total * 0.8)
        elif down_in_bottom > 60:
            down = int(total * down_in_bottom / 80 * 0.7)
        else:
            down = int(total * down_in_bottom / 80 * 0.5)

        idx_text = curl_with_retry(["--connect-timeout", "5", "https://qt.gtimg.cn/q=sh000001"], timeout=10)
        idx_fields = idx_text.split("~")
        total_amount = float(idx_fields[37]) / 10000 if len(idx_fields) > 37 else 0

        return {
            "up": up, "down": down,
            "limit_up": limit_up, "limit_down": limit_down,
            "total_amount": total_amount, "total": total
        }
    except Exception:
        return None

# === 技术指标 ===
def calc_ma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period

def calc_rsi(closes, period=14):
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

def calc_volatility(closes, period=20):
    if len(closes) < period:
        return 0
    subset = closes[-period:]
    mean = sum(subset) / period
    variance = sum((x - mean) ** 2 for x in subset) / period
    return math.sqrt(variance) / mean * 100

def calc_adx(klines, period=14):
    """计算 ADX（Average Directional Index）趋势强度指标"""
    if len(klines) < period + 1:
        return 0, 0, 0  # adx, +di, -di
    true_ranges = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(klines)):
        high = float(klines[i][3])
        low = float(klines[i][4])
        prev_high = float(klines[i-1][3])
        prev_low = float(klines[i-1][4])
        prev_close = float(klines[i-1][2])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
    # Wilder 平滑
    def wilder_smooth(data, p):
        result = [sum(data[:p])]
        for i in range(p, len(data)):
            result.append(result[-1] - result[-1] / p + data[i])
        return result
    tr_smoothed = wilder_smooth(true_ranges, period)
    plus_smoothed = wilder_smooth(plus_dm, period)
    minus_smoothed = wilder_smooth(minus_dm, period)
    if not tr_smoothed or tr_smoothed[-1] == 0:
        return 0, 0, 0
    plus_di = 100 * plus_smoothed[-1] / tr_smoothed[-1]
    minus_di = 100 * minus_smoothed[-1] / tr_smoothed[-1]
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return 0, round(plus_di, 1), round(minus_di, 1)
    dx = abs(plus_di - minus_di) / di_sum * 100
    # ADX 是 DX 的 14 日均值（简化：取最近 DX 序列的均值）
    adx = dx  # 简化为当前 DX，足够判断趋势强度
    return round(adx, 1), round(plus_di, 1), round(minus_di, 1)

def calc_index_atr(klines, period=20):
    """计算指数 ATR（用于动态止损基准）"""
    if len(klines) < 2:
        return 0
    true_ranges = []
    for i in range(1, len(klines)):
        high = float(klines[i][3])
        low = float(klines[i][4])
        prev_close = float(klines[i-1][2])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    return sum(true_ranges[-period:]) / min(len(true_ranges), period)

def classify_regime():
    rows = get_kline("sh000001", 120)
    closes = [float(r[2]) for r in rows]

    if len(closes) < 60:
        return {"regime": "range", "confidence": 0, "error": "数据不足"}

    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    change_20d = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0
    rsi = calc_rsi(closes, 14)
    volatility = calc_volatility(closes, 20)
    index_atr = calc_index_atr(rows, 20)

    ma20_series = []
    for i in range(25, len(closes) + 1):
        ma20_series.append(sum(closes[i-20:i]) / 20)
    ma20_slope = 0
    if len(ma20_series) >= 25:
        recent = sum(ma20_series[-5:]) / 5
        old = sum(ma20_series[-25:-20]) / 5
        ma20_slope = (recent - old) / old * 100

    # ADX 趋势强度
    adx, plus_di, minus_di = calc_adx(rows, 14)

    # 成交额趋势（3日均量 vs 20日均量）
    amounts = [float(r[5]) * float(r[2]) / 1e8 for r in rows if len(r) > 5]  # 亿元
    vol_trend = None
    if len(amounts) >= 20:
        avg_3 = sum(amounts[-3:]) / 3
        avg_20 = sum(amounts[-20:]) / 20
        vol_trend = round(avg_3 / avg_20, 2) if avg_20 > 0 else None

    # Bull 打分
    bull_score = 0
    if ma20 and ma60 and ma20 > ma60 * 1.02:
        bull_score += 2
    if change_20d > 5:
        bull_score += 2
    elif change_20d > 3:
        bull_score += 1
    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
        bull_score += 1
    if rsi > 55:
        bull_score += 1
    if ma20_slope > 0.5:
        bull_score += 1
    # ADX 强趋势加分
    if adx >= 25 and plus_di > minus_di:
        bull_score += 1

    # Bear 打分
    bear_score = 0
    if ma20 and ma60 and ma20 < ma60 * 0.98:
        bear_score += 2
    if change_20d < -5:
        bear_score += 2
    elif change_20d < -3:
        bear_score += 1
    if ma5 and ma10 and ma20 and ma5 < ma10 < ma20:
        bear_score += 1
    if rsi < 40:
        bear_score += 1
    if ma20_slope < -0.5:
        bear_score += 1
    # ADX 强下跌趋势加分
    if adx >= 25 and minus_di > plus_di:
        bear_score += 1

    # 情绪面调整
    emotion = get_emotion_data()
    emotion_adj = 0
    emotion_detail = {}
    if emotion:
        ratio = emotion["up"] / max(emotion["down"], 1)
        emotion_detail["涨跌比"] = round(ratio, 2)
        emotion_detail["涨停"] = emotion["limit_up"]
        emotion_detail["跌停"] = emotion["limit_down"]
        emotion_detail["成交额(亿)"] = round(emotion["total_amount"], 0)

        if ratio >= 2.0:
            emotion_adj += 2
        elif ratio >= 1.5:
            emotion_adj += 1
        elif ratio <= 0.5:
            emotion_adj -= 2
        elif ratio <= 0.7:
            emotion_adj -= 1

        if emotion["limit_up"] >= 80:
            emotion_adj += 1
        elif emotion["limit_up"] <= 15:
            emotion_adj -= 1
        if emotion["limit_down"] >= 30:
            emotion_adj -= 2
        if emotion["total_amount"] < 7000:
            emotion_adj -= 1
        elif emotion["total_amount"] > 15000:
            emotion_adj += 1

    bull_score += max(0, emotion_adj)
    bear_score += max(0, -emotion_adj)

    # 判定
    if bull_score >= 4 and bull_score > bear_score:
        regime = "bull"
        confidence = min(bull_score / 8 * 100, 100)
    elif bear_score >= 4 and bear_score > bull_score:
        regime = "bear"
        confidence = min(bear_score / 8 * 100, 100)
    else:
        regime = "range"
        confidence = max(0, 100 - abs(bull_score - bear_score) * 15)

    # 强趋势标记（ADX >= 25 且置信度 >= 60）
    strong_trend = adx >= 25 and confidence >= 60

    # 参数库（ATR 动态止损 + 涨幅窗口 1-9.5%）
    params = {
        "bull": {
            "stop_loss": -0.08, "tp1": 0.15, "tp2": 0.25, "tp3": 0.35,
            "max_position": 0.90, "max_holdings": 4,
            "min_change": 1, "max_change": 9.5,
            "atr_multiplier": 1.5,
        },
        "range": {
            "stop_loss": -0.07, "tp1": 0.15, "tp2": 0.25, "tp3": 0.35,
            "max_position": 0.85, "max_holdings": 3,
            "min_change": 1, "max_change": 9.5,
            "atr_multiplier": 1.5,
        },
        "bear": {
            "stop_loss": -0.04, "tp1": 0.10, "tp2": 0.18, "tp3": 0.25,
            "max_position": 0.60, "max_holdings": 2,
            "min_change": 1, "max_change": 7,
            "atr_multiplier": 1.2,
        },
    }

    # 强趋势时 ATR 倍数自适应（ADX >= 25：放宽止损防扫损）
    if strong_trend and adx >= 30:
        params[regime]["atr_multiplier"] = 1.8
    elif strong_trend:
        params[regime]["atr_multiplier"] = 1.6

    return {
        "regime": regime,
        "strong_trend": strong_trend,
        "confidence": round(confidence, 1),
        "bull_score": bull_score,
        "bear_score": bear_score,
        "emotion_adj": emotion_adj,
        "technicals": {
            "ma5": round(ma5, 2) if ma5 else None,
            "ma10": round(ma10, 2) if ma10 else None,
            "ma20": round(ma20, 2) if ma20 else None,
            "ma60": round(ma60, 2) if ma60 else None,
            "change_20d": round(change_20d, 2),
            "rsi_14": round(rsi, 1),
            "volatility": round(volatility, 2),
            "ma20_slope": round(ma20_slope, 2),
            "index_atr": round(index_atr, 2),
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "vol_trend_3d": vol_trend,
        },
        "emotion": emotion_detail,
        "params_active": params[regime],
        "timestamp": datetime.now(CST).isoformat(),
    }

if __name__ == "__main__":
    try:
        result = classify_regime()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
