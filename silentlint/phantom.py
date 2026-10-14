#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SilentLint · D1 幻工检测器（Phantom Artifact Detector）

产品论点
--------
AI 最危险的失败不是报错，是**说完成了但工件不存在**：
报告里写着「✅ 已生成 report.html」，磁盘上什么都没有，
而读者已经信了 —— 错误在交付那一刻发生，在用户那里才爆炸。

本检测器只做一件事：把文本里「完成声明」引用的工件路径，
拿到磁盘上验存。**确定性判定 —— 不用 LLM，不猜语义，只看文件在不在。**

完成声明形态（保守优先，宁漏勿错）：
  A. 勾选框已勾    - [x] 已创建 foo.py
  B. 完成标记      ✅ / ☑ / ✓ 同行出现
  C. 完成动词      已创建/已写入/已生成/已交付/已完成/已实现/已修复/
                   已上线/已发布/已跑通/已部署（英文同表）
未来时态（将/待/明天/计划/TODO）不是幻工，是未兑现承诺 —— 单列 INFO，
不混入 P0（把承诺当事故会毁掉工具的可信度）。

已知边界（诚实清单，不装作没有）：
  · 只验「行内可解析出路径」的声明；「已完成全部工作」这类无路径声明不判
  · ``` 围栏内的示例文本跳过（README 里的用法示例不算声明）
  · URL 引用不算本地声明
  · 路径按 --root 解析（默认：被扫文件所在目录）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ------------------------------------------------------------------ 词表

DONE_VERBS_CN = (
    "已创建", "已写入", "已生成", "已交付", "已完成", "已实现",
    "已修复", "已上线", "已发布", "已跑通", "已部署", "已装弹",
)

DONE_VERBS_EN = (
    "created", "written", "generated", "delivered", "implemented",
    "fixed", "deployed", "shipped", "passed",
)

FUTURE_HINTS = (
    "将", "待", "明天", "下周", "计划", "后续", "TODO", "todo",
    "will ", "planned", "upcoming", "roadmap",
)

ARTIFACT_EXTS = (
    "py", "md", "markdown", "html", "htm", "json", "js", "ts", "txt",
    "csv", "xlsx", "xls", "png", "jpg", "jpeg", "gif", "svg",
    "mp3", "mp4", "wav", "pdf", "docx", "zip",
)

PATH_TOKEN_RE = re.compile(
    r"(?P<path>(?:[\w.\-]+[/\\])*[\w.\-]+\.(?:%s))\b" % "|".join(ARTIFACT_EXTS),
    re.IGNORECASE,
)
EN_VERB_RE = re.compile(r"\b(?:%s)\b" % "|".join(DONE_VERBS_EN), re.IGNORECASE)
CHECKBOX_DONE_RE = re.compile(r"^\s*[-*+]\s+\[[xX]\]\s+(?P<rest>.+)$")
DONE_MARK_RE = re.compile(r"[✅☑✓]")
URL_RE = re.compile(r"https?://\S+")
TRAILING_JUNK = ".,;:!?，。；：！？、）】」》'\"”’"

# ------------------------------------------------------------------ 结构


@dataclass
class PhantomFinding:
    rule: str        # PHANTOM-MISSING / PROMISE-OPEN
    severity: str    # BLOCK / INFO
    lineno: int
    path: str
    message: str
    evidence: str


@dataclass
class PhantomScan:
    target: str
    root: str
    lines: int
    findings: list = field(default_factory=list)
    verified: int = 0   # 声明且工件确实存在的条数（承诺兑现）

    @property
    def blocked(self) -> bool:
        return any(f.severity == "BLOCK" for f in self.findings)

    @property
    def exit_code(self) -> int:
        return 1 if self.blocked else 0


# ------------------------------------------------------------------ 内部


def _strip_junk(p: str) -> str:
    return p.rstrip(TRAILING_JUNK)


def _find_same_name(root: Path, name: str):
    """root 下是否存在同名文件（仅作提示，绝不作放行依据）。

    2026-10-14 实测：声明写「桌面/成果/X.html」而 --root 已是 桌面/成果 时，
    路径基准翻倍 → 直接解析必缺。工具仍报 BLOCK（严格解析，不制造假 PASS），
    但给一句可行动提示，省得人工瞎猜。
    """
    try:
        for hit in root.rglob(name):
            return str(hit)
    except OSError:
        pass
    return None


def _resolve_path(raw: str, root: Path):
    """把声明里的路径 token 解析成磁盘 Path；URL/无法解析返回 None。"""
    p = _strip_junk(raw.strip().strip("`"))
    if "://" in p:
        return None
    p_norm = p.replace("\\", "/")
    if re.match(r"^[a-zA-Z]:/", p_norm) or p_norm.startswith("/"):
        return Path(p_norm)
    return root / p_norm


def _iter_claim_lines(text: str):
    """产出 (行号, 原始行, 是否已完成声明, 去URL行)。跳过 ``` 围栏内的示例。"""
    in_fence = False
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        is_done = bool(
            CHECKBOX_DONE_RE.match(line)
            or DONE_MARK_RE.search(line)
            or any(v in line for v in DONE_VERBS_CN)
            or EN_VERB_RE.search(line)
        )
        yield i, line, is_done, URL_RE.sub(" ", line)


# ------------------------------------------------------------------ 主入口


def scan_phantoms(target: Path, root: Path | None = None) -> PhantomScan:
    target = Path(target)
    root = Path(root) if root else target.parent
    text = target.read_text(encoding="utf-8", errors="replace")
    scan = PhantomScan(target=str(target), root=str(root),
                       lines=len(text.splitlines()))
    seen = set()

    for lineno, line, is_done, line_no_url in _iter_claim_lines(text):
        for m in PATH_TOKEN_RE.finditer(line_no_url):
            raw = m.group("path")
            key = (lineno, _strip_junk(raw).lower())
            if key in seen:
                continue
            seen.add(key)
            resolved = _resolve_path(raw, root)
            if resolved is None:
                continue
            evidence = line.strip()[:160]
            if is_done:
                if resolved.exists():
                    scan.verified += 1
                else:
                    hint = ""
                    same = _find_same_name(root, resolved.name)
                    if same:
                        hint = (f"（提示：{root} 下存在同名文件 {same} —— "
                                f"可能是声明路径与 --root 基准不一致，请人工确认后修正其一）")
                    scan.findings.append(PhantomFinding(
                        rule="PHANTOM-MISSING",
                        severity="BLOCK",
                        lineno=lineno,
                        path=str(resolved),
                        message=(
                            f"声明已完成的工件不存在于磁盘：{resolved}{hint}。"
                            f"「说完成但没落地」= 沉默失败 —— 读者已信，工件却没有"
                        ),
                        evidence=evidence,
                    ))
            elif any(h in line for h in FUTURE_HINTS):
                scan.findings.append(PhantomFinding(
                    rule="PROMISE-OPEN",
                    severity="INFO",
                    lineno=lineno,
                    path=str(resolved),
                    message=f"未兑现承诺（非幻工）：声明将产出 {resolved.name}，交付时需核验",
                    evidence=evidence,
                ))

    scan.findings.sort(key=lambda f: (f.severity != "BLOCK", f.lineno))
    return scan


def render_phantom(scan: PhantomScan) -> str:
    out = []
    out.append("=" * 78)
    out.append(f"D1 幻工检测 · {scan.target}  ({scan.lines} lines, root={scan.root})")
    out.append("=" * 78)
    blocks = [f for f in scan.findings if f.severity == "BLOCK"]
    infos = [f for f in scan.findings if f.severity == "INFO"]
    if not blocks:
        out.append("  [P0] 未发现幻工：所有完成声明引用的工件均存在于磁盘。")
    for f in blocks:
        out.append(f"  [P0] PHANTOM-MISSING L{f.lineno:<5} {f.path}")
        out.append(f"       {f.message}")
        out.append(f"       > {f.evidence}")
    for f in infos:
        out.append(f"  [P2] PROMISE-OPEN   L{f.lineno:<5} {f.path}")
        out.append(f"       {f.message}")
    out.append("-" * 78)
    out.append(f"D1 合计：幻工 {len(blocks)} 处 · 未兑现承诺 {len(infos)} 条 · "
               f"承诺兑现 {scan.verified} 条")
    out.append(f"D1 退出码：{scan.exit_code}"
               f"（{'有 P0，CI 应拦截' if scan.exit_code else '通过'}）")
    out.append("-" * 78)
    return "\n".join(out)
