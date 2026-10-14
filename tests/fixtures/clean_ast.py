#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""clean fixture：python-ast 规则集正例 —— 五条规则全部 PASS（exit 0）。
同时满足 ai-output 对 .py 的形式检查（含 SIZE-ANOMALY 的 20 行下限）。
"""
import traceback


def load_message(resp):
    """空值防护到位的取用点：空串/缺失都会走到显式分支。"""
    msg = resp.get("message") or resp.get("error") or ""
    if not msg:
        return None
    return msg


def safe_divide(a, b):
    """单条空返回 + 显式成功路径：调用方靠 raise 区分失败，不靠 None。"""
    if b == 0:
        raise ValueError("divisor is zero")
    return a / b


def render_error(exc):
    """堆栈在 ERROR 级记录：生产 INFO 运行下依然可见。"""
    logger.error("operation failed: %s", traceback.format_exc())
    return str(exc)


def main():
    resp = {"message": "ok"}
    msg = load_message(resp)
    print(msg)
    print(safe_divide(1, 2))
    return msg


if __name__ == "__main__":
    main()
