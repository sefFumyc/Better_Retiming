# -*- coding: utf-8 -*-
"""``import seqvel`` 的纯 Python 安全性（票 04）。

回归根因：``seqvel/__init__.py`` 曾在**模块级**挂
``@bpy.app.handlers.persistent``。纯 Python 下 ``bpy`` 是 ``None``，而所有测试
都写 ``from seqvel.curve import Curve`` —— 这会先执行包 ``__init__``，于是
崩在装饰器上：

    AttributeError: 'NoneType' object has no attribute 'app'

13 个测试模块因此全部导入失败，``python tests/run_tests.py`` 整条路径报废
（只剩 ``test_pinned_scene`` 的 7 项能跑且全被跳过）。

这里不检查"源码里有没有某个字符串"——那种断言改个写法就绕过去了。做法是
**真的让 bpy 变成不可导入**，再把包与零 bpy 层重新导一遍：测的是行为。
"""

import importlib
import sys
import unittest
from contextlib import contextmanager

_PKG = "seqvel"

#: 文档里承诺"零 bpy"的层：纯 Python 环境必须能导入（分层铁律的另一半）
BPY_FREE_MODULES = (
    "seqvel.curve",
    "seqvel.geometry",
    "seqvel.ram",
    "seqvel.regions",
    "seqvel.rules",
    "seqvel.status",
    "seqvel.store",
    "seqvel.watch",
)


#: 依赖 bpy 的层：零 bpy 层不得以任何方式把它们带进来
BPY_LAYERS = (
    "seqvel.carrier",
    "seqvel.hover",
    "seqvel.interaction",
    "seqvel.overlay",
    "seqvel.preview",
    "seqvel.ui",
)


def _package_modules():
    return {name: module for name, module in sys.modules.items()
            if name == _PKG or name.startswith(_PKG + ".")}


@contextmanager
def bpy_unavailable():
    """让 ``import bpy`` 抛 ``ImportError``，并在退出时逐键还原 ``sys.modules``。

    ``sys.modules["bpy"] = None`` 是 CPython 的"已阻止导入"标记：之后再
    ``import bpy`` 会抛 ``ModuleNotFoundError``（``ImportError`` 的子类，
    正是纯 Python 环境的真实行为）。用真模块名而不是打桩，测的就是那条路径。
    """
    saved_modules = _package_modules()
    saved_bpy = sys.modules.get("bpy")
    for name in saved_modules:
        del sys.modules[name]
    sys.modules["bpy"] = None
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name == _PKG or name.startswith(_PKG + "."):
                del sys.modules[name]
        if saved_bpy is None:
            sys.modules.pop("bpy", None)
        else:
            sys.modules["bpy"] = saved_bpy
        sys.modules.update(saved_modules)


class TestPackageImportsWithoutBpy(unittest.TestCase):
    def test_package_imports_when_bpy_is_unavailable(self):
        with bpy_unavailable():
            package = importlib.import_module(_PKG)
            self.assertIsNone(package.bpy, "bpy 不可导入时 seqvel.bpy 应为 None")
            self.assertTrue(package.VERSION_TAG.startswith("v"),
                            package.VERSION_TAG)
            self.assertIsNotNone(package.bl_info["version"])

    def test_bpy_free_layers_import_when_bpy_is_unavailable(self):
        """零 bpy 层必须整条链都能导入——包 ``__init__`` 崩则全军覆没。"""
        with bpy_unavailable():
            for name in BPY_FREE_MODULES:
                with self.subTest(module=name):
                    self.assertIsNotNone(importlib.import_module(name))

    def test_importing_the_package_drags_in_no_bpy_layer(self):
        """零 bpy 层不得把 bpy 层带进来（分层铁律）。

        上一条只证明"这几层能导入"：它拦得住"零 bpy 层 import 一个**硬**依赖 bpy
        的层"（那种层在 bpy 缺席时自己就抛），但拦不住"自带 try/except 兜底的 bpy
        层"——`store` / `watch` 就是这个写法，谁照抄一份到 bpy 层，上一条是绿的。
        这一条正面钉住"导入零 bpy 层不会带出任何 bpy 层模块"。
        """
        with bpy_unavailable():
            for name in BPY_FREE_MODULES:
                importlib.import_module(name)
            leaked = [name for name in BPY_LAYERS if name in sys.modules]
            self.assertEqual(leaked, [], "零 bpy 层把 bpy 层带进来了：%r" % (leaked,))


if __name__ == "__main__":
    unittest.main()
