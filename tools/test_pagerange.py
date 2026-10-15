# -*- coding: utf-8 -*-
"""
页码范围解析的回归测试

针对 v1.0.0 的致命崩溃：
    App.start() 里用 parse_page_range(spec, 10**9) 做格式校验，
    而 spec="all"（界面默认值）会走 list(range(10**9)) 分支
    → 先卡死约 70 秒 → MemoryError。
    即所有用户第一次点「开始处理」都会崩溃。

本测试确保：
    ① 校验走 validate_page_range，与 total 无关、恒为 O(字符串长度)
    ② parse_page_range 即使收到荒谬的 total 也不会撑爆内存
    ③ 正常解析结果与修复前完全一致（无回归）
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app.engine import parse_page_range, validate_page_range   # noqa: E402

FAILED: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILED.append(name)


print("=" * 66)
print("① 语法校验（与 total 无关，必须瞬时完成）")
print("=" * 66)
for spec in ["", "all", "全部", "*", "  ALL  ", "1-3,5", " 1 - 3 , 5 ", "5", "100"]:
    ok, why = validate_page_range(spec)
    check(f"接受 {spec!r}", ok, why)

print()
for spec in ["abc", "1-x", "a-b", "1--3", "1,,x", "第3页", "1.5", "1_0"]:
    ok, why = validate_page_range(spec)
    check(f"拒绝 {spec!r}", not ok, f"却返回了合法：{why!r}")

print()
print("=" * 66)
print("② 崩溃复现验证：spec='all' + 荒谬 total（v1.0.0 的崩溃点）")
print("=" * 66)
for total in (10 ** 9, 10 ** 12, -5, 0):
    t = time.perf_counter()
    try:
        r = parse_page_range("all", total)
        dt = time.perf_counter() - t
        from app.engine import MAX_PAGE_RANGE
        check(f"parse_page_range('all', {total}) 不爆内存且够快",
              dt < 1.0 and len(r) <= MAX_PAGE_RANGE,
              f"用时 {dt:.2f}s，返回 {len(r)} 项")
    except MemoryError:
        check(f"parse_page_range('all', {total}) 不爆内存", False, "抛了 MemoryError")

t = time.perf_counter()
validate_page_range("all")
dt = time.perf_counter() - t
check(f"validate_page_range('all') 瞬时完成（{dt * 1000:.3f} ms）", dt < 0.05)

t = time.perf_counter()
try:
    parse_page_range("1-999999999", 10 ** 9)
    dt = time.perf_counter() - t
    check(f"超长区间被截断（{dt * 1000:.0f} ms）", dt < 2.0)
except MemoryError:
    check("超长区间被截断", False, "抛了 MemoryError")

print()
print("=" * 66)
print("③ 正常解析无回归")
print("=" * 66)
cases = [
    ("all", 3, [0, 1, 2]),
    ("", 3, [0, 1, 2]),
    ("全部", 3, [0, 1, 2]),
    ("1", 5, [0]),
    ("3", 5, [2]),
    ("9", 5, []),                 # 超出总页数 → 空
    ("1-3", 5, [0, 1, 2]),
    ("3-1", 5, [0, 1, 2]),        # 逆序自动纠正
    ("1-99", 5, [0, 1, 2, 3, 4]),  # 自动截断到总页数
    ("1-3,5,8-10", 10, [0, 1, 2, 4, 7, 8, 9]),
    (" 1-3 , 5 ", 10, [0, 1, 2, 4]),
    ("1-3，5", 10, [0, 1, 2, 4]),   # 中文逗号
    ("2,2,2", 5, [1]),              # 去重
    ("abc", 5, []),                 # 非法片段忽略
    ("0", 5, []),                   # 页码从 1 开始
    ("-1", 5, []),
]
for spec, total, expect in cases:
    got = parse_page_range(spec, total)
    check(f"parse_page_range({spec!r}, {total}) == {expect}", got == expect,
          f"实际 {got}")

print()
print("=" * 66)
print(f"结果：{'全部通过 ✓' if not FAILED else f'{len(FAILED)} 项失败 ✗'}")
print("=" * 66)
sys.exit(1 if FAILED else 0)