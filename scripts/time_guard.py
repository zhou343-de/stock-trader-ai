#!/usr/bin/env python3
"""
时间守卫 + 交易时段检查 + 交易日历
完整实现A股交易时间表（集合竞价、连续竞价、午休、收盘集合竞价）
"""
import sys, json
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
CALENDAR_FILE = Path(__file__).parent.parent / "data" / "trading_calendar.json"

def load_calendar():
    """加载交易日历"""
    if not CALENDAR_FILE.exists():
        return None
    with open(CALENDAR_FILE) as f:
        return json.load(f)

def is_holiday(now):
    """检查今天是否为节假日"""
    cal = load_calendar()
    if not cal:
        return False, None
    today = now.strftime("%Y-%m-%d")
    for h in cal.get("holidays", []):
        if h["date"] == today:
            return True, h["name"]
    return False, None

def is_extra_trading_day(now):
    """检查今天是否为调休交易日"""
    cal = load_calendar()
    if not cal:
        return False
    today = now.strftime("%Y-%m-%d")
    for d in cal.get("extra_trading_days", []):
        if d["date"] == today:
            return True
    return False

# A股交易时间表
# 09:15-09:25  开盘集合竞价（09:20后不可撤单）
# 09:25-09:30  静默期
# 09:30-11:30  上午连续竞价
# 11:30-13:00  午间休市（可挂单）
# 13:00-14:57  下午连续竞价
# 14:57-15:00  收盘集合竞价
# 15:05-15:30  盘后定价（科创板/创业板）

TRADING_WINDOWS = {
    "pre_market":     (9*60+15, 9*60+25),   # 开盘集合竞价
    "silent":         (9*60+25, 9*60+30),   # 静默期
    "morning":        (9*60+30, 11*60+30),  # 上午连续竞价
    "lunch":          (11*60+30, 13*60),    # 午间休市
    "afternoon":      (13*60, 14*60+57),    # 下午连续竞价
    "close_auction":  (14*60+57, 15*60),    # 收盘集合竞价
    "after_hours":    (15*60+5, 15*60+30),  # 盘后定价
}

def get_session(t):
    """判断当前属于哪个交易时段"""
    for name, (start, end) in TRADING_WINDOWS.items():
        if start <= t < end:
            return name
    return "closed"

def is_trading_day(now):
    """判断是否为交易日（排除周末+节假日，包含调休交易日）"""
    # 调休交易日（周末但正常开市）
    if is_extra_trading_day(now):
        return True
    # 周末
    if now.weekday() >= 5:
        return False
    # 节假日
    holiday, _ = is_holiday(now)
    if holiday:
        return False
    return True

def check():
    """返回 (is_trading, session_name, detail)"""
    now = datetime.now(CST)
    t = now.hour * 60 + now.minute

    # 节假日检查
    holiday, holiday_name = is_holiday(now)
    if holiday:
        return False, "holiday", f"节假日休市（{holiday_name}）"

    # 周末检查（调休交易日除外）
    if now.weekday() >= 5:
        if is_extra_trading_day(now):
            pass  # 调休交易日，继续检查时段
        else:
            return False, "weekend", "周末休市"

    session = get_session(t)

    if session == "closed":
        if t < 9*60+15:
            return False, "pre_open", "尚未开盘"
        else:
            return False, "closed", "已收盘"

    # 可交易时段
    tradeable = session in ("morning", "afternoon", "close_auction")
    return tradeable, session, {
        "pre_market": "开盘集合竞价（09:20后不可撤单）",
        "silent": "静默期（不可申报）",
        "morning": "上午连续竞价",
        "lunch": "午间休市（可挂单）",
        "afternoon": "下午连续竞价",
        "close_auction": "收盘集合竞价（不可撤单）",
        "after_hours": "盘后定价交易",
    }.get(session, session)

if __name__ == "__main__":
    tradeable, session, detail = check()
    now = datetime.now(CST)
    result = {
        "tradeable": tradeable,
        "session": session,
        "detail": detail,
        "time": now.strftime("%H:%M:%S"),
        "weekday": ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()],
    }
    if tradeable:
        print("OK")
    else:
        print("NO_REPLY")
    # 始终输出详情到 stderr 供调试
    print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
