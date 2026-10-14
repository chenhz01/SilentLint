#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SilentLint 测试套件（unittest，零第三方依赖）

测试哲学：每个检测器必须先在「种植了 bug 的 fixture」上抓到问题，
再在「干净对照件」上不误报 —— 抓不到真问题的工具比没有工具更危险。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "silentlint"))

from phantom import scan_phantoms                    # noqa: E402
from entropy import analyze, entropy_exit_code       # noqa: E402
from engine.engine import Engine                     # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


class TestPhantom(unittest.TestCase):
    """D1：幻工检测 —— 产品名的核心卖出点"""

    def test_detects_missing_artifacts(self):
        scan = scan_phantoms(FIX / "phantom_claims.md", root=FIX / "planted")
        blocks = [f for f in scan.findings if f.severity == "BLOCK"]
        paths = " ".join(f.path for f in blocks)
        self.assertIn("missing_report.html", paths)
        self.assertIn("missing_notes.md", paths)
        self.assertIn("test_all.py", paths)
        self.assertEqual(scan.exit_code, 1)

    def test_existing_artifact_not_flagged(self):
        """exists.py 真实存在 → 承诺兑现，不得误报"""
        scan = scan_phantoms(FIX / "phantom_claims.md", root=FIX / "planted")
        self.assertFalse(any("exists.py" in f.path for f in scan.findings))
        self.assertGreaterEqual(scan.verified, 1)

    def test_url_reference_not_flagged(self):
        """URL 引用不算本地声明"""
        scan = scan_phantoms(FIX / "phantom_claims.md", root=FIX / "planted")
        self.assertFalse(any("example.com" in f.path for f in scan.findings))

    def test_clean_file_no_block(self):
        scan = scan_phantoms(FIX / "clean_claims.md", root=FIX / "planted")
        self.assertFalse(scan.blocked)
        self.assertEqual(scan.exit_code, 0)
        self.assertGreaterEqual(scan.verified, 2)

    def test_fenced_code_example_skipped(self):
        """``` 围栏内的示例文本不算声明"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "readme_like.md"
            p.write_text("# doc\n\n```\n- [x] 已创建 ghost.py\n```\n",
                         encoding="utf-8")
            scan = scan_phantoms(p, root=Path(td))
            self.assertFalse(scan.blocked)


    def test_path_base_mismatch_gives_hint(self):
        """路径基准翻倍（声明写 td/x.html 而 root 已含 td/）：
        仍报 BLOCK（严格解析，绝不制造假 PASS），但消息带同名文件提示"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "real.html").write_text("x", encoding="utf-8")
            doc = root / "claims2.md"
            doc.write_text("✅ 已生成 td/real.html\n", encoding="utf-8")
            (root / "td").mkdir()
            scan = scan_phantoms(doc, root=root)
            blocks = [f for f in scan.findings if f.severity == "BLOCK"]
            self.assertEqual(len(blocks), 1)
            self.assertIn("同名文件", blocks[0].message)


class TestEntropy(unittest.TestCase):
    """D3：判决熵 —— 恒 PASS 的规则等于没有规则"""

    def test_flags_zero_discrimination(self):
        records = [json.loads(l) for l in
                   (FIX / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()
                   if l.strip()]
        stats = {s.rule: s for s in analyze(records, min_n=10)}
        self.assertEqual(stats["RUBBER_STAMP"].flag, "ALWAYS_PASS")
        self.assertEqual(stats["BROKEN_RULE"].flag, "ALWAYS_FAIL")
        self.assertEqual(stats["HEALTHY_RULE"].flag, "")
        self.assertEqual(entropy_exit_code(list(stats.values())), 2)

    def test_min_n_guard(self):
        """样本不足时不判「恒」—— 宁可漏判，不可错杀"""
        records = [{"rule": "FEW", "status": "PASS"}] * 3
        stats = analyze(records, min_n=10)
        self.assertEqual(stats[0].flag, "")


class TestRuleRegressions(unittest.TestCase):
    """狗粮咬出来的规则级回归（2026-10-14 自扫商业模式文档时实测命中）"""

    def test_absolute_excludes_compound(self):
        """「绝对化」是规则名不是承诺 —— 复合词不得误报"""
        from engine import schema as S
        hits = S.find_absolute("昨日方案 2 处无源/绝对化表述需修正")
        self.assertEqual(hits, [])

    def test_absolute_still_catches_real_claim(self):
        from engine import schema as S
        hits = S.find_absolute("这个方案绝对没问题")
        self.assertTrue(hits)

    def test_source_hint_accepts_estimate_marker(self):
        """[预估]/假设 标记 = 有效出处（对齐 F11 无源数字铁律的预估约定）"""
        from engine import schema as S
        self.assertTrue(S.SOURCE_HINT.search("转化率 3% [预估]"))
        self.assertTrue(S.SOURCE_HINT.search("收入约 2 万（假设）"))
        self.assertFalse(S.SOURCE_HINT.search("转化率达到 47%"))

    def test_code_span_not_counted_as_claim(self):
        """产品级 FP 类：文档讲解规则本身（`TODO`/`绝对` 在代码引用里），
        不得判成真论断。2026-10-14 交付报告自扫实测命中后修复。"""
        from engine import adapters
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            md = Path(td) / "doc_rules.md"
            md.write_text(
                "# 规则讲解\n\n我们检查 `TODO` 占位符和 `绝对` 化模式。\n"
                "转化率达到 47%。\n", encoding="utf-8")
            d = adapters.adapt(str(md))
            joined = " ".join(c["text"] for c in d.get("claims") or [])
            self.assertNotIn("TODO", joined)
            self.assertNotIn("绝对", joined)
            self.assertIn("47%", joined)  # 正文真论断照样抓
            html_f = Path(td) / "doc_rules.html"
            html_f.write_text(
                "<html><body><p>检查 <code>TODO</code> 与 <code>绝对</code> 模式，"
                "转化率 47%。</p></body></html>", encoding="utf-8")
            d2 = adapters.adapt(str(html_f))
            joined2 = " ".join(c["text"] for c in d2.get("claims") or [])
            self.assertNotIn("TODO", joined2)
            self.assertNotIn("绝对", joined2)


class TestEngineIntegration(unittest.TestCase):
    """引擎 + ai-output 规则集集成：占位符必须 BLOCK（回归假绿守卫）"""

    def test_placeholder_blocks(self):
        rs_dir = ROOT / "silentlint" / "rulesets" / "ai-output"
        rep = Engine(str(rs_dir), "ai-output", str(FIX / "placeholder.md")).run()
        self.assertIsInstance(rep, dict)
        self.assertGreaterEqual(rep["counts"]["BLOCK"], 1)
        self.assertEqual(rep["exit_code"], 1)

    def test_html_adapter_path(self):
        """回归：html 适配器曾因推导式变量名笔误（u vs m）整路 NameError，
        任何 .html 目标都过不了引擎。此测试锁死该路径。"""
        rs_dir = ROOT / "silentlint" / "rulesets" / "ai-output"
        rep = Engine(str(rs_dir), "ai-output", str(FIX / "page.html")).run()
        self.assertIsInstance(rep, dict)
        self.assertIn("exit_code", rep)
        self.assertNotEqual(rep["exit_code"], 3)  # 3 = 引擎故障


class TestCli(unittest.TestCase):
    """端到端：退出码契约是 CI 集成的生命线"""

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, str(ROOT / "silentlint" / "cli.py"), *argv],
            capture_output=True, text=True, encoding="utf-8",
        )

    def test_list(self):
        r = self._run("list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("ai-output", r.stdout)

    def test_scan_block_on_placeholder(self):
        r = self._run("scan", str(FIX / "placeholder.md"))
        self.assertEqual(r.returncode, 1)

    def test_scan_phantom_block(self):
        r = self._run("scan", str(FIX / "phantom_claims.md"),
                      "--root", str(FIX / "planted"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("PHANTOM-MISSING", r.stdout)

    def test_phantom_exit_code(self):
        r = self._run("phantom", str(FIX / "phantom_claims.md"),
                      "--root", str(FIX / "planted"))
        self.assertEqual(r.returncode, 1)

    def test_entropy_exit_code(self):
        r = self._run("entropy", str(FIX / "verdicts.jsonl"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("ALWAYS_PASS", r.stdout)

    def test_missing_target_exit_3(self):
        r = self._run("scan", str(FIX / "no_such_file.md"))
        self.assertEqual(r.returncode, 3)


    def test_json_array_root_fatal_not_crash(self):
        """根节点为数组的 JSON：引擎必须干净地 _fatal（int 3），不许崩栈。
        2026-10-14 dogfooding 总检实测：28 个数组根 JSON 全崩在
        _summarize 的 target.get('title')（AttributeError 上抛）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "arr.json"
            p.write_text('[{"a": 1}, {"b": 2}]', encoding="utf-8")
            rs_dir = ROOT / "silentlint" / "rulesets" / "ai-output"
            rep = Engine(str(rs_dir), "ai-output", str(p)).run()
            self.assertEqual(rep, 3)  # int = 引擎级故障（不是 dict 通过结论）

    def test_cli_json_array_exit_3_no_traceback(self):
        """CLI 层：数组根 JSON → exit 3 + stderr 有说明，且无 Traceback"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "arr2.json"
            p.write_text('[1, 2, 3]', encoding="utf-8")
            r = subprocess.run(
                [sys.executable, str(ROOT / "silentlint" / "cli.py"), "scan",
                 str(p)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(r.returncode, 3)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("根节点", r.stderr)


class TestPythonAst(unittest.TestCase):
    """python-ast 规则集：四类静默失败结构缺陷（源自 305 行 AST 审计器）"""

    RS = ROOT / "silentlint" / "rulesets" / "python-ast"

    def _run_rs(self, target):
        return Engine(str(self.RS), "python-ast", str(target)).run()

    def test_planted_defects_all_caught(self):
        rep = self._run_rs(FIX / "planted" / "ast_defects.py")
        self.assertIsInstance(rep, dict)
        by_rule = {r.rule.id: r.effective_status for r in rep["results"]}
        self.assertEqual(by_rule["DEFAULT-TRAP"], "FAIL")      # R1 高危 → BLOCK
        self.assertEqual(by_rule["ATTR-BLACKHOLE"], "FAIL")    # R2 4 分支 → BLOCK
        self.assertEqual(by_rule["TRACEBACK-DEMOTION"], "FAIL")  # R3 → BLOCK
        self.assertEqual(by_rule["ABORT-CHANNEL"], "WARN")     # R4 → WARN
        self.assertEqual(by_rule["PARSE-FAIL"], "PASS")
        self.assertEqual(rep["exit_code"], 1)

    def test_clean_file_no_false_positive(self):
        rep = self._run_rs(FIX / "clean_ast.py")
        self.assertEqual(rep["counts"]["BLOCK"], 0)
        self.assertEqual(rep["counts"]["WARN"], 0)
        self.assertEqual(rep["exit_code"], 0)

    def test_auto_route_py_runs_both_rulesets(self):
        """.py 无显式 --ruleset 时，CLI 自动跑 ai-output + python-ast"""
        r = subprocess.run(
            [sys.executable, str(ROOT / "silentlint" / "cli.py"), "scan",
             str(FIX / "planted" / "ast_defects.py")],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ai-output", r.stdout)
        self.assertIn("python-ast", r.stdout)

    def test_non_python_target_all_skip_guard(self):
        """对 .md 跑 python-ast：全 SKIP → 引擎守卫报 ERROR（exit_code 3），
        绝不静默放行 —— 假绿守卫在跨规则集误用场景下的实测"""
        rep = self._run_rs(FIX / "placeholder.md")
        self.assertIsInstance(rep, dict)
        self.assertEqual(rep["exit_code"], 3)
        self.assertGreaterEqual(rep["counts"]["ERROR"], 1)

    def test_parse_fail_on_broken_python(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "broken.py"
            p.write_text("def f(:\n    pass\n", encoding="utf-8")
            rep = self._run_rs(p)
            by_rule = {r.rule.id: r.effective_status for r in rep["results"]}
            self.assertEqual(by_rule["PARSE-FAIL"], "FAIL")
            # 其余规则必须 SKIP 而不是误报
            self.assertEqual(by_rule["DEFAULT-TRAP"], "SKIP")

    def test_ruleset_validate_resolves_all_checks(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "rs_pyast", self.RS / "ruleset.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.RuleSet.validate(), [])
        self.assertEqual(len(mod.RuleSet.load()), 5)


class TestCodeTargetFPs(unittest.TestCase):
    """狗粮咬出的代码目标 FP 家族（2026-10-14 自扫 8 BLOCK 实测）

    三个修复各自的回归锁：
    ① 括号平衡走 tokenize（文档串/正则/f-string 引号错位不再误报）
    ② 占位符只扫注释（规则集源码自带 TBD/XXX 模式词不再误报）
    ③ 论断检查对代码目标显式 SKIP（规则讲解文字不再被当断言）
    """

    def _adapt(self, name, src):
        import tempfile
        td = tempfile.mkdtemp()
        p = Path(td) / name
        p.write_text(src, encoding="utf-8")
        from engine import adapters
        return str(p), adapters.adapt(str(p))

    def test_bracket_tokenize_exact_on_tricky_source(self):
        """文档串里引号错位 + 正则字面量：启发式会误报，tokenize 必须全零。"""
        src = (
            'def f(a, b):\n'
            '    """doc with "quoted" text and don\'t worry\n'
            '    about { braces ) misfire ( here\n'
            '    """\n'
            '    pat = r"\\(\\)|\\[\\]|\\{\\}"\n'
            '    items = {"k": [1, 2], "j": (3,)}\n'
            '    return a + b, pat, items\n'
        )
        _, d = self._adapt("tricky.py", src)
        self.assertTrue(d["tokenize_ok"])
        self.assertEqual(d["bracket_balance"], {"(": 0, "[": 0, "{": 0})

    def test_placeholder_scans_comments_not_strings(self):
        """模式词表在字符串里（数据）→ 不判占位符；注释里 TODO → 真命中。"""
        src = (
            'PATTERNS = ["TBD", "XXX", "{{}}"]  # 模式词表自身\n'
            '# TODO: real marker here\n'
            'def g():\n    return 1\n'
        )
        path, _ = self._adapt("vocab.py", src)
        rs_dir = ROOT / "silentlint" / "rulesets" / "ai-output"
        rep = Engine(str(rs_dir), "ai-output", path).run()
        by = {r.rule.id: r.effective_status for r in rep["results"]}
        self.assertEqual(by["PLACEHOLDER"], "FAIL")
        detail = next(r.detail for r in rep["results"]
                      if r.rule.id == "PLACEHOLDER")
        self.assertIn("第2行", detail)  # 行号必须指向注释，不是字符串行

    def test_claim_rules_skip_on_code_target(self):
        """代码目标：CLAIM-* 显式 SKIP（没查就没查，不制造假 PASS）"""
        src = 'x = "转化率达到 47%，一定能赚"  # 字符串里的绝对化教学例句\n'
        path, _ = self._adapt("claims.py", src)
        rs_dir = ROOT / "silentlint" / "rulesets" / "ai-output"
        rep = Engine(str(rs_dir), "ai-output", path).run()
        by = {r.rule.id: r.effective_status for r in rep["results"]}
        self.assertEqual(by["CLAIM-NO-SOURCE"], "SKIP")
        self.assertEqual(by["CLAIM-ABSOLUTE"], "SKIP")

    def test_bracket_skips_when_tokenize_fails(self):
        """不可解析的 .py：括号检查 SKIP（不是 PASS）；PARSE-FAIL 负责 BLOCK"""
        src = 'def broken(:\n    pass\n'
        path, d = self._adapt("broken2.py", src)
        self.assertFalse(d["tokenize_ok"])
        rs_dir = ROOT / "silentlint" / "rulesets" / "ai-output"
        rep = Engine(str(rs_dir), "ai-output", path).run()
        by = {r.rule.id: r.effective_status for r in rep["results"]}
        self.assertEqual(by["BRACKET-UNBALANCED"], "SKIP")


class TestCliDirScan(unittest.TestCase):
    """目录扫描（Dogfooding 总检入口）+ exclude + compact 汇总"""

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, str(ROOT / "silentlint" / "cli.py"), *argv],
            capture_output=True, text=True, encoding="utf-8")

    def test_dir_scan_aggregates_and_exit_code(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "bad.md").write_text(
                "# t\n\n待补充：TODO 占位\n", encoding="utf-8")
            r = self._run("scan", str(root))
            self.assertEqual(r.returncode, 1)  # bad.md 有 BLOCK 占位符
            self.assertIn("总计", r.stdout)
            self.assertIn("bad.md", r.stdout)

    def test_dir_exclude_pattern(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fixtures").mkdir()
            (root / "fixtures" / "bad.md").write_text(
                "# t\n\nTODO\n", encoding="utf-8")
            (root / "good.md").write_text(
                "# ok\n\n## 一\n\n" + "\n".join(
                    ["干净内容，无占位符无绝对化表述，段落足够长。"] * 5)
                + "\n\n## 二\n\n" + "\n".join(
                    ["干净内容，无占位符无绝对化表述，段落足够长。"] * 5)
                + "\n\n## 三\n\n" + "\n".join(
                    ["干净内容，无占位符无绝对化表述，段落足够长。"] * 5) + "\n",
                encoding="utf-8")
            r = self._run("scan", str(root), "--exclude", "fixtures")
            self.assertEqual(r.returncode, 0)
            self.assertIn("1 个被排除", r.stdout)

    def test_dir_skips_pycache_and_git(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "junk.md").write_text("TODO\n",
                                                          encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "cfg.md").write_text("TODO\n", encoding="utf-8")
            r = self._run("scan", str(root))
            self.assertEqual(r.returncode, 0)
            self.assertNotIn("junk.md", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
