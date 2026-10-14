#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
适配器：把任意 AI 产出转成统一中间结构
=========================================
引擎不读文件、不解析文本，只接受一个 dict。
所以需要一个转换层。**加一种产出形态 = 在此加一个适配器，引擎与规则零改动。**

已支持：
  .md / .markdown  → markdown 适配器（日报、报告、方案）
  .html / .htm    → html 适配器（交付页面）
  .py / .js / .ts → code 适配器（AI 生成代码）
  .json           → 视为已适配的结构，原样返回
"""
import os
import re
import json

from engine import schema as S

_ADAPTERS = {}


def register(exts):
    """扩展名 → 适配器函数的注册装饰器"""
    def deco(fn):
        for e in exts:
            _ADAPTERS[e] = fn
        return fn
    return deco


# ---------- markdown ----------
@register(['.md', '.markdown'])
def _md(path, text):
    d = S.empty_schema('markdown')
    d['title'] = next((l.lstrip('# ').strip() for l in text.splitlines()
                       if l.strip().startswith('# ')), '')
    # 标题层级
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r'^(#{1,6})\s+(.+?)\s*$', line)
        if m:
            d['sections'].append({'level': len(m.group(1)),
                                  'heading': m.group(2), 'line': i})
    # 代码块闭合
    fence = 0
    start = 0
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith('```'):
            if fence == 0:
                fence, start = 1, i
            else:
                d['code_blocks'].append({
                    'lang': text.splitlines()[start].strip().strip('`') or '',
                    'start_line': start, 'end_line': i, 'closed': True})
                fence = 0
    if fence == 1:                       # 未闭合
        d['code_blocks'].append({'lang': '', 'start_line': start,
                                 'end_line': None, 'closed': False})
    d['todos'] = re.findall(r'^\s*[-*]\s+\[([ xX])\]\s+(.+)$', text, flags=re.M)
    d['tables'] = len(re.findall(r'^\|.*\|\s*$', text, flags=re.M))
    d['links'] = [{'url': m.group(2), 'line': text[:m.start()].count('\n') + 1,
                   'text': m.group(1)}
                  for m in re.finditer(r'\[([^\]]{0,60})\]\(([^)]+)\)', text)]
    d['claims'] = S.find_claims(text)
    d['numbers'] = S.find_numbers(text)
    # 围栏代码块内的行不参与占位符扫描：`validate()` / `r.items()` 里的
    # 空半角括号、示例代码里的 `{k: v}` 都不是占位符（实测误报，v1.1.0 修）。
    _fenced = set()
    for _b in d.get('code_blocks') or []:
        if _b.get('end_line'):
            _fenced.update(range(_b['start_line'], _b['end_line'] + 1))
    d['placeholders'] = S.find_placeholders(text, skip_lines=_fenced)
    d['absolute'] = S.find_absolute(text)
    d['raw_lines'] = len(text.splitlines())
    d['raw_chars'] = len(text)
    return d


# ---------- html ----------
@register(['.html', '.htm'])
def _html(path, text):
    d = S.empty_schema('html')
    m = re.search(r'<title[^>]*>(.*?)</title>', text, flags=re.S | re.I)
    d['title'] = m.group(1).strip() if m else ''
    for i, mt in enumerate(re.finditer(r'<h([1-6])[^>]*>(.*?)</h\1>',
                                       text, flags=re.S | re.I), 1):
        d['sections'].append({'level': int(mt.group(1)),
                              'heading': re.sub(r'<[^>]+>', '', mt.group(2)).strip()[:60],
                              'line': text[:mt.start()].count('\n') + 1})
    # v0.1.1 修正（2026-10-14 dogfooding 实测）：原写 {'url': u, ...}，
    # 但推导式循环变量是 m —— NameError 直接炸掉整个 html 适配路径，
    # 任何 .html 目标都过不了引擎（被防假绿闸拦成退出码 3，未静默通过）。
    d['links'] = [{'url': m.group(1), 'line': text[:m.start()].count('\n') + 1, 'text': ''}
                  for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)', text, flags=re.I)]
    d['todos'] = re.findall(r'<li[^>]*>(.*?)</li>', text, flags=re.S | re.I)[:50]
    # ⚠ v1.0.1 修正：原先把整页 body 去标签后交给 find_claims，
    # 结果 UI 装饰性文本全被当成论断 —— metric 卡片的裸数字
    # （<div class="v">0</div>）、页头元信息行、表格分隔线，
    # 一份正常报告能产出 60+ 条「无源数字断言」，假阳性率 >80%。
    # 修法：只从语义正文标签提取（p / li / td / th / blockquote / h1-h6），
    # 跳过 div / span 等纯布局容器。
    prose = []          # [(原文行号, 文本)]
    for mt in re.finditer(
            r'<(p|li|td|th|blockquote|figcaption|h[1-6])[^>]*>(.*?)</\1>',
            text, flags=re.S | re.I):
        chunk = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', mt.group(2),
                       flags=re.S | re.I)
        chunk = re.sub(r'<[^>]+>', '', chunk)
        for a, b in (('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                     ('&gt;', '>'), ('&quot;', '"')):
            chunk = chunk.replace(a, b)
        # 原文起始行号 —— 扫描时报的行号必须对得上原文
        line = text[:mt.start()].count('\n') + 1
        # 标题加 '# ' 前缀：让 schema._HEADING 能识别并排除标题行
        # （否则「0 · 费曼层」这类章节编号会被当成无源数字断言）
        prefix = '# ' if re.match(r'h[1-6]$', mt.group(1), re.I) else ''
        # 记录【原文起始行号】，扫描时报的行号才对得上原文
        prose.append((line, prefix + chunk))
    d['claims'] = S._claims_from_lines(prose)
    d['placeholders'] = S.find_placeholders_lines(prose)
    d['absolute'] = S.find_absolute_lines(prose)
    body = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', text,
                  flags=re.S | re.I)
    body = re.sub(r'<[^>]+>', '\n', body)
    d['numbers'] = S.find_numbers(body)
    d['tables'] = len(re.findall(r'<table', text, flags=re.I))
    d['raw_lines'] = len(text.splitlines())
    d['raw_chars'] = len(text)
    return d


def _py_token_facts(text):
    """Python 专属：tokenize 级精确事实（括号平衡 + 注释token）。

    为什么不再用全文启发式（2026-10-14 自扫实测的两个 FP 家族）：
    ① 括号平衡：文档字符串/正则字面量/f-string 里的引号会让「剥字符串」
       正则错乱 —— adapters.py 被误报 '(' 差 10、test_all.py 差 -1，
       而这些文件每天都在正常运行。tokenize 是语法级事实，不可能错。
    ② 占位符：规则集源码的描述文字里自带 TBD/XXX/{{}} 模式词
       （检测器的词汇表出现在检测器自己源码里）—— 对代码目标只扫注释，
       字符串/文档串里的模式词是数据，不是「未替换的占位符」。
    """
    import io
    import tokenize
    brackets = {'(': 0, '[': 0, '{': 0}
    comments = []
    ok = True
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.OP:
                if tok.string in brackets:
                    brackets[tok.string] += 1
                elif tok.string == ')':
                    brackets['('] -= 1
                elif tok.string == ']':
                    brackets['['] -= 1
                elif tok.string == '}':
                    brackets['{'] -= 1
            elif tok.type == tokenize.COMMENT:
                comments.append((tok.start[0], tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        ok = False
    return brackets, comments, ok


# ---------- code ----------
@register(['.py', '.js', '.ts', '.tsx', '.jsx'])
def _code(path, text):
    d = S.empty_schema('code')
    d['title'] = os.path.basename(path)
    if path.lower().endswith('.py'):
        brackets, comments, tok_ok = _py_token_facts(text)
        d['bracket_balance'] = brackets
        d['tokenize_ok'] = tok_ok
        # 占位符只扫注释 token（带真实行号）：字符串里的模式词是数据
        d['placeholders'] = S.find_placeholders_lines(comments)
        d['todos'] = [c for _, c in comments
                      if re.match(r'#\s*(TODO|FIXME|XXX|HACK)\b', c)]
        # 代码目标不做论断检查：注释/文档串不是论断载体 ——
        # 规则讲解文字（「一定错」「100% 判定」）不是作者的断言。
        # claims_checked=False 让 CLAIM-* 规则显式 SKIP，不制造假 PASS。
        d['claims'] = []
        d['absolute'] = []
        d['claims_checked'] = False
        d['numbers'] = S.find_numbers(text)
        d['raw_lines'] = len(text.splitlines())
        d['raw_chars'] = len(text)
        return d
    # 未闭合的引号/括号（按行累计，末行仍不平衡即报）
    depth = {'(': 0, '[': 0, '{': 0}
    pairs = {')': '(', ']': '[', '}': '{'}
    for i, line in enumerate(text.splitlines(), 1):
        s = re.sub(r'#.*$|//.*$', '', line)
        s = re.sub(r'["\'].*?["\']', '', s)
        for ch in s:
            if ch in depth:
                depth[ch] += 1
            elif ch in pairs:
                depth[pairs[ch]] -= 1
    d['bracket_balance'] = depth
    d['todos'] = re.findall(r'^\s*#\s*(TODO|FIXME|XXX|HACK)\b.*$', text, flags=re.M)
    d['claims'] = S.find_claims(re.sub(r'#.*$', '', text, flags=re.M))
    d['numbers'] = S.find_numbers(text)
    d['placeholders'] = S.find_placeholders(text)
    d['absolute'] = S.find_absolute(text)
    d['raw_lines'] = len(text.splitlines())
    d['raw_chars'] = len(text)
    return d


def _mask_code_spans(text: str) -> str:
    """把「代码引用」掩成中性词，再交给论断提取。

    ⚠ v0.1.2（2026-10-14 dogfooding 实测新增）：
    此前反引号/<code> 里的内容原样进入论断提取，导致一类**产品级误报**——
    AI 报告讲解规则本身时（「检查 `TODO` 占位符」「`绝对` 是绝对化模式」），
    被引用的模式名被判成真论断：PLACEHOLDER 直接 BLOCK、CLAIM-ABSOLUTE WARN。
    文档在讲解规则，被规则判死刑 —— 这类 FP 会直接毁掉工具可信度。
    代码 span 是字面引用，不是作者的主张，按规则自身哲学不应算论断。
    围栏代码块（```）不受影响：其行首三反引号无闭合内容，本正则不匹配。
    """
    text = re.sub(r'`[^`\n]+`', '`码`', text)                 # md 行内代码
    text = re.sub(r'<code[^>]*>.*?</code>', '〔码〕', text,   # html 行内代码
                  flags=re.S | re.I)
    return text


def adapt(path):
    """主入口：任意文件 → 统一结构"""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.json':
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    fn = _ADAPTERS.get(ext)
    if not fn:
        raise ValueError(f'无适配器支持该扩展名: {ext}（支持: '
                         f'{", ".join(sorted(_ADAPTERS))}）')
    with open(path, encoding='utf-8', errors='replace') as f:
        raw = f.read()
    # 仅对「文档类」适配器掩码代码 span；源码适配器（.py/.js）要原样看
    if ext in ('.md', '.markdown', '.html', '.htm'):
        raw = _mask_code_spans(raw)
    return fn(path, raw)


def supported():
    return sorted(_ADAPTERS)
