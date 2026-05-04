#!/usr/bin/env python3
"""
财经新闻搜索（新浪财经 API）
用法: get_news.py [关键词] [--limit 5]
"""
import json, sys, subprocess
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

def search_news(keyword="", limit=5):
    """新浪财经新闻 API"""
    # lid=2516 是财经新闻, lid=2517 是股票新闻
    url = f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k={keyword}&num={limit}&page=1&r=0.1"
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "10",
             "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
             url],
            capture_output=True, timeout=15)
        data = json.loads(r.stdout.decode("utf-8"))
        articles = data.get("result", {}).get("data", [])
        results = []
        for a in articles[:limit]:
            title = a.get("title", "")
            intro = a.get("intro", "")
            news_url = a.get("url", "")
            media = a.get("media_name", "")
            ctime = int(a.get("ctime", 0))
            time_str = datetime.fromtimestamp(ctime, CST).strftime("%H:%M") if ctime else ""
            if title:
                results.append({
                    "title": title,
                    "summary": intro[:100] if intro else "",
                    "url": news_url,
                    "source": media,
                    "time": time_str,
                })
        return results if results else [{"title": f"未找到关于 '{keyword}' 的新闻"}]
    except Exception as e:
        return [{"title": f"获取新闻失败: {str(e)}"}]

if __name__ == "__main__":
    args = sys.argv[1:]
    keyword = ""
    limit = 5
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif not args[i].startswith("--"):
            keyword = args[i]
            i += 1
        else:
            i += 1
    results = search_news(keyword, limit)
    print(json.dumps(results, ensure_ascii=False, indent=2))
