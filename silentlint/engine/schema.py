#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一中间结构（agent_output schema）
====================================
这是「③ 适配器层」的核心契约。

问题：AI 产出的形态是 markdown / html / 代码 / 邮件，各垂类规则却要写
「怎么从文本里抽出可判定的事实」。若每条规则各自解析文本，
既慢（每条规则重解析一次）又不可维护（解析逻辑散落各处）。

解法：**适配器把任意文本一次性转成统一结构，规则只面对这个结构。**
于是「加一种产出形态」= 写一个适配器（技术活），
「加一条检查」= 写一条规则（内容活），两者都不碰引擎。

结构里的每个字段都必须是**可确定性提取的事实**，不能含判断：
  claims        句子列表（带行号），供规则判断有无溯源/绝对化表述
  numbers       数字与百分比，供规则做交叉一致性检查
  placeholders  未替换的占位符残留
  links         链接及其可疑特征
  code_blocks   代码块及其闭合状态
  sections      标题层级结构（报告完整性检查用）
  stats         行数/字数等规模指标
"""
import re

# --- 通用提取工具（各适配器共用，避免重复实现）---

ABSOLUTE_PATTERNS = [
    r'一定(?:能|会|可以)?', r'绝对(?!化|性|值)', r'100\s*%', r'永久', r'必然',
    r'肯定会', r'毫无疑问', r'永远', r'所有(?:情况|时候)?都',
    r'从(?:不|绝不)', r'完全(?:不|没)', r'零风险', r'包(?:治|赚)',
    r'稳赚不赔', r'无风险',
]
# ⚠ v0.1.1（2026-10-14 dogfooding 实测修正）：`绝对` 原为裸模式，
#    把正文里的「绝对化表述」这个【规则名本身】判成绝对化承诺 —— 自指误报。
#    排除复合词：绝对化/绝对性/绝对值 是描述词，不是承诺。

PLACEHOLDER_PATTERNS = [
    (r'\bTBD\b', 'TBD'), (r'\bTODO\b', 'TODO'), (r'\bFIXME\b', 'FIXME'),
    (r'\bXXX+\b', 'XXX'), (r'待补', '待补'), (r'待定', '待定'),
    (r'待填', '待填'), (r'\{\{.*?\}\}', '{{占位符}}'),
    (r'【\s*】', '空书名号'), (r'（\s*）', '空全角括号'),
]

# ⚠ v1.1.0 删掉两条误报率极高的判据 —— 用本项目自己的 README 实测坐实：
#   ① 裸 `占位`：命中正文里「占位符残留 · 无源数字」这种【描述规则的词】。
#      一个词无法与正文区分时，就不配做 BLOCK 判据。
#   ② `\(\s*\)` 空半角括号：命中 `validate()` / `r.items()` 里的空括号，
#      而空半角括号在代码与表格里遍地都是，不是「该填没填」的标记。
#      只保留全角空括号对作为判据（其字面写法不落在此注释里 ——
#      注释自带判据词汇会被本规则判成占位符，2026-10-14 自扫实测）。
#   另：围栏代码块内的行由调用方 skip_lines 排除（见 find_placeholders_lines）。

SOURCE_HINT = re.compile(
    r'(https?://|来源|出处|依据|参见|见\s*第?\s*\d+\s*[章节页条]'
    r'|commit\s+[0-9a-f]{7,}|#[0-9]{3,}|'
    r'《[^》]{2,}》|第[一二三四五六七八九十百\d]+条|'
    r'[A-Z]{2}\s*第?\s*[\d.]+条|'
    # ⚠ v0.1.1（2026-10-14 增补）：认「假设声明」为有效出处。
    # 规则自己的红线说「无源数字≠数据假，只=读者无法验证」——
    # 标注 [预估]/[假设] 的数字，读者能验证「这是假设而非实测」，
    # 按规则自身哲学即属有出处（对齐正明 F11 无源数字铁律的预估标记约定）。
    r'\[(?:预估|假设|预测|观点|规划|测算)\]|预估|假设|测算|推算|目标值)'
)

NUMBER_RE = re.compile(r'(?<![\w.])(\d+(?:\.\d+)?)\s*(%|％|万|亿|元|美元|人|天|小时|分钟|倍|个|次|条|件)?')
STRONG_NUM = re.compile(r'(?<![\w.])(\d+(?:\.\d+)?)\s*(%|％)')


# 先剥离「不该算作论断/数字」的片段：日期、时间、版本号、commit sha、纯序号
_NOISE = re.compile(
    r'\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?'   # 2026-10-04 / 2026年10月4日
    r'|\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?'                    # 18:00 / 18:00:22
    r'|\b[0-9a-f]{7,40}\b'                                      # commit sha
    r'|\bv?\d+\.\d+(?:\.\d+)?\b'                               # 版本号
    r'|#\s*\d+'                                                # issue 编号
)
_HEADING = re.compile(r'^\s*(#{1,6}\s|\*\*\d+[\.、]|标题[:：])')


def strip_noise(text):
    return _NOISE.sub(' ', text)


def find_placeholders(text, line_offset=0, skip_lines=None):
    """skip_lines: 需跳过的行号集合（如 markdown 围栏代码块内的行）。

    代码块里几乎必然出现 `()`、`{}`、以及描述性字样，把它们当占位符
    BLOCK 掉，只会训练用户忽略 BLOCK —— 那比没有 BLOCK 更糟。
    """
    skip = skip_lines or ()
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if i in skip:
            continue
        for pat, label in PLACEHOLDER_PATTERNS:
            m = re.search(pat, line, flags=re.I)
            if m:
                out.append({'label': label, 'line': line_offset + i,
                            'text': m.group(0)[:40]})
                break
    return out


def find_absolute(text, line_offset=0):
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if _HEADING.match(line):
            continue
        for pat in ABSOLUTE_PATTERNS:
            m = re.search(pat, line)
            if m:
                out.append({'text': m.group(0), 'line': line_offset + i,
                            'context': line.strip()[:80]})
                break
    return out


def find_numbers(text):
    """抽取数字及其单位，供一致性检查。

    ⚠ 必须先剥离日期/时间/sha/版本号，否则「2026-10-04」里的 2026 会被
    当成统计数字 —— 实测未剥离时，一份正常报告能产出 135 个「数字」，
    其中绝大多数是日期与章节号，规则的假阳性率超过 80%。
    """
    out = []
    clean = strip_noise(text)
    for m in NUMBER_RE.finditer(clean):
        num, unit = m.group(1), (m.group(2) or '')
        try:
            out.append({'raw': m.group(0).strip(), 'value': float(num),
                        'unit': unit, 'index': m.start()})
        except ValueError:
            continue
    return out


def find_claims(text, line_offset=0):
    """按句切分，标记有无溯源线索（跳过标题行与表格分隔行）"""
    return _claims_from_lines(
        [(line_offset + i, ln) for i, ln in enumerate(text.splitlines(), 1)])


def _claims_from_lines(lines):
    """lines: [(原文行号, 该行文本)] —— 报告的必须是【原文行号】。

    ⚠ v1.0.2 修正：早先对「去标签后的拼接正文」扫描，行号是拼接体的位置，
    与原文完全对不上（实测把原文第 205 行的占位符报成 626）。
    **报错行号比不报错更糟** —— 用户找不到地方就等于没报。
    """
    out = []
    for lineno, raw in lines:
        if _HEADING.match(raw):
            continue
        # 表格分隔行（如 |---|---|）。⚠ 字符类里的 \ 必须写成 \\ 或放最后，
        # 否则经 shell heredoc 传递会被吞掉，字符类静默变形。
        if re.match(r'^\s*\|?[\s:\-=|]+\|[\s:\-=|]+\|?\s*$', raw):
            continue
        s = raw.strip().lstrip('#>-|* ').strip()
        if len(s) < 8:
            continue
        for sent in re.split(r'[。；！？]', strip_noise(s)):
            sent = sent.strip()
            if len(sent) < 8:
                continue
            out.append({
                'text': sent[:160], 'line': lineno,
                'has_source': bool(SOURCE_HINT.search(sent)),
                'has_number': bool(STRONG_NUM.search(sent)
                                   or re.search(r'\d+\s*(?:万|亿|元|美元|人|天|倍|个|次|条|件|小时|分钟)', sent)),
                'is_absolute': any(re.search(p, sent) for p in ABSOLUTE_PATTERNS),
            })
    return out


def find_placeholders_lines(lines, skip_lines=None):
    skip = skip_lines or ()
    out = []
    for lineno, raw in lines:
        if lineno in skip:
            continue
        for pat, label in PLACEHOLDER_PATTERNS:
            m = re.search(pat, raw, flags=re.I)
            if m:
                out.append({'label': label, 'line': lineno, 'text': m.group(0)[:40]})
                break
    return out


def find_absolute_lines(lines):
    out = []
    for lineno, raw in lines:
        if _HEADING.match(raw):
            continue
        for pat in ABSOLUTE_PATTERNS:
            m = re.search(pat, raw)
            if m:
                out.append({'text': m.group(0), 'line': lineno,
                            'context': raw.strip()[:80]})
                break
    return out

def empty_schema(kind='text'):
    return {
        '_schema': 'agent_output/1.0', 'kind': kind,
        'title': '', 'sections': [], 'claims': [], 'numbers': [],
        'placeholders': [], 'absolute': [], 'links': [], 'code_blocks': [],
        'todos': [], 'tables': [], 'raw_lines': 0, 'raw_chars': 0,
    }
