#!/usr/bin/env python3
"""
单股实时行情
用法: get_price.py <代码>（如 sh000001 / sz002475）
"""
import json, sys, subprocess, re

def get_price(code):
    url = f"https://qt.gtimg.cn/q={code}"
    r = subprocess.run(["curl", "-s", "--connect-timeout", "5", url],
                       capture_output=True, timeout=10)
    text = r.stdout.decode("gbk", errors="ignore")
    m = re.search(r'v_(\w+)="(.+)"', text)
    if not m:
        print(json.dumps({"error": f"无法获取 {code} 行情"}, ensure_ascii=False))
        sys.exit(1)

    fields = m.group(2).split("~")
    if len(fields) < 46:
        print(json.dumps({"error": f"数据字段不足"}, ensure_ascii=False))
        sys.exit(1)

    result = {
        "name": fields[1],
        "code": fields[2],
        "price": float(fields[3]) if fields[3] else 0,
        "pre_close": float(fields[4]) if fields[4] else 0,
        "open": float(fields[5]) if fields[5] else 0,
        "volume": int(fields[6]) if fields[6] else 0,
        "buy_volume": int(fields[7]) if fields[7] else 0,
        "sell_volume": int(fields[8]) if fields[8] else 0,
        "high": float(fields[33]) if fields[33] else 0,
        "low": float(fields[34]) if fields[34] else 0,
        "change": float(fields[31]) if fields[31] else 0,
        "change_pct": float(fields[32]) if fields[32] else 0,
        "turnover": float(fields[38]) if fields[38] else 0,
        "amount": float(fields[37]) if fields[37] else 0,
        "pe": float(fields[39]) if fields[39] else 0,
        "market_cap": float(fields[45]) if fields[45] else 0,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: get_price.py <代码>")
        sys.exit(1)
    get_price(sys.argv[1])
