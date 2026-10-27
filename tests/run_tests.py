# -*- coding: utf-8 -*-
"""测试运行器。

权威跑法（本机统一走 Blender 自带 Python）::

    blender.exe --factory-startup --background --python tests/run_tests.py

纯 Python 3 也能跑**零 bpy** 的那一层（模型、几何、规则、区域、存放、监听，
以及导入安全性），便于快速迭代::

    python tests/run_tests.py

需要 bpy 的用例按 ``HAVE_BPY`` 跳过，并在结尾按原因归并打印——快，但不权威：
装配、键位挂载、真机区域坐标、承载条带都在跳过之列，收尾必须再跑 Blender 那条。

退出码：全部通过 0，否则 1；并打印 ``RESULT: OK`` / ``RESULT: FAILED`` 供脚本判定。
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_HERE, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _skip_reasons(result):
    """按原因归并跳过项（票 04：跳过时必须能一眼看到"为什么没跑"）。"""
    counts = {}
    for _test, reason in result.skipped:
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def main():
    try:
        import bpy  # noqa: F401

        print("[runner] 运行于 Blender：%s" % sys.version.split()[0])
    except ImportError:
        print("[runner] 运行于纯 Python（bpy 相关测试将被跳过）：%s" % sys.version.split()[0])

    suite = unittest.TestLoader().discover(_HERE, pattern="test_*.py", top_level_dir=_HERE)
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    skipped = len(result.skipped)
    print("\n[runner] 用例 %d，失败 %d，跳过 %d" % (total, failed, skipped))
    for reason, count in _skip_reasons(result).items():
        print("[runner]   跳过 %d 项：%s" % (count, reason))
    if result.wasSuccessful():
        print("RESULT: OK")
        return 0
    print("RESULT: FAILED")
    return 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.exit(code)
