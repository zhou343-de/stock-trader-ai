#!/usr/bin/env python3
"""
邮件推送
用法: send_email.py <主题> <正文文件路径>
或: echo "body" | send_email.py <主题> --stdin
配置从 config/smtp.env 读取
"""
import json, sys, os, ssl, smtplib, subprocess, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
CONFIG_DIR = Path(__file__).parent.parent / "config"

def load_smtp_config():
    env_file = CONFIG_DIR / "smtp.env"
    config = {}
    if not env_file.exists():
        return None
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                config[k.strip()] = v.strip()
    return config

def send_email(subject, body, max_retries=3):
    config = load_smtp_config()
    if not config:
        print(json.dumps({"error": "smtp.env 未配置"}, ensure_ascii=False))
        sys.exit(1)

    smtp_user = config.get("SMTP_USER", "")
    smtp_pass = config.get("SMTP_PASS", "")
    from_email = config.get("FROM_EMAIL", smtp_user)
    to_emails = [e.strip() for e in config.get("TO_EMAIL", "").split(",") if e.strip()]

    if not all([smtp_user, smtp_pass, to_emails]):
        print(json.dumps({"error": "smtp.env 配置不完整"}, ensure_ascii=False))
        sys.exit(1)

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = ", ".join(to_emails)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    last_error = None
    for attempt in range(max_retries):
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.163.com", 465, context=context) as server:
                server.login(smtp_user, smtp_pass)
                server.sendmail(from_email, to_emails, msg.as_string())
            print(json.dumps({"status": "ok", "to": to_emails, "subject": subject,
                              "attempt": attempt + 1}, ensure_ascii=False))
            return
        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))  # 递增等待

    # 所有重试失败，记录到日志文件
    log_file = Path(__file__).parent.parent / "data" / "email_failures.log"
    with open(log_file, "a") as f:
        f.write(f"{datetime.now(CST).isoformat()} | {subject} | {last_error}\n")
    print(json.dumps({"error": f"邮件发送失败({max_retries}次重试)", "last_error": last_error,
                       "report_saved": "日报文件已保存，可手动发送"}, ensure_ascii=False))
    sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: send_email.py <主题> <正文文件>")
        sys.exit(1)
    subject = sys.argv[1]
    if len(sys.argv) >= 3 and sys.argv[2] != "--stdin":
        with open(sys.argv[2]) as f:
            body = f.read()
    else:
        body = sys.stdin.read()
    send_email(subject, body)
