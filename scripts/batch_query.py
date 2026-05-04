#!/usr/bin/env python3
"""
批量行情获取 + 数据校验
用法: batch_query.py <代码1> <代码2> ...
输出 JSON，校验失败 sys.exit(2)
"""
import json, sys, subprocess, re, time

def batch_query(codes, max_retries=3, retry_delay=0.5):
    if not codes:
        print(json.dumps({"error": "无代码"}, ensure_ascii=False))
        sys.exit(1)

    batch = ",".join(codes)
    url = f"https://qt.gtimg.cn/q={batch}"

    text = ""
    for attempt in range(max_retries):
        try:
            r = subprocess.run(["curl", "-s", "--connect-timeout", "10", url],
                               capture_output=True, timeout=15)
            text = r.stdout.decode("gbk", errors="ignore")
            if text.strip():
                break
        except Exception:
            pass
        if attempt < max_retries - 1:
            time.sleep(retry_delay * (attempt + 1))  # 递增间隔

    if not text.strip():
        print(json.dumps({"error": "行情API无响应", "retries": max_retries}, ensure_ascii=False))
        sys.exit(1)

    results = {}
    for line in text.strip().split("\n"):
        m = re.search(r'v_(\w+)="(.+)"', line)
        if not m:
            continue
        code = m.group(1)
        fields = m.group(2).split("~")
        if len(fields) < 46:
            continue
        results[code] = {
            "name": fields[1],
            "code": fields[2],
            "price": float(fields[3]) if fields[3] else 0,
            "pre_close": float(fields[4]) if fields[4] else 0,
            "open": float(fields[5]) if fields[5] else 0,
            "volume": int(fields[6]) if fields[6] else 0,
            "high": float(fields[33]) if fields[33] else 0,
            "low": float(fields[34]) if fields[34] else 0,
            "change": float(fields[31]) if fields[31] else 0,
            "change_pct": float(fields[32]) if fields[32] else 0,
            "turnover": float(fields[38]) if fields[38] else 0,
            "amount": float(fields[37]) if fields[37] else 0,
            "pe": float(fields[39]) if fields[39] else 0,
            "market_cap": float(fields[45]) if fields[45] else 0,
        }

    # 数据校验
    errors = validate_data(results, codes)
    if errors:
        print(json.dumps({"errors": errors, "data": results}, ensure_ascii=False, indent=2))
        sys.exit(2)
    # 空数据过滤
    for code in list(results.keys()):
        d = results[code]
        if d["price"] == 0 and d["volume"] == 0:
            print(json.dumps({"warning": f"{code} 返回空数据，已跳过"}, ensure_ascii=False), file=sys.stderr)
            del results[code]

    print(json.dumps(results, ensure_ascii=False, indent=2))

def validate_data(results, codes_requested):
    errors = []
    warnings = []
    # 完整性
    for code in codes_requested:
        if code not in results:
            errors.append(f"{code}: 未返回数据")
    # 价格校验
    for code, d in results.items():
        if d["price"] <= 0 and d["name"] != "":
            errors.append(f"{code} ({d['name']}): 价格为0，跳过")
        if abs(d["change_pct"]) > 20:
            errors.append(f"{code} ({d['name']}): 涨跌幅异常 {d['change_pct']}%")
        if d["price"] == 0 and d["open"] == 0 and d["high"] == 0:
            errors.append(f"{code} ({d['name']}): 全零数据")
        if d["volume"] == 0 and d["price"] > 0:
            warnings.append(f"{code} ({d['name']}): 成交量为0（停牌/集合竞价前）")

        # [新增] 日内振幅验证：|最高-最低|/昨收 > 15%（非创业板/科创板）
        if d["pre_close"] > 0 and d["high"] > 0 and d["low"] > 0:
            amplitude = (d["high"] - d["low"]) / d["pre_close"]
            is_gem_star = code.startswith("3") or code.startswith("688")
            threshold = 0.20 if is_gem_star else 0.15
            if amplitude > threshold:
                warnings.append(f"{code} ({d['name']}): 日内振幅 {amplitude*100:.1f}% 超阈值 {threshold*100:.0f}%，数据可疑")

        # [新增] 停牌检测：价格不变且成交量为0
        if d["volume"] == 0 and d["price"] == d["pre_close"] and d["price"] > 0:
            warnings.append(f"{code} ({d['name']}): 疑似停牌（价格不变+零成交）")

        # [新增] 连续性检查：涨跌幅与成交量不匹配（价格大幅波动但无量 → 可能是数据毛刺）
        if d["volume"] > 0 and d["pre_close"] > 0:
            price_change = abs(d["price"] - d["pre_close"]) / d["pre_close"]
            if price_change > 0.05 and d["amount"] < 5000000:  # 涨跌>5%但成交额<500万
                warnings.append(f"{code} ({d['name']}): 价格波动 {price_change*100:.1f}% 但成交额仅 {d['amount']/10000:.0f}万，疑似脏数据")

    if warnings:
        print(json.dumps({"warnings": warnings}, ensure_ascii=False), file=sys.stderr)
    return errors

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: batch_query.py <代码1> [代码2] ...")
        sys.exit(1)
    batch_query(sys.argv[1:])
