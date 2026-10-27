# -*- coding: utf-8 -*-
"""回归（v1.0.8）：VSE 显示场景（序列场景）≠ 活动场景时的目标解析。

真实故障模式（5.blend，Blender 5.2.1）：工作区的「序列场景」是「场景」
（Scene.001 / SPD_Scene / mp3），窗口活动场景是「Scene」（只有一条 19468 帧
mp3）。Blender 5.0 起 VSE 时间线显示的是序列场景（workspace.sequencer_scene，
context.sequencer_scene 直接暴露），不是活动场景——插件读 context.scene 就会
看错场景：面板永远悬停在 mp3 上（"音频条带不能编辑"），速度线画不出、
点条带没反应。

接缝：regions.displayed_scene + carrier.scan_targets，用假 context（真实 bpy
场景 + 假 space_data/workspace）驱动。无头 Blender 运行。
"""

import unittest

from support import HAVE_BPY

if HAVE_BPY:
    from seqvel import carrier, regions

_UNSET = object()


class FakeSpace(object):
    def __init__(self, type_):
        self.type = type_


class FakeWorkspace(object):
    def __init__(self, sequencer_scene=_UNSET):
        if sequencer_scene is not _UNSET:
            self.sequencer_scene = sequencer_scene


class FakeContext(object):
    """只提供 displayed_scene 会读的成员；未传的成员**不存在**（模拟旧版/裸环境）。"""

    def __init__(self, scene, space=None, area=None, screen=None,
                 workspace=_UNSET, sequencer_scene=_UNSET):
        self.scene = scene
        self.space_data = space
        self.area = area
        self.screen = screen
        if workspace is not _UNSET:
            self.workspace = workspace
        if sequencer_scene is not _UNSET:
            self.sequencer_scene = sequencer_scene


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestDisplayedScene(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import bpy

        bpy.ops.wm.read_factory_settings(use_empty=True)

        def build(scene_name, strip_defs):
            scene = bpy.data.scenes.new(scene_name)
            editor = scene.sequence_editor_create()
            made = []
            channel = 1
            for name, type_, editable in strip_defs:
                if type_ == "COLOR":
                    s = editor.strips.new_effect(name=name, type="COLOR",
                                                 channel=channel, frame_start=1,
                                                 length=100)
                else:  # SPEED 承载（效果条带，不可编辑）
                    s = editor.strips.new_effect(name=name, type="SPEED",
                                                 channel=channel, frame_start=1,
                                                 length=100, input1=made[0])
                s.select = False
                made.append(s)
                channel += 1
            return scene, made

        # 场景 A（活动场景）：只有一条不可编辑的条带（等价于用户文件里的 mp3）
        cls.scene_active, strips_a = build(
            "活动", [("音乐", "COLOR", False)])
        editor_a = cls.scene_active.sequence_editor
        editor_a.active_strip = strips_a[0]
        strips_a[0].select = True

        # 场景 B（VSE 显示的序列场景）：有可编辑的源条带
        cls.scene_shown, strips_b = build(
            "显示", [("SceneA", "COLOR", True), ("SceneB", "COLOR", True)])
        editor_b = cls.scene_shown.sequence_editor
        editor_b.active_strip = strips_b[1]
        strips_b[1].select = True

        cls.vse_space = FakeSpace("SEQUENCE_EDITOR")

    # ------------------------------------------------ displayed_scene 回退链
    def test_context_sequencer_scene_wins(self):
        """5.x：context.sequencer_scene 直接可用 → 优先于活动场景。"""
        ctx = FakeContext(self.scene_active, space=self.vse_space,
                          sequencer_scene=self.scene_shown)
        self.assertIs(regions.displayed_scene(ctx), self.scene_shown)

    def test_fallback_to_workspace_sequencer_scene(self):
        """旧 context 没有 sequencer_scene 成员 → 退回 workspace.sequencer_scene。"""
        ctx = FakeContext(self.scene_active, space=self.vse_space,
                          workspace=FakeWorkspace(self.scene_shown))
        self.assertIs(regions.displayed_scene(ctx), self.scene_shown)
        target, _notes = carrier.scan_targets(ctx)
        self.assertIsNotNone(target)
        self.assertIs(target.id_data, self.scene_shown)

    def test_fallback_to_active_scene(self):
        """两者皆无（4.x 行为 / 裸环境）→ 退回活动场景。"""
        ctx = FakeContext(self.scene_active, space=self.vse_space,
                          workspace=FakeWorkspace())
        self.assertIs(regions.displayed_scene(ctx), self.scene_active)
        target, _notes = carrier.scan_targets(ctx)
        self.assertIsNotNone(target)
        self.assertIs(target.id_data, self.scene_active)

    def test_non_vse_space_ignores_workspace(self):
        """非 VSE 空间：不采信 workspace（没有 sequencer_scene 成员时）。"""
        ctx = FakeContext(self.scene_active, space=FakeSpace("VIEW_3D"),
                          workspace=FakeWorkspace(self.scene_shown))
        self.assertIs(regions.displayed_scene(ctx), self.scene_active)

    def test_no_space_data_still_prefers_sequencer_scene(self):
        """无 space_data（脚本 / F3 路径）：context.sequencer_scene 仍可信。"""
        ctx = FakeContext(self.scene_active, space=None,
                          sequencer_scene=self.scene_shown)
        self.assertIs(regions.displayed_scene(ctx), self.scene_shown)

    def test_none_sequencer_scene_falls_back_to_active(self):
        """序列场景为 None（VSE 从未选场景）→ 退回活动场景。"""
        ctx = FakeContext(self.scene_active, space=self.vse_space,
                          workspace=FakeWorkspace(None))
        self.assertIs(regions.displayed_scene(ctx), self.scene_active)

    # ------------------------------------------------ 端到端目标解析
    def test_scan_targets_resolves_into_shown_scene(self):
        """真故障模式：活动场景只有 mp3，目标必须落在序列场景的可编辑条带上。"""
        ctx = FakeContext(self.scene_active, space=self.vse_space,
                          sequencer_scene=self.scene_shown)
        target, _notes = carrier.scan_targets(ctx)
        self.assertIsNotNone(target)
        self.assertIs(target.id_data, self.scene_shown)
        self.assertEqual(target.name, "SceneB")


if __name__ == "__main__":
    unittest.main()
