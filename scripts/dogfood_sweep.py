#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dogfooding 总检（scripts/dogfood_sweep.py）

在一个目录树的所有支持文件上跑 SilentLint 全量检测，产出结构化汇总：
  · 每文件状态（PASS/WARN/BLOCK/ERROR）
  · 规则命中分布（哪些规则在真实工件上开火）
  · Top 发现清单（带文件+行号+证据）

用途：① 产品的 North Star Metric（被抓到的真实问题数）第一次大读数
      ② 发布前的自我体检 ③ 规则阈值的真实语料标定数据来源。

与 CLI 的关系：本脚本走库 API（Engine/scan_phantoms）求速度与结构化，
CLI 的退出码契约由 tests/test_all.py::TestCli 独立验证，两不相干。
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "silentlint"))

from engine.engine import Engine                       # noqa: E402
from phantom import scan_phantoms                      # noqa: E402
from entropy import analyze, entropy_exit_code         # noqa: E402,F401

ENGINE_EXTS = (".md", ".markdown", ".html", ".htm", ".py", ".js", ".ts", ".json")
PROSE_EXTS = (".md", ".markdown", ".txt")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
RANK = {0: "PASS", 1: "BLOCK", 2: "WARN", 3: "ERROR"}


def sweep(root: Path, exclude_patterns=()):
    files, per_file, rule_hits, findings = [], [], Counter(), []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        if any(rel.startswith(e.rstrip("/") + "/") or rel == e.rstrip("/")
               for e in exclude_patterns):
            continue
        if p.suffix.lower() in ENGINE_EXTS + PROSE_EXTS:
            files.append((rel, p))

    for rel, p in files:
        ext = p.suffix.lower()
        status, detail = 0, []
        try:
            if ext in ENGINE_EXTS:
                rulesets = (["ai-output", "python-ast"] if ext == ".py"
                            else ["ai-output"])
                for rs in rulesets:
                    rs_dir = ROOT / "silentlint" / "rulesets" / rs
                    rep = Engine(str(rs_dir), rs, str(p)).run()
                    if isinstance(rep, int):
                        status = max(status, 3)
                        detail.append(f"{rs}:ENGINE_ERROR")
                        continue
                    status = max(status, rep["exit_code"])
                    for r in rep["results"]:
                        st = r.effective_status
                        if st in ("FAIL", "WARN", "ERROR"):
                            rule_hits[f"{rs}:{r.rule.id}"] += 1
                            findings.append({
                                "file": rel, "ruleset": rs,
                                "rule": r.rule.id, "status": st,
                                "line": None,
                                "detail": (r.detail or "")[:200]})
            if ext in PROSE_EXTS:
                sc = scan_phantoms(p, root=root)
                status = max(status, sc.exit_code)
                for f in sc.findings:
                    rule_hits[f"D1:{f.rule}"] += 1
                    findings.append({
                        "file": rel, "ruleset": "D1-phantom",
                        "rule": f.rule, "status": f.severity,
                        "line": f.lineno,
                        "detail": (f.message or "")[:200]})
        except Exception as e:  # 总检脚本自身不许静默失败
            status = 3
            detail.append(f"SWEEP_ERROR:{type(e).__name__}:{e}")
        per_file.append({"file": rel, "status": RANK[status],
                         "detail": ";".join(detail)})

    tally = Counter(x["status"] for x in per_file)
    return {
        "root": str(root),
        "files_scanned": len(files),
        "tally": dict(tally),
        "rule_hits": dict(rule_hits.most_common()),
        "top_findings": findings[:60],
        "per_file": per_file,
    }


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT.parent)
    excludes = sys.argv[2:] or []
    if "SilentLint/tests/fixtures" not in excludes:
        excludes = excludes + ["SilentLint/tests/fixtures"]
    result = sweep(root, excludes)
    out = ROOT / "dogfood_sweep_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    t = result["tally"]
    print(f"总检根目录：{result['root']}")
    print(f"扫描文件数：{result['files_scanned']}（排除：{excludes}）")
    print(f"结果分布：BLOCK {t.get('BLOCK', 0)}｜WARN {t.get('WARN', 0)}｜"
          f"ERROR {t.get('ERROR', 0)}｜PASS {t.get('PASS', 0)}")
    print(f"规则命中 Top15：")
    for k, v in list(result["rule_hits"].items())[:15]:
        print(f"  {k:38} {v}")
    print(f"\n完整结果：{out}")
    blocks = [f for f in result["top_findings"]
              if f["status"] in ("BLOCK", "HIGH")]
    print(f"\nBLOCK 级发现（前 20，共 {sum(1 for x in result['per_file'] if x['status'] == 'BLOCK')} 个文件）：")
    for f in blocks[:20]:
        loc = f"L{f['line']}" if f["line"] else ""
        print(f"  [{f['ruleset']}:{f['rule']}] {f['file']} {loc}")
        print(f"      {f['detail'][:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
