#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
agent-output-audit · 引擎层 v1.0
=================================
设计铁律（违反即失去意义）：
  1. **引擎不认识任何业务**。它不知道"音乐"是什么、不知道 ISRC、
     不知道法条。它只认识：规则集声明的数据结构、严重度等级、退出码。
  2. **加规则集 = 放一个目录**。核心代码零改动。放进去就能跑。
  3. **规则自己声明检查逻辑**（可以是 python 函数，但由规则集提供），
     引擎只负责注册、调度、汇总、出报告。
  4. **引擎管置信度**：规则可以上报 confidence/coverage，引擎统管降权
     策略——避免每个检查器各写一套。

这是「护城河」真正所在：规则一周能抄，抽象抄不动。
"""
import os
import sys
import json
import importlib.util

SEVERITY_ORDER = ['BLOCK', 'WARN', 'INFO', 'PASS', 'SKIP']

# 退出码契约（固定对外承诺，不随规则集变化）
EXIT_CODES = {
    'clean': 0,      # 无 BLOCK 且无 WARN
    'warn': 2,       # 无 BLOCK，有 WARN
    'block': 1,      # 有 BLOCK
    'error': 3,      # 引擎/规则集自身故障
}


class Rule:
    """一条规则。字段全部由规则集声明，引擎不加不减。

    ⚠ v1.0.1 修正：初版把 rule 声明成普通对象，规则集里 `rule.get('params')`
    全部抛 AttributeError，被引擎的异常降级兜成 WARN —— **掩盖了规则失效**，
    且违反了「规则集零改动」的承诺（业务代码原本按 dict 写）。
    修法：Rule 实现 dict-like 代理（get/__getitem__/__contains__），
    使规则集代码可以原样复用历史实现，不必因引擎抽象而改动业务判定。
    """

    def __init__(self, rid, desc, severity, check_fn, **meta):
        self.id = rid
        self.desc = desc
        self.severity = severity          # BLOCK / WARN / INFO
        self.check_fn = check_fn
        self.meta = meta                  # enforce_type / group / source / params ...

    # -- dict-like 代理：让规则集代码把 rule 当 policy dict 用 --
    def get(self, key, default=None):
        return self.meta.get(key, default)

    def __getitem__(self, key):
        return self.meta[key]

    def __contains__(self, key):
        return key in self.meta

    def keys(self):
        return self.meta.keys()

    def items(self):
        return self.meta.items()

    def __repr__(self):
        return f'<Rule {self.id} {self.severity}>'


class RuleResult:
    def __init__(self, rule, status, detail, confidence=None, coverage=None):
        self.rule = rule
        self.status = status              # PASS / FAIL / WARN / INFO / SKIP
        self.detail = detail
        self.confidence = confidence
        self.coverage = coverage

    @property
    def effective_status(self):
        """置信降权：coverage 低于 confidence_floor 时，FAIL 降为 WARN。"""
        floor = self.rule.meta.get('confidence_floor')
        if (self.status == 'FAIL' and self.coverage is not None
                and floor is not None and self.coverage < floor):
            return 'WARN'
        return self.status


class RuleSet:
    """一个规则集。加载器只要求它提供 load() -> [Rule, ...]。"""

    name = 'abstract'
    display = 'abstract'
    schema_hint = {}
    version = '0'

    @classmethod
    def load(cls):
        raise NotImplementedError


class Engine:
    def __init__(self, ruleset_path, name, target_path, extra_ctx=None):
        # ruleset_path 既接受目录，也接受 ruleset.py 文件全路径
        self.ruleset_file = (os.path.join(ruleset_path, 'ruleset.py')
                             if os.path.isdir(ruleset_path) else ruleset_path)
        self.ruleset_dir = os.path.dirname(self.ruleset_file)
        self.name = name
        self.target_path = target_path
        self.extra_ctx = extra_ctx or {}

    # -- 规则集加载（发现机制：新目录即被识别，核心零改动） --
    @staticmethod
    def discover(base_dir):
        """扫描 rulesets/*/ruleset.py，返回可用规则集清单"""
        found = []
        rs_root = os.path.join(base_dir, 'rulesets')
        if not os.path.isdir(rs_root):
            return found
        for d in sorted(os.listdir(rs_root)):
            p = os.path.join(rs_root, d, 'ruleset.py')
            if os.path.isfile(p):
                found.append((d, p))
        return found

    def _load_ruleset(self):
        spec = importlib.util.spec_from_file_location(
            f'ruleset_{self.name.replace("-", "_")}', self.ruleset_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.RuleSet

    # -- 主流程 --
    def run(self):
        try:
            rs = self._load_ruleset()
        except Exception as e:
            return self._fatal(f'规则集加载失败: {type(e).__name__}: {e}')
        # v1.1：目标可以是「已适配的结构(json)」或「原始文本(md/html/py)」。
        # 非 json 一律过适配器层转成统一中间结构 —— 规则只面对结构，不面对文本。
        raw_text = ''
        try:
            with open(self.target_path, encoding='utf-8', errors='replace') as f:
                raw_text = f.read()
            if self.target_path.lower().endswith('.json'):
                target = json.loads(raw_text)
            else:
                from engine import adapters
                target = adapters.adapt(self.target_path)
                target['_target_path'] = self.target_path
        except Exception as e:
            return self._fatal(f'适配器转换失败: {type(e).__name__}: {e}')

        # v1.1.1：JSON 数组/标量根守卫。
        # 2026-10-14 dogfooding 总检实测：根节点为数组的 .json 让
        # _summarize 的 target.get('title') 抛 AttributeError 直接崩栈
        # （CLI 直扫这些文件会 traceback，而不是干净地报错）。
        # 与零规则守卫同源：结构不匹配 → 明错，不猜、不假绿。
        if self.target_path.lower().endswith('.json') and not isinstance(target, dict):
            return self._fatal(
                f'JSON 根节点是 {type(target).__name__} 而非对象 —— '
                f'规则集按字段取用，数组/标量根没有可检字段。'
                f'已中止，不输出通过结论。')

        rules = rs.load()
        # v1.0.2：零规则守卫。
        # 实测教训：某规则集因 CHECKERS 键名与 policy 的 check 字段对不上，
        # 产出 0 条规则，引擎却报「结论：通过 / exit 0」——**静默假绿**，
        # 比报错更危险（调用方会以为检查过了）。
        # 空规则集一定是配置错误，不可能是合法状态。
        if not rules:
            return self._fatal(
                f'规则集 {rs.name} 产出 0 条规则 —— 这是配置错误（'
                f'通常是 CHECKERS 键名与 policy 的 check 字段不匹配），'
                f'不是「没有问题」。已中止，不输出通过结论。')

        # v1.0.3：未解析检查器的守卫。
        # 教训：键名不匹配这个坑一天踩了 3 次，每次都表现为「0 条规则」
        # 或「部分规则静默失效」。若规则集提供 validate()，逐条点名未解析的
        # check，让错误直接指向出问题的规则 ID，而不是让人猜。
        if hasattr(rs, 'validate'):
            try:
                missing = rs.validate()
            except Exception as e:
                return self._fatal(f'规则集 validate() 异常: '
                                   f'{type(e).__name__}: {e}')
            if missing:
                return self._fatal(
                    f'规则集 {rs.name} 有 {len(missing)} 条规则的 check 字段'
                    f'无法解析（键名不匹配）：{", ".join(missing[:5])}'
                    f'{"…" if len(missing) > 5 else ""}。已中止。')
        ctx = dict(target.get('_context', {}))
        ctx.update(self.extra_ctx)
        ctx['_ruleset'] = rs
        ctx['_target'] = target
        # 原文交给需要做文本级扫描的规则（如凭据泄漏），
        # 但**不进统一结构** —— 否则「规则只面对结构」的契约就破了。
        ctx['_raw'] = raw_text

        results = []
        for rule in rules:
            if not rule.meta.get('enabled', True):
                results.append(RuleResult(rule, 'SKIP', '规则已停用(enabled=false)'))
                continue
            try:
                out = rule.check_fn(target, rule, ctx)
                if isinstance(out, RuleResult):
                    r = out
                    r.rule = rule
                else:
                    status, detail = out
                    r = RuleResult(rule, status, detail)
                # 规则可在返回元组外通过 ctx 回填置信度
                last = ctx.get('_last_confidence')
                if last and r.confidence is None:
                    r.confidence, r.coverage = last
                    ctx['_last_confidence'] = None
            except Exception as e:
                r = RuleResult(rule, 'ERROR',
                               f'检查器异常: {type(e).__name__}: {e}')
            results.append(r)

        return self._summarize(rs, results, target)

    def _fatal(self, msg):
        """引擎级故障：区别于「检出了问题」，退出码 3"""
        print(f'✗ 引擎错误: {msg}', file=sys.stderr)
        return EXIT_CODES['error']

    def _summarize(self, rs, results, target):
        counts = {k: 0 for k in ['BLOCK', 'WARN', 'INFO', 'PASS', 'SKIP',
                                 'FAIL', 'ERROR']}
        downgraded, errored = [], []
        for r in results:
            st = r.effective_status
            if st == 'ERROR':
                # v1.0.1：检查器异常单列，不再混入 WARN。
                # 理由：异常降级成 WARN 会让「规则失效」看起来像「有待人工确认」，
                #       从而掩盖 bug —— 实测中它确实掩盖了 2 条规则静默失效。
                counts['ERROR'] += 1
                errored.append(r)
            elif st == 'FAIL':
                counts['BLOCK'] += 1
            elif st == 'WARN':
                counts['WARN'] += 1
                if r.status == 'FAIL':
                    downgraded.append(r)
            elif st == 'INFO':
                counts['INFO'] += 1
            elif st == 'PASS':
                counts['PASS'] += 1
            else:
                counts['SKIP'] += 1
        # v1.0.4：全 SKIP 守卫 —— 假绿的最后一闸。
        # 实测（ecommerce 规则集）：规则集只认 track['listing'] 嵌套结构，
        # 扁平 listing 被 SKIP 9 条、PASS 0 条，counts 是
        # BLOCK 0 / WARN 0 / INFO 0 / PASS 0 / SKIP 9，
        # 引擎却按 'clean' 退出 0 —— 一份**什么都没查**的体检被当成
        # 「通过」交出去。全 SKIP 只能是适配或结构出错，不可能是「没问题」。
        # 与 v1.0.2 零规则守卫同源：宁可吵，不可静默绿灯。
        all_skipped = (counts['SKIP'] == len(results)
                       and counts['BLOCK'] == 0 and counts['WARN'] == 0
                       and counts['INFO'] == 0 and counts['PASS'] == 0)
        if all_skipped:
            counts['ERROR'] += 1
            # v2.1.1：原引用 rules[0]，但 rules 是 run() 的局部变量 —— 全 SKIP
            # 守卫一触发就 NameError 崩溃（design 规则集接 JSON 目标时实测命中）。
            # 改用 results[0].rule（全 SKIP 时 results 必非空且每条带 rule）。
            results.append(RuleResult(
                results[0].rule, 'ERROR',
                f'全 {len(results)} 条规则均 SKIP —— 未检出任何可判定内容。'
                f'典型原因：① 规则集与目标形态不匹配（用 ecommerce 去体检某首歌）'
                f'② 适配器没把业务字段提出来 ③ 目标文件取错路径。'
                f'**这不是「通过」，请不要据此放行。**'))
        # 规则集自身故障 → 退出码 3（区别于「检出了问题」）
        code = ('error' if counts['ERROR'] else
                'block' if counts['BLOCK'] else
                'warn' if counts['WARN'] else 'clean')
        return {
            'engine': 'agent-output-audit',
            'ruleset': rs.name, 'ruleset_version': rs.version,
            'target': target.get('title', self.target_path),
            'counts': counts, 'exit_code': EXIT_CODES[code],
            'results': results, 'downgraded': downgraded, 'errored': errored,
            'meta': rs.meta() if hasattr(rs, 'meta') else {},
        }
