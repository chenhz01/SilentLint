#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SilentLint · 统一入口
======================
eslint for AI output —— 交付前质检。

  silentlint list                      列出规则集
  silentlint scan <file|dir>           一把梭：引擎规则 + D1 幻工（按扩展名自动路由）
  silentlint phantom <file>            只跑 D1 幻工检测
  silentlint entropy <verdicts.jsonl>  只跑 D3 判决熵

设计：silentlint 层只做编排，不认识任何业务；
引擎 / 规则集 / 检测器全部可插拔（加规则 = 放一个目录，核心零改动）。

v0.1.1（2026-10-14）：
  · scan 支持目录（递归，逐文件一行摘要 + 总计）—— Dogfooding 总检的入口
  · .py 自动路由 ai-output + python-ast 双规则集（显式 --ruleset 优先）
  · --exclude 通配排除（CI 自扫需排除 tests/fixtures 里的种植 bug）
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # 让 engine 包与 rulesets 的文件级加载保持与插件期一致

try:
    from .engine.engine import Engine, EXIT_CODES
    from .engine import report as R
    from .phantom import scan_phantoms, render_phantom
    from .entropy import analyze, render_entropy, entropy_exit_code, load_verdicts
except ImportError:  # 源码直跑模式（python silentlint/cli.py）
    from engine.engine import Engine, EXIT_CODES  # noqa: F401
    from engine import report as R
    from phantom import scan_phantoms, render_phantom
    from entropy import analyze, render_entropy, entropy_exit_code, load_verdicts

VERSION = "0.1.1"

ENGINE_EXTS = (".md", ".markdown", ".html", ".htm", ".py", ".js", ".ts", ".json")
PROSE_EXTS = (".md", ".markdown", ".txt")
SUPPORTED_EXTS = ENGINE_EXTS + PROSE_EXTS

# 目录扫描时永远跳过的目录名（与产物无关的机械噪声）
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}

_STATUS_WORD = {0: "PASS", 1: "BLOCK", 2: "WARN", 3: "ERROR"}

# 严重度秩：BLOCK(1) 必须压过 WARN(2) —— 数值上 2>1，直接 max() 会让
# BLOCK 被同批次的 WARN 掩盖（目录扫描 / .py 双规则路由的聚合场景）。
_RANK = {0: 0, 2: 1, 1: 2, 3: 3}


def _worst(a: int, b: int) -> int:
    """按严重度取更坏的那个退出码（不是按数值大小）。"""
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def cmd_list() -> int:
    found = Engine.discover(HERE)
    if not found:
        print("（未发现任何规则集）")
        return 0
    print("可用规则集：")
    for name, path in found:
        try:
            eng = Engine(path, name, None)
            rs = eng._load_ruleset()
            n = len(rs.load())
            miss = rs.validate() if hasattr(rs, "validate") else []
            flag = f"   ⚠ {len(miss)} 条 check 未解析" if miss else ""
            print(f"  {name:14} {getattr(rs, 'display', ''):24} "
                  f"{n:3} 条规则  v{getattr(rs, 'version', '?')}{flag}")
        except Exception as e:
            print(f"  {name:14} ⚠ 加载失败: {type(e).__name__}: {e}")
    print("\nD1 幻工检测 / D3 判决熵：内置，无需规则集")
    return 0


def _default_rulesets(ext: str):
    """.py 双规则路由：形式体检 + 静默失败结构审计一起跑。"""
    if ext == ".py":
        return ["ai-output", "python-ast"]
    return ["ai-output"]


def _validate_rulesets(rulesets):
    for rs in rulesets:
        rs_dir = os.path.join(HERE, "rulesets", rs)
        if not os.path.isdir(rs_dir):
            print(f"✗ 未知规则集: {rs}（用 list 查看）", file=sys.stderr)
            return False
    return True


def _scan_one(target: Path, rulesets, fmt: str, root=None):
    """扫单个文件。返回退出码；不支持的扩展名返回 None。"""
    ext = target.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        return None
    code = 0
    if ext in ENGINE_EXTS:
        for rs in rulesets:
            rs_dir = os.path.join(HERE, "rulesets", rs)
            rep = Engine(rs_dir, rs, str(target)).run()
            if isinstance(rep, int):  # 引擎级故障（退出码 3）直接上传
                return rep
            if fmt == "compact":
                c = rep["counts"]
                hits = [f'{r.rule.id}:{r.effective_status}'
                        for r in rep["results"]
                        if r.effective_status in ("FAIL", "WARN", "ERROR")]
                print(f"    {rs:12} B{c['BLOCK']} W{c['WARN']} "
                      f"E{c['ERROR']}  {' '.join(hits[:4])}")
            else:
                print(R.RENDERERS[fmt](rep))
            code = _worst(code, rep["exit_code"])
    if ext in PROSE_EXTS:
        scan = scan_phantoms(target, root=Path(root) if root else target.parent)
        if fmt == "compact":
            n_block = len([f for f in scan.findings if f.severity == "BLOCK"])
            n_warn = len([f for f in scan.findings if f.severity == "WARN"])
            print(f"    {'D1-phantom':12} B{n_block} W{n_warn}")
        else:
            print()
            print(render_phantom(scan))
        code = _worst(code, scan.exit_code)
    return code


def _is_excluded(rel_posix: str, patterns) -> bool:
    for pat in patterns or []:
        p = pat.replace("\\", "/").rstrip("/")
        if fnmatch.fnmatch(rel_posix, p) or rel_posix.startswith(p + "/"):
            return True
    return False


def _scan_dir(args, root_dir: Path) -> int:
    patterns = list(args.exclude or [])
    files, skipped = [], 0
    for p in sorted(root_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root_dir).as_posix()
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        if _is_excluded(rel, patterns):
            skipped += 1
            continue
        if p.suffix.lower() in SUPPORTED_EXTS:
            files.append(p)
    tally = {"files": 0, "BLOCK": 0, "WARN": 0, "ERROR": 0, "clean": 0}
    worst = 0
    print(f"SilentLint 目录扫描：{root_dir}  （{len(files)} 个支持文件"
          + (f"，{skipped} 个被排除" if skipped else "") + "）")
    print("-" * 72)
    for p in files:
        rel = p.relative_to(root_dir).as_posix()
        ext = p.suffix.lower()
        rulesets = [args.ruleset] if args.ruleset else _default_rulesets(ext)
        code = _scan_one(p, rulesets, "compact", root=args.root or root_dir)
        if code is None:
            continue
        tally["files"] += 1
        word = _STATUS_WORD.get(code, "?")
        if code == 1:
            tally["BLOCK"] += 1
        elif code == 2:
            tally["WARN"] += 1
        elif code == 3:
            tally["ERROR"] += 1
        else:
            tally["clean"] += 1
        worst = _worst(worst, code)
        mark = {"PASS": "✓", "WARN": "!", "BLOCK": "✗", "ERROR": "✗"}[word]
        print(f"  {mark} {word:5} {rel}")
    print("-" * 72)
    print(f"总计：{tally['files']} 文件｜BLOCK {tally['BLOCK']}｜"
          f"WARN {tally['WARN']}｜ERROR {tally['ERROR']}｜"
          f"通过 {tally['clean']}｜最严重 {_STATUS_WORD.get(worst, '?')}")
    return worst


def cmd_scan(args) -> int:
    target = Path(args.target)
    if target.is_dir():
        if args.ruleset and not _validate_rulesets([args.ruleset]):
            return EXIT_CODES["error"]
        return _scan_dir(args, target)
    if not target.is_file():
        print(f"✗ 目标不是文件: {target}", file=sys.stderr)
        return EXIT_CODES["error"]
    ext = target.suffix.lower()
    rulesets = [args.ruleset] if args.ruleset else _default_rulesets(ext)
    if ext in ENGINE_EXTS and not _validate_rulesets(rulesets):
        return EXIT_CODES["error"]
    code = _scan_one(target, rulesets, args.format, root=args.root)
    if code is None:
        print(f"✗ 不支持的扩展名 {ext or '(无)'}："
              f"引擎支持 {'/'.join(ENGINE_EXTS)}，幻工检测支持 {'/'.join(PROSE_EXTS)}",
              file=sys.stderr)
        return EXIT_CODES["error"]
    return code


def cmd_phantom(args) -> int:
    target = Path(args.target)
    if not target.is_file():
        print(f"✗ 目标不是文件: {target}", file=sys.stderr)
        return EXIT_CODES["error"]
    root = Path(args.root) if args.root else target.parent
    scan = scan_phantoms(target, root=root)
    print(render_phantom(scan))
    return scan.exit_code


def cmd_entropy(args) -> int:
    path = Path(args.target)
    if not path.is_file():
        print(f"✗ 目标不是文件: {path}", file=sys.stderr)
        return EXIT_CODES["error"]
    records = load_verdicts(path)
    if not records:
        print("✗ 未读到任何合法 verdict 记录（需 JSONL，每行含 rule/status）",
              file=sys.stderr)
        return EXIT_CODES["error"]
    stats = analyze(records, min_n=args.min_n)
    print(render_entropy(stats, args.min_n))
    return entropy_exit_code(stats)


def _force_utf8_output():
    """Windows CI 实录（2026-10-14 首跑）：GitHub Windows runner 的管道
    stdout 编码是 cp1252，报告里的 ✓/✗/⛔/💥 在 print 时直接
    UnicodeEncodeError 崩栈 —— Ubuntu 三个版本全过、Windows 两个版本
    全败，本地 Git Bash（UTF-8）永远复现不了。双平台矩阵的价值实录。
    修法：入口处把 stdout/stderr 重配为 UTF-8（errors=replace 兜底），
    流不支持 reconfigure 时静默降级，绝不让输出编码决定程序生死。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _force_utf8_output()
    ap = argparse.ArgumentParser(
        prog="silentlint",
        description="eslint for AI output —— 交付前质检："
                    "抓「说完成但工件不存在」的沉默失败",
    )
    ap.add_argument("--version", action="version", version=f"silentlint {VERSION}")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出可用规则集")

    p = sub.add_parser("scan", help="一把梭：引擎规则 + D1 幻工（自动路由，支持目录）")
    p.add_argument("target")
    p.add_argument("--format", default="text",
                   choices=["text", "json", "sarif", "junit"],
                   help="引擎报告格式（目录模式固定 compact；幻工段固定 text）")
    p.add_argument("--ruleset", default=None, help="引擎规则集（默认按扩展名自动路由）")
    p.add_argument("--root", default=None,
                   help="幻工检测的工件根目录（默认：目标文件/目录所在位置）")
    p.add_argument("--exclude", action="append", default=None,
                   help="目录扫描排除的通配模式（可重复，如 'tests/fixtures'）")

    p = sub.add_parser("phantom", help="只跑 D1 幻工检测")
    p.add_argument("target")
    p.add_argument("--root", default=None)

    p = sub.add_parser("entropy", help="只跑 D3 判决熵（规则鉴别力审计）")
    p.add_argument("target", help="verdict 历史 JSONL")
    p.add_argument("--min-n", type=int, default=10,
                   help="判定「恒」所需最小样本数（默认 10）")

    args = ap.parse_args(argv)
    if args.cmd in (None, "list"):
        return cmd_list()
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "phantom":
        return cmd_phantom(args)
    if args.cmd == "entropy":
        return cmd_entropy(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
