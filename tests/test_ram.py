# -*- coding: utf-8 -*-
"""内存预览纯规则层的单测（纯 Python，无需 Blender）。

这批用例守住的是 **ADR-0011** 里最容易被后人"简化"坏的三件事：范围的来源
优先级、构建水位取播放头（不取字节数）、以及"贴上限 + 停止增长"才中止。
用例名一律用语义句：改动这些规则时，红起来的应当是一句人话。
"""

import types
import unittest

from seqvel import ram


class _FakeScene(object):
    """最小场景替身：ram 只读这几个帧属性。"""

    def __init__(self, start=1, end=240,
                 use_preview=False, preview_start=101, preview_end=150):
        self.frame_start = start
        self.frame_end = end
        self.use_preview_range = use_preview
        self.frame_preview_start = preview_start
        self.frame_preview_end = preview_end


class TestResolveRange(unittest.TestCase):
    """渲染范围：预览范围优先，否则场景渲染范围（ADR-0011 决定 4）。"""

    def test_uses_scene_range_when_no_preview_range(self):
        """没勾预览范围：用场景渲染范围，来源标 scene。"""
        scene = _FakeScene(start=1, end=240)
        self.assertEqual(ram.resolve_range(scene), (1, 240, ram.SOURCE_SCENE))

    def test_preview_range_wins_over_scene_range(self):
        """勾了预览范围：即使场景范围更宽也只用预览范围（Vegas 语义）。"""
        scene = _FakeScene(start=1, end=240, use_preview=True,
                           preview_start=101, preview_end=150)
        self.assertEqual(ram.resolve_range(scene), (101, 150, ram.SOURCE_PREVIEW))

    def test_endpoints_are_truncated_to_whole_frames(self):
        """端点是整数帧：带小数的场景属性一律截断，不四舍五入。"""
        scene = _FakeScene(start=1.9, end=99.9)
        self.assertEqual(ram.resolve_range(scene), (1, 99, ram.SOURCE_SCENE))

    def test_empty_range_is_refused_not_silently_swapped(self):
        """空范围 / 首尾反了：拒绝（返回 None），不偷偷替用户交换端点。"""
        scene = _FakeScene(start=200, end=200)
        self.assertEqual(ram.resolve_range(scene), (None, None, ram.SOURCE_SCENE))
        reversed_scene = _FakeScene(start=200, end=100)
        self.assertEqual(ram.resolve_range(reversed_scene),
                         (None, None, ram.SOURCE_SCENE))

    def test_missing_scene_gives_nothing(self):
        """场景取不到（N 面板偶发）：给 None，由调用方报告而不是崩。"""
        self.assertEqual(ram.resolve_range(None), (None, None, None))

    def test_unreadable_endpoint_is_refused(self):
        """端点属性缺/坏值：拒绝，不拿 0 冒充起点。"""
        scene = _FakeScene()
        del scene.frame_end
        self.assertEqual(ram.resolve_range(scene), (None, None, ram.SOURCE_SCENE))


class TestMemoryBudget(unittest.TestCase):
    """内存账：单帧估算与整段预估（只用于按下 Shift+B 时的预告）。"""

    def test_1080p_frame_is_about_eight_megabytes(self):
        """1080p 一帧约 7.9MB（RGBA8 理论值，实测 24 帧→189MB 与之吻合）。"""
        self.assertAlmostEqual(ram.frame_mb(1920, 1080), 7.91, places=2)

    def test_estimate_scales_linearly_with_frame_count(self):
        """整段预估＝帧数 × 单帧。"""
        self.assertAlmostEqual(ram.estimate_mb(10, 1920, 1080), 79.1, places=1)

    def test_unknown_resolution_degrades_to_zero_not_crash(self):
        """分辨率读不到：估算不可用时给 0（调用方按"预告不可用"处理）。"""
        self.assertEqual(ram.frame_mb(None, None), 0.0)
        self.assertEqual(ram.estimate_mb(None, None, None), 0.0)

    def test_frame_count_includes_both_ends(self):
        """范围帧数含首尾：1..240 是 240 帧，不是 239。"""
        self.assertEqual(ram.frame_count(1, 240), 240)
        self.assertEqual(ram.frame_count(5, 5), 1)
        self.assertEqual(ram.frame_count(None, None), 0)


class TestBuildTermination(unittest.TestCase):
    """构建何时算完工：**只看播放头**，不看缓存体积（背景：体积非线性增长）。"""

    def test_end_frame_counts_as_finished(self):
        """播放头走到范围末尾＝完工（末帧本身要算进去）。"""
        self.assertTrue(ram.passed_end(240, 240))
        self.assertTrue(ram.passed_end(250, 240))

    def test_one_frame_before_end_is_not_finished(self):
        """还差一帧没走到末尾就不算完工：末帧的内容还得渲。"""
        self.assertFalse(ram.passed_end(239, 240))

    def test_missing_values_never_claim_finished(self):
        """播放头/末尾读不到：不许判定完工（否则会早退，构建悄悄不完整）。"""
        self.assertFalse(ram.passed_end(None, 240))
        self.assertFalse(ram.passed_end(240, None))

    def test_progress_is_measured_from_playhead(self):
        """进度按播放头算，与缓存体积无关。"""
        self.assertAlmostEqual(ram.progress(1, 241, 121), 0.5, places=3)
        self.assertEqual(ram.progress(1, 241, 1), 0.0)
        self.assertEqual(ram.progress(1, 241, 999), 1.0)


class TestMemoryWatch(unittest.TestCase):
    """水位判定：贴上限 **且** 停止增长 才中止，两者缺一不可。"""

    def _watch(self, limit=100):
        return ram.MemoryWatch(limit_mb=limit, stall_window=3)

    def _stall(self, watch, size, times):
        """持续喂同一个体积，返回最后一次的结果。"""
        result = False
        for _ in range(times):
            result = watch.observe(size)
        return result

    def test_keeps_going_while_cache_still_grows(self):
        """还在涨就别停：新建/增长期每帧都在变大，停了就是误判。"""
        watch = self._watch(limit=100)
        for size in (10, 20, 30, 40, 50, 60):
            self.assertFalse(watch.observe(size),
                             "体积仍在增长时不该中止（size=%d）" % size)

    def test_plateau_below_limit_is_not_a_stop(self):
        """低于上限时的停滞不是中止理由——可能只是缓存还没把大块记进来。"""
        watch = self._watch(limit=1000)
        self.assertFalse(self._stall(watch, 50, times=10))

    def test_stops_when_full_and_stalled(self):
        """贴到上限 + 连续不再增长（在为腾空间反复淘汰）→ 中止。"""
        watch = self._watch(limit=100)
        watch.observe(96)
        self.assertTrue(self._stall(watch, 96, times=3))

    def test_does_not_stop_before_stall_window_elapses(self):
        """窗口未走完不许停：单次采样不动可能只是账还没落下来。"""
        watch = self._watch(limit=100)
        watch.observe(96)
        self.assertFalse(watch.observe(96))
        self.assertFalse(watch.observe(96))

    def test_unknown_limit_never_stops_the_build(self):
        """上限读不到：永不主动中止（宁可渲完被挤出，也不替用户做决定）。"""
        watch = ram.MemoryWatch(limit_mb=None, stall_window=2)
        self.assertFalse(self._stall(watch, 999999, times=10))

    def test_shrinking_cache_counts_as_stalled(self):
        """体积回落（正在被淘汰）也算没涨：判据停在"保守不停"的那一侧。"""
        watch = self._watch(limit=100)
        watch.observe(98)
        watch.observe(90)
        watch.observe(88)
        self.assertTrue(watch.observe(86))

    def test_junk_samples_do_not_crash_the_build(self):
        """采样值是垃圾（None / 字符串）：按 0 处理，绝不让构建崩在采样上。"""
        watch = self._watch(limit=100)
        self.assertFalse(watch.observe(None))
        self.assertFalse(watch.observe("??"))


class TestBuildState(unittest.TestCase):
    """运行时状态：成果什么时候算有、什么时候算没了（ADR-0011 决定 5）。

    use case 之间必须 tearDown 归零——``STATE`` 是模块级单例，串味会让某条
    用例"站在上一条的尸体上变绿"。
    """

    def setUp(self):
        ram.reset()

    def tearDown(self):
        ram.reset()

    def test_begin_records_range_and_clears_previous_result(self):
        """每次 Shift+B 都是从零开始：先把上一轮的成果抹掉再开工。"""
        ram.begin(1, 240, ram.SOURCE_SCENE)
        ram.finish(ram.OUTCOME_FINISHED, 240)
        self.assertTrue(ram.STATE["built"])

        ram.begin(1, 240, ram.SOURCE_SCENE)
        self.assertFalse(ram.STATE["built"], "新构建开工时旧成果必须作废")
        self.assertEqual(ram.STATE["outcome"], ram.OUTCOME_IDLE)
        self.assertIsNone(ram.STATE["watermark"])
        self.assertTrue(ram.STATE["building"])

    def test_finished_build_claims_the_whole_range(self):
        """正常跑完：成果＝从范围起点到范围末尾，整段都算数。"""
        ram.begin(101, 150, ram.SOURCE_PREVIEW)
        ram.note_progress(126, 47)
        ram.finish(ram.OUTCOME_FINISHED, 150)
        self.assertEqual(ram.cached_range(), (101, 150))
        self.assertFalse(ram.STATE["building"])

    def test_memory_full_keeps_built_prefix(self):
        """内存装不下而提前中止：渲到哪算哪，已构建的那一段仍然有效。"""
        ram.begin(1, 1000, ram.SOURCE_SCENE)
        ram.finish(ram.OUTCOME_FULL, 312)
        self.assertTrue(ram.STATE["built"], "Vegas 语义：渲到一半也算数")
        self.assertEqual(ram.cached_range(), (1, 312))

    def test_escape_is_not_a_change_and_keeps_what_was_built(self):
        """Esc 是"我不想看了"，不是"我改了时间线"：保留已构建的部分。"""
        ram.begin(1, 240, ram.SOURCE_SCENE)
        ram.finish(ram.OUTCOME_STOPPED, 88)
        self.assertTrue(ram.STATE["built"], "Esc 留下的半截照样能播")
        self.assertEqual(ram.cached_range(), (1, 88))
        self.assertEqual(ram.STATE["outcome"], ram.OUTCOME_STOPPED)

    def test_aborted_before_any_frame_leaves_nothing(self):
        """起手就失败（连播放都没起来）：没有水位，也就没有成果可言。"""
        ram.begin(1, 240, ram.SOURCE_SCENE)
        self.assertFalse(ram.finish(ram.OUTCOME_ABORTED, None))
        self.assertIsNone(ram.cached_range())

    def test_invalidation_drops_the_built_range(self):
        """改过东西（拖动首尾 / 速度线）：写入的数据失效，整体作废。"""
        ram.begin(1, 240, ram.SOURCE_SCENE)
        ram.finish(ram.OUTCOME_FINISHED, 240)
        self.assertTrue(ram.invalidate())
        self.assertFalse(ram.STATE["built"])
        self.assertIsNone(ram.cached_range(), "失效后不许再宣称有一截能播")
        self.assertEqual(ram.STATE["outcome"], ram.OUTCOME_INVALIDATED)

    def test_invalidation_is_ignored_while_building(self):
        """构建期间的更新一律忽略：那时 UI 是锁的，能来的更新只可能是我们自己
        改 ``use_cache_final`` / ``sync_mode`` 激起的自家心跳。"""
        ram.begin(1, 240, ram.SOURCE_SCENE)
        self.assertFalse(ram.invalidate())
        self.assertTrue(ram.STATE["building"], "构建不该被自家更新打断")
        self.assertEqual(ram.STATE["outcome"], ram.OUTCOME_IDLE)

    def test_another_scenes_edits_leave_the_preview_alone(self):
        """多场景工程：别的时间线动了，不影响本场景成果（ADR-0011 决定 5 说的
        是**序列场景**有改动，不是"文件里有场景动了"）。

        不校验的话，在 B 场景里拖一下条带，A 场景辛苦渲出来的预览就白白作废，
        用户会以为是自己按错了键。
        """
        ram.begin(1, 240, ram.SOURCE_SCENE, scene_name="Timeline A")
        ram.finish(ram.OUTCOME_FINISHED, 240)
        self.assertFalse(ram.invalidate("Timeline B"))
        self.assertTrue(ram.STATE["built"], "别人的场景改动不该作废我这份")
        self.assertEqual(ram.cached_range(), (1, 240))

    def test_edits_in_the_scene_it_was_built_for_do_invalidate(self):
        """自家场景一动就作废：留出多场景推理的同时，别把真正的判据弄丢了。"""
        ram.begin(1, 240, ram.SOURCE_SCENE, scene_name="Timeline A")
        ram.finish(ram.OUTCOME_FINISHED, 240)
        self.assertTrue(ram.invalidate("Timeline A"))
        self.assertIsNone(ram.cached_range())

    def test_a_caller_that_cannot_name_the_scene_still_invalidates(self):
        """调用方说不出是谁动的手（旧路径 / 拿不到名字）→ 保守作废。

        方向是刻意的：宁可白渲一次，也不要把过期画面当用户已经渲好的那一截。
        """
        ram.begin(1, 240, ram.SOURCE_SCENE, scene_name="Timeline A")
        ram.finish(ram.OUTCOME_FINISHED, 240)
        self.assertTrue(ram.invalidate())

    def test_invalidating_nothing_reports_nothing(self):
        """压根没构建过就没有东西可失效——别把面板刷成"已失效"。"""
        self.assertFalse(ram.invalidate())
        self.assertEqual(ram.STATE["outcome"], ram.OUTCOME_IDLE)

    def test_cached_range_is_none_without_a_result(self):
        """没有成果时给 None，调用方据此决定面板显示"未构建"。"""
        self.assertIsNone(ram.cached_range())

    def test_progress_is_served_from_state_frame(self):
        """面板进度直接拿 STATE 里的播放头算（心跳喂进来的是播放头，不是字节）。"""
        ram.begin(1, 201, ram.SOURCE_SCENE)
        ram.note_progress(101, 63)
        self.assertAlmostEqual(
            ram.progress(ram.STATE["start"], ram.STATE["end"],
                         ram.STATE["frame"]), 0.5, places=3)


class TestCacheCeiling(unittest.TestCase):
    """「内存什么时候算装满了」——这条门槛是两次真机测绘标定出来的。

    Blender **不会**把偏好里的上限用满：上限 4096 MB 时，最终水位实测停在
    3860 MB（94.2%，单层短条带）与 3978 MB（97.1%，8 层长条带）两个不同位置。
    留的空隙是**绝对量**（几百 MB）而不是固定比例，所以只认比例时：

    - 门槛取 95% ⇒ 3860 那次永远等不到"满"（probe 011 的实红：宣称整段构建
      完成，其实有四五百帧根本没留下）；
    - 上限被用户调小（1~2 GB）时，几百 MB 的空隙占比更大 ⇒ 水位落在比例门槛
      之下，同样等不到。
    """

    def _repeated(self, watch, size, times=3):
        """同一个体积连喂若干次，把每次的结论原样收回来便于逐条断言。"""
        return [watch.observe(size) for _ in range(times)]

    def test_the_3860_watermark_counts_as_full(self):
        """真机 1：4096 上限停在 3860 MB（94.2%）——这就是装满了。"""
        watch = ram.MemoryWatch(limit_mb=4096, stall_window=2)
        verdicts = self._repeated(watch, 3860)
        self.assertEqual(verdicts[:2], [False, False], "刚见稳定还不到两拍")
        self.assertTrue(verdicts[2], "94.2% 且不再长 ⇒ 已到顶")

    def test_the_3978_watermark_counts_as_full(self):
        """真机 2：4096 上限停在 3978 MB（97.1%）——同样是装满了。"""
        watch = ram.MemoryWatch(limit_mb=4096, stall_window=2)
        verdicts = self._repeated(watch, 3978)
        self.assertEqual(verdicts[:2], [False, False])
        self.assertTrue(verdicts[2])

    def test_a_small_limit_is_full_below_the_ratio_gate(self):
        """上限 1024 MB 停在 800 MB（78%）：按比例永远够不着，按空隙已经到顶。"""
        watch = ram.MemoryWatch(limit_mb=1024, stall_window=2)
        verdicts = self._repeated(watch, 800)
        self.assertEqual(verdicts[:2], [False, False])
        self.assertTrue(verdicts[2])

    def test_a_range_that_fits_is_never_called_full(self):
        """装得下就先停下不动（比如整range 早就在缓存里）：不许谎报"内存不够"。

        这条是上面三条的守卫——把门槛调宽最容易付的代价就是把"刚好装完"误判成
        "装不下"，那样用户会在内存还够的时候被告知内存没了。
        """
        watch = ram.MemoryWatch(limit_mb=4096, stall_window=2)
        for _ in range(6):
            self.assertFalse(watch.observe(500), "500/4096 远没到 top")

    def test_growth_resets_the_stall_counter(self):
        """还在长 = 还没到顶：停滞计数必须清零，否则一次抖动就误停。"""
        watch = ram.MemoryWatch(limit_mb=4096, stall_window=2)
        verdicts = [watch.observe(size)
                    for size in (3800, 3800, 3900, 3900, 3900)]
        self.assertEqual(verdicts, [False, False, False, False, True],
                         "又长了就得从头数起")

    def test_no_known_limit_means_never_stop_early(self):
        """读不到上限：宁可让用户看到"渲完了但可能被挤出去"，也不擅自替他停。"""
        watch = ram.MemoryWatch(limit_mb=0, stall_window=1)
        self.assertFalse(watch.observe(999999))
        self.assertFalse(watch.observe(999999))

    def test_a_tiny_limit_still_has_a_sane_ceiling(self):
        """上限被调到 200MB：绝对空隙（300MB）会算出负数 ⇒ 门槛必须有个地板。

        地板取上限的一半：再往下就真成了"缓存刚动就喊满了"。
        """
        watch = ram.MemoryWatch(limit_mb=200, stall_window=2)
        verdicts = [watch.observe(size) for size in (100, 100, 100)]
        self.assertEqual(verdicts[:2], [False, False])
        self.assertTrue(verdicts[2], "200MB 的上限在 100MB 处就是天花板")
        small = ram.MemoryWatch(limit_mb=200, stall_window=2)
        self.assertFalse(any(small.observe(30) for _ in range(4)),
                         "30/200 还远得很，不许误报")


class _WatchedScene(_FakeScene):
    """带接管设置的场景替身（用于临时接管 / 还原的记账测试）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sync_mode = "AUDIO_SYNC"
        self.use_audio = True
        self.render = types.SimpleNamespace(resolution_x=1920,
                                            resolution_y=1080,
                                            resolution_percentage=100)


class _WatchedEditor(object):
    """序列编辑器替身：记录 ``use_cache_final`` 的每一次写入。"""

    def __init__(self, cache_final=True):
        self._cache_final = cache_final
        self.writes = []

    @property
    def use_cache_final(self):
        return self._cache_final

    @use_cache_final.setter
    def use_cache_final(self, value):
        self._cache_final = value
        self.writes.append(value)


class TestBuildDecision(unittest.TestCase):
    """构建循环的一步判定（modal 不许自己另发明一套）。"""

    def test_reaching_the_end_wins_over_a_stalled_cache(self):
        """主判据是播放头：末帧刚好填满缓存是"装完"，不是"装不下"。"""
        watch = ram.MemoryWatch(limit_mb=100, stall_window=1)
        watch.observe(99)  # 已经贴上上限
        action = ram.decide(240, 240, 99, watch, True)
        self.assertEqual(action, ram.ACTION_DONE)

    def test_playing_stops_when_cache_is_full_and_stalled(self):
        """还在播、还没到末尾，但内存已经装不下 → 提前中止。"""
        watch = ram.MemoryWatch(limit_mb=100, stall_window=2)
        watch.observe(99)
        self.assertEqual(ram.decide(120, 240, 99, watch, True),
                         ram.ACTION_WAIT)
        self.assertEqual(ram.decide(121, 240, 99, watch, True),
                         ram.ACTION_FULL)

    def test_playback_dying_before_the_end_is_a_cancellation(self):
        """播放自己停了而没到末尾：当作本次被中止，已构建的部分照样保留。"""
        watch = ram.MemoryWatch(limit_mb=None)
        self.assertEqual(ram.decide(120, 240, 5, watch, False),
                         ram.ACTION_STOPPED)

    def test_still_playing_and_still_room_keeps_going(self):
        watch = ram.MemoryWatch(limit_mb=4096)
        self.assertEqual(ram.decide(120, 240, 400, watch, True),
                         ram.ACTION_WAIT)

    def test_every_poll_feeds_the_watch(self):
        """每次判定都必须喂一次采样——漏喂会让"停滞"计数永远攒不满。"""
        watch = ram.MemoryWatch(limit_mb=4096, stall_window=3)
        for size in (10, 10, 10):
            ram.decide(120, 240, size, watch, True)
        self.assertEqual(watch.peak, 10)
        self.assertEqual(watch.stalls, 2, "三次同样体积应累计两次未增长")


class TestSettingsTakeover(unittest.TestCase):
    """构建期间接管三项用户设置（ADR-0011 决定 8）：一律存旧值、一律还原。"""

    def test_build_settings_are_mute_no_sync_and_final_cache_on(self):
        editor = _WatchedEditor(cache_final=True)
        scene = _WatchedScene()
        saved = ram.apply_build_settings(editor, scene)
        self.assertEqual(saved["sync_mode"], "AUDIO_SYNC")
        self.assertEqual(saved["audio"], True)
        self.assertEqual(scene.sync_mode, "NONE", "卡同步会把构建锁死成素材时长")
        self.assertFalse(scene.use_audio)
        self.assertTrue(editor.use_cache_final)

    def test_restore_puts_the_users_values_back(self):
        editor = _WatchedEditor(cache_final=True)
        scene = _WatchedScene()
        saved = ram.apply_build_settings(editor, scene)
        report = ram.restore_settings(editor, scene, saved)
        self.assertEqual(scene.sync_mode, "AUDIO_SYNC")
        self.assertTrue(scene.use_audio)
        self.assertFalse(report["final_left_on"])

    def test_settings_that_do_not_exist_are_not_fabricated(self):
        """缺的设置项不许写回去：拿一个默认值补给用户场景是插件最伤人的地方。"""
        editor = _WatchedEditor()
        scene = _WatchedScene()
        del scene.use_audio
        saved = ram.apply_build_settings(editor, scene)
        self.assertNotIn("audio", saved)
        ram.restore_settings(editor, scene, saved)
        self.assertFalse(hasattr(scene, "use_audio"),
                         "还原不许凭空造出一个用户原本没有的设置项")

    def test_final_cache_stays_on_when_the_user_had_it_off(self):
        """旧值为 False 时不照抄：照抄＝把关掉前方才构建出来的东西清空。"""
        editor = _WatchedEditor(cache_final=False)
        scene = _WatchedScene()
        saved = ram.apply_build_settings(editor, scene)
        self.assertTrue(editor.use_cache_final, "构建必须开 final 缓存")
        report = ram.restore_settings(editor, scene, saved)
        self.assertTrue(editor.use_cache_final,
                        "还原不许把刚构建的东西清空（白干一次）")
        self.assertTrue(report["final_left_on"], "这种改动必须上报而不是偷着做")

    def test_clearing_the_cache_toggles_off_then_on(self):
        """清空 final 缓存＝拨 False 再拨回 True（唯一手段，别再找别的）。"""
        editor = _WatchedEditor(cache_final=True)
        self.assertTrue(ram.clear_final_cache(editor))
        self.assertEqual(editor.writes, [False, True])

    def test_clear_without_editor_does_not_crash(self):
        self.assertFalse(ram.clear_final_cache(None))


class TestBudgetInputs(unittest.TestCase):
    """估算的两个输入：内存上限与预览画面尺寸。"""

    def test_unreadable_limit_falls_back_to_the_factory_default(self):
        self.assertEqual(ram.limit_of(None), ram.DEFAULT_CACHE_LIMIT_MB)
        zero = types.SimpleNamespace(system=types.SimpleNamespace(
            memory_cache_limit=0))
        self.assertEqual(ram.limit_of(zero), ram.DEFAULT_CACHE_LIMIT_MB)

    def test_limit_is_read_when_there(self):
        prefs = types.SimpleNamespace(system=types.SimpleNamespace(
            memory_cache_limit=8192))
        self.assertEqual(ram.limit_of(prefs), 8192)

    def test_resolution_applies_the_render_percentage(self):
        scene = _WatchedScene()
        scene.render.resolution_percentage = 50
        self.assertEqual(ram.resolution_of(scene), (960, 540))

    def test_resolution_reads_zero_when_render_is_missing(self):
        self.assertEqual(ram.resolution_of(object()), (0, 0))


if __name__ == "__main__":
    unittest.main()
