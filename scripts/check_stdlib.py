#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖守卫（CI gate #5 的机械执行器）

为什么需要这个脚本而不是靠 README 声明：
2026-09-28 事故 —— 批量生成 README 时一律套模板写 "Zero external
dependencies"，实情是某件在函数内部 import 了第三方库，模块级扫描
根本看不到。**宣称不符 = 我们自己造了一个专门抓的病。**

判据：silentlint/ 与 tests/ 下全部 .py（含函数内 import、含 try 分支）
的顶层模块必须 ∈ 标准库 ∪ 本仓一级模块。Python < 3.10 无
sys.stdlib_module_names 时跳过并明说（CI 矩阵最低 3.10，不会触发）。
"""
import ast
import sys
from pathlib import Path

FIRST_PARTY = {"silentlint", "engine", "phantom", "entropy"}
TARGETS = ("silentlint", "tests", "scripts")


def imports_of(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def main() -> int:
    if not hasattr(sys, "stdlib_module_names"):
        print("SKIP 零依赖守卫：Python < 3.10 无 sys.stdlib_module_names"
              "（CI 矩阵最低 3.10，不会触发此分支）")
        return 0
    std = set(sys.stdlib_module_names)
    bad = {}
    checked = 0
    for base in TARGETS:
        for p in sorted(Path(base).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            checked += 1
            stray = imports_of(p) - std - FIRST_PARTY
            if stray:
                bad[p.as_posix()] = sorted(stray)
    if bad:
        for k, v in bad.items():
            print(f"✗ {k}: 非标准库导入 {v}")
        print(f"\n{len(bad)} 个文件违反零依赖承诺（共检 {checked} 个）")
        return 1
    print(f"✓ 零第三方依赖：{checked} 个 .py 的全部 import 均为标准库或本仓模块")
    return 0


if __name__ == "__main__":
    sys.exit(main())
