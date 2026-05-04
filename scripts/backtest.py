#!/usr/bin/env python3
"""
量化回测引擎 v1
基于腾讯日K线数据，模拟 stock_screener + factor_scorer + simulate_trend 全流程
输入：起始日期、初始资金、策略参数
输出：累计收益率、最大回撤、夏普比率、胜率、盈亏比、每笔交易明细
用法: backtest.py --start 2026-01-05 --end 2026-04-25 --cash 100000
"""
import json, sys, subprocess, math, time, argparse
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).parent

# === 数据获取 ===

def get_index_kline(start_date, end_date, code="sh000001", max_retries=3):
    """获取指数历史日K线（腾讯接口）"""
    # 腾讯接口不支持日期范围，获取足够的日线数据
    days = max((datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days + 30, 120)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
    for attempt in range(max_retries):
        try:
            r = subprocess.run(["curl", "-s", "--connect-timeout", "10", url],
                               capture_output=True, timeout=15)
            data = json.loads(r.stdout.decode("utf-8"))
            klines = data.get("data", {}).get(code, {})
            rows = klines.get("qfqday") or klines.get("day", [])
            if rows:
                # 过滤日期范围
                filtered = []
                for row in rows:
                    d = row[0]
                    if start_date <= d <= end_date:
                        filtered.append(row)
                return filtered
        except Exception:
            pass
        if attempt < max_retries - 1:
            time.sleep(1 * (attempt + 1))
    return []


def get_stock_kline(code, days=120, max_retries=3):
    """获取个股历史日K线"""
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
            time.sleep(0.5 * (attempt + 1))
    return []


def get_all_stocks_snapshot(date_str):
    """获取指定日期全市场股票快照（模拟：用当日实时数据作为近似）
    实际回测中，我们用K线数据倒推历史行情
    """
    # 回测模式下不需要这个，直接用K线数据
    return []


# === 模拟市场环境分类（简化版）===

def classify_regime_from_klines(index_rows, lookback=60):
    """从指数K线数据判断市场环境（简化版）"""
    if len(index_rows) < lookback:
        return "range", 50

    closes = [float(r[2]) for r in index_rows[-lookback:]]
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60

    # 20日动量
    change_20d = (closes[-1] - closes[-20]) / closes[-20] * 100

    bull_score = 0
    bear_score = 0

    if ma20 > ma60 * 1.02:
        bull_score += 2
    if change_20d > 5:
        bull_score += 2
    elif change_20d > 3:
        bull_score += 1
    if change_20d < -5:
        bear_score += 2
    elif change_20d < -3:
        bear_score += 1

    # MA5 > MA10 > MA20
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    if ma5 > ma10 > ma20:
        bull_score += 1
    if ma5 < ma10 < ma20:
        bear_score += 1

    # RSI
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(-14, 0):
            diff = closes[i] - closes[i-1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        else:
            rsi = 100
        if rsi > 55:
            bull_score += 1
        if rsi < 40:
            bear_score += 1

    if bull_score >= 4 and bull_score > bear_score:
        return "bull", min(bull_score / 8 * 100, 100)
    elif bear_score >= 4 and bear_score > bull_score:
        return "bear", min(bear_score / 8 * 100, 100)
    else:
        return "range", max(0, 100 - abs(bull_score - bear_score) * 15)


REGIME_PARAMS = {
    "bull":  {"stop_loss": -0.08, "tp1": 0.15, "tp2": 0.25, "tp3": 0.35, "max_position": 0.90, "max_holdings": 4},
    "range": {"stop_loss": -0.07, "tp1": 0.15, "tp2": 0.25, "tp3": 0.35, "max_position": 0.85, "max_holdings": 3},
    "bear":  {"stop_loss": -0.04, "tp1": 0.10, "tp2": 0.18, "tp3": 0.25, "max_position": 0.60, "max_holdings": 2},
}


# === 简化选股（基于K线数据）===

def screen_stocks_from_klines(date_str, all_stock_klines, holdings_count, max_holdings, sold_recently=None):
    """从历史K线数据中筛选候选股（简化版选股漏斗）
    改进：放宽技术验证、排除负动量、增加 5 日动量评分、冷却期
    """
    if sold_recently is None:
        sold_recently = set()
    candidates = []

    for code, rows in all_stock_klines.items():
        # 冷却期：近 5 个交易日内卖出过的股票不选
        if code in sold_recently:
            continue

        # 找到 date_str 及之前的数据
        available = [r for r in rows if r[0] <= date_str]
        if len(available) < 25:
            continue

        today = available[-1]
        prev = available[-2] if len(available) >= 2 else today

        price = float(today[2])
        pre_close = float(prev[2])
        if pre_close <= 0 or price <= 0:
            continue

        change_pct = (price - pre_close) / pre_close * 100

        # 涨幅窗口 0.5-9.5%（放宽下限）
        if change_pct < 0.5 or change_pct > 9.5:
            continue

        # 价格 2-25 元（放宽范围）
        if price < 2.0 or price > 25.0:
            continue

        # 成交额估算
        volume = float(today[5]) if len(today) > 5 else 0
        amount_est = volume * price
        if amount_est < 20000000:  # 2000万（放宽）
            continue

        # 技术验证（基于 date_str 当日及之前的数据）
        closes = [float(r[2]) for r in available[-20:]]
        if len(closes) < 20:
            continue

        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20

        tech_pass = 0
        if ma5 > ma10:
            tech_pass += 1
        if ma10 > ma20:
            tech_pass += 1
        if len(available) >= 26:
            ema12 = sum(closes[-12:]) / 12
            ema26 = sum(closes[-26:]) / 26 if len(closes) >= 26 else closes[-1]
            if ema12 > ema26:
                tech_pass += 1
        volumes = [float(r[5]) for r in available[-20:] if len(r) > 5]
        if len(volumes) >= 20:
            avg5 = sum(volumes[-5:]) / 5
            avg20 = sum(volumes[-20:]) / 20
            if avg20 > 0 and avg5 / avg20 > 1.2:
                tech_pass += 1

        # 至少满足 1 项技术条件（放宽，原为 2）
        if tech_pass < 1:
            continue

        # 计算动量评分（多周期，动态计算）
        momentum_20d = (closes[-1] - closes[-20]) / closes[-20] * 100
        momentum_5d = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] > 0 else 0

        # 排除负动量（20日动量必须为正）
        if momentum_20d <= 0:
            continue

        # 排除数据过期的股票（最新数据距今 > 7 天）
        latest_date = available[-1][0]
        try:
            ld = datetime.strptime(latest_date, "%Y-%m-%d")
            cd = datetime.strptime(date_str, "%Y-%m-%d")
            if (cd - ld).days > 7:
                continue
        except Exception:
            continue

        # 排除低波动股票（日均振幅 < 1%）
        if len(available) >= 10:
            recent_10 = available[-10:]
            avg_amplitude = sum((float(r[3]) - float(r[4])) / float(r[2]) for r in recent_10 if float(r[2]) > 0) / len(recent_10)
            if avg_amplitude < 0.01:
                continue

        candidates.append({
            "code": code,
            "price": price,
            "change_pct": round(change_pct, 2),
            "momentum_20d": round(momentum_20d, 2),
            "momentum_5d": round(momentum_5d, 2),
            "tech_pass": tech_pass,
        })

    # 综合评分排序（20日动量为主 + 5日动量 + 技术面）
    candidates.sort(key=lambda x: x["momentum_20d"] + x["momentum_5d"] * 0.5 + x["tech_pass"] * 3, reverse=True)
    return candidates[:max_holdings - holdings_count]


# === 回测引擎 ===

class BacktestEngine:
    def __init__(self, initial_cash=100000.0):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions = {}  # code -> {name, shares, avg_price, buy_date, max_profit_pct}
        self.trade_history = []
        self.daily_snapshots = []
        self.peak_assets = initial_cash
        self.sold_recently = {}  # code -> date_sold (冷却期，5日内不重复买)
        self.stats = {
            "total_trades": 0, "total_buys": 0, "total_sells": 0,
            "win_trades": 0, "lose_trades": 0,
            "stop_loss_count": 0, "trailing_stop_count": 0,
            "total_fees": 0.0, "total_profit": 0.0,
        }

    def calc_fees(self, amount, is_sell=False):
        commission = max(amount * 0.0003, 5.0)
        transfer = amount * 0.00001
        stamp = amount * 0.0005 if is_sell else 0
        return round(commission + transfer + stamp, 2)

    def buy(self, code, name, price, date_str, reason=""):
        params = REGIME_PARAMS.get(self.current_regime, REGIME_PARAMS["range"])
        if len(self.positions) >= params["max_holdings"]:
            return False

        # 资金分配（均分，简化）
        max_pos = params["max_position"]
        total_assets = self.calc_total_assets({})
        available = total_assets * max_pos / params["max_holdings"]
        available = min(available, self.cash * 0.95)

        shares = int(available / price / 100) * 100
        if shares < 100:
            return False

        gross = shares * price
        fees = self.calc_fees(gross)
        total = gross + fees

        if total > self.cash:
            return False

        self.cash -= total
        self.positions[code] = {
            "name": name, "shares": shares, "avg_price": price,
            "buy_date": date_str, "max_profit_pct": 0,
        }
        self.stats["total_trades"] += 1
        self.stats["total_buys"] += 1
        self.stats["total_fees"] += fees
        self.trade_history.append({
            "type": "buy", "code": code, "name": name,
            "shares": shares, "price": price, "fees": fees,
            "date": date_str, "reason": reason,
        })
        return True

    def sell(self, code, price, date_str, reason=""):
        if code not in self.positions:
            return False
        pos = self.positions[code]
        shares = pos["shares"]
        gross = shares * price
        fees = self.calc_fees(gross, is_sell=True)
        net = gross - fees
        cost = shares * pos["avg_price"]
        profit = net - cost

        self.cash += net
        self.stats["total_trades"] += 1
        self.stats["total_sells"] += 1
        self.stats["total_fees"] += fees
        self.stats["total_profit"] += profit
        if profit > 0:
            self.stats["win_trades"] += 1
        else:
            self.stats["lose_trades"] += 1
        if reason == "stop_loss":
            self.stats["stop_loss_count"] += 1
        elif reason == "trailing_stop":
            self.stats["trailing_stop_count"] += 1

        self.trade_history.append({
            "type": "sell", "code": code, "name": pos["name"],
            "shares": shares, "price": price, "fees": fees,
            "profit": round(profit, 2), "reason": reason,
            "date": date_str, "full_exit": True,
        })
        # 冷却期：记录卖出日期，5日内不重复买
        self.sold_recently[code] = date_str
        del self.positions[code]
        return True

    def calc_total_assets(self, current_prices):
        total = self.cash
        for code, pos in self.positions.items():
            p = current_prices.get(code, pos["avg_price"])
            total += pos["shares"] * p
        return round(total, 2)

    def check_stops(self, current_prices, date_str, regime_params):
        """检查止损/止盈/移动止盈"""
        actions = []
        for code, pos in list(self.positions.items()):
            price = current_prices.get(code)
            if price is None:
                continue

            # T+1：买入当日不卖出
            if pos["buy_date"] == date_str:
                continue

            avg = pos["avg_price"]
            profit_pct = (price - avg) / avg

            # 更新最高浮盈
            if profit_pct > pos["max_profit_pct"]:
                pos["max_profit_pct"] = profit_pct

            # 止损
            stop_loss = regime_params.get("stop_loss", -0.07)
            if profit_pct <= stop_loss:
                actions.append((code, price, "stop_loss"))
                continue

            # 移动止盈
            max_pct = pos["max_profit_pct"]
            if max_pct >= 0.10:
                trailing_level = max_pct * 0.50
                if profit_pct <= trailing_level:
                    actions.append((code, price, "trailing_stop"))
                    continue

            # 分级止盈
            tp3 = regime_params.get("tp3", 0.35)
            tp2 = regime_params.get("tp2", 0.25)
            tp1 = regime_params.get("tp1", 0.15)
            if profit_pct >= tp3:
                actions.append((code, price, "take_profit"))
                continue
            if profit_pct >= tp2:
                actions.append((code, price, "take_profit"))
                continue

            # 持仓天数 > 10 且未达 TP1
            try:
                bd = datetime.strptime(pos["buy_date"], "%Y-%m-%d")
                cd = datetime.strptime(date_str, "%Y-%m-%d")
                hold_days = (cd - bd).days
                if hold_days >= 10 and profit_pct < tp1:
                    actions.append((code, price, "max_holding"))
            except Exception:
                pass

        return actions

    def run(self, start_date, end_date, stock_codes=None, index_data=None, stock_klines=None):
        """运行回测"""
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"  回测启动: {start_date} → {end_date}", file=sys.stderr)
        print(f"  初始资金: ¥{self.initial_cash:,.2f}", file=sys.stderr)
        print(f"{'='*60}\n", file=sys.stderr)

        self.current_regime = "range"

        # 获取指数K线（用于市场环境判断）
        if index_data is None:
            index_data = get_index_kline(start_date, end_date)

        if not index_data:
            print("错误: 无法获取指数K线数据", file=sys.stderr)
            return None

        # 构建日期→指数数据映射
        index_by_date = {r[0]: r for r in index_data}

        # 获取所有候选股的K线
        if stock_klines is None:
            stock_klines = {}
            codes = stock_codes or self._get_default_stock_pool()
            print(f"获取 {len(codes)} 只股票的K线数据...", file=sys.stderr)
            for i, code in enumerate(codes):
                rows = get_stock_kline(code, 250)
                if rows:
                    stock_klines[code] = rows
                if (i + 1) % 10 == 0:
                    time.sleep(0.3)
                    print(f"  已获取 {i+1}/{len(codes)}", file=sys.stderr)

        # 生成交易日序列
        trading_dates = sorted([r[0] for r in index_data if start_date <= r[0] <= end_date])

        # 阶段性市场环境更新（每5天重新判断一次）
        regime_update_interval = 5
        day_count = 0

        for date_str in trading_dates:
            day_count += 1

            # 获取当日指数数据
            idx_row = index_by_date.get(date_str)
            if not idx_row:
                continue

            # 定期更新市场环境
            if day_count % regime_update_interval == 1 or day_count == 1:
                idx_available = [r for r in index_data if r[0] <= date_str]
                regime, confidence = classify_regime_from_klines(idx_available)
                self.current_regime = regime

            regime_params = REGIME_PARAMS.get(self.current_regime, REGIME_PARAMS["range"])

            # 获取当日持仓价格
            current_prices = {}
            for code in self.positions:
                rows = stock_klines.get(code, [])
                available = [r for r in rows if r[0] <= date_str]
                if available:
                    current_prices[code] = float(available[-1][2])

            # 1. 检查止损/止盈
            stop_actions = self.check_stops(current_prices, date_str, regime_params)
            for code, price, reason in stop_actions:
                self.sell(code, price, date_str, reason)

            # 2. 如果空仓，选股建仓
            if not self.positions:
                # 清理过期的冷却记录（5日）
                expired = [c for c, d in self.sold_recently.items()
                          if (datetime.strptime(date_str, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days > 5]
                for c in expired:
                    del self.sold_recently[c]

                candidates = screen_stocks_from_klines(
                    date_str, stock_klines, 0, regime_params["max_holdings"],
                    sold_recently=set(self.sold_recently.keys()))
                bought = 0
                for cand in candidates:
                    if bought >= regime_params["max_holdings"]:
                        break
                    name = cand["code"]
                    success = self.buy(cand["code"], name, cand["price"], date_str,
                                       f"动量20d={cand['momentum_20d']:.1f}% 5d={cand['momentum_5d']:.1f}% 技术{cand['tech_pass']}项")
                    if success:
                        bought += 1

            # 3. 记录每日快照
            total_assets = self.calc_total_assets(current_prices)
            if total_assets > self.peak_assets:
                self.peak_assets = total_assets

            self.daily_snapshots.append({
                "date": date_str,
                "total_assets": total_assets,
                "cash": self.cash,
                "positions": len(self.positions),
                "regime": self.current_regime,
            })

        # 回测结束：清仓
        last_date = trading_dates[-1] if trading_dates else end_date
        for code in list(self.positions):
            rows = stock_klines.get(code, [])
            available = [r for r in rows if r[0] <= last_date]
            if available:
                price = float(available[-1][2])
                self.sell(code, price, last_date, "backtest_end")

        return self._generate_report(start_date, end_date)

    def _get_default_stock_pool(self):
        """默认股票池：覆盖多行业多市值（约80只）"""
        return [
            # 大盘蓝筹
            "600519", "000858", "601318", "600036", "000333",
            "000651", "600900", "601888", "600276", "300760",
            "000568", "601012", "300274", "600031", "000157",
            # 科技成长
            "002475", "002410", "600588", "300496", "002236",
            "300750", "002594", "300124", "002008", "601100",
            "300586", "300033", "002371", "603986", "002049",
            # 军工
            "600893", "601989", "000768", "600760", "002179",
            # 化工材料
            "600309", "002601", "600989", "000792", "002460",
            # 银行金融
            "601398", "601939", "601166", "000001", "600019",
            # 钢铁有色
            "601899", "603993", "000709", "002466",
            # 房地产
            "001979", "600048", "000002", "600383",
            # 汽车
            "600104", "000625", "601238", "002920", "600741",
            # 中小盘活跃股（补充）
            "002049", "002180", "002241", "002261", "002340",
            "002371", "002384", "002432", "002460", "002497",
            "300014", "300033", "300059", "300122", "300136",
            "300142", "300274", "300316", "300347", "300408",
            "300433", "300496", "300529", "300568", "300586",
            "300628", "300661", "300676", "300750", "300760",
        ]

    def _generate_report(self, start_date, end_date):
        """生成回测报告"""
        if not self.daily_snapshots:
            return {"error": "无快照数据"}

        assets = [s["total_assets"] for s in self.daily_snapshots]
        initial = self.initial_cash
        final = assets[-1]
        total_return = (final - initial) / initial * 100

        # 最大回撤
        peak = assets[0]
        max_dd = 0
        dd_peak_date = self.daily_snapshots[0]["date"]
        dd_trough_date = self.daily_snapshots[0]["date"]
        for i, a in enumerate(assets):
            if a > peak:
                peak = a
                dd_peak_date = self.daily_snapshots[i]["date"]
            dd = (peak - a) / peak
            if dd > max_dd:
                max_dd = dd
                dd_trough_date = self.daily_snapshots[i]["date"]

        # 夏普比率
        returns = []
        for i in range(1, len(assets)):
            if assets[i-1] > 0:
                returns.append((assets[i] - assets[i-1]) / assets[i-1])
        sharpe = None
        if len(returns) >= 3:
            mean_r = sum(returns) / len(returns)
            std_r = math.sqrt(sum((r - mean_r)**2 for r in returns) / len(returns))
            if std_r > 0:
                sharpe = round((mean_r - 0.03/252) / std_r * math.sqrt(252), 2)

        # 胜率/盈亏比
        sell_trades = [t for t in self.trade_history if t["type"] == "sell"]
        wins = [t for t in sell_trades if t.get("profit", 0) > 0]
        losses = [t for t in sell_trades if t.get("profit", 0) <= 0]
        win_rate = len(wins) / len(sell_trades) * 100 if sell_trades else 0
        avg_win = sum(t["profit"] for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t["profit"] for t in losses) / len(losses)) if losses else 0
        pl_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

        # 指数基准收益
        benchmark_return = None
        if self.daily_snapshots:
            first_regime = self.daily_snapshots[0].get("regime", "range")
            # 简化：用首末日指数涨幅做基准
            idx_first = None
            idx_last = None
            for s in self.daily_snapshots:
                if idx_first is None:
                    idx_first = s["total_assets"]
                idx_last = s["total_assets"]
            if idx_first and idx_last:
                benchmark_return = round((idx_last - idx_first) / idx_first * 100, 2)

        # 平均持仓天数
        hold_days_list = []
        buy_dates = {}
        for t in self.trade_history:
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
        avg_hold = sum(hold_days_list) / len(hold_days_list) if hold_days_list else 0

        report = {
            "backtest_period": f"{start_date} → {end_date}",
            "trading_days": len(self.daily_snapshots),
            "initial_cash": initial,
            "final_assets": round(final, 2),
            "total_return_pct": round(total_return, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "max_dd_period": f"{dd_peak_date} → {dd_trough_date}",
            "sharpe_ratio": sharpe,
            "total_trades": self.stats["total_trades"],
            "buy_trades": self.stats["total_buys"],
            "sell_trades": self.stats["total_sells"],
            "win_rate_pct": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_loss_ratio": round(pl_ratio, 2) if pl_ratio != float("inf") else "∞",
            "avg_holding_days": round(avg_hold, 1),
            "total_fees": round(self.stats["total_fees"], 2),
            "realized_profit": round(self.stats["total_profit"], 2),
            "stop_loss_count": self.stats["stop_loss_count"],
            "trailing_stop_count": self.stats["trailing_stop_count"],
        }

        # 输出报告
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"  回测结果", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        print(f"  期间: {start_date} → {end_date} ({len(self.daily_snapshots)} 个交易日)", file=sys.stderr)
        print(f"  初始资金: ¥{initial:,.2f}", file=sys.stderr)
        print(f"  最终资产: ¥{final:,.2f}", file=sys.stderr)
        print(f"  累计收益: {total_return:+.2f}%", file=sys.stderr)
        print(f"  最大回撤: {max_dd*100:.2f}% ({dd_peak_date} → {dd_trough_date})", file=sys.stderr)
        print(f"  夏普比率: {sharpe}", file=sys.stderr)
        print(f"  胜率: {win_rate:.1f}% ({len(wins)}胜/{len(losses)}负)", file=sys.stderr)
        print(f"  盈亏比: {report['profit_loss_ratio']}", file=sys.stderr)
        print(f"  平均持仓: {avg_hold:.1f}天", file=sys.stderr)
        print(f"  累计费用: ¥{self.stats['total_fees']:,.2f}", file=sys.stderr)
        print(f"{'='*60}\n", file=sys.stderr)

        # 输出每笔交易
        print(f"\n📋 交易明细:", file=sys.stderr)
        for t in self.trade_history:
            if t["type"] == "buy":
                print(f"  🟢 {t['date']} 买入 {t['code']} {t['shares']}股 @ {t['price']:.2f} ({t.get('reason', '')})", file=sys.stderr)
            else:
                emoji = "🔴" if t.get("profit", 0) < 0 else "🟢"
                print(f"  {emoji} {t['date']} 卖出 {t['code']} {t['shares']}股 @ {t['price']:.2f} 盈亏:{t.get('profit', 0):+,.2f} ({t.get('reason', '')})", file=sys.stderr)

        return report


def main():
    parser = argparse.ArgumentParser(description="量化回测引擎")
    parser.add_argument("--start", type=str, default="2026-01-05", help="起始日期")
    parser.add_argument("--end", type=str, default="2026-04-25", help="结束日期")
    parser.add_argument("--cash", type=float, default=100000, help="初始资金")
    parser.add_argument("--stocks", type=str, help="股票代码列表（逗号分隔）")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    args = parser.parse_args()

    stock_codes = None
    if args.stocks:
        stock_codes = [c.strip() for c in args.stocks.split(",")]

    engine = BacktestEngine(initial_cash=args.cash)
    report = engine.run(args.start, args.end, stock_codes=stock_codes)

    if report:
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            # 已经输出到 stderr，stdout 只输出 JSON
            print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"error": "回测失败"}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
