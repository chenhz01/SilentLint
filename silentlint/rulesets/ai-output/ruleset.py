#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
规则集：AI 产出通用体检（Agent Output Audit）
================================================
**这是整个项目的本体。** music / ecommerce / shortvideo 都只是它的一种应用。

它检查的是「AI 写出来的东西」本身，而不是某个垂直行业的法规：
  · 有没有没替换的占位符
  · 数字结论有没有出处
  · 有没有不可验证的绝对化承诺
  · 结构完整性（代码块闭合、括号平衡、标题层级）
  · 敏感信息泄漏
  · 规模异常（过短=敷衍 / 过长=灌水）

分级原则（**这是本规则集最重要的设计决策**）：
  BLOCK 只给**确定性硬伤** —— 机器能 100% 判定「一定错」的东西。
  WARN 给**需要人判断**的东西 —— 无源数字、绝对化表述、规模异常。
  绝不用 WARN 冒充 BLOCK：那会让人对工具整体失去信任。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..')))
from engine.engine import Rule  # noqa: E402

POLICY = {
    'version': '1.0.0',
    'display': 'AI 产出通用体检',
    'applies_to': ['markdown', 'html', 'code'],
    'red_lines': [
        '本规则集做【形式与可追溯性】检查，不判断内容对错',
        'WARN 项需人判断 —— 不要把 WARN 当成「有问题」的证据',
        '「无源数字」不等于「数据是假的」，只等于「读者无法验证」',
    ],
    'rules': {
        'PLACEHOLDER': {
            'group': '完整性',
            'severity': 'BLOCK',
            'enforce_type': 'project_custom',
            'desc': '存在未替换的占位符（TBD/待补/XXX/{{}}）—— 发出去就是半成品',
            'check': 'chk_placeholder',
            'params': {'max_allowed': 0},
            'source': '交付铁律：无指标不标完毕 / 占位符必须清零',
        },
        'CODEBLOCK-UNCLOSED': {
            'group': '结构',
            'severity': 'BLOCK',
            'enforce_type': 'project_custom',
            'desc': '代码块未闭合（``` 数量为奇数）',
            'check': 'chk_codeblock_unclosed',
            'source': 'markdown 结构完整性',
        },
        'BRACKET-UNBALANCED': {
            'group': '结构',
            'severity': 'BLOCK',
            'enforce_type': 'project_custom',
            'desc': '代码括号不平衡（() [] {} 未配对）—— 直接导致无法运行',
            'check': 'chk_bracket',
            'params': {'min_abs': 1},
            'source': '语法完整性',
        },
        'SECRET-LEAK': {
            'group': '安全',
            'severity': 'BLOCK',
            'enforce_type': 'project_custom',
            'desc': '疑似密钥/令牌泄漏（sk-…、api_key、token=、private key）',
            'check': 'chk_secret',
            'source': '凭据泄漏防护',
        },
        'PII-LEAK': {
            'group': '安全',
            'severity': 'WARN',
            'enforce_type': 'project_custom',
            'desc': '疑似个人信息（手机号/身份证/银行卡）—— 需人判断是否为公开信息',
            'check': 'chk_pii',
            'source': '个人信息保护',
        },
        'CLAIM-NO-SOURCE': {
            'group': '可追溯',
            'severity': 'WARN',
            'enforce_type': 'project_custom',
            'desc': '含统计数字的断言没有出处标记 —— 读者无法验证',
            'check': 'chk_claim_no_source',
            'params': {'report_top': 3, 'warn_ratio': 0.15},
            'source': '来源核实铁律（RA-3）：数字必指源，无源数字禁入对外文本',
        },
        'CLAIM-ABSOLUTE': {
            'group': '可验证性',
            'severity': 'WARN',
            'enforce_type': 'project_custom',
            'desc': '不可验证的绝对化表述（一定/绝对/100%/永久/零风险）',
            'check': 'chk_claim_absolute',
            'params': {'report_top': 3},
            'source': '反自欺：把预测当事实',
        },
        'STRUCTURE-SHALLOW': {
            'group': '结构',
            'severity': 'WARN',
            'enforce_type': 'project_custom',
            'desc': '标题层级过浅（无 h2 及以下）—— 多半是平铺直叙，难读',
            'check': 'chk_structure',
            'params': {'need_level': 2, 'min_sections': 3},
            'source': '可读性基线',
        },
        'SIZE-ANOMALY': {
            'group': '规模',
            'severity': 'WARN',
            'enforce_type': 'project_custom',
            'desc': '篇幅异常（过短疑似敷衍 / 过长疑似灌水）',
            'check': 'chk_size',
            'params': {'min_lines': 20, 'max_lines': 4000},
            'source': '交付质量基线',
        },
    },
}


# ================= 检查器 =================

def chk_placeholder(target, rule, ctx):
    ps = target.get('placeholders') or []
    allow = rule.get('params', {}).get('max_allowed', 0)
    if len(ps) > allow:
        # v1.0.2：计数与明细必须一致 —— 原写法报「6 处」却只列 4 行，
        # 属于 BLOCK 级消息自相矛盾，读者无法据此定位。超 4 条时明说省略。
        shown = ps if len(ps) <= 4 else ps[:3]
        tail = f'……（共 {len(ps)} 处）' if len(ps) > 4 else ''
        return ('FAIL', f'{len(ps)} 处占位符未替换：' +
                '、'.join(f'第{p["line"]}行 {p["label"]}' for p in shown) + tail)
    return ('PASS', '无未替换占位符')


def chk_codeblock_unclosed(target, rule, ctx):
    if target.get('kind') not in ('markdown',):
        return ('SKIP', '非 markdown，不适用')
    bad = [b for b in (target.get('code_blocks') or []) if not b.get('closed')]
    if bad:
        shown = bad if len(bad) <= 4 else bad[:3]
        tail = f'……（共 {len(bad)} 处）' if len(bad) > 4 else ''
        return ('FAIL', f'{len(bad)} 处代码块未闭合，起始行：' +
                '、'.join(str(b.get('start_line')) for b in shown) + tail)
    return ('PASS', f'{(len(target.get("code_blocks") or []))} 处代码块均闭合')


def chk_bracket(target, rule, ctx):
    if target.get('kind') != 'code':
        return ('SKIP', '非代码文件，不适用')
    # tokenize 失败（源码不可解析）时括号平衡无法判定 —— SKIP 而不是 PASS：
    # 没查就说没问题 = 假绿。不可解析的 .py 由 python-ast 的 PARSE-FAIL BLOCK。
    if target.get('tokenize_ok') is False:
        return ('SKIP', 'tokenize 失败，括号平衡无法判定')
    bal = target.get('bracket_balance') or {}
    bad = {k: v for k, v in bal.items()
           if abs(v) >= rule.get('params', {}).get('min_abs', 1)}
    if bad:
        return ('FAIL', f'括号不平衡：{bad} —— 代码无法运行')
    return ('PASS', '括号配对平衡')


SECRET_PATS = [
    (r'sk-[A-Za-z0-9]{16,}', 'openai 类 key'),
    (r'ghp_[A-Za-z0-9]{20,}', 'github token'),
    (r'AKIA[0-9A-Z]{16}', 'aws access key'),
    (r'-----BEGIN [A-Z ]*PRIVATE KEY-----', '私钥'),
    (r'(?:api[_-]?key|secret|token|password)\s*[=:]\s*["\']?[A-Za-z0-9/+_-]{12,}',
     '硬编码凭据'),
]


def chk_secret(target, rule, ctx):
    raw = ctx.get('_raw', '')
    if not raw:
        return ('SKIP', '未提供原文（JSON 目标不做文本扫描）')
    for pat, label in SECRET_PATS:
        m = re.search(pat, raw, flags=re.I)
        if m:
            line = raw[:m.start()].count('\n') + 1
            return ('FAIL', f'疑似{label}（第 {line} 行）：{m.group(0)[:16]}…')
    return ('PASS', '未检出疑似凭据')


PII_PATS = [
    (r'(?<!\d)1[3-9]\d{9}(?!\d)', '手机号'),
    (r'(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])', '身份证'),
    (r'(?<!\d)[1-9]\d{14}(?!\d)', '银行卡'),
]


def chk_pii(target, rule, ctx):
    nums = [n for n in (target.get('numbers') or []) if n.get('unit') == '']
    hits = []
    for pat, label in PII_PATS:
        for n in nums:
            if re.fullmatch(pat, n['raw']):
                hits.append(label)
                break
    if hits:
        return ('WARN', f'疑似{sorted(set(hits))} —— 需人判断是否为公开信息')
    return ('PASS', '未检出个人信息特征')


def chk_claim_no_source(target, rule, ctx):
    # 代码目标（.py 等）显式 SKIP：注释/文档串/字符串不是论断载体 ——
    # 2026-10-14 自扫实测，规则集自己的讲解文字（含数字无出处）被当论断 WARN。
    # SKIP 而不是置空 claims 后 PASS：没查就说没问题是假绿，比漏检更危险。
    if target.get('claims_checked') is False:
        return ('SKIP', '代码目标不做论断检查（注释/字符串不是论断载体）')
    claims = target.get('claims') or []
    if len(claims) < 5:
        return ('SKIP', f'论断仅 {len(claims)} 条，样本不足')
    ns = [c for c in claims if c['has_number'] and not c['has_source']]
    ratio = len(ns) / len(claims)
    thr = rule.get('params', {}).get('warn_ratio', 0.15)
    if not ns:
        return ('PASS', f'{len(claims)} 条论断中，含数字者均有出处')
    if ratio < thr:
        return ('INFO', f'{len(ns)}/{len(claims)} 条含数字无出处（{ratio:.0%} < {thr:.0%}，可接受）')
    top = rule.get('params', {}).get('report_top', 3)
    return ('WARN', f'{len(ns)}/{len(claims)} 条含数字无出处（{ratio:.0%} ≥ {thr:.0%}）。例：' +
            '；'.join(f'第{c["line"]}行「{c["text"][:28]}」' for c in ns[:top]))


def chk_claim_absolute(target, rule, ctx):
    if target.get('claims_checked') is False:
        return ('SKIP', '代码目标不做论断检查（注释/字符串不是论断载体）')
    ab = target.get('absolute') or []
    if not ab:
        return ('PASS', '无绝对化表述')
    top = rule.get('params', {}).get('report_top', 3)
    return ('WARN', f'{len(ab)} 处绝对化表述。例：' +
            '；'.join(f'第{a["line"]}行「{a["text"]}」' for a in ab[:top]) +
            ' —— 绝对化承诺通常无法兑现')


def chk_structure(target, rule, ctx):
    secs = target.get('sections') or []
    if not secs:
        return ('SKIP', '无标题结构')
    p = rule.get('params', {})
    deep = [s for s in secs if s['level'] >= p.get('need_level', 2)]
    if len(deep) < p.get('min_sections', 3):
        return ('WARN', f'仅 {len(deep)} 个二级及以下标题（共 {len(secs)} 个标题），'
                        f'结构偏平')
    return ('PASS', f'{len(secs)} 个标题，{len(deep)} 个二级及以下')


def chk_size(target, rule, ctx):
    n = target.get('raw_lines') or 0
    p = rule.get('params', {})
    if n < p.get('min_lines', 20):
        return ('WARN', f'仅 {n} 行，低于 {p.get("min_lines")} 行 —— 疑似敷衍')
    if n > p.get('max_lines', 4000):
        return ('WARN', f'{n} 行，超过 {p.get("max_lines")} 行 —— 疑似灌水')
    return ('PASS', f'{n} 行，规模正常')


CHECKERS = {k: v for k, v in list(globals().items())
            if k.startswith('chk_') and callable(v)}


class RuleSet:
    name = 'ai-output'
    display = POLICY['display']
    version = POLICY['version']

    @classmethod
    def load(cls):
        rules = []
        for rid, rd in POLICY['rules'].items():
            fn = CHECKERS.get(rd['check'])
            if not fn:
                continue
            meta = {k: v for k, v in rd.items()
                    if k not in ('check', 'severity', 'desc')}
            rules.append(Rule(rid, rd['desc'], rd.get('severity', 'WARN'),
                              fn, **meta))
        return rules

    @classmethod
    def meta(cls):
        return {'display': cls.display, 'red_lines': POLICY['red_lines'],
                'needs_pro_review': [
                    {'rule': 'CLAIM-NO-SOURCE', 'risk': 'medium',
                     'question': '「无源数字」阈值 15% 是经验值，未在真实语料上标定。'
                                 '不同文体（研究报告 vs 日报）的合理区间差异很大，'
                                 '是否应按文体分别设阈值？',
                     'who': '需用真实语料做标定实验'},
                    {'rule': 'SIZE-ANOMALY', 'risk': 'low',
                     'question': '篇幅阈值 20/4000 行同样未标定，'
                                 '短交付件（一条回复）会被误判为敷衍。',
                     'who': '需按文体分层设阈值'}]}

    @classmethod
    def validate(cls):
        """返回 check 字段无法解析的规则 ID 列表（空=全部对得上）"""
        return [rid for rid, rd in POLICY['rules'].items()
                if rd.get('check') not in CHECKERS]

