# -*- coding: utf-8 -*-
"""回归（v1.0.6）：双承载条带时，第二个承载首次写入不得清掉第一个的曲线。

真实故障模式（5.blend）：片段2（Scene.001）首次 commit → ensure_carrier 新建
SPD_Scene.001（自身尚无 fcurve）→ write_curve 里 clear_keys 走 locate_speed_curve
的"唯一 speed_factor 曲线"兜底 → 误抢 SPD_Scene 的曲线并清空其关键帧。

接缝：carrier.write_curve / locate_speed_curve + 真实 5.x slotted action（无头
Blender）。源条带用 COLOR（承载创建与 fcurve 归属判断与条带类型无关，接缝等价）。
"""

import unittest

from seqvel.curve import Curve

from support import HAVE_BPY, reset_scene

if HAVE_BPY:
    from seqvel import carrier, store


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestTwoCarriers(unittest.TestCase):
    def setUp(self):
        import bpy

        reset_scene()
        store.STORE.invalidate()
        editor = bpy.context.scene.sequence_editor
        self.src1 = editor.strips.new_effect(name="Src1", type="COLOR", channel=1,
                                             frame_start=1, length=100)
        self.src2 = editor.strips.new_effect(name="Src2", type="COLOR", channel=2,
                                             frame_start=1, length=100)

    def test_second_carrier_first_write_keeps_first_curve(self):
        # 片段1 已有速度线：3 个关键帧
        diag1 = carrier.write_curve(
            self.src1, Curve([(0.0, 1.0), (50.0, 1.5), (100.0, 1.0)], 100.0))
        self.assertEqual(diag1["errors"], [])
        keys1 = carrier.read_keys(carrier.carrier_of(self.src1))
        self.assertIsNotNone(keys1)
        self.assertEqual(len(keys1), 3)

        # 用户编辑片段2：首次 commit（新建 SPD_Src2，自身无 fcurve）
        diag2 = carrier.write_curve(self.src2, Curve.default(100.0))
        self.assertEqual(diag2["errors"], [])

        # 红线：兜底逻辑不得抢走片段1 的曲线
        self.assertEqual(diag2["cleared"], 0)
        keys1_after = carrier.read_keys(carrier.carrier_of(self.src1))
        self.assertIsNotNone(keys1_after)
        self.assertEqual(len(keys1_after), 3,
                         "片段2 首次写入清掉了片段1 的速度关键帧")
        keys2_after = carrier.read_keys(carrier.carrier_of(self.src2))
        self.assertIsNotNone(keys2_after)
        self.assertEqual(len(keys2_after), 2)


if __name__ == "__main__":
    unittest.main()
