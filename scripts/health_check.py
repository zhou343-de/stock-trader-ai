#!/usr/bin/env python3
"""
交易系统健康检查
每日 16:00 运行，验证日报是否生成、系统是否正常
输出：OK 或告警详情
用法: health_check.py [--send]
"""
import json, sys, subprocess, os
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "data"
ACCOUNT_FILE = DATA_DIR / "trend_account.json"

def check_report_exists():
    """检查今日日报是否生成"""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    report_file = DATA_DIR / f"trend_report_{today}.md"
    return report_file.exists(), str(report_file)

def check_account_integrity():
    """检查账户文件完整性"""
    if not ACCOUNT_FILE.exists():
        return False, "账户文件不存在"
    try:
        with open(ACCOUNT_FILE) as f:
            acc = json.load(f)
        required = ["version", "initial_cash", "current_cash", "positions", "market_regime"]
        for key in required:
            if key not in acc:
                return False, f"缺少字段: {key}"
        # 检查资产是否异常
        total = acc["current_cash"]
        for code, pos in acc["positions"].items():
            total += pos["shares"] * pos.get("avg_price", 0)
        if total < acc["initial_cash"] * 0.5:
            return False, f"总资产异常低: ¥{total:,.2f}（初始 ¥{acc['initial_cash']:,.2f}）"
        return True, "OK"
    except Exception as e:
        return False, f"文件损坏: {str(e)}"

def check_last_snapshot():
    """检查最后快照是否是今天"""
    if not ACCOUNT_FILE.exists():
        return False, "无账户文件"
    try:
        with open(ACCOUNT_FILE) as f:
            acc = json.load(f)
        snapshots = acc.get("daily_snapshots", [])
        if not snapshots:
            return False, "无快照数据"
        last = snapshots[-1]["date"]
        today = datetime.now(CST).strftime("%Y-%m-%d")
        if last != today:
            return False, f"最后快照: {last}（非今日）"
        return True, last
    except Exception:
        return False, "快照读取失败"

def check_cron_jobs():
    """检查 Cron 任务是否正常运行（通过最后运行时间）"""
    # 这个需要外部调用，这里只做文件级检查
    return True, "需外部验证"

def send_alert(subject, body):
    """发送告警邮件"""
    try:
        r = subprocess.run(
            ["python3", str(SCRIPT_DIR / "send_email.py"), subject, "--stdin"],
            input=body.encode(), capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False

def run_health_check(send_email=False):
    """运行所有健康检查"""
    now = datetime.now(CST)
    checks = []
    alerts = []

    # 1. 日报检查
    report_ok, report_detail = check_report_exists()
    checks.append({"name": "日报生成", "ok": report_ok, "detail": report_detail})
    if not report_ok:
        alerts.append(f"❌ 今日日报未生成: {report_detail}")

    # 2. 账户完整性
    account_ok, account_detail = check_account_integrity()
    checks.append({"name": "账户完整性", "ok": account_ok, "detail": account_detail})
    if not account_ok:
        alerts.append(f"❌ 账户异常: {account_detail}")

    # 3. 快照检查
    snapshot_ok, snapshot_detail = check_last_snapshot()
    checks.append({"name": "每日快照", "ok": snapshot_ok, "detail": snapshot_detail})
    if not snapshot_ok:
        alerts.append(f"⚠️ 快照: {snapshot_detail}")

    # 汇总
    all_ok = all(c["ok"] for c in checks)
    result = {
        "status": "ok" if all_ok else "alert",
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "checks": checks,
        "alerts": alerts,
    }

    # 发送告警邮件
    if not all_ok and send_email:
        body = f"量化交易系统健康检查告警\n\n时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        for a in alerts:
            body += f"{a}\n"
        body += "\n请检查系统状态。"
        sent = send_alert(f"[ALERT] 量化系统健康检查异常 - {now.strftime('%Y-%m-%d')}", body)
        result["email_sent"] = sent

    return result


if __name__ == "__main__":
    send = "--send" in sys.argv
    result = run_health_check(send_email=send)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "ok" else 1)
