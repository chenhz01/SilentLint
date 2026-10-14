#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
规则集：Python 静默失败结构审计（python-ast）
================================================
提炼自 MaiBot issue #2052 的代码级审计（公开案例，
github.com/Mai-with-u/MaiBot/issues/2052），2026-10-14 按 SilentLint
引擎契约重写为可插拔规则集（原 305 行独立审计器的四条规则，逻辑未改）。

它检查的不是风格，是「日志说谎」的四类结构性缺陷 —— 不是日志不够详细，
而是日志本身给出错误信号，让失败在生产中沉默：

  R1 DEFAULT-TRAP      默认值陷阱：`.get(key, default)` 的 default 只在
                       「键不存在」时生效；上游回空串/None 时默认值静默
                       失效，错误文本在赋值那一行就已经丢了。
  R2 ATTR-BLACKHOLE    归因黑洞：同一函数 >=2 条空返回分支，调用方以单一
                       日志记录该返回值 —— N 条失败路径塌缩成一行 INFO。
  R3 TRACEBACK-DEMOTION 堆栈降级：`traceback.format_exc()` 落在 debug 级，
                       生产 INFO 运行下堆栈永久不可见。
  R4 ABORT-CHANNEL     误报污染：业务「正常中止」与真实失败共用同一返回/
                       告警通道，假告警稀释真实错误的信噪比。

分级原则（与 ai-output 规则集同一设计哲学）：
  BLOCK 只给确定性硬伤 —— 机器能 100% 判定「一定错」的结构。
  WARN 给需要人判断的 —— 如 2 条空返回（简单脚本里可能完全正常）。
  绝不用 WARN 冒充 BLOCK，也绝不用 BLOCK 夸大结构信号。
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..')))
from engine.engine import Rule  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

POLICY = {
    'version': '1.0.0',
    'display': 'Python 静默失败结构审计',
    'applies_to': ['code'],
    'red_lines': [
        '本规则集做【结构审计】，不判断业务逻辑对错',
        'R2「空返回分支多」是结构信号不是 bug 判决 —— 简单脚本多分支返回 None 完全正常',
        'R1 只在「错误字段取用点」触发，普通 .get(k, default) 不在范围内',
    ],
    'rules': {
        'DEFAULT-TRAP': {
            'group': '错误处理',
            'severity': 'BLOCK',
            'enforce_type': 'project_custom',
            'desc': '错误字段 .get(key, default) 未防空串 —— 上游回空值时默认值静默失效',
            'check': 'chk_default_trap',
            'params': {'report_top': 3},
            'source': 'MaiBot issue #2052 实证案例',
        },
        'ATTR-BLACKHOLE': {
            'group': '归因',
            'severity': 'BLOCK',
            'enforce_type': 'project_custom',
            'desc': '函数 >=3 条空返回分支，调用方无法从返回值区分失败原因',
            'check': 'chk_attr_blackhole',
            'params': {'report_top': 3, 'block_at': 3},
            'source': 'MaiBot issue #2052 实证案例',
        },
        'TRACEBACK-DEMOTION': {
            'group': '可观测',
            'severity': 'BLOCK',
            'enforce_type': 'project_custom',
            'desc': 'traceback.format_exc() 记录在 debug 级 —— 生产 INFO 下堆栈不可见',
            'check': 'chk_traceback_demotion',
            'params': {'report_top': 3},
            'source': '可观测性基线：真异常必须与瞬时抖动可区分',
        },
        'ABORT-CHANNEL': {
            'group': '归因',
            'severity': 'WARN',
            'enforce_type': 'project_custom',
            'desc': '正常中止与真实失败共用同一返回/告警通道（误报污染风险）',
            'check': 'chk_abort_channel',
            'params': {'report_top': 3},
            'source': '信噪比：假告警稀释真实错误',
        },
        'PARSE-FAIL': {
            'group': '完整性',
            'severity': 'BLOCK',
            'enforce_type': 'project_custom',
            'desc': 'Python 文件无法被 ast.parse 解析 —— 对 Python 3 而言是硬伤',
            'check': 'chk_parse_fail',
            'source': '语法完整性',
        },
    },
}

# 被视作「错误文本」的字段名 —— 这些字段取值时必须防空串
ERROR_KEYS = ("message", "error", "err", "detail", "reason", "msg", "result")

# `.get(k, default)` 里 default 是字符串字面量 → 作者以为"取不到就会用默认值"
GET_DEFAULT_RE = re.compile(
    r"\.get\(\s*[\"'](?P<key>%s)[\"']\s*,\s*(?P<default>[\"'][^\"']*[\"']|None)\s*\)"
    % "|".join(ERROR_KEYS)
)

# 空值防护特征：出现其中之一，视为该取值点已做「空即未提供」处理
EMPTY_GUARD_RE = re.compile(
    r"\.strip\(\)\s*or\b|\bor\s+[\"']|\bor\s+f[\"']|if\s+not\s+\w|\bnot\s+\w+\s+else|or\s+None"
)

TRACEBACK_CALL = "traceback.format_exc()"

# 日志级别严重度（越大越显眼），用于 R3 判定
LOG_LEVELS = ("debug", "info", "warning", "error", "exception", "critical")

# 日志/打印语句的整体匹配（跨行到右括号）
LOG_CALL_RE = re.compile(r"\b(?:logger|log|logging\.\w+|print)\.(%s)\s*\(" % "|".join(LOG_LEVELS))

# 「正常中止」语义关键词 —— 出现在失败分支中提示这可能是误报通道
ABORT_HINTS = ("abort", "cancel", "skip", "suppress", "stop", "ignore",
               "已中止", "中止", "跳过")


@dataclass
class Finding:
    rule: str
    severity: str          # HIGH / MEDIUM / LOW
    lineno: int
    symbol: str
    message: str
    evidence: str


@dataclass
class AuditOutcome:
    findings: list = field(default_factory=list)
    parse_error: str = ""


# ================= 核心审计（原 305 行审计器逻辑，未改判定） =================

def _iter_enclosing_symbol(tree):
    """产出 (节点, 名称, 起止行) —— 仅函数/方法层。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, 'end_lineno', node.lineno)
            yield node, node.name, node.lineno, end


def rule_r1(lines, tree):
    """R1 默认值陷阱：错误字段取用点未防空串。"""
    out = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for m in GET_DEFAULT_RE.finditer(line):
            window = " ".join(lines[max(0, i - 1): min(len(lines), i + 2)])
            guarded = bool(EMPTY_GUARD_RE.search(window))
            key, default = m.group("key"), m.group("default")
            if guarded:
                continue
            out.append(Finding(
                rule="R1",
                severity="HIGH" if key in ("error", "message") else "MEDIUM",
                lineno=i,
                symbol=f".get({key!r}, {default})",
                message=(
                    f"错误字段 {key!r} 的默认值仅在『键不存在』时生效；上游回空串/None 时 "
                    f"默认值静默失效，错误文本在此行丢失且日志仍会打印 error="
                ),
                evidence=stripped[:160],
            ))
    return out


def rule_r2(lines, tree):
    """R2 归因黑洞：多分支空返回值塌缩到调用方单一日志。"""
    out = []
    for node, name, start, end in _iter_enclosing_symbol(tree):
        none_returns = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and (
                sub.value is None
                or (isinstance(sub.value, ast.Constant) and sub.value.value is None)
            ):
                none_returns.append(sub.lineno)
        if len(none_returns) < 2:
            continue
        aborts = []
        for ln in none_returns:
            ctx = " ".join(lines[max(0, ln - 4): ln]).lower()
            if any(h in ctx for h in ABORT_HINTS):
                aborts.append(ln)
        sev = "HIGH" if len(none_returns) >= 3 else "MEDIUM"
        msg = (
            f"{name}() 有 {len(none_returns)} 条空返回分支（行 {none_returns}）；"
            f"每个分支只写自己的局部日志，调用方无法从返回值区分失败原因 —— 归因信息在结构上不可恢复"
        )
        if aborts:
            msg += (
                f"；其中行 {aborts} 属于『正常中止』语义，却与真实失败共用同一返回通道（R4 误报污染风险）"
            )
        out.append(Finding(
            rule="R2",
            severity=sev,
            lineno=start,
            symbol=f"{name}()",
            message=msg,
            evidence=f"函数体 {start}-{end}，空返回 {len(none_returns)} 处",
        ))
        if aborts:
            out.append(Finding(
                rule="R4",
                severity="MEDIUM",
                lineno=aborts[0],
                symbol=f"{name}()",
                message=(
                    "正常中止与真实失败共用同一返回/告警通道：调用方对空返回统一记 ERROR，"
                    "会把业务性中止误报为故障"
                ),
                evidence=f"中止语义空返回行 {aborts}",
            ))
    return out


def rule_r3(lines, tree):
    """R3 堆栈降级：traceback 落在 debug 级。"""
    out = []
    for i, line in enumerate(lines, 1):
        if TRACEBACK_CALL not in line:
            continue
        level = None
        m = LOG_CALL_RE.search(line)
        if m:
            level = m.group(1)
        else:
            for j in range(i - 1, max(0, i - 4), -1):
                m = LOG_CALL_RE.search(lines[j - 1])
                if m:
                    level = m.group(1)
                    break
        if level in ("debug",):
            out.append(Finding(
                rule="R3",
                severity="HIGH",
                lineno=i,
                symbol=TRACEBACK_CALL,
                message=(
                    f"异常堆栈记录在 {level.upper()} 级：生产以 INFO 运行时堆栈永久不可见，"
                    "真异常在日志里与瞬时抖动无法区分"
                ),
                evidence=line.strip()[:160],
            ))
    return out


def audit_source(text):
    """对一份 Python 源码跑全部四条规则。语法错误时只回 PARSE 发现。"""
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return AuditOutcome(parse_error=f"语法解析失败：{exc.msg}（第 {exc.lineno or 0} 行）")
    findings = []
    findings.extend(rule_r1(lines, tree))
    findings.extend(rule_r2(lines, tree))
    findings.extend(rule_r3(lines, tree))
    return AuditOutcome(findings=findings)


# ================= 引擎契约层 =================

def _findings(ctx):
    """按 ctx 缓存审计结果 —— 四条规则共享一次 ast.parse，不重复劳动。"""
    if '_pyast_outcome' not in ctx:
        raw = ctx.get('_raw', '')
        ctx['_pyast_outcome'] = audit_source(raw)
    return ctx['_pyast_outcome']


def _is_python(target, ctx):
    """只对 .py 生效；其余形态 SKIP（引擎全 SKIP 守卫会兜住误用）。"""
    path = (target or {}).get('_target_path', '') or ''
    return path.lower().endswith('.py')


def _fmt(findings, top, label):
    shown = findings if len(findings) <= top else findings[:top]
    tail = f'……（共 {len(findings)} 处）' if len(findings) > top else ''
    return (f'{len(findings)} 处{label}。例：' +
            '；'.join(f'第{f.lineno}行 {f.symbol}' for f in shown) + tail)


def chk_default_trap(target, rule, ctx):
    if not _is_python(target, ctx):
        return ('SKIP', '非 Python 文件，不适用')
    out = _findings(ctx)
    if out.parse_error:
        return ('SKIP', f'源码不可解析，本规则跳过（{out.parse_error}）')
    fs = [f for f in out.findings if f.rule == 'R1']
    if not fs:
        return ('PASS', '错误字段取用点均已防空串')
    high = [f for f in fs if f.severity == 'HIGH']
    top = rule.get('params', {}).get('report_top', 3)
    if high:
        return ('FAIL', _fmt(high, top, '高危错误字段默认值陷阱（error/message）') +
                ' —— 空值抵达时错误文本静默丢失')
    return ('WARN', _fmt(fs, top, '错误字段默认值陷阱') +
            ' —— 需人判断该字段是否可能收到空值')


def chk_attr_blackhole(target, rule, ctx):
    if not _is_python(target, ctx):
        return ('SKIP', '非 Python 文件，不适用')
    out = _findings(ctx)
    if out.parse_error:
        return ('SKIP', f'源码不可解析，本规则跳过（{out.parse_error}）')
    fs = [f for f in out.findings if f.rule == 'R2']
    if not fs:
        return ('PASS', '无多分支空返回函数')
    top = rule.get('params', {}).get('report_top', 3)
    block_at = rule.get('params', {}).get('block_at', 3)
    hard = [f for f in fs if f.severity == 'HIGH']
    if hard:
        return ('FAIL', _fmt(hard, top, f'函数有 >={block_at} 条空返回分支') +
                ' —— 失败原因在结构上不可归因')
    return ('WARN', _fmt(fs, top, '函数有 2 条空返回分支') +
            ' —— 简单脚本可能正常，需人判断调用方能否区分失败原因')


def chk_traceback_demotion(target, rule, ctx):
    if not _is_python(target, ctx):
        return ('SKIP', '非 Python 文件，不适用')
    out = _findings(ctx)
    if out.parse_error:
        return ('SKIP', f'源码不可解析，本规则跳过（{out.parse_error}）')
    fs = [f for f in out.findings if f.rule == 'R3']
    if not fs:
        return ('PASS', 'traceback 均未降级到 debug 级')
    top = rule.get('params', {}).get('report_top', 3)
    return ('FAIL', _fmt(fs, top, 'traceback 落在 debug 级') +
            ' —— 生产 INFO 运行下堆栈永久不可见')


def chk_abort_channel(target, rule, ctx):
    if not _is_python(target, ctx):
        return ('SKIP', '非 Python 文件，不适用')
    out = _findings(ctx)
    if out.parse_error:
        return ('SKIP', f'源码不可解析，本规则跳过（{out.parse_error}）')
    fs = [f for f in out.findings if f.rule == 'R4']
    if not fs:
        return ('PASS', '未发现正常中止与失败共用通道')
    top = rule.get('params', {}).get('report_top', 3)
    return ('WARN', _fmt(fs, top, '正常中止与真实失败共用返回/告警通道') +
            ' —— 假告警会稀释真实错误信噪比')


def chk_parse_fail(target, rule, ctx):
    if not _is_python(target, ctx):
        return ('SKIP', '非 Python 文件，不适用')
    out = _findings(ctx)
    if out.parse_error:
        return ('FAIL', f'{out.parse_error} —— 该文件对 Python 3 不可执行')
    return ('PASS', 'ast.parse 解析通过')


CHECKERS = {k: v for k, v in list(globals().items())
            if k.startswith('chk_') and callable(v)}


class RuleSet:
    name = 'python-ast'
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
                    {'rule': 'ATTR-BLACKHOLE', 'risk': 'medium',
                     'question': '「>=3 条空返回 = BLOCK」的阈值来自 MaiBot 单案例，'
                                 '未在真实 Python 语料上标定；数据管道类代码'
                                 '（多来源合并）可能天然多分支。',
                     'who': '需用真实语料做标定实验'},
                    {'rule': 'DEFAULT-TRAP', 'risk': 'low',
                     'question': 'ERROR_KEYS 字段名表现有 7 个，是否覆盖了'
                                 '各框架的错误字段约定（如 exc_info / errMsg）。',
                     'who': '需按主流框架错误字段扩充词表并回归'}]}

    @classmethod
    def validate(cls):
        """返回 check 字段无法解析的规则 ID 列表（空=全部对得上）"""
        return [rid for rid, rd in POLICY['rules'].items()
                if rd.get('check') not in CHECKERS]
