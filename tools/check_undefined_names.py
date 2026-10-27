# -*- coding: utf-8 -*-
"""pyflakes 未定义名门禁（v1.5.1 起进验证链）。

起因：v1.5.0 的段菜单 ``draw`` 里用了未导入的 ``TYPE_LABELS``，
``NameError`` 在真机上让"右键线段弹不出菜单"存活了一个版本——单元测试
没画过菜单、pyflakes 不在验证链里，两层都漏了。本脚本把后者钉死。

用法（统一用 Blender 自带 Python 或托管 Python）::

    python tools/check_undefined_names.py

门禁：``undefined name`` 报告**只允许出现在 ``seqvel/ui.py``**——那里的
命中是 pyflakes 对 ``class X: prop: bpy.props.EnumProperty(items=(...))``
注解式属性里 items 元组字符串的已知误报（把字面量当成名字；真伪由 Blender
套件仲裁）。**其它任何文件的未定义名一律红**。

依赖：``pip install pyflakes``（纯 Python，不挑解释器）。没装时退出码 2
并给安装提示——不许静默放行。
"""

import glob
import os
import re
import sys

# 允许出现 undefined name 报告的文件（详见模块文档）
WHITELIST_FILES = {"ui.py"}

_UNDEFINED = re.compile(r"undefined name '([^']+)'")

if __name__ == "__main__":
    try:
        from pyflakes.api import checkPath
        from pyflakes.reporter import Reporter
    except ImportError:
        print("[check_undefined_names] pyflakes 未安装：pip install pyflakes")
        sys.exit(2)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import io

    out = io.StringIO()
    for path in sorted(glob.glob(os.path.join(root, "seqvel", "*.py"))):
        checkPath(path, Reporter(out, out))

    violations = []
    # 路径形如 "D:\dir\seqvel\ui.py:125:17: ..."——按 Drive 冒号硬 split 会碎，
    # 用正则取 ".py:" 之前的文件名
    file_re = re.compile(r"^(.+\.py):\d+:\d+:")
    for line in out.getvalue().splitlines():
        if "undefined name" not in line:
            continue
        match = file_re.match(line)
        name = os.path.basename(match.group(1)) if match else ""
        if name in WHITELIST_FILES:
            continue
        violations.append(line.strip())

    if violations:
        print("[check_undefined_names] 发现未定义名（白名单外）：")
        for line in violations:
            print("  " + line)
        sys.exit(1)
    print("[check_undefined_names] OK（白名单内 %d 条已知误报）"
          % sum(1 for line in out.getvalue().splitlines()
                if "undefined name" in line))
    sys.exit(0)
