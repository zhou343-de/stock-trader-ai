#!/usr/bin/env python3
"""
盯盘全流程自动化 v2
整合：时间守卫、行情获取、回撤熔断、移动止盈、ATR止损、止盈检查、防踩踏
v2 新增：
  - 最低持仓天数（2天）：避免盘中噪音扫损
  - 收盘确认止损：盘中只记录潜在止损，尾盘统一执行
  - 最高持仓天数（20天）：未达TP1则强制平仓，释放资金
输出：NO_REPLY（无操作）或 JSON 报告（有操作/异常需AI介入）
用法: monitor.py [--mode morning|afternoon|closing]
"""
import json, sys, os, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "data"
ACCOUNT_FILE = DATA_DIR / "trend_account.json"

# === v2 新增配置 ===
MIN_HOLDING_DAYS = 2    # 最低持仓天数：买入后至少持有2天才检查止损
MAX_HOLDING_DAYS = 20   # 最高持仓天数：持有超过20天未达TP1则强制平仓

# === P0 告警邮件 ===
def send_alert_email(subject, body):
    """P0 级事件即时告警邮件"""
    try:
        r = subprocess.run(
            ["python3", str(SCRIPT_DIR / "send_email.py"), subject, "--stdin"],
            input=body.encode(), capture_output=True, timeout=20)
        if r.returncode == 0:
            print(json.dumps({"alert_email": "sent", "subject": subject}), file=sys.stderr)
        else:
            print(json.dumps({"alert_email": "failed", "subject": subject}), file=sys.stderr)
    except Exception as e:
        print(json.dumps({"alert_email": "error", "error": str(e)}), file=sys.stderr)

# === 导入兄弟脚本 ===
sys.path.insert(0, str(SCRIPT_DIR))

def load_account():
    if not ACCOUNT_FILE.exists():
        return None
    with open(ACCOUNT_FILE) as f:
        return json.load(f)

def save_account(acc):
    import fcntl
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = ACCOUNT_FILE.with_suffix(".tmp")
    lock_file = ACCOUNT_FILE.with_suffix(".lock")
    try:
        with open(lock_file, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            with open(tmp_file, "w") as f:
                json.dump(acc, f, ensure_ascii=False, indent=2)
            tmp_file.rename(ACCOUNT_FILE)
            fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass

def run_script(script, args=None):
    """运行兄弟脚本，返回 (returncode, stdout_json)"""
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

def run_simulate(args):
    """运行 simulate_trend.py 子命令"""
    return run_script("simulate_trend.py", args)

# === 辅助函数 ===

def calc_holding_days(buy_date_str):
    """计算持仓天数（自然日）"""
    try:
        buy_date = datetime.strptime(buy_date_str, "%Y-%m-%d").date()
        today = datetime.now(CST).date()
        return (today - buy_date).days
    except Exception:
        return 999  # 解析失败视为长期持仓

def check_limit_down(acc, prices):
    """跌停封板检测：持仓股票开盘直接一字跌停，无法卖出
    返回: (limit_down_stocks, details)
    """
    from simulate_trend import calc_price_limit
    limit_down_stocks = []
    for code, pos in acc["positions"].items():
        price_data = prices.get(code)
        if not price_data:
            continue
        if price_data.get("_suspended"):
            continue

        current_price = price_data.get("price", 0)
        pre_close = price_data.get("pre_close", 0)
        if pre_close <= 0 or current_price <= 0:
            continue

        # 计算跌停价
        name = pos.get("name", "")
        limit_up, limit_down, _ = calc_price_limit(code, name, pre_close)

        # 检测跌停封板：当前价 == 跌停价 且 卖一价为0（封死）
        sell1 = price_data.get("sell1", 0)
        if current_price <= limit_down and sell1 == 0:
            avg_price = pos.get("avg_price", 0)
            loss_pct = (current_price - avg_price) / avg_price * 100 if avg_price > 0 else 0
            limit_down_stocks.append({
                "code": code,
                "name": name,
                "price": current_price,
                "limit_down_price": limit_down,
                "loss_pct": round(loss_pct, 2),
                "shares": pos["shares"],
                "detail": f"跌停封板，无法卖出，需挂单排队",
            })
    return limit_down_stocks

# === 核心检查逻辑 ===

def check_time_guard():
    """时间守卫：非交易时段直接退出"""
    from time_guard import check
    tradeable, session, detail = check()
    return tradeable, session, detail

def fetch_prices(positions):
    """批量获取持仓行情（含停牌检测）"""
    if not positions:
        return {}
    codes = list(positions.keys())
    market_codes = []
    for c in codes:
        prefix = "sh" if c.startswith("6") else "sz"
        market_codes.append(f"{prefix}{c}")
    rc, data = run_script("batch_query.py", market_codes)
    if rc != 0 or isinstance(data, str):
        return {}
    result = {}
    suspended = []
    for k, v in data.items():
        pure_code = k.lstrip("shszSHSZ")
        if pure_code in codes:
            if v.get("volume", 0) == 0 and v.get("price", 0) == v.get("pre_close", 0) and v.get("price", 0) > 0:
                v["_suspended"] = True
                suspended.append(f"{pure_code} ({v.get('name', '')})")
            result[pure_code] = v
    if suspended:
        print(json.dumps({"alert": f"停牌股: {', '.join(suspended)}，已冻结风控"}, ensure_ascii=False), file=sys.stderr)
    return result

def fetch_index():
    """获取上证指数"""
    rc, data = run_script("get_price.py", ["sh000001"])
    if rc == 0 and isinstance(data, dict):
        return data
    return None

def check_circuit_breaker(acc):
    """回撤熔断检查（含智能解冻条件）"""
    from simulate_trend import check_drawdown_circuit_breaker, is_frozen, DRAWDOWN_THRESHOLD
    triggered, drawdown_pct = check_drawdown_circuit_breaker(acc)
    frozen = is_frozen(acc)

    if frozen:
        today = datetime.now(CST).strftime("%Y-%m-%d")
        frozen_until = acc.get("frozen_until", "")
        if frozen_until and today > frozen_until:
            regime = acc.get("market_regime", {}).get("current", "range")
            if regime == "bear":
                from simulate_trend import save_account
                new_frozen = (datetime.now(CST) + timedelta(days=1)).strftime("%Y-%m-%d")
                acc["frozen_until"] = new_frozen
                save_account(acc)
                return triggered, drawdown_pct, True
    return triggered, drawdown_pct, frozen

def check_trailing_stops(acc, prices, mode="normal"):
    """移动止盈检查（v2: 含最低持仓天数过滤。停牌股跳过）"""
    from simulate_trend import check_trailing_stop, TRAILING_STOP_TRIGGER, TRAILING_STOP_RATIO
    today = datetime.now(CST).strftime("%Y-%m-%d")
    actions = []
    skipped_short_hold = []
    updated = False
    for code, pos in list(acc["positions"].items()):
        price_data = prices.get(code)
        if not price_data:
            continue
        if price_data.get("_suspended"):
            continue
        current_price = price_data["price"]
        avg_price = pos.get("avg_price", 0)
        if avg_price <= 0:
            continue

        # v2: 最低持仓天数检查
        buy_date = pos.get("buy_date", pos.get("last_buy_date", ""))
        holding_days = calc_holding_days(buy_date)
        if holding_days < MIN_HOLDING_DAYS:
            skipped_short_hold.append(f"{code}({holding_days}天)")
            continue

        # T+1 意识：买入当日不更新峰值浮盈
        if buy_date == today:
            continue

        current_pct = (current_price - avg_price) / avg_price
        old_max = pos.get("max_profit_pct", pos.get("highest_profit_pct", 0))
        if current_pct > old_max:
            pos["max_profit_pct"] = round(current_pct, 4)
            pos["highest_profit_pct"] = round(current_pct, 4)
            updated = True
        triggered, detail = check_trailing_stop(pos, current_price)
        if triggered:
            actions.append({
                "action": "sell",
                "code": code,
                "name": pos["name"],
                "shares": pos["shares"],
                "price": current_price,
                "reason": "trailing_stop",
                "detail": detail,
            })
    if updated and not actions:
        save_account(acc)
    if skipped_short_hold:
        print(json.dumps({"info": f"移动止盈跳过短期持仓: {', '.join(skipped_short_hold)}"}, ensure_ascii=False), file=sys.stderr)
    return actions

# === 逻辑止损检查（来自辩论的 falsification 条件）===
def check_logic_stops(acc, prices):
    """检查辩论中设定的逻辑止损条件"""
    positions = acc.get("positions", {})
    if not positions:
        return []
    actions = []
    for code, pos in positions.items():
        logic_stop = pos.get("logic_stop_price")
        if logic_stop is None:
            continue
        price_info = prices.get(code, {})
        current_price = price_info.get("price", 0)
        if current_price <= 0:
            continue
        if current_price <= logic_stop:
            falsification = pos.get("falsification", "跌破逻辑止损位")
            shares = pos.get("shares", 0)
            if shares > 0:
                actions.append({
                    "type": "sell", "code": code,
                    "name": pos.get("name", code),
                    "shares": shares,
                    "price": current_price,
                    "reason": f"logic_stop: {falsification}",
                })
    return actions


def check_atr_stops(acc, prices, mode="normal"):
    """ATR 动态止损检查（v2: 含最低持仓天数 + 收盘确认）"""
    from simulate_trend import calc_dynamic_stop_loss
    params = acc["market_regime"]["params_active"]
    fixed_stop = params.get("stop_loss", -0.07)
    atr_multiplier = params.get("atr_multiplier", 1.5)
    actions = []
    pending_stops = []  # 盘中记录的潜在止损（收盘确认用）

    for code, pos in list(acc["positions"].items()):
        price_data = prices.get(code)
        if not price_data:
            continue
        if price_data.get("_suspended"):
            continue
        current_price = price_data["price"]
        avg_price = pos["avg_price"]
        if avg_price <= 0:
            continue
        profit_pct = (current_price - avg_price) / avg_price

        # v2: 最低持仓天数检查
        buy_date = pos.get("buy_date", pos.get("last_buy_date", ""))
        holding_days = calc_holding_days(buy_date)
        if holding_days < MIN_HOLDING_DAYS:
            continue

        atr_stop = pos.get("atr_stop_loss")
        if atr_stop is None:
            dynamic_stop, _ = calc_dynamic_stop_loss(code, avg_price, atr_multiplier)
            atr_stop = dynamic_stop

        effective_stop = fixed_stop
        if atr_stop:
            regime = acc.get("market_regime", {}).get("current", "range")
            if regime == "bull":
                effective_stop = max(fixed_stop, atr_stop)
            else:
                effective_stop = min(fixed_stop, atr_stop)

        if profit_pct <= effective_stop:
            if mode == "closing":
                # 收盘模式：直接执行
                actions.append({
                    "action": "sell",
                    "code": code,
                    "name": pos["name"],
                    "shares": pos["shares"],
                    "price": current_price,
                    "reason": "stop_loss",
                    "detail": f"浮亏 {profit_pct*100:.2f}% 触发止损线 {effective_stop*100:.2f}%",
                })
            else:
                # 盘中模式：记录但不执行（收盘确认）
                pending_stops.append({
                    "code": code,
                    "name": pos["name"],
                    "profit_pct": round(profit_pct * 100, 2),
                    "stop_line": round(effective_stop * 100, 2),
                    "current_price": current_price,
                })

    if pending_stops:
        print(json.dumps({
            "info": f"盘中潜在止损（待收盘确认）",
            "pending_stops": pending_stops,
        }, ensure_ascii=False), file=sys.stderr)

    return actions

def check_index_circuit_breaker(index_data):
    """指数跌幅熔断：上证跌幅 >= 3%"""
    if not index_data:
        return False, 0
    change_pct = index_data.get("change_pct", 0)
    return change_pct <= -3.0, change_pct

def check_stampede(acc, prices):
    """防踩踏：>= 50% 持仓今日跌幅 > 3%（停牌股不计入）"""
    if not acc["positions"]:
        return False, 0, 0
    total = 0
    dropping = 0
    for code, pos in acc["positions"].items():
        price_data = prices.get(code)
        if not price_data:
            continue
        if price_data.get("_suspended"):
            continue
        total += 1
        if price_data["change_pct"] < -3:
            dropping += 1
    ratio = dropping / total if total > 0 else 0
    return ratio >= 0.5, dropping, total

def check_take_profits(acc, prices):
    """止盈检查（分级减仓）"""
    params = acc["market_regime"]["params_active"]
    tp1, tp2, tp3 = params["tp1"], params["tp2"], params["tp3"]
    actions = []

    for code, pos in list(acc["positions"].items()):
        price_data = prices.get(code)
        if not price_data:
            continue
        if price_data.get("_suspended"):
            continue
        current_price = price_data["price"]
        avg_price = pos["avg_price"]
        if avg_price <= 0:
            continue
        profit_pct = (current_price - avg_price) / avg_price
        shares = pos["shares"]

        if profit_pct >= tp3:
            sell_shares = max(shares // 3, 100)
            actions.append({
                "action": "sell_partial",
                "code": code,
                "name": pos["name"],
                "shares": sell_shares,
                "price": current_price,
                "reason": "take_profit_3",
                "detail": f"浮盈 {profit_pct*100:.1f}% ≥ TP3 {tp3*100}%，减仓1/3",
            })
        elif profit_pct >= tp2:
            sell_shares = max(shares // 3, 100)
            actions.append({
                "action": "sell_partial",
                "code": code,
                "name": pos["name"],
                "shares": sell_shares,
                "price": current_price,
                "reason": "take_profit_2",
                "detail": f"浮盈 {profit_pct*100:.1f}% ≥ TP2 {tp2*100}%，减仓1/3",
            })
        elif profit_pct >= tp1:
            sell_shares = max(shares // 3, 100)
            actions.append({
                "action": "sell_partial",
                "code": code,
                "name": pos["name"],
                "shares": sell_shares,
                "price": current_price,
                "reason": "take_profit_1",
                "detail": f"浮盈 {profit_pct*100:.1f}% ≥ TP1 {tp1*100}%，减仓1/3",
            })
    return actions

def check_max_holding_period(acc, prices):
    """v2 新增：最高持仓天数检查
    持有超过 MAX_HOLDING_DAYS 且浮盈未达 TP1 → 强制平仓释放资金
    """
    params = acc["market_regime"]["params_active"]
    tp1 = params.get("tp1", 0.15)
    today = datetime.now(CST).strftime("%Y-%m-%d")
    actions = []

    for code, pos in list(acc["positions"].items()):
        price_data = prices.get(code)
        if not price_data:
            continue
        if price_data.get("_suspended"):
            continue

        buy_date = pos.get("buy_date", pos.get("last_buy_date", ""))
        holding_days = calc_holding_days(buy_date)

        # T+1 意识：买入当日不执行
        if buy_date == today:
            continue

        if holding_days >= MAX_HOLDING_DAYS:
            current_price = price_data["price"]
            avg_price = pos["avg_price"]
            profit_pct = (current_price - avg_price) / avg_price if avg_price > 0 else 0

            if profit_pct < tp1:
                actions.append({
                    "action": "sell",
                    "code": code,
                    "name": pos["name"],
                    "shares": pos["shares"],
                    "price": current_price,
                    "reason": "max_holding_period",
                    "detail": f"持仓 {holding_days} 天（上限 {MAX_HOLDING_DAYS} 天），浮盈 {profit_pct*100:.1f}% 未达 TP1 {tp1*100}%，强制平仓",
                })
    return actions

def execute_sell(code, shares, price, reason):
    """执行卖出"""
    rc, data = run_simulate(["sell", str(code), str(shares), str(price), "--reason", reason])
    return rc, data

# === 主流程 ===

def run_monitor(mode="normal"):
    """盯盘主流程，返回 (need_ai, output)
    v2 mode 含义：
      - normal: 盘中轻量检查（回撤熔断+指数熔断+防踩踏+止盈），止损仅记录不执行
      - morning/afternoon: 同 normal
      - closing: 尾盘完整检查（所有风控 + 最大持仓天数 + 止损执行）
    """
    now = datetime.now(CST)
    report = {
        "time": now.strftime("%H:%M:%S"),
        "mode": mode,
        "actions_taken": [],
        "alerts": [],
        "need_ai": False,
    }

    # 1. 时间守卫
    tradeable, session, detail = check_time_guard()
    if not tradeable:
        return False, "NO_REPLY"

    # 2. 加载账户
    acc = load_account()
    if not acc:
        return True, {"error": "账户未初始化"}

    # 2.5 同步市场环境（每次盯盘都更新，防止 regime 变化后参数不跟）
    try:
        rc_regime, regime_data = run_script("market_regime.py")
        if rc_regime == 0 and isinstance(regime_data, dict) and "regime" in regime_data:
            old_regime = acc.get("market_regime", {}).get("current", "unknown")
            new_regime = regime_data.get("regime", "unknown")
            if old_regime != new_regime:
                report["alerts"].append({
                    "type": "regime_change",
                    "from": old_regime,
                    "to": new_regime,
                    "confidence": regime_data.get("confidence", 0),
                })
            # 构造 update_regime 参数
            regime_update = {
                "current": new_regime,
                "regime": new_regime,
                "confidence": regime_data.get("confidence", 0),
                "params_active": regime_data.get("params_active", {}),
            }
            run_simulate(["update_regime", json.dumps(regime_update, ensure_ascii=False)])
            # 重新加载（update_regime 可能更新了文件）
            acc = load_account()
            if not acc:
                return True, {"error": "账户更新后丢失"}
    except Exception as e:
        print(json.dumps({"warn": f"regime同步失败: {str(e)}"}, ensure_ascii=False), file=sys.stderr)

    # 3. 获取行情
    prices = fetch_prices(acc["positions"])
    index_data = fetch_index()

    # 4. 回撤熔断（最优先，任何模式都执行）
    triggered, drawdown_pct, frozen = check_circuit_breaker(acc)
    if triggered:
        report["alerts"].append({
            "type": "drawdown_circuit_breaker",
            "drawdown_pct": f"{drawdown_pct*100:.2f}%",
            "action": "清仓所有非T+1持仓",
        })
        today = now.strftime("%Y-%m-%d")
        for code, pos in list(acc["positions"].items()):
            if pos.get("last_buy_date", "") != today:
                price_data = prices.get(code)
                sell_price = price_data["price"] if price_data else pos["avg_price"]
                rc, data = execute_sell(code, pos["shares"], sell_price, "drawdown_breaker")
                report["actions_taken"].append({
                    "action": "sell", "code": code, "name": pos["name"],
                    "reason": "drawdown_breaker", "result": data,
                })
        # P0 告警：回撤熔断即时邮件
        send_alert_email(
            f"[URGENT] 量化系统-回撤熔断已触发 ({drawdown_pct*100:.2f}%)",
            f"回撤熔断已触发！\n"
            f"回撤幅度：{drawdown_pct*100:.2f}%\n"
            f"操作：已清仓所有非T+1持仓\n"
            f"冻结期：3个交易日\n"
            f"时间：{now.strftime('%H:%M:%S')}"
        )
        report["need_ai"] = True
        return True, report

    if frozen:
        report["alerts"].append({
            "type": "frozen",
            "detail": f"交易冻结中（至 {acc.get('frozen_until', '')}），仅检查止损止盈",
        })

    # 5. 指数跌幅熔断
    index_break, index_change = check_index_circuit_breaker(index_data)
    if index_break:
        report["alerts"].append({
            "type": "index_circuit_breaker",
            "index_change": f"{index_change:.2f}%",
            "action": "暂停买入 + 收紧止损到 -3%",
        })
        # P1 告警：指数熔断即时邮件
        send_alert_email(
            f"[ALERT] 量化系统-指数熔断 ({index_change:.2f}%)",
            f"上证指数跌幅 {index_change:.2f}%，触发指数熔断\n"
            f"操作：暂停买入 + 收紧止损到 -3%\n"
            f"时间：{now.strftime('%H:%M:%S')}"
        )

    # 6. 防踩踏
    stampede, dropping, total = check_stampede(acc, prices)
    if stampede:
        report["alerts"].append({
            "type": "stampede",
            "detail": f"{dropping}/{total} 持仓跌幅>3%",
            "action": "暂停买入",
        })

    # 6.5 跌停封板检测（无法卖出，需挂单排队）
    limit_down_stocks = check_limit_down(acc, prices)
    if limit_down_stocks:
        for ld in limit_down_stocks:
            report["alerts"].append({
                "type": "limit_down",
                "code": ld["code"],
                "name": ld["name"],
                "price": ld["price"],
                "limit_down_price": ld["limit_down_price"],
                "loss_pct": f"{ld['loss_pct']}%",
                "action": "跌停封板，无法卖出，次日开盘优先处理",
            })
        # P0 告警：跌停封板即时邮件
        names = ", ".join(f"{ld['name']}({ld['code']}) 浮亏{ld['loss_pct']}%" for ld in limit_down_stocks)
        send_alert_email(
            f"[URGENT] 量化系统-持仓跌停封板 ({len(limit_down_stocks)}只)",
            f"持仓跌停封板，无法卖出！\n"
            f"标的：{names}\n"
            f"操作：挂单排队，次日开盘优先处理\n"
            f"时间：{now.strftime('%H:%M:%S')}"
        )
        report["need_ai"] = True

    # 7. 策略失效自检（20日滚动收益）
    snapshots = acc.get("daily_snapshots", [])
    if len(snapshots) >= 20:
        first = snapshots[-20]["total_assets"]
        last = snapshots[-1]["total_assets"]
        if first > 0:
            rolling_return = (last - first) / first
            if rolling_return <= -0.08:
                report["alerts"].append({
                    "type": "strategy_warning",
                    "rolling_20d_return": f"{rolling_return*100:.2f}%",
                    "detail": "近20日累计亏损≥8%，策略可能不适应当前市场",
                    "action": "建议人工复盘，考虑暂停自动交易",
                })
                report["need_ai"] = True

    # 8. 移动止盈（v2: 含最低持仓天数过滤）
    trailing_actions = check_trailing_stops(acc, prices, mode)
    for act in trailing_actions:
        rc, data = execute_sell(act["code"], act["shares"], act["price"], act["reason"])
        act["result"] = data
        report["actions_taken"].append(act)
    # P0 告警：移动止盈触发即时邮件
    if trailing_actions:
        names = ", ".join(f"{a['name']}({a['code']})" for a in trailing_actions)
        send_alert_email(
            f"[URGENT] 量化系统-移动止盈触发 ({len(trailing_actions)}只)",
            f"移动止盈已触发！\n"
            f"卖出：{names}\n"
            f"原因：浮盈从峰值回撤超过50%\n"
            f"时间：{now.strftime('%H:%M:%S')}"
        )

    # 9. ATR 止损（v2: 盘中仅记录，收盘确认执行）
    atr_actions = check_atr_stops(acc, prices, mode)
    for act in atr_actions:
        rc, data = execute_sell(act["code"], act["shares"], act["price"], act["reason"])
        act["result"] = data
        report["actions_taken"].append(act)
    # P1 告警：ATR止损触发即时邮件
    if atr_actions:
        names = ", ".join(f"{a['name']}({a['code']})" for a in atr_actions)
        send_alert_email(
            f"[ALERT] 量化系统-ATR止损触发 ({len(atr_actions)}只)",
            f"ATR动态止损已触发！\n"
            f"卖出：{names}\n"
            f"时间：{now.strftime('%H:%M:%S')}"
        )

    # 9.5 逻辑止损（来自辩论的 falsification 条件）
    logic_stop_actions = check_logic_stops(acc, prices)
    for act in logic_stop_actions:
        rc, data = execute_sell(act["code"], act["shares"], act["price"], act["reason"])
        act["result"] = data
        report["actions_taken"].append(act)
    if logic_stop_actions:
        names = ", ".join(f"{a['name']}({a['code']})" for a in logic_stop_actions)
        send_alert_email(
            f"[ALERT] 量化系统-逻辑止损触发 ({len(logic_stop_actions)}只)",
            f"辩论逻辑止损已触发！\n"
            f"卖出：{names}\n"
            f"时间：{now.strftime('%H:%M:%S')}"
        )

    # 10. 止盈（分级减仓，任何模式都执行）
    if not frozen:
        tp_actions = check_take_profits(acc, prices)
        for act in tp_actions:
            rc, data = execute_sell(act["code"], act["shares"], act["price"], act["reason"])
            act["result"] = data
            report["actions_taken"].append(act)

    # 11. 最高持仓天数（仅收盘模式执行）
    if mode == "closing":
        max_hold_actions = check_max_holding_period(acc, prices)
        for act in max_hold_actions:
            rc, data = execute_sell(act["code"], act["shares"], act["price"], act["reason"])
            act["result"] = data
            report["actions_taken"].append(act)

    # 12. 判断是否需要 AI 介入
    has_actions = len(report["actions_taken"]) > 0
    has_alerts = len(report["alerts"]) > 0

    if has_actions or has_alerts:
        report["need_ai"] = True
        report["market_summary"] = {
            "index": index_data,
            "positions": {
                code: {
                    "name": prices[code]["name"],
                    "price": prices[code]["price"],
                    "change_pct": prices[code]["change_pct"],
                    "holding_days": calc_holding_days(pos.get("buy_date", "")),
                    "profit_pct": round((prices[code]["price"] - pos["avg_price"]) / pos["avg_price"] * 100, 2) if pos["avg_price"] > 0 else 0,
                }
                for code, pos in acc["positions"].items()
                if code in prices
            },
            "total_assets": sum(p["shares"] * prices.get(code, {}).get("price", p["avg_price"]) for code, p in acc["positions"].items()) + acc["current_cash"],
        }
        return True, report

    return False, "NO_REPLY"


if __name__ == "__main__":
    mode = "normal"
    if "--mode" in sys.argv:
        idx = sys.argv.index("--mode")
        if idx + 1 in range(len(sys.argv)):
            mode = sys.argv[idx + 1]

    need_ai, result = run_monitor(mode)

    if result == "NO_REPLY":
        print("NO_REPLY")
        sys.exit(0)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if not need_ai else 2)
