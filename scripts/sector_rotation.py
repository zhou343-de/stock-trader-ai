#!/usr/bin/env python3
"""
市场细分 + 行业轮动检测器 v2
基于代表股计算行业表现（腾讯行情API），不依赖行业板块专用接口
输出：行业排名、风格分析、市场宽度、选股方向建议
"""
import json, sys, subprocess, time, math
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).parent

# === 行业代表股（每行业5只龙头） ===
SECTOR_STOCKS = {
    "电子半导体": {
        "style": "growth", "cap": "mid",
        "stocks": [
            ("002371", "北方华创"), ("603986", "兆易创新"),
            ("002049", "紫光国微"), ("300661", "圣邦股份"),
            ("688008", "澜起科技"),
        ]
    },
    "计算机软件": {
        "style": "growth", "cap": "mid",
        "stocks": [
            ("002410", "广联达"), ("600588", "用友网络"),
            ("300496", "中科创达"), ("002236", "大华股份"),
            ("300033", "同花顺"),
        ]
    },
    "通信5G": {
        "style": "growth", "cap": "mid",
        "stocks": [
            ("000063", "中兴通讯"), ("600487", "亨通光电"),
            ("002396", "星网锐捷"), ("300627", "华测导航"),
            ("603236", "移远通信"),
        ]
    },
    "新能源": {
        "style": "growth", "cap": "large",
        "stocks": [
            ("300750", "宁德时代"), ("002594", "比亚迪"),
            ("601012", "隆基绿能"), ("300274", "阳光电源"),
            ("002459", "晶澳科技"),
        ]
    },
    "军工": {
        "style": "growth", "cap": "large",
        "stocks": [
            ("600893", "航发动力"), ("601989", "中国重工"),
            ("000768", "中航西飞"), ("600760", "中航沈飞"),
            ("002179", "中航光电"),
        ]
    },
    "医药生物": {
        "style": "growth", "cap": "large",
        "stocks": [
            ("600276", "恒瑞医药"), ("300760", "迈瑞医疗"),
            ("000538", "云南白药"), ("300122", "智飞生物"),
            ("002007", "华兰生物"),
        ]
    },
    "食品饮料": {
        "style": "value", "cap": "large",
        "stocks": [
            ("600519", "贵州茅台"), ("000858", "五粮液"),
            ("000568", "泸州老窖"), ("603288", "海天味业"),
            ("002304", "洋河股份"),
        ]
    },
    "家电消费": {
        "style": "value", "cap": "large",
        "stocks": [
            ("000333", "美的集团"), ("000651", "格力电器"),
            ("600690", "海尔智家"), ("002032", "苏泊尔"),
            ("002508", "老板电器"),
        ]
    },
    "银行金融": {
        "style": "value", "cap": "large",
        "stocks": [
            ("601398", "工商银行"), ("601939", "建设银行"),
            ("600036", "招商银行"), ("601166", "兴业银行"),
            ("000001", "平安银行"),
        ]
    },
    "房地产": {
        "style": "value", "cap": "large",
        "stocks": [
            ("001979", "招商蛇口"), ("600048", "保利发展"),
            ("000002", "万科A"), ("600383", "金地集团"),
            ("002146", "荣盛发展"),
        ]
    },
    "钢铁有色": {
        "style": "value", "cap": "mid",
        "stocks": [
            ("600019", "宝钢股份"), ("000709", "河钢股份"),
            ("601899", "紫金矿业"), ("603993", "洛阳钼业"),
            ("002460", "赣锋锂业"),
        ]
    },
    "化工材料": {
        "style": "value", "cap": "mid",
        "stocks": [
            ("600309", "万华化学"), ("002601", "龙蟒佰利"),
            ("600989", "宝丰能源"), ("000792", "盐湖股份"),
            ("300586", "美联新材"),
        ]
    },
    "机械设备": {
        "style": "value", "cap": "mid",
        "stocks": [
            ("600031", "三一重工"), ("000157", "中联重科"),
            ("601100", "恒立液压"), ("002008", "大族激光"),
            ("300124", "汇川技术"),
        ]
    },
    "汽车": {
        "style": "value", "cap": "large",
        "stocks": [
            ("600104", "上汽集团"), ("000625", "长安汽车"),
            ("601238", "广汽集团"), ("002920", "德赛西威"),
            ("600741", "华域汽车"),
        ]
    },
}


def get_batch_prices(stock_list):
    """批量获取股票行情（腾讯接口）"""
    codes = [f"sh{s[0]}" if s[0].startswith("6") else f"sz{s[0]}" for s in stock_list]
    market_codes = codes

    # 用 batch_query.py 获取
    cmd = ["python3", str(SCRIPT_DIR / "batch_query.py")] + market_codes
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0:
            data = json.loads(r.stdout.decode("utf-8"))
            return data
    except Exception:
        pass
    return {}


def get_index_kline(code="sh000001", days=60):
    """获取指数K线"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
    try:
        r = subprocess.run(["curl", "-s", "--connect-timeout", "10", url],
                           capture_output=True, timeout=15)
        data = json.loads(r.stdout.decode("utf-8"))
        klines = data.get("data", {}).get(code, {})
        return klines.get("qfqday") or klines.get("day", [])
    except Exception:
        return []


# === 分析函数 ===

def analyze_sectors():
    """分析所有行业板块表现"""
    results = []

    for sector_name, info in SECTOR_STOCKS.items():
        stock_list = info["stocks"]
        prices = get_batch_prices(stock_list)
        time.sleep(0.2)

        changes = []
        for code, name in stock_list:
            # 查找价格数据
            found = False
            for key, val in prices.items():
                pure_code = key.lstrip("shszSHSZ")
                if pure_code == code and isinstance(val, dict):
                    change = val.get("change_pct", 0)
                    if change is None:
                        change = 0
                    changes.append({"code": code, "name": name, "change": float(change)})
                    found = True
                    break
            if not found:
                changes.append({"code": code, "name": name, "change": 0})

        if changes:
            avg_change = sum(c["change"] for c in changes) / len(changes)
            max_change = max(c["change"] for c in changes)
            min_change = min(c["change"] for c in changes)
            up_count = sum(1 for c in changes if c["change"] > 0)
        else:
            avg_change = max_change = min_change = 0
            up_count = 0

        results.append({
            "name": sector_name,
            "style": info["style"],
            "cap": info["cap"],
            "avg_change": round(avg_change, 2),
            "max_change": round(max_change, 2),
            "min_change": round(min_change, 2),
            "up_count": up_count,
            "total": len(changes),
            "stocks": changes,
        })

    # 按涨幅排序
    results.sort(key=lambda x: x["avg_change"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1

    return results


def detect_style(sector_data):
    """风格分析"""
    growth = [s for s in sector_data if s["style"] == "growth"]
    value = [s for s in sector_data if s["style"] == "value"]
    large = [s for s in sector_data if s["cap"] == "large"]
    mid = [s for s in sector_data if s["cap"] == "mid"]

    growth_avg = sum(s["avg_change"] for s in growth) / len(growth) if growth else 0
    value_avg = sum(s["avg_change"] for s in value) / len(value) if value else 0
    large_avg = sum(s["avg_change"] for s in large) / len(large) if large else 0
    mid_avg = sum(s["avg_change"] for s in mid) / len(mid) if mid else 0

    style_diff = growth_avg - value_avg
    if style_diff > 0.8:
        style, style_label = "growth_leading", "成长主导"
    elif style_diff < -0.8:
        style, style_label = "value_leading", "价值主导"
    else:
        style, style_label = "balanced", "风格均衡"

    cap_diff = mid_avg - large_avg
    if cap_diff > 0.8:
        cap_style, cap_label = "small_cap_leading", "小盘主导"
    elif cap_diff < -0.8:
        cap_style, cap_label = "large_cap_leading", "大盘主导"
    else:
        cap_style, cap_label = "cap_balanced", "市值均衡"

    return {
        "style": style, "style_label": style_label,
        "style_diff": round(style_diff, 2),
        "growth_avg": round(growth_avg, 2), "value_avg": round(value_avg, 2),
        "cap_style": cap_style, "cap_label": cap_label,
        "cap_diff": round(cap_diff, 2),
        "large_avg": round(large_avg, 2), "mid_avg": round(mid_avg, 2),
    }


def assess_technical_regime(index_klines):
    """技术面市场环境"""
    if not index_klines or len(index_klines) < 20:
        return {"status": "unknown", "score": 0, "confidence": 0}

    closes = [float(k[2]) for k in index_klines]
    current = closes[-1]
    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else current
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else current
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else current
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else ma20

    score = 0
    if ma5 > ma10 > ma20 > ma60:
        score += 30
    elif ma5 > ma10 > ma20:
        score += 20
    elif ma5 < ma10 < ma20 < ma60:
        score -= 30
    elif ma5 < ma10 < ma20:
        score -= 20

    if current > ma5: score += 10
    if current > ma20: score += 10
    if current > ma60: score += 10

    if len(closes) >= 5:
        ret5 = (closes[-1] - closes[-5]) / closes[-5] * 100
        if ret5 > 3: score += 15
        elif ret5 > 0: score += 5
        elif ret5 < -3: score -= 15
        elif ret5 < 0: score -= 5

    if score >= 40:
        status = "bull"
    elif score <= -30:
        status = "bear"
    else:
        status = "range"

    return {
        "status": status, "score": score, "confidence": min(max(score + 50, 0), 100),
        "ma5": round(ma5, 2), "ma10": round(ma10, 2),
        "ma20": round(ma20, 2), "ma60": round(ma60, 2), "current": round(current, 2),
    }


def get_index_info(klines):
    """指数行情摘要"""
    if not klines or len(klines) < 2:
        return {}
    latest = klines[-1]
    prev = klines[-2]
    price = float(latest[2])
    pre_close = float(prev[2])
    return {"price": price, "change": round((price - pre_close) / pre_close * 100, 2)}


def get_market_state():
    """获取完整市场状态"""
    # 1. 行业分析
    sector_data = analyze_sectors()
    if not sector_data:
        return {"error": "无法获取行业数据"}

    # 2. 风格分析
    style = detect_style(sector_data)

    # 3. 指数
    sh_klines = get_index_kline("sh000001", 60)
    sz_klines = get_index_kline("sz399001", 5)
    cy_klines = get_index_kline("sz399006", 5)

    # 4. 技术面
    technical = assess_technical_regime(sh_klines)

    # 5. 综合环境
    regime_score = technical["score"]
    # 行业强度调整
    strong = sum(1 for s in sector_data if s["avg_change"] > 1.5)
    weak = sum(1 for s in sector_data if s["avg_change"] < -1.5)
    if strong > 8: regime_score += 10
    elif weak > 8: regime_score -= 10

    if regime_score >= 40:
        regime = "bull"
    elif regime_score <= -30:
        regime = "bear"
    else:
        regime = "range"

    return {
        "regime": regime,
        "regime_score": regime_score,
        "confidence": min(max(regime_score + 50, 0), 100),
        "indices": {
            "上证指数": get_index_info(sh_klines),
            "深证成指": get_index_info(sz_klines),
            "创业板指": get_index_info(cy_klines),
        },
        "style": style,
        "sector_ranking": [{
            "name": s["name"], "rank": s["rank"], "change": s["avg_change"],
            "style": s["style"], "cap": s["cap"],
            "up_ratio": f"{s['up_count']}/{s['total']}",
        } for s in sector_data],
        "technical": technical,
        "timestamp": datetime.now(CST).isoformat(),
    }


def get_stock_suggestions(market_state):
    """选股方向建议"""
    suggestions = []
    style = market_state.get("style", {})
    ranking = market_state.get("sector_ranking", [])
    regime = market_state.get("regime", "range")

    # 风格建议
    if style.get("style") == "growth_leading":
        suggestions.append({
            "direction": "成长风格主导",
            "focus": "电子、计算机、新能源、军工",
            "reason": f"成长涨{style.get('growth_avg', 0):.1f}% vs 价值{style.get('value_avg', 0):.1f}%",
        })
    elif style.get("style") == "value_leading":
        suggestions.append({
            "direction": "价值风格主导",
            "focus": "银行、钢铁、有色、消费",
            "reason": f"价值涨{style.get('value_avg', 0):.1f}% vs 成长{style.get('growth_avg', 0):.1f}%",
        })

    # 强势板块
    if ranking:
        top3 = [s["name"] for s in ranking[:3]]
        top3_str = ", ".join(s["name"] + f"{s['change']:+.1f}%" for s in ranking[:3])
        suggestions.append({
            "direction": "今日强势行业",
            "focus": "、".join(top3),
            "reason": f"涨幅前三：{top3_str}",
        })

    # 市值建议
    if style.get("cap_style") == "small_cap_leading":
        suggestions.append({
            "direction": "小盘活跃",
            "focus": "中证500、中小市值成长股",
            "reason": f"中盘涨{style.get('mid_avg', 0):.1f}% vs 大盘{style.get('large_avg', 0):.1f}%",
        })

    # 环境建议
    if regime == "bull":
        suggestions.append({"direction": "偏多环境", "focus": "积极进攻", "reason": "技术面偏多"})
    elif regime == "bear":
        suggestions.append({"direction": "偏空环境", "focus": "防守为主", "reason": "技术面偏空"})

    return suggestions


# === CLI ===

def main():
    import argparse
    parser = argparse.ArgumentParser(description="行业轮动 + 市场细分")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    parser.add_argument("--suggest", action="store_true", help="选股建议")
    parser.add_argument("--regime-only", action="store_true", help="仅市场环境")
    args = parser.parse_args()

    state = get_market_state()

    if "error" in state:
        print(json.dumps(state, ensure_ascii=False))
        sys.exit(1)

    if args.regime_only:
        print(json.dumps({
            "regime": state["regime"],
            "regime_score": state["regime_score"],
            "confidence": state["confidence"],
        }, ensure_ascii=False))
        return

    if args.json:
        output = state
        if args.suggest:
            output["suggestions"] = get_stock_suggestions(state)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        regime_emoji = {"bull": "🐂", "range": "📊", "bear": "🐻"}
        print(f"\n{'='*60}")
        print(f"  {regime_emoji.get(state['regime'], '❓')} 市场环境: {state['regime'].upper()}")
        print(f"  综合得分: {state['regime_score']}  置信度: {state['confidence']}%")
        print(f"{'='*60}")

        print(f"\n📈 主要指数:")
        for name, info in state.get("indices", {}).items():
            if info:
                emoji = "📈" if info.get("change", 0) >= 0 else "📉"
                print(f"  {emoji} {name}: {info.get('price', 0):.2f} ({info.get('change', 0):+.2f}%)")

        s = state.get("style", {})
        print(f"\n🎨 风格: {s.get('style_label', '?')} (成长{s.get('growth_avg', 0):+.1f}% vs 价值{s.get('value_avg', 0):+.1f}%)")
        print(f"  市值: {s.get('cap_label', '?')} (大盘{s.get('large_avg', 0):+.1f}% vs 中盘{s.get('mid_avg', 0):+.1f}%)")

        ranking = state.get("sector_ranking", [])
        if ranking:
            print(f"\n🏆 行业排名:")
            for s in ranking[:5]:
                print(f"  🟢 #{s['rank']} {s['name']:<10} {s['change']:+.2f}%  [{s['style']}/{s['cap']}] 上涨{s['up_ratio']}")
            print(f"  ---")
            for s in ranking[-5:]:
                print(f"  🔴 #{s['rank']} {s['name']:<10} {s['change']:+.2f}%  [{s['style']}/{s['cap']}] 上涨{s['up_ratio']}")

        if args.suggest:
            suggestions = get_stock_suggestions(state)
            print(f"\n💡 选股方向:")
            for i, sug in enumerate(suggestions, 1):
                print(f"  {i}. {sug['direction']}: {sug['focus']}")
                print(f"     {sug['reason']}")

        print(f"\n⏰ {state.get('timestamp', '')}")


if __name__ == "__main__":
    main()
