# -*- coding: utf-8 -*-
"""overlay._clip_rect：scissor 裁剪矩形的收口逻辑（需 bpy 才能 import overlay）。

覆盖三个约定：
- mask 量得出来 → 就近取整成整数像素矩形（真实 mask 是整数，取整即还原）；
- mask 量不出来 → 退回整个区域矩形（至少止住"往远处伸"）；
- 区域尺寸非法 → None（绘制方跳过裁剪，不让坏数据拖垮绘制回调）。
"""

import unittest
from types import SimpleNamespace

from support import HAVE_BPY, FakeView2D, LegacyView2D

from seqvel import geometry

if HAVE_BPY:
    from seqvel import overlay


@unittest.skipUnless(HAVE_BPY, "需要 bpy（overlay 顶层 import gpu/blf）")
class TestClipRect(unittest.TestCase):
    WIDTH, HEIGHT = 640, 480

    def clip_rect(self, view, width=WIDTH, height=HEIGHT):
        return overlay._clip_rect(view, SimpleNamespace(width=width, height=height))

    def test_mask_becomes_int_rect(self):
        view = FakeView2D(clip_rect=(0.0, 0.0, 600.0, 420.0))
        self.assertEqual(self.clip_rect(view), (0, 0, 600, 420))

    def test_subpixel_mask_rounds_to_nearest(self):
        """就近取整：非整数边界按最近像素收（真实 mask 为整数，此为退化场景）。"""
        view = FakeView2D(clip_rect=(0.4, 0.6, 599.6, 419.2))
        self.assertEqual(self.clip_rect(view), (0, 1, 600, 419))

    def test_integer_boundary_noise_snaps_exactly(self):
        """整数边界 + 探测噪声 → 取整必须还原出精确整数（回归：ceil 放大成 421）。"""
        view = FakeView2D(clip_rect=(0.0, 0.0, 600.0, 420.0))
        rect = self.clip_rect(view)
        self.assertEqual(rect, (0, 0, 600, 420))
        for value in rect:
            self.assertIsInstance(value, int)

    def test_unprobeable_view2d_falls_back_to_region(self):
        """mask 量不出来（旧双参 view2d）→ 整个区域矩形兜底。"""
        self.assertEqual(self.clip_rect(LegacyView2D()),
                         (0, 0, self.WIDTH, self.HEIGHT))

    def test_degenerate_region_yields_none(self):
        view = FakeView2D(clip_rect=(0.0, 0.0, 600.0, 420.0))
        self.assertIsNone(self.clip_rect(view, width=0))
        self.assertIsNone(self.clip_rect(view, height=None))


if __name__ == "__main__":
    unittest.main()
