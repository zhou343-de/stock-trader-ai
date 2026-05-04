#!/usr/bin/env python3
"""
交易执行引擎（虚拟盘）v3
子命令：init / buy / sell / status / history / reset / snapshot / update_regime / update_atr_stops
完整实现A股规则：T+1、100股取整、涨跌幅限制、价格笼子、板块限制、费用
新增：组合级回撤熔断、移动止盈、ATR动态止损、冻结期
"""
import json, sys, os, shutil, subprocess, math
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "data"
ACCOUNT_FILE = DATA_DIR / "trend_account.json"

# === A股规则配置 ===

PRICE_LIMITS = {
    "main":  0.10, "gem": 0.20, "star": 0.20, "bse": 0.30,
    "st": 0.05, "gem_st": 0.20,
}

LOT_SIZES = {"main": 100, "gem": 100, "star": 200}

ALLOWED_BOARDS = {"main", "gem", "st", "gem_st"}

FEES = {
    "commission_rate": 0.0003, "commission_min": 5.0,
    "transfer_rate": 0.00001, "stamp_rate": 0.0005,
    "etf_commission": 0.0001, "bond_commission": 0.0001,
}

# 模拟滑点（动态：根据成交额调整）
BASE_SLIPPAGE = 0.002  # 基础 0.2%
def calc_slippage(amount_yuan):
    """成交额越小，滑点越大（小盘股冲击成本更高）"""
    if amount_yuan < 30000000:      # < 3000万
        return 0.005                # 0.5%
    elif amount_yuan < 50000000:    # 3000万-5000万
        return 0.004                # 0.4%
    elif amount_yuan < 100000000:   # 5000万-1亿
        return 0.003                # 0.3%
    else:                           # >= 1亿
        return BASE_SLIPPAGE        # 0.2%
def apply_slippage(price, is_buy, amount_yuan=200000000):
    """买入价上浮，卖出价下浮（动态滑点）"""
    slippage = calc_slippage(amount_yuan)
    if is_buy:
        return round(price * (1 + slippage), 2)
    else:
        return round(price * (1 - slippage), 2)

# === 新增：回撤熔断配置 ===
DRAWDOWN_THRESHOLD = 0.10      # 峰值回撤 10% 触发熔断
DRAWDOWN_FREEZE_DAYS = 3       # 熔断后冻结交易天数
TRAILING_STOP_TRIGGER = 0.10   # 移动止盈触发：浮盈 ≥ 10%
TRAILING_STOP_RATIO = 0.50     # 移动止盈：回落至最高浮盈的 50%

DEFAULT_ACCOUNT = {
    "version": 3,
    "created_at": None,
    "initial_cash": 100000.0,
    "current_cash": 100000.0,
    "total_assets": 100000.0,
    "peak_total_assets": 100000.0,  # 历史峰值（回撤熔断用）
    "frozen_until": None,           # 熔断冻结截止日期
    "positions": {},
    "trade_history": [],
    "daily_snapshots": [],
    "market_regime": {
        "current": "range",
        "confidence": 0,
        "params_active": {
            "stop_loss": -0.07,
            "tp1": 0.15, "tp2": 0.25, "tp3": 0.35,
            "max_position": 0.85,
            "max_holdings": 3,
            "min_change": 1, "max_change": 9.5,
            "atr_multiplier": 1.5,
        }
    },
    "stats": {
        "total_trades": 0, "total_buys": 0, "total_sells": 0,
        "stop_loss_count": 0, "take_profit_count": 0,
        "trailing_stop_count": 0,
        "drawdown_circuit_breaks": 0,
        "total_fees": 0.0, "total_profit": 0.0,
        "win_trades": 0, "lose_trades": 0,
    }
}

# === 辅助函数 ===

def detect_board(code):
    if code.startswith("688"):
        return "star"
    elif code.startswith("300") or code.startswith("301"):
        return "gem"
    elif code.startswith("6") or code.startswith("000") or code.startswith("001") or code.startswith("002") or code.startswith("003"):
        return "main"
    elif code.startswith("8") or code.startswith("4"):
        return "bse"
    else:
        return "main"

def is_st_stock(name):
    return "ST" in name.upper() or "*ST" in name.upper()

def calc_price_limit(code, name, pre_close):
    board = detect_board(code)
    if is_st_stock(name):
        limit = PRICE_LIMITS["gem_st"] if board in ("gem", "star") else PRICE_LIMITS["st"]
    else:
        limit = PRICE_LIMITS.get(board, 0.10)
    limit_up = round(pre_close * (1 + limit), 2)
    limit_down = round(pre_close * (1 - limit), 2)
    return limit_up, limit_down, limit

def check_price_cage(code, price, pre_close):
    board = detect_board(code)
    if board in ("star", "gem"):
        upper, lower = pre_close * 1.02, pre_close * 0.98
    else:
        upper = min(pre_close * 1.02, pre_close + 0.10)
        lower = max(pre_close * 0.98, pre_close - 0.10)
    return lower <= price <= upper

def calc_fees(amount, is_sell=False, stock_type="stock"):
    if stock_type == "etf":
        commission = max(amount * FEES["etf_commission"], 0.1)
        transfer, stamp = 0, 0
    elif stock_type == "bond":
        commission = max(amount * FEES["bond_commission"], 0.1)
        transfer, stamp = 0, 0
    else:
        commission = max(amount * FEES["commission_rate"], FEES["commission_min"])
        transfer = amount * FEES["transfer_rate"]
        stamp = amount * FEES["stamp_rate"] if is_sell else 0
    return round(commission + transfer + stamp, 2)

def load_account():
    if not ACCOUNT_FILE.exists():
        return None
    with open(ACCOUNT_FILE) as f:
        return json.load(f)

def save_account(acc):
    import fcntl
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(CST).strftime("%Y-%m-%d")
    backup = DATA_DIR / f"trend_account_{today}.json"
    if ACCOUNT_FILE.exists() and not backup.exists():
        shutil.copy2(ACCOUNT_FILE, backup)
    # 原子写入：写临时文件再 rename，避免并发读到半写状态
    tmp_file = ACCOUNT_FILE.with_suffix(".tmp")
    lock_file = ACCOUNT_FILE.with_suffix(".lock")
    try:
        with open(lock_file, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)  # 排他锁
            with open(tmp_file, "w") as f:
                json.dump(acc, f, ensure_ascii=False, indent=2)
            tmp_file.rename(ACCOUNT_FILE)  # 原子操作
            fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass

def calc_total_assets(acc, current_prices=None):
    total = acc["current_cash"]
    for code, pos in acc["positions"].items():
        price = current_prices.get(code, pos["avg_price"]) if current_prices else pos["avg_price"]
        total += pos["shares"] * price
    return round(total, 2)

# === 新增：ATR 计算 ===
def calc_atr(code, period=20):
    """获取日K线并计算 ATR（Average True Range）"""
    market_code = f"sh{code}" if code.startswith("6") else f"sz{code}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market_code},day,,,{period+5},qfq"
    try:
        r = subprocess.run(["curl", "-s", "--connect-timeout", "8", url],
                           capture_output=True, timeout=12)
        data = json.loads(r.stdout.decode("utf-8"))
        klines = data.get("data", {}).get(market_code, {})
        rows = klines.get("qfqday") or klines.get("day", [])
        if len(rows) < 2:
            return None
        true_ranges = []
        for i in range(1, len(rows)):
            high = float(rows[i][3])
            low = float(rows[i][4])
            prev_close = float(rows[i-1][2])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        atr = sum(true_ranges[-period:]) / min(len(true_ranges), period)
        return round(atr, 4)
    except Exception:
        return None

def calc_dynamic_stop_loss(code, buy_price, atr_multiplier=1.5):
    """基于 ATR 计算动态止损百分比（含波动率自适应）"""
    atr = calc_atr(code)
    if atr and buy_price > 0:
        # 波率自适应：个股 20 日波动率越高，ATR 倍数越大（防盘中长影线扫损）
        market_code = f"sh{code}" if code.startswith("6") else f"sz{code}"
        try:
            import subprocess, json
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market_code},day,,,25,qfq"
            r = subprocess.run(["curl", "-s", "--connect-timeout", "8", url],
                               capture_output=True, timeout=12)
            data = json.loads(r.stdout.decode("utf-8"))
            klines = data.get("data", {}).get(market_code, {})
            rows = klines.get("qfqday") or klines.get("day", [])
            if len(rows) >= 10:
                closes = [float(r[2]) for r in rows[-20:]]
                highs = [float(r[3]) for r in rows[-20:]]
                lows = [float(r[4]) for r in rows[-20:]]
                # 日内振幅均值
                avg_amplitude = sum((h - l) / c for h, l, c in zip(highs, lows, closes)) / len(closes)
                if avg_amplitude > 0.06:       # 日均振幅 > 6%（高波动小盘股）
                    atr_multiplier = 1.8
                elif avg_amplitude > 0.04:     # 日均振幅 > 4%
                    atr_multiplier = 1.6
        except Exception:
            pass  # 获取失败时用默认倍数
        stop_pct = -(atr * atr_multiplier) / buy_price
        return round(stop_pct, 4), atr
    return None, None

# === 新增：回撤熔断检查 ===
def check_drawdown_circuit_breaker(acc):
    """检查是否触发组合级回撤熔断，返回 (triggered, drawdown_pct)"""
    peak = acc.get("peak_total_assets", acc["initial_cash"])
    if peak <= 0:
        return False, 0
    total_assets = calc_total_assets(acc)
    drawdown = (peak - total_assets) / peak
    if drawdown >= DRAWDOWN_THRESHOLD:
        return True, round(drawdown, 4)
    return False, round(drawdown, 4)

def is_frozen(acc):
    """检查是否处于熔断冻结期"""
    frozen_until = acc.get("frozen_until")
    if not frozen_until:
        return False
    today = datetime.now(CST).strftime("%Y-%m-%d")
    return today <= frozen_until

# === 新增：移动止盈检查 ===
def check_trailing_stop(pos, current_price):
    """检查移动止盈：浮盈曾 ≥10% 后回落至最高浮盈的 50% → 卖出全部
    返回 (triggered, detail_str)
    """
    avg_price = pos["avg_price"]
    if avg_price <= 0:
        return False, ""
    current_profit_pct = (current_price - avg_price) / avg_price
    # 兼容新旧字段
    highest_profit_pct = max(
        pos.get("highest_profit_pct", 0),
        pos.get("max_profit_pct", 0)
    )

    # 更新历史最高浮盈（两个字段都更新）
    if current_profit_pct > highest_profit_pct:
        pos["highest_profit_pct"] = round(current_profit_pct, 4)
        pos["max_profit_pct"] = round(current_profit_pct, 4)
        highest_profit_pct = current_profit_pct

    # 触发条件：最高浮盈曾 ≥ 10%，当前回落至最高浮盈的 50% 以下
    if highest_profit_pct >= TRAILING_STOP_TRIGGER:
        trailing_level = highest_profit_pct * TRAILING_STOP_RATIO
        if current_profit_pct <= trailing_level:
            detail = (f"移动止盈触发：最高浮盈 {highest_profit_pct*100:.1f}%，"
                     f"当前浮盈 {current_profit_pct*100:.1f}% ≤ 阈值 {trailing_level*100:.1f}%")
            return True, detail
    return False, ""

def update_peak_assets(acc, total_assets):
    """更新历史峰值"""
    peak = acc.get("peak_total_assets", acc["initial_cash"])
    if total_assets > peak:
        acc["peak_total_assets"] = total_assets

# === 命令实现 ===

def cmd_init(args):
    cash = float(args[0]) if args else 100000.0
    acc = DEFAULT_ACCOUNT.copy()
    acc["initial_cash"] = cash
    acc["current_cash"] = cash
    acc["total_assets"] = cash
    acc["peak_total_assets"] = cash
    acc["frozen_until"] = None
    acc["created_at"] = datetime.now(CST).isoformat()
    save_account(acc)
    print(json.dumps({"status": "ok", "cash": cash}, ensure_ascii=False))

def cmd_buy(args):
    """买入：buy <代码> <名称> <数量> <价格> [--reason <理由>] [--logic-stop <价格>] [--falsification <条件>] [--vote-score <分数>]"""
    if len(args) < 4:
        print(json.dumps({"error": "用法: buy <代码> <名称> <数量> <价格> [--reason <理由>] [--logic-stop <价格>] [--falsification <条件>] [--vote-score <分数>]"}, ensure_ascii=False))
        sys.exit(1)

    raw_code, name, shares_str, price_str = args[0], args[1], args[2], args[3]
    reason = ""
    logic_stop = None
    falsification = ""
    vote_score = 0
    if "--reason" in args:
        idx = args.index("--reason")
        if idx + 1 < len(args):
            reason = args[idx + 1]
    if "--logic-stop" in args:
        idx = args.index("--logic-stop")
        if idx + 1 < len(args):
            try:
                logic_stop = float(args[idx + 1])
            except ValueError:
                pass
    if "--falsification" in args:
        idx = args.index("--falsification")
        if idx + 1 < len(args):
            falsification = args[idx + 1]
    if "--vote-score" in args:
        idx = args.index("--vote-score")
        if idx + 1 < len(args):
            try:
                vote_score = int(args[idx + 1])
            except ValueError:
                pass
    shares = int(shares_str)
    query_price = float(price_str)
    # 估算日成交额（用查询价×数量×300倍模拟）
    est_daily_amount = query_price * shares * 300
    price = apply_slippage(query_price, is_buy=True, amount_yuan=est_daily_amount)
    code = raw_code.lstrip("shszSHSZ")
    board = detect_board(code)

    acc = load_account()
    if not acc:
        print(json.dumps({"error": "账户未初始化"}, ensure_ascii=False))
        sys.exit(1)

    # 熔断冻结检查
    if is_frozen(acc):
        frozen_until = acc.get("frozen_until", "")
        print(json.dumps({"error": f"交易冻结中（回撤熔断），冻结至 {frozen_until}"}, ensure_ascii=False))
        sys.exit(1)

    # 板块权限检查
    if board not in ALLOWED_BOARDS:
        print(json.dumps({"error": f"10万资金无法交易 {board} 板块（需50万+）"}, ensure_ascii=False))
        sys.exit(1)

    # 最小买入单位
    lot_size = LOT_SIZES.get(board, 100)
    shares = (shares // lot_size) * lot_size
    if shares < lot_size:
        print(json.dumps({"error": f"{board}板最小买入 {lot_size} 股"}, ensure_ascii=False))
        sys.exit(1)

    params = acc["market_regime"]["params_active"]
    if code not in acc["positions"] and len(acc["positions"]) >= params["max_holdings"]:
        print(json.dumps({"error": f"已达最大持仓数 {params['max_holdings']}"}, ensure_ascii=False))
        sys.exit(1)

    gross = shares * price
    fees = calc_fees(gross, is_sell=False)
    total_cost = gross + fees

    if total_cost > acc["current_cash"]:
        print(json.dumps({"error": f"资金不足：需要 {total_cost:.2f}，可用 {acc['current_cash']:.2f}"}, ensure_ascii=False))
        sys.exit(1)

    total_assets = calc_total_assets(acc)
    max_position_value = total_assets * params["max_position"]
    current_position_value = sum(p["shares"] * p["avg_price"] for p in acc["positions"].values())
    if current_position_value + gross > max_position_value:
        print(json.dumps({"error": f"超过仓位限制 {params['max_position']*100:.0f}%"}, ensure_ascii=False))
        sys.exit(1)

    today = datetime.now(CST).strftime("%Y-%m-%d")

    # 计算 ATR 动态止损
    atr_multiplier = params.get("atr_multiplier", 1.5)
    dynamic_stop, atr_value = calc_dynamic_stop_loss(code, price, atr_multiplier)

    # 加仓 or 新建
    if code in acc["positions"]:
        pos = acc["positions"][code]
        old_total = pos["shares"] * pos["avg_price"]
        new_total = old_total + gross
        pos["shares"] += shares
        pos["avg_price"] = new_total / pos["shares"]
        pos["last_buy_date"] = today
        # 更新 ATR 止损（取更宽的）
        if dynamic_stop and (not pos.get("atr_stop_loss") or dynamic_stop > pos.get("atr_stop_loss", 0)):
            pos["atr_stop_loss"] = dynamic_stop
            pos["atr_value"] = atr_value
    else:
        pos_data = {
            "name": name,
            "shares": shares,
            "avg_price": price,
            "buy_date": today,
            "last_buy_date": today,
            "highest_price": price,
            "highest_profit_pct": 0,
            "max_profit_pct": 0,  # 历史最高浮盈（只增不减，移动止盈用）
            "peak_intraday_profit": 0,  # T+1 意识：今日买入此字段为0，次日开始追踪
            "board": board,
            "logic_stop_price": logic_stop,  # 辩论逻辑止损价位
            "falsification": falsification,  # 可证伪条件
            "vote_score": vote_score,  # 辩论投票得分
            "debate_date": today,  # 辩论日期
        }
        if dynamic_stop:
            pos_data["atr_stop_loss"] = dynamic_stop
            pos_data["atr_value"] = atr_value
        acc["positions"][code] = pos_data

    acc["current_cash"] -= total_cost
    acc["stats"]["total_trades"] += 1
    acc["stats"]["total_buys"] += 1
    acc["stats"]["total_fees"] += fees

    # 更新峰值
    new_total_assets = calc_total_assets(acc)
    update_peak_assets(acc, new_total_assets)

    result = {
        "status": "ok", "action": "buy",
        "code": code, "name": name, "board": board,
        "shares": shares, "query_price": query_price, "exec_price": price,
        "slippage": f"+{calc_slippage(est_daily_amount)*100:.1f}%",
        "fees": fees, "total_cost": total_cost,
        "remaining_cash": round(acc["current_cash"], 2),
    }
    if dynamic_stop:
        result["atr_stop_loss"] = f"{dynamic_stop*100:.2f}%"
        result["atr_value"] = atr_value

    acc["trade_history"].append({
        "type": "buy", "code": code, "name": name, "board": board,
        "shares": shares, "price": price, "fees": fees, "total": total_cost,
        "atr_stop_loss": dynamic_stop, "atr_value": atr_value,
        "reason": reason,
        "date": today, "time": datetime.now(CST).strftime("%H:%M:%S"),
    })

    save_account(acc)
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_sell(args):
    """卖出：sell <代码> <数量> <价格> [--reason stop_loss|take_profit|trailing_stop|drawdown_breaker|manual]"""
    if len(args) < 3:
        print(json.dumps({"error": "用法: sell <代码> <数量> <价格> [--reason ...]"}, ensure_ascii=False))
        sys.exit(1)

    raw_code = args[0]
    sell_shares = int(args[1])
    query_price = float(args[2])
    price = apply_slippage(query_price, is_buy=False, amount_yuan=query_price*sell_shares*300)  # 模拟滑点：卖出价下浮
    reason = "manual"
    if "--reason" in args:
        idx = args.index("--reason")
        if idx + 1 < len(args):
            reason = args[idx + 1]

    code = raw_code.lstrip("shszSHSZ")
    acc = load_account()
    if not acc:
        print(json.dumps({"error": "账户未初始化"}, ensure_ascii=False))
        sys.exit(1)

    if code not in acc["positions"]:
        print(json.dumps({"error": f"未持有 {code}"}, ensure_ascii=False))
        sys.exit(1)

    pos = acc["positions"][code]
    today = datetime.now(CST).strftime("%Y-%m-%d")

    # T+1 检查
    if pos.get("last_buy_date", "") == today:
        print(json.dumps({"error": f"T+1规则：{code} 今日买入，不可当日卖出！"}, ensure_ascii=False))
        sys.exit(1)

    pre_close = pos.get("pre_close", pos["avg_price"])
    if pre_close:
        limit_up, limit_down, _ = calc_price_limit(code, pos["name"], pre_close)
        if price > limit_up:
            print(json.dumps({"error": f"卖出价 {price} 超过涨停价 {limit_up}"}, ensure_ascii=False))
            sys.exit(1)
        if price < limit_down:
            print(json.dumps({"error": f"卖出价 {price} 低于跌停价 {limit_down}"}, ensure_ascii=False))
            sys.exit(1)

    board = pos.get("board", detect_board(code))
    lot_size = LOT_SIZES.get(board, 100)

    if sell_shares >= pos["shares"]:
        sell_shares = pos["shares"]
    else:
        if board in ("gem", "star") and sell_shares >= 200:
            pass
        else:
            sell_shares = (sell_shares // lot_size) * lot_size
        if sell_shares <= 0:
            print(json.dumps({"error": "卖出数量不足"}, ensure_ascii=False))
            sys.exit(1)

    gross = sell_shares * price
    fees = calc_fees(gross, is_sell=True)
    net = gross - fees
    cost = sell_shares * pos["avg_price"]
    profit = net - cost
    profit_pct = profit / cost * 100 if cost > 0 else 0

    acc["current_cash"] += net
    acc["stats"]["total_trades"] += 1
    acc["stats"]["total_sells"] += 1
    acc["stats"]["total_fees"] += fees
    acc["stats"]["total_profit"] += profit
    if profit > 0:
        acc["stats"]["win_trades"] += 1
    else:
        acc["stats"]["lose_trades"] += 1
    if reason == "stop_loss":
        acc["stats"]["stop_loss_count"] += 1
    elif reason == "take_profit":
        acc["stats"]["take_profit_count"] += 1
    elif reason == "trailing_stop":
        acc["stats"]["trailing_stop_count"] += 1
    elif reason == "drawdown_breaker":
        acc["stats"]["drawdown_circuit_breaks"] += 1

    is_full = sell_shares >= pos["shares"]
    name = pos["name"]
    if is_full:
        del acc["positions"][code]
    else:
        pos["shares"] -= sell_shares

    # 更新峰值
    new_total_assets = calc_total_assets(acc)
    update_peak_assets(acc, new_total_assets)

    acc["trade_history"].append({
        "type": "sell", "code": code, "name": name, "board": board,
        "shares": sell_shares, "price": price, "fees": fees,
        "net": net, "profit": round(profit, 2), "profit_pct": round(profit_pct, 2),
        "reason": reason, "full_exit": is_full,
        "date": today, "time": datetime.now(CST).strftime("%H:%M:%S"),
    })

    est_daily_amount = query_price * sell_shares * 300
    save_account(acc)
    print(json.dumps({
        "status": "ok", "action": "sell",
        "code": code, "name": name, "board": board,
        "shares": sell_shares, "query_price": query_price, "exec_price": price,
        "slippage": f"-{calc_slippage(est_daily_amount)*100:.1f}%",
        "fees": fees, "net": round(net, 2), "profit": round(profit, 2),
        "profit_pct": round(profit_pct, 2), "reason": reason, "full_exit": is_full,
        "remaining_cash": round(acc["current_cash"], 2),
    }, ensure_ascii=False, indent=2))

def cmd_status(args):
    acc = load_account()
    if not acc:
        print(json.dumps({"error": "账户未初始化"}, ensure_ascii=False))
        sys.exit(1)
    total_market_value = sum(p["shares"] * p["avg_price"] for p in acc["positions"].values())
    acc["total_assets"] = round(acc["current_cash"] + total_market_value, 2)
    acc["total_return"] = round(acc["total_assets"] - acc["initial_cash"], 2)
    acc["total_return_pct"] = round(acc["total_return"] / acc["initial_cash"] * 100, 2)

    # 峰值回撤
    peak = acc.get("peak_total_assets", acc["initial_cash"])
    drawdown_pct = round((peak - acc["total_assets"]) / peak * 100, 2) if peak > 0 else 0

    output = {
        "initial_cash": acc["initial_cash"],
        "current_cash": round(acc["current_cash"], 2),
        "total_assets": acc["total_assets"],
        "total_return": acc["total_return"],
        "total_return_pct": f"{acc['total_return_pct']}%",
        "peak_total_assets": peak,
        "drawdown_from_peak": f"{drawdown_pct}%",
        "frozen_until": acc.get("frozen_until"),
        "positions_count": len(acc["positions"]),
        "positions": acc["positions"],
        "market_regime": acc["market_regime"],
        "stats": acc["stats"],
        "allowed_boards": list(ALLOWED_BOARDS),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

def cmd_history(args):
    acc = load_account()
    if not acc:
        print(json.dumps({"error": "账户未初始化"}, ensure_ascii=False))
        sys.exit(1)
    limit = int(args[0]) if args else 20
    trades = acc["trade_history"][-limit:]
    print(json.dumps(trades, ensure_ascii=False, indent=2))

def cmd_update_regime(args):
    if not args:
        print(json.dumps({"error": "用法: update_regime <json_string>"}, ensure_ascii=False))
        sys.exit(1)
    acc = load_account()
    if not acc:
        print(json.dumps({"error": "账户未初始化"}, ensure_ascii=False))
        sys.exit(1)
    regime_data = json.loads(args[0])

    # 兼容：market_regime.py 输出 "regime" 字段，内部存储用 "current"
    if "regime" in regime_data and "current" not in regime_data:
        regime_data["current"] = regime_data["regime"]

    old_regime = acc.get("market_regime", {})
    old_current = old_regime.get("current", "unknown")
    new_current = regime_data.get("current", "unknown")

    # 幂等性：如果环境完全相同，跳过写入
    if old_regime == regime_data:
        print(json.dumps({
            "status": "unchanged",
            "regime": new_current,
            "msg": "环境未变化，跳过更新"
        }, ensure_ascii=False, indent=2))
        return

    # 记录环境切换历史
    if "regime_history" not in acc:
        acc["regime_history"] = []
    acc["regime_history"].append({
        "time": datetime.now(CST).isoformat(),
        "from": old_current,
        "to": new_current,
        "confidence": regime_data.get("confidence", 0),
    })
    # 只保留最近 20 条
    acc["regime_history"] = acc["regime_history"][-20:]

    acc["market_regime"] = regime_data
    save_account(acc)
    print(json.dumps({
        "status": "ok",
        "regime": new_current,
        "changed_from": old_current,
        "confidence": regime_data.get("confidence", 0),
        "logged": True
    }, ensure_ascii=False, indent=2))

def cmd_update_atr_stops(args):
    """为所有持仓更新 ATR 动态止损"""
    acc = load_account()
    if not acc:
        print(json.dumps({"error": "账户未初始化"}, ensure_ascii=False))
        sys.exit(1)

    params = acc["market_regime"]["params_active"]
    atr_multiplier = params.get("atr_multiplier", 1.5)
    updated = {}

    for code, pos in acc["positions"].items():
        dynamic_stop, atr_value = calc_dynamic_stop_loss(code, pos["avg_price"], atr_multiplier)
        if dynamic_stop:
            old_stop = pos.get("atr_stop_loss")
            regime = acc.get("market_regime", {}).get("current", "range")
            if old_stop and dynamic_stop < old_stop:
                # ATR止损更宽（更负）时更新
                pos["atr_stop_loss"] = dynamic_stop
                pos["atr_value"] = atr_value
            elif not old_stop:
                pos["atr_stop_loss"] = dynamic_stop
                pos["atr_value"] = atr_value
            # 有效止损：牛市取更宽（给趋势空间），其他取更窄（保护优先）
            fixed_stop = params.get("stop_loss", -0.07)
            if regime == "bull":
                pos["effective_stop"] = max(fixed_stop, pos.get("atr_stop_loss", fixed_stop))
            else:
                pos["effective_stop"] = min(fixed_stop, pos.get("atr_stop_loss", fixed_stop))
            updated[code] = {
                "atr_stop_loss": f"{pos.get('atr_stop_loss', 0)*100:.2f}%",
                "atr_value": pos.get("atr_value"),
            }

    save_account(acc)
    print(json.dumps({"status": "ok", "updated": updated}, ensure_ascii=False, indent=2))

def cmd_snapshot(args):
    acc = load_account()
    if not acc:
        sys.exit(1)
    today = datetime.now(CST).strftime("%Y-%m-%d")

    # 获取实时价格计算真实总资产
    current_prices = {}
    if acc["positions"]:
        market_codes = []
        for code in acc["positions"]:
            prefix = "sh" if code.startswith("6") else "sz"
            market_codes.append(f"{prefix}{code}")
        try:
            r = subprocess.run(["python3", str(SCRIPT_DIR / "batch_query.py")] + market_codes,
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                price_data = json.loads(r.stdout.decode("utf-8"))
                for k, v in price_data.items():
                    pure_code = k.lstrip("shszSHSZ")
                    if pure_code in acc["positions"]:
                        current_prices[pure_code] = v.get("price", 0)
        except Exception:
            pass  # 获取失败时回退到成本价

    total_assets = calc_total_assets(acc, current_prices)
    update_peak_assets(acc, total_assets)
    snapshot = {
        "date": today, "total_assets": total_assets,
        "cash": acc["current_cash"], "positions": len(acc["positions"]),
        "regime": acc["market_regime"]["current"],
        "peak_total_assets": acc.get("peak_total_assets", total_assets),
    }
    acc["daily_snapshots"] = [s for s in acc["daily_snapshots"] if s["date"] != today]
    acc["daily_snapshots"].append(snapshot)
    save_account(acc)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))

def cmd_reset(args):
    cash = float(args[0]) if args else 100000.0
    # 回滚机制：reset 前自动备份
    if ACCOUNT_FILE.exists():
        ts = datetime.now(CST).strftime("%Y%m%d_%H%M%S")
        backup_file = DATA_DIR / f"trend_account_before_reset_{ts}.json"
        shutil.copy2(ACCOUNT_FILE, backup_file)
        print(json.dumps({
            "backup": str(backup_file),
            "msg": f"已备份到 {backup_file.name}，如需恢复请手动还原"
        }, ensure_ascii=False), file=sys.stderr)
    cmd_init([str(cash)])

def cmd_rules(args):
    rules = {
        "涨跌幅限制": PRICE_LIMITS,
        "最小买入单位": LOT_SIZES,
        "可交易板块": list(ALLOWED_BOARDS),
        "费用标准": FEES,
        "回撤熔断": {"阈值": f"{DRAWDOWN_THRESHOLD*100}%", "冻结天数": DRAWDOWN_FREEZE_DAYS},
        "移动止盈": {"触发": f"{TRAILING_STOP_TRIGGER*100}%", "回落比": f"{TRAILING_STOP_RATIO*100}%"},
        "10万资金不可交易": ["科创板(50万)", "北交所(50万)", "港股通(50万)", "融资融券(50万)", "期权(50万)"],
        "T+0品种": ["可转债", "跨境ETF", "货币ETF", "债券ETF", "黄金ETF"],
    }
    print(json.dumps(rules, ensure_ascii=False, indent=2))

def cmd_circuit_breaker_check(args):
    """检查回撤熔断状态，返回是否需要清仓"""
    acc = load_account()
    if not acc:
        print(json.dumps({"error": "账户未初始化"}, ensure_ascii=False))
        sys.exit(1)

    triggered, drawdown_pct = check_drawdown_circuit_breaker(acc)
    today = datetime.now(CST).strftime("%Y-%m-%d")

    result = {
        "triggered": triggered,
        "drawdown_pct": f"{drawdown_pct*100:.2f}%",
        "peak_total_assets": acc.get("peak_total_assets", acc["initial_cash"]),
        "current_total_assets": calc_total_assets(acc),
        "frozen_until": acc.get("frozen_until"),
        "is_frozen": is_frozen(acc),
    }

    if triggered and not is_frozen(acc):
        # 设置冻结期：今天 + 3个交易日
        freeze_end = today
        days_added = 0
        from datetime import date
        d = date.today()
        while days_added < DRAWDOWN_FREEZE_DAYS:
            d += timedelta(days=1)
            if d.weekday() < 5:
                days_added += 1
        acc["frozen_until"] = d.strftime("%Y-%m-%d")
        acc["stats"]["drawdown_circuit_breaks"] = acc["stats"].get("drawdown_circuit_breaks", 0) + 1

        # 标记需要清仓的持仓
        positions_to_sell = []
        for code, pos in acc["positions"].items():
            if pos.get("last_buy_date", "") != today:  # 排除今日买入（T+1）
                positions_to_sell.append({
                    "code": code, "name": pos["name"],
                    "shares": pos["shares"], "avg_price": pos["avg_price"],
                })
        result["action_required"] = "清仓所有非T+1持仓"
        result["positions_to_sell"] = positions_to_sell
        result["freeze_until"] = acc["frozen_until"]
        save_account(acc)

    print(json.dumps(result, ensure_ascii=False, indent=2))

COMMANDS = {
    "init": cmd_init, "buy": cmd_buy, "sell": cmd_sell,
    "status": cmd_status, "history": cmd_history,
    "update_regime": cmd_update_regime, "update_atr_stops": cmd_update_atr_stops,
    "snapshot": cmd_snapshot, "reset": cmd_reset, "rules": cmd_rules,
    "circuit_breaker_check": cmd_circuit_breaker_check,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"用法: simulate_trend.py <{'|'.join(COMMANDS.keys())}> [args...]")
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
