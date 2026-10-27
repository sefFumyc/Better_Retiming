# -*- coding: utf-8 -*-
"""内存预览算子（``preview.py``）的胶水单测。

判断都在零 bpy 的 ``ram``（见 ``test_ram``）；本文件钉住的是**胶水本身**：

- ``play_override`` 落在**序列场景**上（ADR-0003）：拿活动场景凑数，多场景
  工程里会把别的场景播一遍——播放头扫得欢天喜地，缓存里装的全是另一条时间线；
- ``_modal`` 的每条出口一律 ``FINISHED``，绝不 ``PASS_THROUGH``（ADR-0005
  修订 R7：模态不结束又放行 = 僵尸 modal 吞掉一切鼠标事件）；
- 走哪条出口，用户设置都要还原；**起手半途失败也不例外**——异常绕过
  cleanup 会把用户留在断音画同步＋静音的状态；
- 构建水位只认**自家场景**的帧。

"final 缓存真的写进了字节"在这里证明不了（headless 从不 Paint，见
ADR-0011「测试影响」），那一半仍由 GUI 真机脚本（``.scratch/ram-preview/``）
验证。
"""

import time
import types
import unittest

from support import HAVE_BPY, make_event

if HAVE_BPY:
    from seqvel import preview, ram, status


class FakeEditor(object):
    """SequenceEditor 替身：只带本功能碰到的两个属性。"""

    def __init__(self):
        self.use_cache_final = True
        self.cache_final_size = 0


class FakeScene(object):
    """序列场景替身：渲染范围 + 接管三件套 + final 缓存。"""

    def __init__(self, name="Timeline", start=1, end=241):
        self.name = name
        self.frame_start = start
        self.frame_end = end
        self.frame_current = start
        self.use_preview_range = False
        self.sync_mode = "AUDIO"
        self.use_audio = True
        self.sequence_editor = FakeEditor()
        self.render = types.SimpleNamespace(
            resolution_x=1920, resolution_y=1080, resolution_percentage=100)


class FakeManager(object):
    """window_manager 替身：计时器与 modal 注册都只记账。"""

    def __init__(self, fail_timer=False):
        self.fail_timer = fail_timer
        self.added = []
        self.removed = []
        self.modal_added = []

    def event_timer_add(self, seconds, window=None):
        if self.fail_timer:
            raise RuntimeError("timer denied")
        token = "timer-%d" % (len(self.added) + 1)
        self.added.append(token)
        return token

    def event_timer_remove(self, timer):
        self.removed.append(timer)

    def modal_handler_add(self, op):
        self.modal_added.append(op)
        return True


class PlaybackFaker(object):
    """替掉真 ops：起播/停播翻动屏幕的播放旗子（可强迫失败）。"""

    def __init__(self):
        self.calls = []
        self.result = True

    def __call__(self, context):
        self.calls.append(1)
        if self.result:
            context.screen.is_animation_playing = \
                not context.screen.is_animation_playing
        return self.result


def make_context(scene, active_scene=None, manager=None, playing=False):
    area = types.SimpleNamespace(
        type="SEQUENCE_EDITOR",
        regions=[types.SimpleNamespace(name="WINDOW")],
        tag_redraw=lambda: None)
    screen = types.SimpleNamespace(is_animation_playing=playing, areas=[area])
    window = types.SimpleNamespace(screen=screen)
    return types.SimpleNamespace(
        window=window, screen=screen, area=area,
        window_manager=manager or FakeManager(),
        sequencer_scene=scene,
        scene=active_scene if active_scene is not None else scene,
        preferences=types.SimpleNamespace(system=types.SimpleNamespace(
            memory_cache_limit=4096)))


def make_op():
    """把算子的方法借到普通对象上（同 gui_smoke 的替身思路）。"""
    real = preview.SEQVEL_OT_build_memory_preview

    class _Op(object):
        def __init__(self):
            self.reports = []

        def report(self, level, message):
            self.reports.append((level, message))

    for name, value in vars(real).items():
        if callable(value) and not name.startswith("__"):
            setattr(_Op, name, value)
    return _Op()


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestPlayOverride(unittest.TestCase):
    """起播上下文落在哪：ADR-0003 的"序列场景"纪律在算子侧的体现。"""

    def test_playback_lands_on_the_displayed_sequence_scene(self):
        shown = FakeScene("Shown")
        active = FakeScene("Active")
        override = preview.play_override(make_context(shown, active_scene=active))
        self.assertIs(override["scene"], shown,
                      "播错场景 = 缓存里装的全是另一条时间线的画面")

    def test_falls_back_to_the_active_scene_without_a_sequencer_scene(self):
        scene = FakeScene("Solo")
        context = make_context(scene)
        del context.sequencer_scene  # 没有这个属性 = 旧版行为
        self.assertIs(preview.play_override(context)["scene"], scene)

    def test_sequence_area_finds_the_editor_area(self):
        context = make_context(FakeScene())
        window, screen, area = preview.sequence_area(context)
        self.assertIs(area, context.area)
        self.assertEqual(area.type, "SEQUENCE_EDITOR")


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestBuildOutcomes(unittest.TestCase):
    """构建的每条出口：结果、收摊、还原，一样都不能少。"""

    def setUp(self):
        ram.reset()
        status.clear_error()
        self._real_toggle = preview.toggle_playback
        self.playback = PlaybackFaker()
        preview.toggle_playback = self.playback
        self.scene = FakeScene()
        self.context = make_context(self.scene)
        self.ops = []

    def tearDown(self):
        preview.toggle_playback = self._real_toggle
        for op in self.ops:
            if getattr(op, "handler", None) is not None:
                op._remove_frame_handler()
        ram.reset()
        status.clear_error()

    def _enter(self, context=None, **manager_kw):
        if manager_kw:
            self.context.window_manager = FakeManager(**manager_kw)
        op = make_op()
        self.ops.append(op)
        result = op._enter(self.context)
        return op, result

    def _age_clock(self, op):
        """越过起播宽限：单测里没有真播放，判定得立刻可达。"""
        op.started_at = time.monotonic() - 100.0

    def test_reaching_the_end_finishes_and_restores_everything(self):
        op, result = self._enter()
        self.assertEqual(result, {"RUNNING_MODAL"})
        self.assertTrue(ram.STATE["building"])
        self.assertIn(op, self.context.window_manager.modal_added)
        self.assertEqual(self.scene.frame_current, self.scene.frame_start)
        self.assertEqual(self.scene.sync_mode, "NONE")
        self.assertFalse(self.scene.use_audio)
        self.assertTrue(self.scene.sequence_editor.use_cache_final)

        self._age_clock(op)
        self.scene.frame_current = self.scene.frame_end
        self.scene.sequence_editor.cache_final_size = 500
        result = op._modal(self.context, make_event("TIMER"))

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(ram.STATE["outcome"], ram.OUTCOME_FINISHED)
        self.assertTrue(ram.STATE["built"])
        self.assertEqual(ram.cached_range(),
                         (self.scene.frame_start, self.scene.frame_end))
        # 收摊四件套：播放头送回开头 / 设置还原 / handler 摘掉 / 计时器摘掉
        self.assertEqual(self.scene.frame_current, self.scene.frame_start)
        self.assertEqual(self.scene.sync_mode, "AUDIO")
        self.assertTrue(self.scene.use_audio)
        self.assertTrue(self.scene.sequence_editor.use_cache_final)
        self.assertIsNone(op.handler)
        self.assertEqual(len(self.context.window_manager.removed), 1)
        self.assertFalse(self.context.screen.is_animation_playing)

    def test_esc_stops_and_keeps_the_prefix(self):
        op, _result = self._enter()
        self._age_clock(op)
        self.scene.frame_current = 100
        self.scene.sequence_editor.cache_final_size = 40
        op._on_frame_change(self.scene)  # 真机上水位由 frame handler 实时喂

        result = op._modal(self.context, make_event("ESC"))

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(ram.STATE["outcome"], ram.OUTCOME_STOPPED)
        self.assertTrue(ram.STATE["built"], "Esc 是'不想看了'，不是'改了时间线'")
        self.assertEqual(ram.cached_range(),
                         (self.scene.frame_start, 100))
        self.assertEqual(self.scene.frame_current, self.scene.frame_start)
        self.assertEqual(self.scene.sync_mode, "AUDIO")

    def test_a_full_cache_stops_early_with_the_prefix_kept(self):
        op, _result = self._enter()
        op.watch = ram.MemoryWatch(limit_mb=40, stall_window=2)
        self._age_clock(op)
        self.scene.frame_current = 100
        self.scene.sequence_editor.cache_final_size = 100

        first = op._modal(self.context, make_event("TIMER"))
        second = op._modal(self.context, make_event("TIMER"))
        third = op._modal(self.context, make_event("TIMER"))

        self.assertEqual(first, {"RUNNING_MODAL"},
                         "第一次采样在长：还没到顶，不许停")
        self.assertEqual(second, {"RUNNING_MODAL"},
                         "停滞要攒满窗口才作数，一次抖动不许误停")
        self.assertEqual(third, {"FINISHED"})
        self.assertEqual(ram.STATE["outcome"], ram.OUTCOME_FULL)
        self.assertTrue(ram.STATE["built"], "渲到哪算哪：半截照样算数")
        self.assertEqual(ram.cached_range(),
                         (self.scene.frame_start, 100))

    def test_playback_that_never_starts_aborts(self):
        self.playback.result = False
        op, result = self._enter()

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(ram.STATE["outcome"], ram.OUTCOME_ABORTED)
        self.assertFalse(ram.STATE["built"])
        self.assertIsNone(ram.cached_range())
        self.assertEqual(self.scene.sync_mode, "AUDIO")
        self.assertTrue(self.scene.use_audio)
        self.assertIsNone(op.handler)

    def test_a_failure_mid_setup_still_restores_the_settings(self):
        """起手半途炸掉也不许把用户留在断同步＋静音的状态（本功能的红线）。"""
        op, result = self._enter(fail_timer=True)

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(ram.STATE["outcome"], ram.OUTCOME_ABORTED)
        self.assertFalse(ram.STATE["building"])
        self.assertEqual(self.scene.sync_mode, "AUDIO")
        self.assertTrue(self.scene.use_audio)
        self.assertEqual(self.scene.frame_current, self.scene.frame_start)
        self.assertIsNone(op.handler)

    def test_a_second_press_while_building_is_refused(self):
        op, _result = self._enter()
        second, result = self._enter()

        self.assertEqual(result, {"CANCELLED"})
        self.assertTrue(any("already" in str(message) or "已经在" in str(message)
                            for _level, message in second.reports),
                        second.reports)
        self.assertTrue(ram.STATE["building"], "第二层不许把第一层顶掉")
        self.assertIs(ram.STATE.get("scene"), "Timeline")

    def test_the_watermark_only_follows_its_own_scene(self):
        op, _result = self._enter()
        other = FakeScene("Other Timeline")
        other.frame_current = 500

        op._on_frame_change(other)
        self.assertEqual(op.peak, self.scene.frame_start,
                         "别的场景挪播放头不许带跑构建水位")

        self.scene.frame_current = 150
        op._on_frame_change(self.scene)
        self.assertEqual(op.peak, 150)

    def test_the_frame_handler_is_registered_once_and_removed(self):
        op, _result = self._enter()
        handlers = __import__("bpy").app.handlers.frame_change_post
        self.assertEqual(
            sum(1 for item in handlers if item == op.handler), 1,
            "同一条 handler 挂两遍 = 水位翻倍快进")

        op._remove_frame_handler()
        self.assertNotIn(op.handler, handlers)


if __name__ == "__main__":
    unittest.main()
