# -*- coding: utf-8 -*-
"""条带端点监听（``watch``）的单测：末端跟随 / 吞点落地路径（ADR-0004）。

两层：
- 假对象层（纯 Python）：``observe_strip`` / ``observe_scene`` 的判定矩阵，
  ``_commit`` 被替身替换，只关心"何时落盘"；
- bpy 层（headless Blender）：真条带收短 → 模型跟随 + 属性 + 承载关键帧
  三处一致。
"""

import types
import unittest

from seqvel import watch
from seqvel.store import STORE

from support import HAVE_BPY

PROP_KEY = "seqvel_curve"


class FakeID(object):
    def __init__(self, name):
        self.name = name


class FakeStrip(object):
    """store / watch 需要的最小条带表面（属性读写 + 句柄）。"""

    def __init__(self, name, right, left=1, channel=1,
                 strip_type="MOVIE", prop=None, scene_name="场景"):
        self.name = name
        self.type = strip_type
        self.channel = channel
        self.left_handle = left
        self.right_handle = right
        self.id_data = FakeID(scene_name)
        self._props = {}
        if prop is not None:
            self._props[PROP_KEY] = list(prop)

    def get(self, key, default=None):
        return self._props.get(key, default)

    def __getitem__(self, key):
        return self._props[key]

    def __setitem__(self, key, value):
        self._props[key] = value

    def __contains__(self, key):
        return key in self._props

    def keys(self):
        return list(self._props)


class FakeScene(object):
    def __init__(self, name, strips=()):
        self.name = name
        self.sequence_editor = types_editor(strips)


class types_editor(object):  # noqa: N801 - 保持测试内命名直观
    def __init__(self, strips):
        self.strips = list(strips)


def flat(*pairs):
    out = []
    for frame, speed in pairs:
        out.extend((float(frame), float(speed)))
    return out


class _FakeUpdate(object):
    """依赖图更新项的最小表面：只需要一个 ``id``。

    ``is_updated_geometry`` / ``is_updated_transform`` 这类标志**故意不放**：
    监听器一个都不读，摆出来只会让人以为改它们会影响行为。
    """

    def __init__(self, id_obj):
        self.id = id_obj


class FakeDepsgraph(object):
    """依赖图替身：只有 ``updates``。

    真 GUI 里这一列装的是**求值副本**（``is_evaluated=True``）——本仓库的
    监听器就是栽在这里（ADR-0004 修订 R3）。
    """

    def __init__(self, ids):
        self.updates = [_FakeUpdate(i) for i in ids]


class WatchTestBase(unittest.TestCase):
    def setUp(self):
        STORE.invalidate()
        watch._TRACKED.clear()
        #: 每次落盘记成 ``(strip, points, reanchor, write_prop)``
        self.commits = []
        self._real_commit = watch._commit
        watch._commit = self._fake_commit

    def tearDown(self):
        watch._commit = self._real_commit
        STORE.invalidate()
        watch._TRACKED.clear()

    def _fake_commit(self, strip, curve, reanchor=True, write_prop=True):
        self.commits.append((strip, [tuple(p) for p in curve.points],
                             reanchor, write_prop))
        if write_prop:
            STORE.commit(strip, curve)  # 让属性写入也走真路径

    def only_commit(self):
        """本用例恰好落盘一次时把它取出来（多了少了都当场失败）。"""
        self.assertEqual(len(self.commits), 1, self.commits)
        return self.commits[0]

    def tracked_span(self, strip):
        return watch._TRACKED[(strip.id_data.name, strip.name)]


class TestObserveStrip(WatchTestBase):
    def test_first_sight_records_span_and_heals_without_writing_props(self):
        """首次见到：收编区间基准 + 补一次**只重建映射表**的落地（修订 R1）。

        刻意不写条带属性——读路径的例行自愈不该把用户刚打开的文件标脏。
        """
        strip = FakeStrip("A", right=101, prop=flat((0, 1.0), (100, 1.0)))
        self.assertFalse(watch.observe_strip(strip))
        self.assertEqual(len(self.commits), 1)
        _strip, _points, reanchor, write_prop = self.only_commit()
        self.assertFalse(reanchor, "承载未对齐/映射表重建即可，不该重写关键帧")
        self.assertFalse(write_prop, "首次见到不写属性")
        self.assertEqual(self.tracked_span(strip), (1.0, 101.0))

    def test_right_shortened_carries_tail_and_commits(self):
        strip = FakeStrip("A", right=101, prop=flat((0, 1.0), (50, 1.5), (100, 0.8)))
        watch.observe_strip(strip)
        self.commits.clear()
        strip.right_handle = 81.0
        self.assertTrue(watch.observe_strip(strip))
        strip_ref, points, reanchor, write_prop = self.only_commit()
        self.assertIs(strip_ref, strip)
        self.assertEqual(points, [(0.0, 1.0), (50.0, 1.5), (80.0, 0.8)])
        self.assertTrue(reanchor, "末点被带动 = 点集变了，关键帧必须重写")
        self.assertTrue(write_prop)
        self.assertEqual([float(v) for v in strip[PROP_KEY]],
                         [0.0, 1.0, 50.0, 1.5, 80.0, 0.8])

    def test_shortened_onto_second_point_swallows_follower(self):
        """吞点（决定 2）：末点撞上倒数第二点时删**跟随者**；点集变了 → 必须重锚。"""
        strip = FakeStrip("A", right=101, prop=flat((0, 1.0), (50, 1.5), (100, 0.8)))
        watch.observe_strip(strip)
        self.commits.clear()
        strip.right_handle = 51.0
        self.assertTrue(watch.observe_strip(strip))
        _strip, points, reanchor, _write_prop = self.only_commit()
        self.assertEqual(points, [(0.0, 1.0), (50.0, 1.5)])
        self.assertTrue(reanchor, "吞点改了模型 → 关键帧必须重写")

    def test_right_extended_relaunches_carrier_without_rewriting_keys(self):
        """[病灶] 拉长：模型一点不动，但承载必须重落地（修订 R1）。

        拉长出来的那一段要按**新长度**重算速度映射表，否则它落在旧表之外、
        越界读到垃圾——用户报的"拉长那段播放不正常"就是这个。
        """
        strip = FakeStrip("A", right=101, prop=flat((0, 1.0), (50, 1.5), (100, 0.8)))
        watch.observe_strip(strip)
        self.commits.clear()
        strip.right_handle = 201.0
        self.assertTrue(watch.observe_strip(strip),
                        "拉长也要落一次地：不再有'拉长什么都不做'这条路")
        _strip, points, reanchor, write_prop = self.only_commit()
        self.assertEqual(points, [(0.0, 1.0), (50.0, 1.5), (100.0, 0.8)],
                         "拉长不带动模型：点集与速度值一动不动")
        self.assertFalse(reanchor, "只动右端 → 轻量路径（对齐 + 重建映射表）")
        self.assertTrue(write_prop, "载荷区间要跟着当下区间（ADR-0008 划分链的前提）")
        self.assertEqual(self.tracked_span(strip), (1.0, 201.0))

    def test_left_trim_reanchors_keys(self):
        """左端裁剪：关键帧是按左端写的绝对帧，必须重锚（修订 R1）。"""
        strip = FakeStrip("A", right=101, prop=flat((0, 1.0), (100, 1.2)))
        watch.observe_strip(strip)
        self.commits.clear()
        strip.left_handle = 31.0
        self.assertTrue(watch.observe_strip(strip))
        _strip, points, reanchor, _write_prop = self.only_commit()
        self.assertEqual(points, [(0.0, 1.0), (100.0, 1.2)], "模型不动，只重锚")
        self.assertTrue(reanchor, "左端一动，key 的绝对帧整条平移 → 必须重写")
        self.assertEqual(self.tracked_span(strip), (31.0, 101.0))

    def test_whole_strip_move_reanchors_keys(self):
        """整体移动 = 左端也动了：曲线锚在条带左端，跟着一起平移。"""
        strip = FakeStrip("A", right=101, prop=flat((0, 1.0), (100, 1.2)))
        watch.observe_strip(strip)
        self.commits.clear()
        strip.left_handle = 21.0
        strip.right_handle = 121.0
        self.assertTrue(watch.observe_strip(strip))
        _strip, points, reanchor, _write_prop = self.only_commit()
        self.assertEqual(points, [(0.0, 1.0), (100.0, 1.2)])
        self.assertTrue(reanchor)

    def test_shorten_without_crossing_a_point_still_relaunches(self):
        """收窄但没越过末点（末点本来就在条带内侧）：点集没变，但映射表的
        长度变了，仍要重落地——旧代码在这里是"只推进基准、什么都不做"。"""
        strip = FakeStrip("A", right=101, prop=flat((0, 1.0), (50, 1.5)))
        watch.observe_strip(strip)
        self.commits.clear()
        strip.right_handle = 81.0
        self.assertTrue(watch.observe_strip(strip))
        _strip, points, reanchor, write_prop = self.only_commit()
        self.assertEqual(points, [(0.0, 1.0), (50.0, 1.5)])
        self.assertFalse(reanchor, "末点没被带动 → 关键帧不必重写")
        self.assertTrue(write_prop)

    def test_strip_without_prop_is_ignored_entirely(self):
        strip = FakeStrip("A", right=101, prop=None)
        self.assertFalse(watch.observe_strip(strip))
        strip.right_handle = 51.0
        self.assertFalse(watch.observe_strip(strip))
        self.assertEqual(self.commits, [])
        self.assertNotIn((strip.id_data.name, "A"), watch._TRACKED)

    def test_non_source_type_is_ignored(self):
        strip = FakeStrip("A", right=101, strip_type="SPEED",
                          prop=flat((0, 1.0), (100, 1.0)))
        self.assertFalse(watch.observe_strip(strip))
        self.assertEqual(watch._TRACKED, {})

    def test_none_strip_is_rejected(self):
        self.assertFalse(watch.observe_strip(None))

    def test_repeated_observe_is_idempotent(self):
        """一次变化只消费一次：重入（落地会写 RNA，可能再激起一次更新）
        不得再次落盘——这是防死循环的防线，必须被测试钉住。"""
        strip = FakeStrip("A", right=101, prop=flat((0, 1.0), (100, 1.0)))
        watch.observe_strip(strip)
        self.commits.clear()
        strip.right_handle = 61.0
        self.assertTrue(watch.observe_strip(strip))
        self.assertFalse(watch.observe_strip(strip))
        self.assertFalse(watch.observe_strip(strip))
        self.assertEqual(len(self.commits), 1)

    def test_commit_failure_advances_baseline_and_reports(self):
        """落盘抛异常也不保留信号（carry 已生效且幂等），但原因必须上面板。"""
        strip = FakeStrip("A", right=101, prop=flat((0, 1.0), (100, 1.0)))
        watch.observe_strip(strip)
        strip.right_handle = 61.0

        from seqvel import status

        def boom(_strip, _curve, **_kwargs):
            raise RuntimeError("落盘炸了")

        watch._commit = boom
        try:
            self.assertTrue(watch.observe_strip(strip))
            # 文案随语言模式切换（ADR-0009），钉语言无关的契约：
            # 异常类型与原文必须原样上面板（"不静默"纪律）
            self.assertIn("RuntimeError", status.STATE["error"])
            self.assertIn("落盘炸了", status.STATE["error"])
            self.assertEqual(self.tracked_span(strip), (1.0, 61.0))
        finally:
            watch._commit = self._fake_commit  # 供 tearDown 恢复
            self.commits.clear()
        self.assertFalse(watch.observe_strip(strip))  # 不重试、不空转
        self.assertEqual(self.commits, [])


class TestObserveScene(WatchTestBase):
    def test_scans_strips_and_prunes_stale_entries(self):
        tracked = {(u"场景", u"已删除"): (55.0, 99.0)}
        strip = FakeStrip("A", right=101, prop=flat((0, 1.0), (100, 1.0)))
        scene = FakeScene(u"场景", [strip])
        self.assertEqual(watch.observe_scene(scene, tracked), 0)
        self.assertEqual(tracked, {(u"场景", "A"): (1.0, 101.0)})

    def test_scene_without_editor_is_ignored(self):
        scene = FakeScene(u"空")
        scene.sequence_editor = None
        self.assertEqual(watch.observe_scene(scene, {}), 0)

    def test_multiple_strips_independent(self):
        a = FakeStrip("A", right=101, prop=flat((0, 1.0), (100, 1.0)))
        b = FakeStrip("B", right=201, prop=flat((0, 1.0), (50, 2.0), (200, 0.5)))
        scene = FakeScene(u"场景", [a, b])
        watch.observe_scene(scene)
        self.commits.clear()
        a.right_handle = 61.0
        b.right_handle = 201.0  # 不变
        self.assertEqual(watch.observe_scene(scene), 1)
        self.assertEqual(len(self.commits), 1)
        self.assertEqual(self.commits[0][1], [(0.0, 1.0), (60.0, 1.0)])


class TestDepsgraphTargets(unittest.TestCase):
    """``_on_depsgraph`` 从 ``depsgraph.updates`` 挑目标场景（ADR-0004 修订 R3）。

    ``depsgraph.updates[i].id`` 是**求值副本**（copy-on-write），不是原始数据块。
    经副本做的每一处 RNA 写都被**静默丢弃**：映射表重建、关键帧、模型载荷全都写在
    空气里——而 ``diag`` 照样报 ``invalidated: True``。所以挑目标时必须归一
    ``.original``；归不到就报错，**不许拿副本凑合**（那正是"面板说成功、画面没动"
    的成因）。
    """

    def setUp(self):
        from seqvel import status

        STORE.invalidate()
        watch._TRACKED.clear()
        self.status = status
        self.seen = []
        self._real_observe = watch.observe_scene
        watch.observe_scene = lambda scene, tracker=None: (
            self.seen.append(scene) or 0)
        status.clear_error()

    def tearDown(self):
        watch.observe_scene = self._real_observe
        STORE.invalidate()
        watch._TRACKED.clear()
        self.status.clear_error()

    def _copy_of(self, original):
        """求值副本替身：``is_evaluated`` 为真、``original`` 指回原始。"""
        copy = FakeScene(original.name, original.sequence_editor.strips)
        copy.is_evaluated = True
        copy.original = original
        return copy

    def test_a_copy_in_updates_is_resolved_back_to_its_original(self):
        original = FakeScene(u"场景", [FakeStrip("A", right=101,
                                                prop=flat((0, 1.0), (100, 1.0)))])
        original.is_evaluated = False
        watch._on_depsgraph(None, FakeDepsgraph([self._copy_of(original)]))
        self.assertEqual(self.seen, [original],
                         "必须把写落在**原始**数据块上；写副本等于没写")
        self.assertEqual(self.status.STATE["error"], "",
                         "归一成功就不该报错")

    def test_a_copy_already_seen_once_is_observed_once(self):
        original = FakeScene(u"场景", [FakeStrip("A", right=101,
                                                prop=flat((0, 1.0), (100, 1.0)))])
        original.is_evaluated = False
        watch._on_depsgraph(None, FakeDepsgraph(
            [self._copy_of(original), self._copy_of(original)]))
        self.assertEqual(self.seen, [original], "同一场景的多个更新项只观察一次")

    def test_an_unresolvable_copy_is_reported_not_used(self):
        copy = FakeScene(u"场景")
        copy.is_evaluated = True
        copy.original = None
        watch._on_depsgraph(None, FakeDepsgraph([copy]))
        self.assertEqual(self.seen, [], "拿不到原始就不许动手")
        self.assertTrue(self.status.STATE["error"], "不许静默：必须报出来")
        self.assertIn(u"场景", self.status.STATE["error"])


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestWatchIntegration(unittest.TestCase):
    """真条带收短：模型、条带属性、承载关键帧三处一致。"""

    def setUp(self):
        import bpy

        from seqvel import carrier, store

        reset = __import__("support").reset_scene
        reset()
        store.STORE.invalidate()
        watch._TRACKED.clear()
        self.bpy = bpy
        self.carrier = carrier
        self.store = store

    def tearDown(self):
        STORE.invalidate()
        watch._TRACKED.clear()

    def _make_speedlined_strip(self):
        strip = __import__("support").make_strip(length=100)
        curve = STORE.get_or_create(strip)
        curve.insert_point(50.0, 1.5)
        STORE.commit(strip, curve)
        self.carrier.write_curve(strip, curve)
        return strip

    def test_shorten_carries_model_prop_and_keys(self):
        strip = self._make_speedlined_strip()
        self.assertEqual(watch.observe_strip(strip), False)  # 收编基准
        strip.right_handle = strip.left_handle + 60
        self.assertTrue(watch.observe_strip(strip))
        curve = STORE.get(strip)
        self.assertEqual(curve.points[-1][0], 60.0)
        self.assertEqual([float(v) for v in strip[self.store.PROP_KEY]][-2:], [60.0, 1.0])
        carrier_strip = self.carrier.carrier_of(strip)
        keys = self.carrier.read_keys(carrier_strip)
        self.assertEqual(keys[-1][0], float(strip.right_handle))  # 关键帧跟到新右端

    def test_extension_keeps_points_and_relaunches_carrier(self):
        """[修订 R1 的病灶] 拉长：模型一点不动，但承载必须重落地。

        真机复现见 ``.scratch/carrier-relaunch/evidence.md``：拉长后承载条带那张速度
        映射表仍按**旧长度**，新长出来的那一段越界读到垃圾 ⇒ 画面冻在开头。
        """
        from seqvel import status

        strip = self._make_speedlined_strip()
        self.assertEqual(watch.observe_strip(strip), False)  # 收编基准
        carrier_strip = self.carrier.carrier_of(strip)
        keys_before = self.carrier.read_keys(carrier_strip)

        calls = []
        real = self.carrier.invalidate_speed_map

        def spy(spied):
            calls.append(spied)
            return real(spied)

        self.carrier.invalidate_speed_map = spy
        try:
            strip.right_handle = strip.left_handle + 150
            self.assertTrue(watch.observe_strip(strip),
                            "拉长也要落一次地（不再是'拉长什么都不做'）")
        finally:
            self.carrier.invalidate_speed_map = real

        self.assertEqual(STORE.get(strip).points[-1][0], 100.0, "拉长不带动模型")
        self.assertEqual(self.carrier.read_keys(carrier_strip), keys_before,
                         "只动右端 → 轻量路径，关键帧一字不写")
        self.assertEqual(len(calls), 1, "必须显式让 Blender 重建速度映射表")
        self.assertTrue(self.carrier.is_aligned(strip, carrier_strip))
        self.assertEqual(carrier_strip.right_handle, strip.right_handle)
        self.assertEqual(list(STORE.read_range(strip)),
                         [float(strip.left_handle), float(strip.right_handle)],
                         "载荷区间跟着当下区间（ADR-0008 划分链的前提）")

        # 面板那行"上次写入"读的是 diag 的这几个键：轻量路径**也必须给全**，
        # 否则每次拉长 / 收短之后它就显示成 `?`——用户正在做的就是这个操作。
        diag = status.STATE["diag"]
        self.assertIsNotNone(diag, "落地必须把诊断交给 status（面板是唯一信息出口）")
        self.assertTrue(diag["points"], "点数要如实报，不能缺字段")
        self.assertEqual(diag["written"], 0, "轻量路径一个关键帧都不写：0 就是证据")
        self.assertTrue(diag["invalidated"], "重建映射表是这条路的核心目的")

    def test_left_trim_reanchors_real_keys(self):
        """左端裁剪：关键帧按 ``left_handle + 相对帧`` 写的**绝对帧**必须整条平移。

        这是纯层 ``test_left_trim_reanchors_keys`` 的真条带对照——纯层只能看到
        "``reanchor=True`` 被传下去了"，真正要立的是**盘上证据**：承载条带的
        首个关键帧跟着新左端走，否则"画出来的线"与"真正播的速度"当场错开。
        """
        strip = self._make_speedlined_strip()
        self.assertEqual(watch.observe_strip(strip), False)  # 收编基准
        carrier_strip = self.carrier.carrier_of(strip)
        keys_before = self.carrier.read_keys(carrier_strip)
        shifted = 30
        strip.left_handle = int(strip.left_handle) + shifted
        self.assertTrue(watch.observe_strip(strip))

        keys_after = self.carrier.read_keys(carrier_strip)
        self.assertEqual([key[0] for key in keys_after],
                         [key[0] + shifted for key in keys_before],
                         "整条曲线跟着左端平移：**相对坐标一个不变**")
        self.assertEqual([key[1] for key in keys_after],
                         [key[1] for key in keys_before],
                         "重锚只改绝对帧，不改速度值（模型没动）")
        self.assertEqual(keys_after[0][0], float(strip.left_handle))
        self.assertTrue(self.carrier.is_aligned(strip, carrier_strip))

    def test_first_sight_never_commits_the_payload_but_still_lands(self):
        """首次见到：**不写模型载荷属性**，但确实落了地（修订 R1）。

        纯层那两条只能证明"``write_prop=False`` 被传进 ``_commit``"；这里盯的是
        真 ``STORE.commit`` 有没有被调到——它是唯一会把 ``seqvel_curve`` /
        ``seqvel_curve_range`` 回写盘上的入口，也是 ADR-0008"旧文件区间未知时
        不猜、不迁移"的守门人。
        """
        strip = self._make_speedlined_strip()
        carrier_strip = self.carrier.carrier_of(strip)
        payload_before = list(strip[self.store.PROP_KEY])

        commits = []
        real_commit = self.store.STORE.commit
        self.store.STORE.commit = lambda *a, **kw: commits.append(a)
        try:
            self.assertEqual(watch.observe_strip(strip), False, "首次见到不算一次变化")
        finally:
            self.store.STORE.commit = real_commit

        self.assertEqual(commits, [], "首次见到不该回写载荷属性")
        self.assertEqual(list(strip[self.store.PROP_KEY]), payload_before)
        self.assertTrue(self.carrier.is_aligned(strip, carrier_strip),
                        "没写属性 ≠ 什么都没做：承载照样要落地")

        # 对照：真观测到区间变化时，载荷区间必须跟着走（否则上面那条是空话）
        strip.right_handle = strip.right_handle + 20
        self.assertTrue(watch.observe_strip(strip))
        self.assertEqual(list(STORE.read_range(strip)),
                         [float(strip.left_handle), float(strip.right_handle)])

    def test_landing_through_a_copy_never_reaches_the_original(self):
        """[校准项] 经**求值副本**做的落地到不了原始条带——上面那条判据的前提。

        实测（``.scratch/extend-blackout/probe_04_write_target.json``）：全量落地
        走副本时 `diag` 照样报 `written: 3`，而**原始承载的关键帧一个字没动**。
        仪器分得开"写在哪"，这条断言就是它的自证。
        """
        strip = self._make_speedlined_strip()
        carrier_strip = self.carrier.carrier_of(strip)
        curve = STORE.get(strip)
        shifted = 30
        strip.left_handle = int(strip.left_handle) + shifted
        keys_before = self.carrier.read_keys(carrier_strip)

        scene = strip.id_data
        copy = scene.evaluated_get(self.bpy.context.evaluated_depsgraph_get())
        self.assertIsNot(copy, scene, "前提：求值副本与原始不是同一个数据块")
        copy_strip = None
        for candidate in copy.sequence_editor.strips:
            if candidate.name == strip.name:
                copy_strip = candidate
        self.assertIsNotNone(copy_strip, "前提：副本上也有同名条带（旧代码会挑中它）")

        diag = self.carrier.write_curve(copy_strip, curve)
        self.assertTrue(diag["written"], "它自己以为写成功了——这正是骗人的地方")
        self.assertEqual(self.carrier.read_keys(carrier_strip), keys_before,
                         "写副本不许改到原始承载（改到了说明这套仪器前提不成立）")

        self.carrier.write_curve(strip, curve)  # 对照：同一次调用写在原始上
        self.assertEqual([key[0] for key in self.carrier.read_keys(carrier_strip)],
                         [key[0] + shifted for key in keys_before],
                         "写在原始上，关键帧就得跟着左端平移")

    def test_handler_lands_on_the_original_not_the_evaluated_copy(self):
        """[修订 R3 的病灶] 依赖图回调给的是**求值副本**，落地必须落在原始上。

        真机症状：拉长素材右端 → 那一段永远是黑的；而 `diag` 报
        `invalidated: True / aligned: True`、`observe_strip` 返回 True——链子看着
        全跑了，引擎侧那张映射表从没被重建。分辨测试（用户在控制台跑裸
        `invalidate_speed_map`）当场就好了 ⇒ 病在"写在哪"，不在"写没写"。

        判据用**左裁**：它会真写承载的关键帧（绝对帧整条平移），写在副本上就一个字
        都落不下来。证据与读数见 ``.scratch/extend-blackout/evidence.md``。
        """
        strip = self._make_speedlined_strip()
        scene = strip.id_data
        self.assertEqual(watch.observe_scene(scene), 0)  # 收编基准
        carrier_strip = self.carrier.carrier_of(strip)
        keys_before = self.carrier.read_keys(carrier_strip)

        shifted = 30
        strip.left_handle = int(strip.left_handle) + shifted
        copy = scene.evaluated_get(self.bpy.context.evaluated_depsgraph_get())

        watch._on_depsgraph(scene, FakeDepsgraph([copy]))

        keys_after = self.carrier.read_keys(carrier_strip)
        self.assertEqual([key[0] for key in keys_after],
                         [key[0] + shifted for key in keys_before],
                         "经 handler 落地也必须写在**原始**承载上（副本上的写会被丢）")
        self.assertEqual([key[1] for key in keys_after],
                         [key[1] for key in keys_before],
                         "重锚只改绝对帧，不改速度值")

    def test_scene_level_pass_processes_real_scene(self):
        strip = self._make_speedlined_strip()
        scene = strip.id_data
        watch.observe_scene(scene)
        strip.right_handle = strip.left_handle + 40
        self.assertEqual(watch.observe_scene(scene), 1)
        self.assertEqual(STORE.get(strip).points[-1][0], 40.0)

    def test_handlers_survive_file_load(self):
        """[回归] Blender 默认在加载新文件时清空未标 persistent 的处理器。

        用户"打开 .blend"后监听器全灭 = 末端跟随真机无效的根因（ADR-0004 实现注记）。
        """
        import os
        import tempfile

        from seqvel import store as store_mod

        watch.register_handlers()
        store_mod.register_handlers()
        path = os.path.join(tempfile.gettempdir(), "seqvel_persist_test.blend")
        self.bpy.ops.wm.save_as_mainfile(filepath=path)
        try:
            self.bpy.ops.wm.open_mainfile(filepath=path)
            self.assertIn(watch._on_depsgraph,
                          list(self.bpy.app.handlers.depsgraph_update_post))
            self.assertIn(watch.reset, list(self.bpy.app.handlers.undo_post))
            self.assertIn(watch.reset, list(self.bpy.app.handlers.redo_post))
            self.assertIn(watch.reset, list(self.bpy.app.handlers.load_post))
            self.assertIn(store_mod._invalidate_cache,
                          list(self.bpy.app.handlers.load_post))
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestMemoryPreviewInvalidation(unittest.TestCase):
    """内存预览的失效（ADR-0011 决定 5）：改过东西就作废，构建期除外。

    判据刻意保守：**任何**进了序列场景依赖图更新的事件都算改动。宁可让用户
    多按一次 Shift+B，也不留着过期画面骗人。

    唯一的例外方向是"哪条时间线"：多场景工程里 A 场景的成果不该由 B 场景的
    编辑买单（``observe_memory_preview`` 负责把场景名递下去）。
    """

    def setUp(self):
        from seqvel import ram

        self.ram = ram
        self.ram.reset()
        self.scene = FakeScene("Scene")
        self.other = FakeScene("Other")

    def tearDown(self):
        self.ram.reset()

    def _built(self, scene_name="Scene"):
        self.ram.begin(1, 240, self.ram.SOURCE_SCENE, scene_name=scene_name)
        self.ram.finish(self.ram.OUTCOME_FINISHED, 240)
        self.assertTrue(self.ram.STATE["built"])

    def test_a_change_in_the_sequence_scene_drops_the_preview(self):
        self._built()
        self.assertTrue(watch.observe_memory_preview(self.scene))
        self.assertEqual(self.ram.STATE["outcome"], self.ram.OUTCOME_INVALIDATED)
        self.assertIsNone(self.ram.cached_range())

    def test_updates_are_consumed_once_per_scene(self):
        """多个目标场景重复调用也算一次（按"做到了没有"，不按次数）。"""
        self._built()
        self.assertTrue(watch.observe_memory_preview(
            self.scene, [self.scene, self.other]))
        self.assertFalse(watch.observe_memory_preview(
            self.scene, [self.scene, self.other]))

    def test_building_ignores_updates(self):
        """构建期间不许被打断：那时 UI 锁着，更新只可能是我们自己激起的。"""
        self.ram.begin(1, 240, self.ram.SOURCE_SCENE)
        self.assertFalse(watch.observe_memory_preview(self.scene))
        self.assertTrue(self.ram.STATE["building"])

    def test_scenes_without_a_sequencer_are_ignored(self):
        """没有序列编辑器的场景（纯 3D 场景）动一下不该牵连预览。"""
        self._built()
        bare = types.SimpleNamespace(name="Bare", sequence_editor=None)
        self.assertFalse(watch.observe_memory_preview(bare))
        self.assertTrue(self.ram.STATE["built"])

    def test_another_scenes_change_leaves_the_preview_intact(self):
        """多场景工程：成果记在它自己那条时间线名下，别人动了不算数。"""
        self._built(scene_name="Scene")
        self.assertFalse(watch.observe_memory_preview(self.other))
        self.assertTrue(self.ram.STATE["built"], "别的时间线改动不该作废我这份")
        self.assertEqual(self.ram.cached_range(), (1, 240))

    def test_a_preview_without_a_recorded_scene_still_invalidates(self):
        """没记下场景名（旧路径 / 读不到名字）→ 按最保守处理，照样作废。"""
        self._built(scene_name=None)
        self.assertTrue(watch.observe_memory_preview(self.other))

    def test_nothing_built_means_nothing_to_invalidate(self):
        self.assertFalse(watch.observe_memory_preview(self.scene))
        self.assertEqual(self.ram.STATE["outcome"], self.ram.OUTCOME_IDLE)

    def test_undo_and_redo_reset_the_preview_state(self):
        """撤销 / 重做 / 载入：整体归零，连范围一起忘掉。"""
        self._built()
        watch.reset()
        self.assertEqual(self.ram.STATE["outcome"], self.ram.OUTCOME_IDLE)
        self.assertIsNone(self.ram.STATE["start"])
        self.assertFalse(self.ram.STATE["built"])


if __name__ == "__main__":
    unittest.main()
