# SilentLint

**eslint for AI output** — a pre-delivery audit that catches *silent failures*:
the AI said "done" but the artifact is not on disk; the log printed `error=`
and the error text was already gone at the assignment line.

## The failure mode nobody sees

Four detectors, one CLI, zero dependencies:

| # | Detector | Catches | Determinism |
|---|---|---|---|
| D1 | **phantom** | "Artifact generated" — but the file does not exist on disk | path resolution |
| D2 | **ai-output ruleset** (9 rules) | placeholders left in, unsourced numbers, unverifiable absolutes, credential leaks, PII, broken structure | regex + structure |
| D3 | **verdict entropy** | rules that always PASS — rubber stamps with zero discrimination | pass-rate + entropy |
| D4 | **python-ast ruleset** (5 rules) | `.get("error", default)` traps, multi-branch `return None` blackholes, tracebacks demoted to DEBUG | stdlib `ast` + `tokenize` |

Every rule is deterministic: no LLM judge, no network, no SDK. Your data never leaves your machine.

The python-ast rules come from a public case study (MaiBot issue #2052) in which production logs lied for three structural reasons — not because they were too sparse, but because they were wrong.

## 30-second start

```bash
git clone https://github.com/chenhz01/silentlint
cd silentlint
python silentlint/cli.py scan path/to/report.md      # one file
python silentlint/cli.py scan docs/ --exclude tests  # a whole directory
python silentlint/cli.py list                        # all rulesets
```

Exit codes (the CI contract): `0` clean, `1` BLOCK, `2` WARN, `3` engine fault.

```yaml
# .github/workflows/gate.yml — fail the build on BLOCK, not on WARN
- run: python silentlint/cli.py scan . --exclude tests/fixtures
```

Python 3.9 or newer. Zero third-party dependencies. 35 tests, about 2.5 seconds:
`python -m unittest discover -s tests`.

## Why not just use something else

| Tool | What it does | Why SilentLint is different |
|---|---|---|
| Raindrop, Langfuse, Opik | runtime monitoring of agents in production; SDK and cloud required | SilentLint audits *before* delivery. No SDK, no traffic, local-only |
| RAGAS, DeepEval | eval suites with LLM judges; subjective scores | SilentLint is deterministic. A BLOCK means "certainly wrong", not "probably iffy" |
| ESLint, Pylint | code style | SilentLint audits *AI output*: the docs, reports and code your agent claims to have finished |

## Dogfooding receipt

On 2026-10-14 we scanned 10,173 files across our own artifact tree. Our own deliverables contained **zero** genuinely missing artifacts — and SilentLint found 5 real bugs in itself, including an aggregation bug where a WARN masked a BLOCK.

All 5 are fixed, each with a regression test. A ruler that bites its owner is a ruler you can lend out.

## Writing a ruleset

A ruleset is one directory with a `ruleset.py` that exposes a `RuleSet` class whose `load()` returns `Rule` objects. Drop the directory into `silentlint/rulesets/` and the engine discovers it — no core code changes. `silentlint/rulesets/python-ast/ruleset.py` is a complete worked example.

## What is deliberately not in this repo

| Layer | What it is | Where it lives |
|---|---|---|
| Open | engine, D1/D3 detectors, ai-output and python-ast rulesets, 35 tests, CI | this repo, MIT |
| Partner | vertical rule packs (e-commerce, music, short-video, design), team CI gates, custom rule development | paid tiers; interfaces documented here, rule bodies not shipped |
| Never | calibration data, threshold provenance, internal audit ledgers | not in this repo, not in any repo |

Why the repo version is not enough for the Partner layer: the generic rules check form and traceability; the vertical packs encode domain operations that took real client projects to learn (listing claims, price consistency, release metadata). The free core is genuinely free — read it, run it, fork it. We will never chase you.

## Roadmap

- `silentlint: ignore` line convention, eslint-style
- PyPI listing (clone-and-run until then)
- rule pack registry with versioned interfaces

## Collaboration — the 10% that isn't code

Read it, run it, fork it: the free core stays free and we will never chase you.

**What is behind the lock** — each item exists because the repo version has a specific gap:

1. **Vertical rule packs.** The repo ships generic AI-output rules. The paid packs encode domain operations (cross-border listing claims, price consistency, release metadata) learned from real client work. Interface: drop-in ruleset directories, documented in the engine README section.
2. **Team gates.** Shared verdict ledgers, org-wide baselines, private rule hosting. The repo version is single-machine by design — your verdicts stay yours.
3. **Custom rules.** Built against your domain and your review process. You own the result.

None of these are in this repository. The open list above is the complete inventory of what is here.

**How to ask.** Send two things: (1) what you are building and what the prompts or agents do inside it; (2) which item you want and what you have already tried. One-line messages get one-line replies — the filtering cost sits with the writer, and that is deliberate.

**Is this you?**

- A fit: you ship AI-generated docs, reports or code to clients or users; you run agents in CI; you have been burned by "it said done" before.
- Not a fit: you want a runtime APM (use Langfuse, Opik or Raindrop); you need subjective LLM scoring (use RAGAS or DeepEval); you need a hosted SaaS today (we are pre-release, clone-and-run only).

**The closing hook.** Open an issue describing a failure mode you have actually seen — the sharper the failure, the better. The sharpest ones go into the next release, with credit.

Contact: hcac4735@agent.qq.com (48h response target) or shanlun2029@outlook.com. The qq.com address has a 50-per-day send cap and overseas deliverability is unverified; the outlook.com address is the backup channel.

## License

MIT — Copyright (c) 2026 SilentLint contributors
