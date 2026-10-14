#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
agent-output-audit · 报告层 v1.0
==================================
四种输出，同一份结果：
  text   人类可读（默认）
  json   机器可读
  sarif  静态分析结果交换格式 → **可直接被 GitHub Code Scanning 消费**
          （这是别人接不接你的分界线：能进 CI 才算工具，只打印不算）
  junit  测试报告格式 → CI 流水线通用

引擎层不碰业务；报告层同样不碰业务，只认 results 列表。
"""
import json


def _sev_sarif(status):
    """internal status → SARIF level

    ⚠ v1.0.1 修正：原表用 'BLOCK' 作键，但 effective_status 实际是
    'FAIL'/'WARN'/'PASS'/'SKIP'/'INFO'/'ERROR'，于是 FAIL 落到 default 'note'，
    **所有阻塞项都被降级成提示** —— GitHub Code Scanning 会直接忽略它们。
    这正是「新增指标没验证它对已知案例能出正确值」的又一处实例。
    """
    return {'FAIL': 'error', 'ERROR': 'error',
            'WARN': 'warning', 'INFO': 'note',
            'PASS': 'none', 'SKIP': 'none'}.get(status, 'note')


def render_text(rep):
    rs = rep['ruleset']
    c = rep['counts']
    L = []
    L.append('=' * 74)
    L.append(f"agent-output-audit · 规则集 {rs} v{rep['ruleset_version']}")
    L.append(f"目标：{rep['target']}")
    meta = rep.get('meta') or {}
    if meta.get('display'):
        L.append(f"场景：{meta['display']}")
    L.append('=' * 74)
    for r in rep['results']:
        icon = {'FAIL': '✗', 'WARN': '⚠', 'PASS': '✓', 'ERROR': '💥',
                'SKIP': '–', 'INFO': 'ℹ'}.get(r.effective_status, '?')
        et = r.rule.meta.get('enforce_type', '')
        L.append(f"{icon} [{r.rule.id}]({et}) {r.rule.desc}")
        L.append(f"      → {r.detail}")
    L.append('-' * 74)
    c = rep['counts']
    L.append(f"合计 {len(rep['results'])} 条：BLOCK {c['BLOCK']} ｜ WARN {c['WARN']} "
             f"｜ INFO {c['INFO']} ｜ PASS {c['PASS']} ｜ SKIP {c['SKIP']}"
             + (f" ｜ **ERROR {c['ERROR']}**" if c.get('ERROR') else ''))
    for r in rep.get('errored', []):
        L.append(f"  💥 [{r.rule.id}] 检查器异常 —— 规则失效，非检出问题。"
                 f"退出码 3 表示规则集有 bug，不可当作「检查通过」")
    for r in rep['downgraded']:
        L.append(f"  ⇩ [{r.rule.id}] 置信降权 FAIL→WARN"
                 f"（coverage {r.coverage} < floor {r.rule.meta.get('confidence_floor')}）")
    for line in (meta.get('red_lines') or []):
        L.append(f"  ⛔ {line}")
    npr = meta.get('needs_pro_review') or []
    high = [x for x in npr if x.get('risk') == 'high']
    if high:
        L.append('-' * 74)
        L.append(f"⚠ 需专业复核 {len(npr)} 条（高风险 {len(high)} 条）"
                 f"｜ 核证于 {meta.get('review_metadata', {}).get('reviewed_at', '—')}")
        for x in high:
            L.append(f"  ⚠ [{x.get('rule')}] {x.get('question', '')[:86]}")
    if c['BLOCK']:
        L.append('结论：不通过（存在 BLOCK 项），先修再发布。')
    elif c['WARN']:
        L.append('结论：无 BLOCK，但有 WARN，需人眼确认。')
    else:
        L.append('结论：通过。')
    return '\n'.join(L)


def render_json(rep):
    return json.dumps({
        'engine': rep['engine'], 'ruleset': rep['ruleset'],
        'ruleset_version': rep['ruleset_version'],
        'target': rep['target'], 'exit_code': rep['exit_code'],
        'counts': rep['counts'],
        'findings': [{
            'id': r.rule.id, 'severity': r.effective_status,
            'raw_status': r.status, 'message': r.detail,
            'enforce_type': r.rule.meta.get('enforce_type'),
            'source': r.rule.meta.get('source'),
            'confidence': r.confidence, 'coverage': r.coverage,
        } for r in rep['results']],
    }, ensure_ascii=False, indent=2)


def render_sarif(rep):
    rules = {}
    sarif_results = []
    for r in rep['results']:
        rid = r.rule.id
        if rid not in rules:
            rules[rid] = {
                'id': rid, 'name': rid,
                'shortDescription': {'text': r.rule.desc[:200]},
                'properties': {
                    k: v for k, v in r.rule.meta.items()
                    if k in ('enforce_type', 'group', 'source', 'severity')
                },
            }
        if r.effective_status in ('FAIL', 'WARN'):
            sarif_results.append({
                'ruleId': rid,
                'level': _sev_sarif(r.effective_status),
                'message': {'text': r.detail},
                'locations': [{
                    'physicalLocation': {
                        'artifactLocation': {'uri': rep['target']},
                        'region': {'startLine': 1},
                    }
                }],
            })
    return json.dumps({
        '$schema': 'https://json.schemastore.org/sarif-2.1.0.json',
        'version': '2.1.0',
        'runs': [{
            'tool': {'driver': {
                'name': 'agent-output-audit',
                'informationUri': 'https://github.com/',
                'rules': list(rules.values()),
            }},
            'results': sarif_results,
        }],
    }, ensure_ascii=False, indent=2)


def render_junit(rep):
    c = rep['counts']
    total = len(rep['results'])
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<testsuite name="agent-output-audit/{rep["ruleset"]}" '
             f'tests="{total}" failures="{c["BLOCK"]}" skipped="{c["SKIP"]}">']
    for r in rep['results']:
        name = r.rule.id.replace('<', '&lt;')
        detail = (r.detail or '').replace('&', '&amp;').replace('<', '&lt;')
        if r.effective_status == 'FAIL':
            lines.append(f'  <testcase name="{name}"><failure message="{detail}"/></testcase>')
        elif r.effective_status == 'SKIP':
            lines.append(f'  <testcase name="{name}"><skipped/></testcase>')
        else:
            lines.append(f'  <testcase name="{name}"/>')
    lines.append('</testsuite>')
    return '\n'.join(lines)


RENDERERS = {'text': render_text, 'json': render_json,
             'sarif': render_sarif, 'junit': render_junit}
