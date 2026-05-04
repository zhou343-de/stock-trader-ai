#!/usr/bin/env python3
"""
多Agent辩论选股器
三个角色：猎手（趋势）、账房（价值）、守夜人（风控）
三轮辩论后输出共识决策
"""
import json
import os
import re
import sys
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

# ============================================================
# 辩论 Prompt 模板
# ============================================================
DEBATE_PROMPT = """
## 角色
你是投资决策辩论的主持人。你管理着三个分析师：
- **猎手**（趋势交易者）：追强势，看动量和量价配合，信仰"趋势是你的朋友"
- **账房**（价值投资者）：看估值，要安全边际，信仰"好公司要有好价格"
- **守夜人**（风控官）：管风险，保本金，信仰"活下来比赚钱重要"

三人都有偏见，这很正常。辩论的意义就是让偏见互相制衡。

## 当前环境
- 市场环境：{regime}
- 上证指数：{index}，今日涨跌 {index_change}
- 可用资金：{available_cash} 元
- 当前持仓：{current_holdings}
- 最多可买：{max_new} 只
- 仓位上限：{max_position}%

## 候选股票（来自量化选股漏斗 Top {n}）
{candidate_list}

## 每只候选股详细数据
{candidate_details}

## 今日财经新闻摘要
{news_summary}

## 北向资金动向
{northbound_data}

## 辩论规则

请进行三轮辩论。每轮每位分析师**必须**发言。

### 第一轮：初审
每位分析师对每只候选股给出初步意见：
- 支持买入 / 反对买入 / 有条件支持
- 一句话理由（必须引用具体数据）
- 建议仓位比例（0-50%）

### 第二轮：交锋
每位分析师必须做两件事：
1. 指出另一位分析师观点中的**具体漏洞**（不能只说"我不同意"）
2. 用数据反驳或回应质疑

### 第三轮：共识
综合三轮讨论，给出最终决策。对于每只股票：
- 如果三方都支持 → 高置信度买入
- 如果两方支持一方反对 → 低置信度买入（降低仓位）
- 如果两方反对 → 否决
- 明确列出"如果买入后出现什么情况，说明判断错了"（可证伪条件）

## 输出格式（严格JSON，不要输出其他内容）

```json
{{
 "debate_rounds": [
 {{
 "round": 1,
 "hunter": {{
 "opinions": [
 {{"code": "000066", "name": "中国长城", "stance": "支持", "reason": "具体数据理由", "position_pct": 30}}
 ]
 }},
 "accountant": {{
 "opinions": [
 {{"code": "000066", "name": "中国长城", "stance": "反对", "reason": "具体数据理由", "position_pct": 0}}
 ]
 }},
 "watchman": {{
 "opinions": [
 {{"code": "000066", "name": "中国长城", "stance": "有条件支持", "reason": "具体数据理由", "position_pct": 20}}
 ]
 }}
 }},
 {{
 "round": 2,
 "hunter": {{"challenge_to": "accountant", "target_code": "000066", "argument": "具体反驳"}},
 "accountant": {{"challenge_to": "hunter", "target_code": "000066", "argument": "具体反驳"}},
 "watchman": {{"challenge_to": "hunter", "target_code": "000066", "argument": "具体反驳"}}
 }},
 {{
 "round": 3,
 "consensus": [
 {{
 "code": "000066",
 "name": "中国长城",
 "decision": "买入",
 "vote_score": 2,
 "position_pct": 25,
 "reasons": {{
 "hunter": "趋势理由",
 "accountant": "估值理由",
 "watchman": "风控理由"
 }},
 "risk_note": "主要风险点",
 "logic_stop_price": 17.80,
 "falsification": "跌破17.80说明判断错了"
 }}
 ],
 "rejected": [
 {{
 "code": "601890",
 "name": "亚星锚链",
 "reason": "三方一致否决的理由"
 }}
 ],
 "debate_summary": "一句话总结辩论结果"
 }}
 ]
}}
"""


# ============================================================
# 核心函数
# ============================================================

def load_account():
    """加载账户数据"""
    path = os.path.join(DATA_DIR, 'trend_account.json')
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def build_candidate_details(candidates_json):
    """
    将选股器输出的候选股JSON转为辩论prompt可用的格式
    candidates_json: stock_screener.py --top N --json 的输出中的 candidates 列表
    """
    lines = []
    for i, c in enumerate(candidates_json, 1):
        lines.append(f"""
候选 {i}：{c.get('name', c.get('code', '未知'))}（{c.get('code', '?')}）
板块：{c.get('sector', '未知')}
评分：{c.get('total_score', c.get('score', 0))}/100
现价：¥{c.get('price', 0)} 涨幅：{c.get('change_pct', 0):+.2f}%
PE：{c.get('pe', 'N/A')} 流通市值：{c.get('float_market_cap', 'N/A')}亿
换手率：{c.get('turnover', 0):.2f}% 量比：{c.get('volume_ratio', 0):.2f}
成交额：{c.get('amount', 0)/10000:.2f}万
均线多头：{'是' if c.get('ma_aligned') else '否'}
MACD信号：{'金叉' if c.get('macd_golden') else '无'}
近5日涨幅：{c.get('change_5d', 'N/A')}%
""")
    return '\n'.join(lines)


def build_holdings_summary(account):
    """构建当前持仓摘要"""
    holdings = account.get('positions', account.get('holdings', {}))
    if not holdings:
        return '空仓'

    lines = []
    for code, h in holdings.items():
        avg_price = h.get('avg_price', h.get('cost_price', 0))
        shares = h.get('shares', h.get('quantity', 0))
        profit_pct = h.get('profit_pct', 0)
        if profit_pct == 0 and avg_price > 0:
            # 尝试从 stats 中计算
            profit_pct = 0
        lines.append(f"- {h.get('name', code)}（{code}）：{shares}股，成本¥{avg_price}，浮盈{profit_pct:+.2f}%")
    return '\n'.join(lines)


def build_debate_prompt(account, candidates_json, news_summary='', northbound_data='暂无数据'):
    """
    构建完整的辩论prompt
    account: trend_account.json 内容
    candidates_json: 候选股列表（list of dict）
    news_summary: 新闻摘要文本
    northbound_data: 北向资金数据
    """
    regime_info = account.get('market_regime', {})
    regime = regime_info.get('current', 'range')
    params = regime_info.get('params_active', regime_info.get('params', {}))

    # 计算可买几只
    max_holdings = params.get('max_holdings', 3)
    positions = account.get('positions', account.get('holdings', {}))
    current_count = len(positions)
    max_new = max(0, max_holdings - current_count)

    # 可用资金
    available_cash = account.get('current_cash', account.get('cash', 0))
    max_position_pct = int(params.get('max_position', 0.85) * 100) if params.get('max_position', 0.85) <= 1 else params.get('max_position', 85)

    prompt = DEBATE_PROMPT.format(
        regime=f"{regime}（止损{params.get('stop_loss', '-7%')}，仓位上限{max_position_pct}%，最多{max_holdings}只）",
        index='上证指数',
        index_change='待填入',
        available_cash=f"{available_cash:,.0f}",
        current_holdings=build_holdings_summary(account),
        max_new=max_new,
        max_position=max_position_pct,
        n=len(candidates_json),
        candidate_list='\n'.join([f"- {c.get('name', c['code'])}（{c['code']}）评分{c.get('total_score', c.get('score', 0))}" for c in candidates_json]),
        candidate_details=build_candidate_details(candidates_json),
        news_summary=news_summary or '暂无新闻数据',
        northbound_data=northbound_data,
    )

    return prompt


def parse_debate_result(ai_output):
    """
    解析AI辩论输出的JSON
    返回标准化的买入指令列表
    """
    # 尝试提取JSON
    try:
        result = json.loads(ai_output)
    except json.JSONDecodeError:
        json_match = re.search(r'```json\s*(.*?)\s*```', ai_output, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group(1))
        else:
            start = ai_output.find('{')
            end = ai_output.rfind('}') + 1
            if start >= 0 and end > start:
                result = json.loads(ai_output[start:end])
            else:
                raise ValueError(f"无法从AI输出中提取JSON: {ai_output[:200]}")

    # 提取共识决策
    rounds = result.get('debate_rounds', [])
    round3 = rounds[-1] if rounds else {}
    consensus = round3.get('consensus', [])
    rejected = round3.get('rejected', [])
    summary = round3.get('debate_summary', '')

    buys = []
    for stock in consensus:
        if stock.get('decision') in ['买入', 'buy', 'BUY']:
            buys.append({
                'code': stock['code'],
                'name': stock.get('name', stock['code']),
                'position_pct': stock.get('position_pct', 25),
                'vote_score': stock.get('vote_score', 2),
                'reasons': stock.get('reasons', {}),
                'risk_note': stock.get('risk_note', ''),
                'logic_stop_price': stock.get('logic_stop_price'),
                'falsification': stock.get('falsification', ''),
            })

    return {
        'buys': buys,
        'rejected': rejected,
        'summary': summary,
        'full_debate': result,
    }


def normalize_positions(buys, account):
    """
    归一化仓位比例，确保不超过仓位上限
    """
    regime_info = account.get('market_regime', {})
    params = regime_info.get('params_active', regime_info.get('params', {}))
    max_position_val = params.get('max_position', 0.85)
    max_position_pct = int(max_position_val * 100) if max_position_val <= 1 else max_position_val

    total_assets = account.get('total_assets',
                    account.get('current_cash', 0) + sum(
                        p.get('shares', p.get('quantity', 0)) * p.get('avg_price', p.get('cost_price', 0))
                        for p in account.get('positions', account.get('holdings', {})).values()
                    ))
    cash = account.get('current_cash', account.get('cash', 0))

    # 总仓位不能超过 max_position_pct
    total_pct = sum(b['position_pct'] for b in buys)
    if total_pct > max_position_pct:
        scale = max_position_pct / total_pct
        for b in buys:
            b['position_pct'] = round(b['position_pct'] * scale, 1)

    # 单只不能超过总资金的 40%
    for b in buys:
        if b['position_pct'] > 40:
            b['position_pct'] = 40

    # 计算每只的金额和股数
    for b in buys:
        amount = total_assets * b['position_pct'] / 100
        if amount > cash:
            amount = cash * 0.95
        b['amount'] = round(amount, 2)

    return buys


def format_debate_for_report(debate_result):
    """
    将辩论结果格式化为日报可用的Markdown
    """
    lines = []
    lines.append("## 选股辩论记录\n")

    # 共识买入
    if debate_result.get('buys'):
        lines.append("### 买入决策\n")
        lines.append("| 股票 | 仓位 | 置信度 | 猎手 | 账房 | 守夜人 | 可证伪条件 |")
        lines.append("|------|------|--------|------|------|--------|-----------|")
        for b in debate_result['buys']:
            confidence = '高' if b.get('vote_score', 0) >= 3 else '中' if b.get('vote_score', 0) == 2 else '低'
            reasons = b.get('reasons', {})
            lines.append(
                f"| {b.get('name', b['code'])}({b['code']}) "
                f"| {b.get('position_pct', 0)}% "
                f"| {confidence} "
                f"| {str(reasons.get('hunter', '-'))[:15]} "
                f"| {str(reasons.get('accountant', '-'))[:15]} "
                f"| {str(reasons.get('watchman', '-'))[:15]} "
                f"| {str(b.get('falsification', '-'))[:20]} |"
            )
        lines.append("")

    # 被否决
    if debate_result.get('rejected'):
        lines.append("### 否决决策\n")
        for r in debate_result['rejected']:
            lines.append(f"- ~~{r.get('name', r.get('code', '?'))}({r.get('code', '?')})~~：{r.get('reason', '未说明')}")
        lines.append("")

    # 辩论总结
    if debate_result.get('summary'):
        lines.append(f"### 辩论总结\n{debate_result['summary']}\n")

    return '\n'.join(lines)


# ============================================================
# 独立运行（测试用）
# ============================================================
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'prompt':
        account = load_account()
        candidates_file = os.path.join(DATA_DIR, 'candidates.json')
        if os.path.exists(candidates_file):
            with open(candidates_file, 'r') as f:
                candidates = json.load(f)
        else:
            candidates = []
        prompt = build_debate_prompt(account, candidates)
        print(prompt)

    elif len(sys.argv) > 1 and sys.argv[1] == 'test':
        test_output = '''```json
{
  "debate_rounds": [
    {"round": 1, "hunter": {"opinions": []}, "accountant": {"opinions": []}, "watchman": {"opinions": []}},
    {"round": 2, "hunter": {}, "accountant": {}, "watchman": {}},
    {
      "round": 3,
      "consensus": [
        {
          "code": "300586",
          "name": "美联新材",
          "decision": "买入",
          "vote_score": 3,
          "position_pct": 30,
          "reasons": {"hunter": "趋势强", "accountant": "估值低", "watchman": "流动性好"},
          "risk_note": "化工板块波动大",
          "logic_stop_price": 11.20,
          "falsification": "跌破MA20(11.20)"
        }
      ],
      "rejected": [
        {"code": "601890", "name": "亚星锚链", "reason": "流动性不足"}
      ],
      "debate_summary": "三方一致看好美联新材，亚星锚链被一致否决"
    }
  ]
}
```'''
        result = parse_debate_result(test_output)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        print("用法:")
        print("  python3 debate_selector.py prompt    # 输出辩论prompt")
        print("  python3 debate_selector.py test      # 测试解析")
