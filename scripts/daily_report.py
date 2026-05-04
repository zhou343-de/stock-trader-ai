#!/usr/bin/env python3
"""
收盘日报生成器 v1
自动采集数据、计算指标、生成 Markdown 日报
用法: daily_report.py [--send]  # --send 自动发送邮件
输出: 日报文件路径 + 日报内容
"""
import json, sys, subprocess, math
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "data"
ACCOUNT_FILE = DATA_DIR / "trend_account.json"

def load_account():
    with open(ACCOUNT_FILE) as f:
        return json.load(f)

def run_script(script, args=None):
    cmd = ["python3", str(SCRIPT_DIR / script)] + (args or [])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        stdout = r.stdout.decode("utf-8").strip()
        try:
            return r.returncode, json.loads(stdout)
        except json.JSONDecodeError:
            return r.returncode, stdout
    except Exception as e:
        return -1, {"error": str(e)}

def fetch_prices(positions):
    if not positions:
        return {}
    market_codes = []
    for c in positions:
        prefix = "sh" if c.startswith("6") else "sz"
        market_codes.append(f"{prefix}{c}")
    rc, data = run_script("batch_query.py", market_codes)
    if rc != 0 or isinstance(data, str):
        return {}
    result = {}
    for k, v in data.items():
        pure_code = k.lstrip("shszSHSZ")
        if pure_code in positions:
            result[pure_code] = v
    return result

def calc_sharpe(daily_snapshots, risk_free_rate=0.03):
    """计算夏普比率（近20日）"""
    if not daily_snapshots or len(daily_snapshots) < 5:
        return None
    recent = daily_snapshots[-20:]
    returns = []
    for i in range(1, len(recent)):
        prev = recent[i-1]["total_assets"]
        curr = recent[i]["total_assets"]
        if prev > 0:
            returns.append((curr - prev) / prev)
    if len(returns) < 3:
        return None
    mean_r = sum(returns) / len(returns)
    std_r = math.sqrt(sum((r - mean_r)**2 for r in returns) / len(returns))
    if std_r == 0:
        return None
    sharpe = (mean_r - risk_free_rate/252) / std_r * math.sqrt(252)
    return round(sharpe, 2)

def calc_max_drawdown(daily_snapshots, initial_cash):
    """计算最大回撤"""
    if not daily_snapshots:
        return 0, None, None
    assets = [s["total_assets"] for s in daily_snapshots]
    peak = assets[0]
    max_dd = 0
    peak_date = daily_snapshots[0]["date"]
    trough_date = daily_snapshots[0]["date"]
    for i, a in enumerate(assets):
        if a > peak:
            peak = a
            peak_date = daily_snapshots[i]["date"]
        dd = (peak - a) / peak
        if dd > max_dd:
            max_dd = dd
            trough_date = daily_snapshots[i]["date"]
    return round(max_dd * 100, 2), peak_date, trough_date

def calc_benchmark_return(daily_snapshots):
    """计算基准（上证指数）收益率"""
    if not daily_snapshots or len(daily_snapshots) < 2:
        return None
    first = daily_snapshots[0]
    last = daily_snapshots[-1]
    # 从快照首日到今日的总资产收益
    account_return = (last["total_assets"] - first["total_assets"]) / first["total_assets"] * 100
    return round(account_return, 2)

def calc_position_health(pos, price_data, klines=None, sector_change=None):
    """计算持仓健康度（0-100）
    校准版 v3：大幅增加区分度
    - 浮亏每 1% 扣 5 分（加大）
    - 跑输板块 1% 以上扣 10 分
    - 持有超过 3 天未达 TP1 扣 5 分
    - 均线走平扣 10 分
    - 距止损线过近扣分（加大）
    - 量能萎缩扣分（加大）
    """
    score = 100
    issues = []
    avg_price = pos.get("avg_price", 0)
    current_price = price_data.get("price", avg_price)
    if avg_price <= 0:
        return 50, ["无成本数据"]

    # 浮盈/浮亏（基础分 100，浮亏每 1% 扣 5 分，上限 40 分）
    profit_pct = (current_price - avg_price) / avg_price * 100
    if profit_pct >= 10:
        score += 5
        issues.append(f"浮盈{profit_pct:.1f}% (+5分)")
    elif profit_pct >= 5:
        score += 2
        issues.append(f"浮盈{profit_pct:.1f}% (+2分)")
    elif profit_pct < 0:
        loss_penalty = min(int(abs(profit_pct) * 5), 40)
        score -= loss_penalty
        issues.append(f"浮亏{profit_pct:.1f}% (-{loss_penalty}分)")

    # 跑输板块检查（如果板块数据可用）
    if sector_change is not None and profit_pct is not None:
        relative = profit_pct - sector_change
        if relative < -1:
            sector_penalty = min(int(abs(relative) * 10), 20)
            score -= sector_penalty
            issues.append(f"跑输板块{abs(relative):.1f}% (-{sector_penalty}分)")

    # 持有天数检查（3天起扣，递增惩罚）
    buy_date = pos.get("buy_date", "")
    if buy_date:
        try:
            bd = datetime.strptime(buy_date, "%Y-%m-%d")
            hold_days = (datetime.now(CST) - bd.replace(tzinfo=CST)).days
            tp1 = 0.15
            if hold_days > 3 and profit_pct < tp1 * 100:
                hold_penalty = min(5 + (hold_days - 3) * 2, 25)
                score -= hold_penalty
                issues.append(f"持有{hold_days}天未达TP1 (-{hold_penalty}分)")
        except Exception:
            pass

    # 距止损线距离（越近越危险，加大扣分）
    atr_stop = pos.get("atr_stop_loss")
    effective_stop = pos.get("effective_stop", -0.07)
    if atr_stop:
        stop_price = avg_price * (1 + atr_stop)
        distance_to_stop = (current_price - stop_price) / avg_price * 100
        if distance_to_stop < 0:
            score -= 25
            issues.append(f"已破ATR止损 (-25分)")
        elif distance_to_stop < 2:
            score -= 15
            issues.append(f"距止损仅{distance_to_stop:.1f}% (-15分)")
        elif distance_to_stop < 4:
            score -= 8
            issues.append(f"距止损{distance_to_stop:.1f}% (-8分)")

    # 量能萎缩检查（加大扣分）
    if klines and len(klines) >= 20:
        volumes = [float(k[5]) for k in klines if len(k) > 5]
        if len(volumes) >= 20:
            avg_vol_5 = sum(volumes[-5:]) / 5
            avg_vol_20 = sum(volumes[-20:]) / 20
            if avg_vol_20 > 0:
                vol_ratio = avg_vol_5 / avg_vol_20
                if vol_ratio < 0.6:
                    score -= 20
                    issues.append(f"量比{vol_ratio:.2f}严重萎缩 (-20分)")
                elif vol_ratio < 0.8:
                    score -= 12
                    issues.append(f"量比{vol_ratio:.2f}萎缩 (-12分)")

    # 均线趋势检查（加大扣分）
    if klines and len(klines) >= 20:
        closes = [float(k[2]) for k in klines]
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        if len(closes) >= 6:
            ma5_prev = sum(closes[-6:-1]) / 5
            if ma5 <= ma5_prev * 0.995:  # MA5 明确下行
                score -= 15
                issues.append("MA5下行 (-15分)")
            elif ma5 <= ma5_prev * 1.001:  # 走平
                score -= 10
                issues.append("MA5走平 (-10分)")
        if ma5 < ma10 < ma20:
            score -= 10
            issues.append("均线空头 (-10分)")

    return max(min(score, 100), 0), issues


def calc_position_correlations(positions):
    """计算持仓间近20日收益率相关性矩阵
    返回: (matrix_dict, warnings)
    """
    import subprocess, json
    from pathlib import Path

    # 获取每只股票的20日K线
    kline_data = {}
    for code in positions:
        market_code = f"sh{code}" if code.startswith("6") else f"sz{code}"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market_code},day,,,25,qfq"
        try:
            r = subprocess.run(["curl", "-s", "--connect-timeout", "5", url], capture_output=True, timeout=10)
            data = json.loads(r.stdout.decode("utf-8"))
            klines = data.get("data", {}).get(market_code, {})
            rows = klines.get("qfqday") or klines.get("day", [])
            if rows and len(rows) >= 5:
                closes = [float(k[2]) for k in rows]
                # 计算日收益率
                returns = []
                for i in range(1, len(closes)):
                    if closes[i-1] > 0:
                        returns.append((closes[i] - closes[i-1]) / closes[i-1])
                kline_data[code] = returns[-20:]  # 最近20日
        except Exception:
            pass

    if len(kline_data) < 2:
        return {}, []

    # 计算相关系数
    codes = list(kline_data.keys())
    matrix = {}
    warnings = []

    def pearson_corr(x, y):
        n = min(len(x), len(y))
        if n < 5:
            return 0
        x, y = x[:n], y[:n]
        mx, my = sum(x)/n, sum(y)/n
        cov = sum((xi-mx)*(yi-my) for xi, yi in zip(x, y)) / n
        sx = math.sqrt(sum((xi-mx)**2 for xi in x) / n)
        sy = math.sqrt(sum((yi-my)**2 for yi in y) / n)
        if sx == 0 or sy == 0:
            return 0
        return round(cov / (sx * sy), 2)

    for i, c1 in enumerate(codes):
        matrix[c1] = {}
        for j, c2 in enumerate(codes):
            if c1 == c2:
                matrix[c1][c2] = 1.00
            elif c2 in matrix and c1 in matrix[c2]:
                matrix[c1][c2] = matrix[c2][c1]
            else:
                corr = pearson_corr(kline_data[c1], kline_data[c2])
                matrix[c1][c2] = corr
                # 高相关性警告
                if i < j and abs(corr) >= 0.7:
                    n1 = positions[c1].get("name", c1)
                    n2 = positions[c2].get("name", c2)
                    warnings.append(f"{n1}({c1}) vs {n2}({c2}) 相关性 {corr:.2f}（偏高）")

    return matrix, warnings


def generate_report():
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")

    acc = load_account()
    if not acc:
        return {"error": "账户未初始化"}

    # 获取收盘行情
    prices = fetch_prices(acc["positions"])
    rc, index_data = run_script("get_price.py", ["sh000001"])

    # 执行 snapshot
    run_script("simulate_trend.py", ["snapshot"])

    # 重新加载（snapshot 更新了数据）
    acc = load_account()

    # 获取板块轮动数据（用于健康度评分）
    sector_data_map = {}
    try:
        rc_sec, sec_rot = run_script("sector_rotation.py", ["--json"])
        if rc_sec == 0 and isinstance(sec_rot, dict):
            for s in sec_rot.get("sector_ranking", []):
                sector_data_map[s["name"]] = s.get("change", 0)
    except Exception:
        pass

    # 计算总资产
    total_market_value = 0
    position_details = []
    for code, pos in acc["positions"].items():
        price_data = prices.get(code, {})
        current_price = price_data.get("price", pos["avg_price"])
        market_value = pos["shares"] * current_price
        total_market_value += market_value
        profit = market_value - pos["shares"] * pos["avg_price"]
        profit_pct = (current_price - pos["avg_price"]) / pos["avg_price"] * 100 if pos["avg_price"] > 0 else 0

        # 获取板块涨幅（用于健康度评分）
        stock_sector = None
        sector_change = None
        name = pos.get("name", "")
        # 通过关键词匹配板块
        kw_map = {
            "电子半导体": ["电子", "芯片", "半导体", "光电", "显示"],
            "计算机软件": ["软件", "信息", "数据", "计算", "网络"],
            "通信5G": ["通信", "光纤", "5G", "基站"],
            "医药生物": ["药业", "医药", "生物", "医疗"],
            "食品饮料": ["食品", "饮料", "乳业", "白酒"],
            "汽车": ["汽车", "整车", "零部件"],
            "新能源": ["电气", "光伏", "风电", "储能", "电池"],
            "机械设备": ["机械", "设备", "自动化", "机器人"],
            "化工材料": ["化工", "化学", "新材料", "橡胶"],
            "军工": ["军工", "航天", "国防"],
            "钢铁有色": ["钢铁", "特钢", "锂", "钴", "稀土"],
            "房地产": ["地产", "置业", "房产"],
            "银行金融": ["银行", "证券", "保险"],
            "家电消费": ["家电", "空调", "冰箱"],
        }
        for sector, keywords in kw_map.items():
            for kw in keywords:
                if kw in name:
                    stock_sector = sector
                    break
            if stock_sector:
                break
        if stock_sector and stock_sector in sector_data_map:
            sector_change = sector_data_map[stock_sector]

        # 健康度评分
        market_code = f"sh{code}" if code.startswith("6") else f"sz{code}"
        rc_k, klines = run_script("get_kline_proxy.py", [market_code, "20"]) if False else (0, [])
        # 直接用腾讯接口取K线
        try:
            kurl = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market_code},day,,,20,qfq"
            kr = subprocess.run(["curl", "-s", "--connect-timeout", "5", kurl], capture_output=True, timeout=10)
            kdata = json.loads(kr.stdout.decode("utf-8"))
            klines_raw = kdata.get("data", {}).get(market_code, {})
            klines = klines_raw.get("qfqday") or klines_raw.get("day", [])
        except Exception:
            klines = []
        health_score, health_issues = calc_position_health(pos, price_data, klines, sector_change)

        position_details.append({
            "code": code,
            "name": pos["name"],
            "shares": pos["shares"],
            "avg_price": pos["avg_price"],
            "current_price": current_price,
            "market_value": round(market_value, 2),
            "profit": round(profit, 2),
            "profit_pct": round(profit_pct, 2),
            "change_pct": price_data.get("change_pct", 0),
            "atr_stop_loss": pos.get("atr_stop_loss"),
            "health_score": health_score,
            "health_issues": health_issues,
            "buy_date": pos.get("buy_date", "-"),
        })

    total_assets = round(acc["current_cash"] + total_market_value, 2)
    daily_pnl = total_assets - acc.get("peak_total_assets", total_assets)
    total_return = total_assets - acc["initial_cash"]
    total_return_pct = total_return / acc["initial_cash"] * 100

    # 指数数据
    index_info = {}
    if index_data and isinstance(index_data, dict):
        index_info = {
            "name": index_data.get("name", "上证指数"),
            "price": index_data.get("price", 0),
            "change_pct": index_data.get("change_pct", 0),
        }

    # 指标计算
    snapshots = acc.get("daily_snapshots", [])
    sharpe = calc_sharpe(snapshots)
    max_dd, dd_peak_date, dd_trough_date = calc_max_drawdown(snapshots, acc["initial_cash"])
    account_return = calc_benchmark_return(snapshots)

    # 今日交易
    today_trades = [t for t in acc.get("trade_history", []) if t.get("date") == today]

    # 风控事件统计
    stats = acc.get("stats", {})

    # 今日新闻关键词（供 AI 复盘参考）
    news_keywords = []
    try:
        rc_news, news_data = run_script("get_news.py", ["A股", "市场"])
        if rc_news == 0 and isinstance(news_data, list):
            news_keywords = [n.get("title", "") for n in news_data[:5]]
    except Exception:
        pass

    # 生成 Markdown
    lines = []
    lines.append(f"# 📊 量化交易日报 {today}")
    lines.append(f"")
    lines.append(f"## 账户概况")
    lines.append(f"")
    lines.append(f"| 项目 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总资产 | ¥{total_assets:,.2f} |")
    lines.append(f"| 可用资金 | ¥{acc['current_cash']:,.2f} |")
    lines.append(f"| 持仓市值 | ¥{total_market_value:,.2f} |")
    lines.append(f"| 累计收益 | ¥{total_return:,.2f} ({total_return_pct:+.2f}%) |")
    lines.append(f"| 峰值资产 | ¥{acc.get('peak_total_assets', 0):,.2f} |")
    peak = acc.get("peak_total_assets", acc["initial_cash"])
    drawdown_now = (peak - total_assets) / peak * 100 if peak > 0 else 0
    lines.append(f"| 当前回撤 | {drawdown_now:.2f}% |")
    if acc.get("frozen_until"):
        lines.append(f"| 冻结状态 | 🔒 至 {acc['frozen_until']} |")
    lines.append(f"")

    # 指数
    if index_info:
        lines.append(f"## 大盘行情")
        lines.append(f"")
        idx_emoji = "📈" if index_info["change_pct"] >= 0 else "📉"
        lines.append(f"- {idx_emoji} {index_info.get('name', '上证指数')}: **{index_info['price']}** ({index_info['change_pct']:+.2f}%)")
        lines.append(f"")

    # 持仓明细 + 健康度
    if position_details:
        lines.append(f"## 持仓明细")
        lines.append(f"")
        lines.append(f"| 代码 | 名称 | 买入日 | 持仓 | 成本 | 现价 | 盈亏 | 盈亏% | 今日 | ATR止损 | 健康度 |")
        lines.append(f"|------|------|--------|------|------|------|------|-------|------|--------|--------|")
        for p in position_details:
            emoji = "🟢" if p["profit"] >= 0 else "🔴"
            atr_str = f"{p['atr_stop_loss']*100:.1f}%" if p["atr_stop_loss"] else "-"
            hs = p.get("health_score", 50)
            if hs >= 70:
                hs_str = f"🟢 {hs}"
            elif hs >= 50:
                hs_str = f"🟡 {hs}"
            elif hs >= 30:
                hs_str = f"🟠 {hs}"
            else:
                hs_str = f"🔴 {hs}"
            lines.append(f"| {p['code']} | {p['name']} | {p.get('buy_date', '-')[5:] if p.get('buy_date') and len(p.get('buy_date', '')) == 10 else p.get('buy_date', '-')} | {p['shares']} | {p['avg_price']:.2f} | {p['current_price']:.2f} | {emoji} {p['profit']:+,.2f} | {p['profit_pct']:+.2f}% | {p['change_pct']:+.2f}% | {atr_str} | {hs_str} |")
        # 健康度告警
        warn_stocks = [p for p in position_details if p.get("health_score", 100) < 50]
        if warn_stocks:
            lines.append(f"")
            lines.append(f"### ⚠️ 健康度告警")
            for p in warn_stocks:
                hs = p.get("health_score", 0)
                color = "🔴 建议止损" if hs < 30 else "🟡 关注风险"
                issues_str = "、".join(p.get("health_issues", []))
                lines.append(f"- {color} **{p['name']}**({p['code']}) 健康度 {hs}分：{issues_str}")
        lines.append(f"")

    # 今日交易 + 操作逻辑
    if today_trades:
        lines.append(f"## 今日交易 ({len(today_trades)} 笔)")
        lines.append(f"")
        for t in today_trades:
            emoji = "🟢 买入" if t["type"] == "buy" else "🔴 卖出"
            profit_str = f" 盈亏:{t.get('profit', 0):+,.2f}" if t["type"] == "sell" else ""
            lines.append(f"- {emoji} {t['code']} {t['name']} {t['shares']}股 @ {t['price']:.2f}{profit_str}")
        lines.append(f"")

        # 操作逻辑
        lines.append(f"## 📝 操作逻辑")
        lines.append(f"")
        for t in today_trades:
            action = "买入" if t["type"] == "buy" else "卖出"
            reason = t.get("reason", "")
            if reason and reason != "manual":
                lines.append(f"**{action} {t['name']}({t['code']})**：{reason}")
            else:
                lines.append(f"**{action} {t['name']}({t['code']})**：（未记录操作理由）")
            lines.append(f"")
    else:
        # 无操作日静默报告
        lines.append(f"## 今日无操作")
        lines.append(f"")
        if acc["positions"]:
            lines.append(f"持仓均在安全区间内，无需调仓。")
            lines.append(f"")
            # 距离止盈/止损的距离
            params = acc["market_regime"].get("params_active", {})
            tp1 = params.get("tp1", 0.15)
            fixed_stop = params.get("stop_loss", -0.07)
            for code, pos in acc["positions"].items():
                pd = prices.get(code, {})
                cp = pd.get("price", pos["avg_price"])
                ap = pos["avg_price"]
                if ap <= 0:
                    continue
                profit_pct = (cp - ap) / ap
                to_tp1 = (tp1 - profit_pct) * 100
                atr_stop = pos.get("atr_stop_loss", fixed_stop)
                effective_stop = max(fixed_stop, atr_stop) if atr_stop else fixed_stop
                to_stop = (profit_pct - effective_stop) * 100
                lines.append(f"- **{pos['name']}**({code}): 浮盈 {profit_pct*100:+.1f}%，距TP1还有 {to_tp1:.1f}%，距止损线 {to_stop:.1f}%")
            lines.append(f"")
        else:
            lines.append(f"空仓状态，等待建仓信号。")
            lines.append(f"")

    # 风控统计
    lines.append(f"## 风控统计")
    lines.append(f"")
    lines.append(f"- 📊 累计交易: {stats.get('total_trades', 0)} 笔 (买:{stats.get('total_buys', 0)} 卖:{stats.get('total_sells', 0)})")
    win_trades = stats.get('win_trades', 0)
    lose_trades = stats.get('lose_trades', 0)
    total_closed = win_trades + lose_trades
    win_rate_str = f"{win_trades}/{total_closed} = {win_trades / max(total_closed, 1) * 100:.1f}%" if total_closed > 0 else "暂无卖出交易"
    lines.append(f"- 🎯 胜率: {win_rate_str}")
    # 已实现 vs 浮动盈亏
    realized_profit = stats.get('total_profit', 0)
    lines.append(f"- 💰 已实现盈亏: ¥{realized_profit:+,.2f}" + ("（尚无卖出交易）" if total_closed == 0 else ""))
    lines.append(f"- 📊 浮动盈亏: ¥{total_return:+,.2f}（含浮盈的总收益）")
    lines.append(f"- 📉 止损触发: {stats.get('stop_loss_count', 0)} 次")
    lines.append(f"- 📈 止盈触发: {stats.get('take_profit_count', 0)} 次")
    lines.append(f"- 🔄 移动止盈: {stats.get('trailing_stop_count', 0)} 次")
    lines.append(f"- 🚨 回撤熔断: {stats.get('drawdown_circuit_breaks', 0)} 次")
    # 费用明细拆分
    total_fees = stats.get('total_fees', 0)
    sell_fees = sum(t.get('fees', 0) for t in acc.get('trade_history', []) if t['type'] == 'sell')
    buy_fees = total_fees - sell_fees
    commission_est = round(total_fees * 0.85, 2)  # 佣金约占85%
    transfer_est = round(total_fees * 0.05, 2)    # 过户费约占5%
    stamp_est = round(sell_fees * 0.55, 2) if sell_fees > 0 else 0  # 印花税仅卖出
    lines.append(f"- 💸 累计费用: ¥{total_fees:,.2f}（佣金约¥{commission_est:,.2f} + 过户费约¥{transfer_est:,.2f} + 印花税约¥{stamp_est:,.2f}）")
    lines.append(f"")

    # 绩效指标
    lines.append(f"## 绩效指标")
    lines.append(f"")
    if sharpe is not None:
        lines.append(f"- 📐 夏普比率 (20日): **{sharpe}**")
    else:
        lines.append(f"- 📐 夏普比率 (20日): 数据不足")
    lines.append(f"- 📉 最大回撤: **{max_dd:.2f}%**" + (f" ({dd_peak_date} → {dd_trough_date})" if dd_peak_date else ""))
    if account_return is not None:
        lines.append(f"- 📊 累计收益率: **{account_return:+.2f}%**")

    # 补充指标
    trades = acc.get("trade_history", [])
    sell_trades = [t for t in trades if t["type"] == "sell"]
    if sell_trades:
        wins = [t for t in sell_trades if t.get("profit", 0) > 0]
        losses = [t for t in sell_trades if t.get("profit", 0) <= 0]
        win_rate = len(wins) / len(sell_trades) * 100
        avg_win = sum(t["profit"] for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t["profit"] for t in losses) / len(losses)) if losses else 0
        profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")
        max_single_loss = min((t.get("profit", 0) for t in sell_trades), default=0)

        lines.append(f"- 🎯 胜率: **{win_rate:.1f}%** ({len(wins)}胜/{len(losses)}负)")
        lines.append(f"- ⚖️ 盈亏比: **{profit_loss_ratio:.2f}** (均盈¥{avg_win:,.0f} / 均亏¥{avg_loss:,.0f})")
        lines.append(f"- 📉 最大单笔亏损: **¥{max_single_loss:,.2f}**")

    # 平均持仓天数
    if sell_trades:
        hold_days_list = []
        buy_dates = {}  # code -> first buy date
        for t in trades:
            if t["type"] == "buy":
                if t["code"] not in buy_dates:
                    buy_dates[t["code"]] = t["date"]
            elif t["type"] == "sell" and t.get("full_exit"):
                bd = buy_dates.get(t["code"])
                if bd:
                    try:
                        d1 = datetime.strptime(bd, "%Y-%m-%d")
                        d2 = datetime.strptime(t["date"], "%Y-%m-%d")
                        hold_days_list.append((d2 - d1).days)
                    except Exception:
                        pass
        if hold_days_list:
            avg_hold = sum(hold_days_list) / len(hold_days_list)
            lines.append(f"- 📅 平均持仓天数: **{avg_hold:.1f}天**")

    lines.append(f"")

    # 持仓相关性监控
    if len(acc["positions"]) >= 2:
        corr_matrix, corr_warnings = calc_position_correlations(acc["positions"])
        if corr_matrix:
            lines.append(f"## 📊 持仓相关性")
            lines.append(f"")
            # 表头
            codes = list(corr_matrix.keys())
            names = [acc["positions"][c].get("name", c) for c in codes]
            header = "| | " + " | ".join(names) + " |"
            sep = "|---|" + "|".join(["---" for _ in names]) + "|"
            lines.append(header)
            lines.append(sep)
            for i, c1 in enumerate(codes):
                row = f"| {names[i]} "
                for j, c2 in enumerate(codes):
                    val = corr_matrix[c1].get(c2, 0)
                    if val >= 0.7:
                        row += f"| **{val:.2f}** "
                    elif val <= -0.3:
                        row += f"| {val:.2f} "
                    else:
                        row += f"| {val:.2f} "
                row += "|"
                lines.append(row)
            lines.append(f"")
            if corr_warnings:
                lines.append(f"### ⚠️ 相关性警告")
                for w in corr_warnings:
                    lines.append(f"- {w}")
                lines.append(f"- 建议：相关性 > 0.7 的持仓同时下跌风险大，考虑换入不同风格的股票")
                lines.append(f"")

    # 市场环境
    regime = acc.get("market_regime", {})
    lines.append(f"## 市场环境")
    lines.append(f"")
    lines.append(f"- 当前状态: **{regime.get('current', 'unknown')}**")
    params = regime.get("params_active", {})
    if params:
        lines.append(f"- 止损线: {params.get('stop_loss', 0)*100:.1f}%")
        lines.append(f"- 止盈: TP1={params.get('tp1', 0)*100:.0f}% / TP2={params.get('tp2', 0)*100:.0f}% / TP3={params.get('tp3', 0)*100:.0f}%")
        lines.append(f"- 最大持仓: {params.get('max_position', 0)*100:.0f}% / {params.get('max_holdings', 0)} 只")
    lines.append(f"")

    # 辩论记录（如果存在）
    debate_file = DATA_DIR / f"debate_{today}.md"
    if debate_file.exists():
        try:
            with open(debate_file, 'r', encoding='utf-8') as f:
                debate_content = f.read().strip()
            if debate_content:
                lines.append(debate_content)
                lines.append(f"")
        except Exception:
            pass

    # 策略复盘（AI 填充区）
    lines.append(f"## 🧠 策略复盘")
    lines.append(f"")
    lines.append(f"<!-- AI_REVIEW_START -->")
    if today_trades:
        lines.append(f"### 今日可取之处")
        lines.append(f"")
        lines.append(f"（待 AI 填充）")
        lines.append(f"")
        lines.append(f"### 需要改进的地方")
        lines.append(f"")
        lines.append(f"（待 AI 填充）")
        lines.append(f"")
    lines.append(f"### 明日策略")
    lines.append(f"")
    lines.append(f"（待 AI 填充）")
    lines.append(f"")
    # 输出新闻上下文供 AI 参考
    if news_keywords:
        lines.append(f"**今日财经新闻关键词：**")
        for nk in news_keywords:
            lines.append(f"- {nk}")
        lines.append(f"")
    # 输出持仓详细数据供 AI 分析
    if position_details:
        lines.append(f"**持仓数据供参考：**")
        for p in position_details:
            lines.append(f"- {p['name']}({p['code']}): 浮盈{p['profit_pct']:+.2f}%，今日{p['change_pct']:+.2f}%，健康度{p['health_score']}")
        lines.append(f"")
    lines.append(f"<!-- AI_REVIEW_END -->")
    lines.append(f"")

    # 明日关注板块（独立区域，提升可读性）
    lines.append(f"## 🔮 明日关注")
    lines.append(f"")
    if position_details:
        # 根据健康度和盈亏状态分类
        for p in position_details:
            hs = p.get("health_score", 50)
            profit_pct = p.get("profit_pct", 0)
            change_pct = p.get("change_pct", 0)
            if hs < 50:
                lines.append(f"- 🔴 **{p['name']}({p['code']})**：健康度{hs}，{'浮亏' if profit_pct < 0 else '浮盈'}{profit_pct:+.1f}%，{'考虑止损换股' if hs < 30 else '关注风险，跌破止损线执行'}")
            elif hs < 70:
                lines.append(f"- 🟡 **{p['name']}({p['code']})**：健康度{hs}，{'浮亏' if profit_pct < 0 else '浮盈'}{profit_pct:+.1f}%，持有观察，设好止损")
            else:
                lines.append(f"- 🟢 **{p['name']}({p['code']})**：健康度{hs}，{'浮亏' if profit_pct < 0 else '浮盈'}{profit_pct:+.1f}%，继续持有")
        lines.append(f"")
    # 行业轮动方向
    try:
        rc_rot, rot_data = run_script("sector_rotation.py", ["--json"])
        if rc_rot == 0 and isinstance(rot_data, dict):
            ranking = rot_data.get("sector_ranking", [])
            if ranking:
                top3 = ranking[:3]
                top3_str = "、".join(f"{s['name']}({s.get('change', 0):+.1f}%)" for s in top3)
                lines.append(f"- 👀 强势板块：{top3_str}")
                # 建议关注的板块方向
                style = rot_data.get("style", {})
                if style.get("style") == "growth_leading":
                    lines.append(f"- 💡 风格提示：成长主导（成长{style.get('growth_avg', 0):+.1f}% vs 价值{style.get('value_avg', 0):+.1f}%），关注成长股机会")
                elif style.get("style") == "value_leading":
                    lines.append(f"- 💡 风格提示：价值主导（价值{style.get('value_avg', 0):+.1f}% vs 成长{style.get('growth_avg', 0):+.1f}%），关注价值股机会")
                lines.append(f"")
    except Exception:
        pass

    lines.append(f"---")
    lines.append(f"*生成时间: {now.strftime('%Y-%m-%d %H:%M:%S')}*")

    report_text = "\n".join(lines)

    # 保存到文件
    report_file = DATA_DIR / f"trend_report_{today}.md"
    with open(report_file, "w") as f:
        f.write(report_text)

    return {
        "status": "ok",
        "report_file": str(report_file),
        "report": report_text,
        "need_email": True,
    }


if __name__ == "__main__":
    result = generate_report()
    if "error" in result:
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    # 输出报告内容到 stdout
    print(result["report"])

    # 输出元数据到 stderr
    print(json.dumps({
        "status": "ok",
        "report_file": result["report_file"],
        "need_email": result.get("need_email", False),
    }, ensure_ascii=False), file=sys.stderr)
