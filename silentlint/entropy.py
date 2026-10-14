#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SilentLint · D3 判决熵检测器（Verdict Entropy）

方法论来源：audit-entropy-wiring skill（ACS 坍缩哨兵 P-1/P-2）。

问题
----
一条恒 PASS 的规则等于没有规则 —— 它给每一次体检盖章「通过」，
却从未鉴别过任何东西。更糟的是它让报告**看起来**更安全：
46 条规则的体检，实际鉴别力可能只有 3 条，没人看得出来。

检测
----
读入历史 verdict 流（JSONL，每行 {"rule": ..., "status": ...}），
按规则聚合通过率与分布熵：
  ALWAYS_PASS    n>=min_n 且全部 PASS       → 零鉴别力（橡皮章）
  ALWAYS_FAIL    n>=min_n 且全部 FAIL       → 零鉴别力（规则写错/目标永远不合规）
  NEAR_CONSTANT  分布熵 < entropy_floor     → 近恒定，鉴别力可忽略

退出码：0 = 所有规则有鉴别力；2 = 存在零鉴别力规则（CI 应报警）
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RuleStats:
    rule: str
    n: int = 0
    pass_n: int = 0
    fail_n: int = 0
    other_n: int = 0
    entropy: float = 0.0
    flag: str = ""

    @property
    def pass_rate(self) -> float:
        return self.pass_n / self.n if self.n else 0.0


def load_verdicts(path: Path) -> list:
    """读 JSONL；坏行跳过（样本不足时 min_n 守卫会兜底）。"""
    records = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and "rule" in rec and "status" in rec:
                records.append(rec)
    return records


def _binary_entropy(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -p * math.log2(p) - (1 - p) * math.log2(1 - p)


def analyze(records: list, min_n: int = 10, entropy_floor: float = 0.1) -> list:
    by_rule: dict = {}
    for rec in records:
        st = by_rule.setdefault(rec["rule"], RuleStats(rule=rec["rule"]))
        st.n += 1
        status = str(rec["status"]).upper()
        if status == "PASS":
            st.pass_n += 1
        elif status == "FAIL":
            st.fail_n += 1
        else:
            st.other_n += 1
    out = []
    for st in by_rule.values():
        st.entropy = _binary_entropy(st.pass_rate)
        if st.n >= min_n:
            if st.pass_n == st.n:
                st.flag = "ALWAYS_PASS"
            elif st.fail_n == st.n:
                st.flag = "ALWAYS_FAIL"
            elif st.entropy < entropy_floor:
                st.flag = "NEAR_CONSTANT"
        out.append(st)
    out.sort(key=lambda s: (s.flag == "", s.rule))
    return out


def render_entropy(stats: list, min_n: int) -> str:
    out = []
    out.append("=" * 78)
    out.append(f"D3 判决熵 · 规则鉴别力审计（min_n={min_n}）")
    out.append("=" * 78)
    flagged = [s for s in stats if s.flag]
    if not flagged:
        out.append("  未发现零鉴别力规则。")
    for s in flagged:
        out.append(f"  [{s.flag}] {s.rule}  n={s.n} pass={s.pass_n} "
                   f"fail={s.fail_n} rate={s.pass_rate:.0%} H={s.entropy:.3f}")
    out.append("-" * 78)
    out.append(f"D3 合计：{len(stats)} 条规则 · 零鉴别力 {len(flagged)} 条 · "
               f"退出码 {'2（有规则在假装工作）' if flagged else '0（通过）'}")
    out.append("-" * 78)
    return "\n".join(out)


def entropy_exit_code(stats: list) -> int:
    return 2 if any(s.flag for s in stats) else 0
