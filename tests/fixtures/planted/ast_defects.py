#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""planted fixture：四类静默失败结构缺陷全种植（供 python-ast 规则集反例断言）。
每条规则至少一处；期望：R1 FAIL(BLOCK) + R2 FAIL + R3 FAIL + R4 WARN + PARSE PASS。
"""
import logging
import traceback

logger = logging.getLogger(__name__)


def load_config(resp):
    # R1：错误字段默认值陷阱 —— 上游回 {"message": ""} 时默认值静默失效。
    # 注意：取值点下文没有任何空值防护（无 if not / or 兜底），
    # 否则按规则语义应视为「已做空即未提供处理」而不触发。
    msg = resp.get("message", "unknown error")
    logger.error("config load failed")  # 错误文本已丢，只剩一句无信息日志
    return msg


def merge_results(a, b):
    # R2：归因黑洞 —— 4 条空返回分支，调用方无法区分失败原因
    if a is None:
        logger.error("a missing")
        return None
    if b is None:
        logger.error("b missing")
        return None
    if a == b:
        return None
    return a + b


def cancel_path(hook_says_abort, skip_requested):
    # R4：正常中止与真实失败共用返回通道（abort/skip 语义）。
    # 两条空返回分支让 R2 也以 MEDIUM 命中（2 条 → WARN）。
    if hook_says_abort:
        logger.error("abort by hook")
        return None
    if skip_requested:
        logger.error("skip by user request")
        return None
    return "ok"


def risky():
    try:
        return 1 / 0
    except ZeroDivisionError:
        # R3：堆栈降级 —— 生产 INFO 运行下堆栈永久不可见
        logger.debug(traceback.format_exc())
        return None


def guarded(resp):
    # 对照组：已做空值防护，不应触发 R1
    msg = resp.get("message") or resp.get("error") or "fallback"
    return msg
