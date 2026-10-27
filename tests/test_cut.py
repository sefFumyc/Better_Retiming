# -*- coding: utf-8 -*-
"""切断（Blender 的 Split）之后的对账：划分链判据 + 纯划分切片 + 缓存有效性。

语义与判据见 `docs/adr/0008-cut-is-a-division-not-a-trim.md` 与
`.scratch/strip-cut/spec.md`。两层：

- 替身层（纯 Python）：把"原生切断把载荷**逐字复制**到两半、两半区间各自砍半、
  而载荷自描述的区间仍是切前那一条"这件事搬进假场景，钉死判据与对账分支；
- bpy 层（headless Blender）：真 ``strip.split()`` 之后两半都是忠实切片，
  且**属性侧一字未动**（修补只在内存里，ADR-0008 决定 6）。
"""

import unittest

from seqvel import store as store_mod
from seqvel import watch as watch_mod
from seqvel.curve import LINEAR, SMOOTH, Curve

from support import HAVE_BPY, make_strip, reset_scene

PROP = store_mod.PROP_KEY
TYPES = store_mod.TYPES_KEY
RANGE = store_mod.RANGE_KEY
STORE = store_mod.STORE


class FakeEditor(object):
    def __init__(self):
        self.strips = []


class FakeScene(object):
    def __init__(self, name="场景"):
        self.name = name
        self.sequence_editor = FakeEditor()


class FakeStrip(object):
    """零 bpy 的条带替身：几何 + id_data（含同场景其它条带）+ 属性表。"""

    def __init__(self, name, left=1.0, right=101.0, scene=None, props=None):
        self.name = name
        self.type = "MOVIE"
        self.channel = 1
        self.left_handle = float(left)
        self.right_handle = float(right)
        self.id_data = scene if scene is not None else FakeScene()
        self._props = props if props is not None else {}
        self.id_data.sequence_editor.strips.append(self)

    def get(self, key, default=None):
        return self._props.get(key, default)

    def __getitem__(self, key):
        return self._props[key]

    def __setitem__(self, key, value):
        self._props[key] = value

    def __delitem__(self, key):
        del self._props[key]

    def __contains__(self, key):
        return key in self._props

    def keys(self):
        return list(self._props)


def flat(*pairs):
    """平坦浮点载荷（与 ``Curve.to_flat`` 同格式）。"""
    out = []
    for frame, speed in pairs:
        out.extend((float(frame), float(speed)))
    return out


#: 一条 1.0x → 3.0x 的线性曲线（相对帧 0..100），切断用例的基准
WHOLE = flat((0.0, 1.0), (100.0, 3.0))
#: 整条素材的绝对区间（条带 1..101）
DECLARED = (1.0, 101.0)


def pair_props(points, types, declared):
    out = {PROP: list(points),
           RANGE: [float(declared[0]), float(declared[1])]}
    if types is not None:
        out[TYPES] = list(types)
    return out


def cut_pair(points=WHOLE, types=(0,), declared=DECLARED, seam=50.0, scene=None):
    """造出**原生切断之后**的现场：载荷逐字复制到两半、区间各自砍半。

    关键事实（`.scratch/probe_split/report_final.json`）：载荷与类型是**逐字复制**
    的，谁都没重锚定——右半载荷里的点仍然以**左半的左端**为原点。
    """
    scene = scene if scene is not None else FakeScene()
    left = FakeStrip("A", left=declared[0], right=seam, scene=scene,
                     props=pair_props(points, types, declared))
    right = FakeStrip("A.001", left=seam, right=declared[1], scene=scene,
                      props=pair_props(points, types, declared))
    return left, right


class DivisionChainTest(unittest.TestCase):
    """判据本身：什么算"切断留下的专属证据"（CONTEXT.md「划分链」）。"""

    def setUp(self):
        STORE.invalidate()

    def tearDown(self):
        STORE.invalidate()

    def test_cut_pair_is_a_division_chain_from_either_side(self):
        left, right = cut_pair()
        self.assertEqual(STORE.division_chain(left), [left, right])
        self.assertEqual(STORE.division_chain(right), [left, right])

    def test_three_way_cut_of_the_same_payload_is_one_chain(self):
        """连着切两刀：同一条载荷被劈成三段，仍是同一条链（拼得回声明区间）。"""
        scene = FakeScene()
        strips = [FakeStrip("A", left=1.0, right=40.0, scene=scene,
                            props=pair_props(WHOLE, (0,), DECLARED)),
                  FakeStrip("A.001", left=40.0, right=70.0, scene=scene,
                            props=pair_props(WHOLE, (0,), DECLARED)),
                  FakeStrip("A.002", left=70.0, right=101.0, scene=scene,
                            props=pair_props(WHOLE, (0,), DECLARED))]
        for strip in strips:
            self.assertEqual(STORE.division_chain(strip), strips)

    def test_single_strip_is_not_a_chain(self):
        strip = FakeStrip("solo", props=pair_props(WHOLE, (0,), DECLARED))
        self.assertIsNone(STORE.division_chain(strip))

    def test_butted_copy_that_cannot_tile_is_not_a_chain(self):
        """复制粘贴贴着放：载荷相同、首尾贴合，但**拼不回**声明区间 → 不是划分。"""
        scene = FakeScene()
        origin = FakeStrip("A", left=1.0, right=101.0, scene=scene,
                           props=pair_props(WHOLE, (0,), DECLARED))
        copy = FakeStrip("A.001", left=101.0, right=201.0, scene=scene,
                         props=pair_props(WHOLE, (0,), DECLARED))
        self.assertIsNone(STORE.division_chain(copy))
        self.assertIsNone(STORE.division_chain(origin))

    def test_same_payload_but_not_butted_is_not_a_chain(self):
        scene = FakeScene()
        left = FakeStrip("A", left=1.0, right=50.0, scene=scene,
                         props=pair_props(WHOLE, (0,), DECLARED))
        other = FakeStrip("B", left=60.0, right=111.0, scene=scene,
                          props=pair_props(WHOLE, (0,), DECLARED))
        self.assertIsNone(STORE.division_chain(left))
        self.assertIsNone(STORE.division_chain(other))

    def test_isolated_half_is_not_a_chain(self):
        """孤立半段（另一半被删）：只剩"载荷溢出自身时长"这条孤证，判不成划分。"""
        strip = FakeStrip("A", left=1.0, right=50.0,
                          props=pair_props(WHOLE, (0,), DECLARED))
        self.assertIsNone(STORE.division_chain(strip))

    def test_legacy_payload_without_a_range_is_never_a_chain(self):
        """旧 .blend 没有区间属性 = 区间未知 → 老语义，连判据都不进。"""
        scene = FakeScene()
        props = {PROP: list(WHOLE), TYPES: [0]}
        left = FakeStrip("A", left=1.0, right=50.0, scene=scene, props=dict(props))
        FakeStrip("A.001", left=50.0, right=101.0, scene=scene, props=dict(props))
        self.assertIsNone(STORE.division_chain(left))


class CutReadingTest(unittest.TestCase):
    """读路径对账：划分成立就切片（只在内存里），其余一律退回 ADR-0004 的老语义。"""

    def setUp(self):
        STORE.invalidate()

    def tearDown(self):
        STORE.invalidate()

    # ---------- 两半都是忠实切片 ----------

    def test_left_half_is_a_faithful_slice_not_a_compression(self):
        """左半的末点是**切点处的插值**（1.98），不是被拖过来的原末点值（3.0）。"""
        left, _right = cut_pair()
        curve = STORE.get(left)
        self.assertEqual(curve.points, [(0.0, 1.0), (49.0, 1.98)])
        self.assertTrue(curve.is_valid())

    def test_right_half_starts_at_the_seam(self):
        """右半的首点是**接缝处的插值**，不是"整份复制的前半段抬头"。"""
        _left, right = cut_pair()
        curve = STORE.get(right)
        self.assertEqual(curve.points, [(0.0, 1.98), (51.0, 3.0)])

    def test_seam_speeds_are_equal(self):
        left, right = cut_pair()
        self.assertAlmostEqual(STORE.get(left).line_speed_at(49.0),
                               STORE.get(right).line_speed_at(0.0), places=9)

    def _assert_union_matches(self, points, types, declared, seam, label):
        """两半并集 = 切前**逐帧**：把两半各自的重定基坐标换回绝对帧再比。

        切点那一帧归右半（它与左半的末帧同速，两种取法都相等），与"切点归右半"
        的语义一致。返回两个模型，调用方可以再钉各自的点列。
        """
        left, right = cut_pair(points=points, types=types, declared=declared, seam=seam)
        whole = Curve.from_flat(points, declared[1] - declared[0], types=list(types))
        left_model, right_model = STORE.get(left), STORE.get(right)
        base_left = store_mod.strip_span(left)[0]
        base_right = store_mod.strip_span(right)[0]
        for absolute in range(int(declared[0]), int(declared[1]) + 1):
            expected = whole.line_speed_at(absolute - declared[0])
            if absolute < base_right:
                got, where = left_model.line_speed_at(absolute - base_left), "左半"
            else:
                got, where = right_model.line_speed_at(absolute - base_right), "右半"
            self.assertAlmostEqual(got, expected, places=6,
                                   msg="%s：绝对帧 %d（%s）" % (label, absolute, where))
        return left_model, right_model

    def test_union_of_the_halves_equals_the_curve_before_the_cut(self):
        """切点落在**段内部**（规格「验收判据」三种位置之一）。

        两半各自的点列由上面那两条专职用例钉住，这里只比并集。
        """
        self._assert_union_matches(WHOLE, (0,), DECLARED, 50.0, "段内部")

    def test_union_holds_when_the_seam_lands_on_an_existing_point(self):
        """切点落在**已有点**上：该点由两半共享，不许被谁吞掉、也不许造重。"""
        points = flat((0.0, 1.0), (60.0, 2.0), (100.0, 3.0))
        left_model, right_model = self._assert_union_matches(points, (0, 0), DECLARED,
                                                            61.0, "已有点")
        self.assertEqual(left_model.points, [(0.0, 1.0), (60.0, 2.0)])
        self.assertEqual(right_model.points, [(0.0, 2.0), (40.0, 3.0)])

    def test_union_holds_when_the_curve_ends_before_the_seam(self):
        """切点落在**末段之外**：曲线早于切点结束，两半都读到外推水平线。

        左半必须保住自己的真实形状（**不被压缩**成"末点值搬到切点"），尾端平延到
        自己的右端；右半整段是那条水平线。
        """
        points = flat((0.0, 1.0), (30.0, 2.0))
        left_model, right_model = self._assert_union_matches(points, (0,), DECLARED,
                                                            70.0, "末段之外")
        self.assertEqual(left_model.points, [(0.0, 1.0), (30.0, 2.0), (69.0, 2.0)])
        self.assertEqual(right_model.points, [(0.0, 2.0), (31.0, 2.0)])

    def test_eased_seam_is_interpolated_by_the_segment_type(self):
        points = flat((0.0, 1.0), (80.0, 2.0))
        left, _right = cut_pair(points=points, types=(1,), declared=(1.0, 81.0),
                                seam=40.0)
        whole = Curve.from_flat(points, 80, types=[1])
        self.assertAlmostEqual(STORE.get(left).line_speed_at(39.0),
                               whole.line_speed_at(39.0), places=9)
        self.assertAlmostEqual(whole.line_speed_at(39.0), 1.4812539, places=6,
                               msg="平滑段：插值必须吃段类型（线性会是 1.4875）")

    def test_dirty_cache_on_the_same_name_follows_the_cut(self):
        """症状 1：左半**沿用同名条带**，缓存里还是切前那条曲线 → 读一次就该对账。"""
        scene = FakeScene()
        left = FakeStrip("A", left=1.0, right=101.0, scene=scene,
                         props=pair_props(WHOLE, (0,), DECLARED))
        before = STORE.get(left)
        self.assertEqual(before.points, [(0.0, 1.0), (100.0, 3.0)])
        FakeStrip("A.001", left=50.0, right=101.0, scene=scene,
                  props=pair_props(WHOLE, (0,), DECLARED))
        left.right_handle = 50.0  # 原生切断：左半区间当场变成 1..50
        self.assertEqual(STORE.get(left).points, [(0.0, 1.0), (49.0, 1.98)])

    def test_cold_read_of_the_right_half_is_the_slice(self):
        """症状 2：右半是新名字（冷读）→ 不许再是"整份复制的前半段"。"""
        _left, right = cut_pair()
        STORE.invalidate()
        self.assertEqual(STORE.get(right).points, [(0.0, 1.98), (51.0, 3.0)])

    def test_reading_never_writes_the_payload_back(self):
        """[决定 6] 修补只在内存里：两半的属性一字未动，且不影响后续读。"""
        left, right = cut_pair()
        before = [float(v) for v in left[PROP]]
        STORE.get(left)
        STORE.get(right)
        self.assertEqual([float(v) for v in left[PROP]], before)
        self.assertEqual([float(v) for v in right[PROP]], before)

    # ---------- 老语义不许被顺手改掉 ----------

    def test_trim_without_a_sibling_keeps_the_end_carry_reading(self):
        """**拖右端句柄**（裁剪）不许被误判成划分：没有贴合的同载荷邻居 →
        老语义（读取时静默钳回 = 压缩，末点速度**不变**）。"""
        strip = FakeStrip("A", left=1.0, right=101.0,
                          props=pair_props(WHOLE, (0,), DECLARED))
        strip.right_handle = 50.0
        self.assertIsNone(STORE.division_chain(strip))
        self.assertEqual(STORE.get(strip).points, [(0.0, 1.0), (49.0, 3.0)])

    def test_legacy_payload_without_range_reads_the_old_way(self):
        strip = FakeStrip("A", left=1.0, right=50.0,
                          props={PROP: list(WHOLE), TYPES: [0]})
        self.assertEqual(STORE.get(strip).points, [(0.0, 1.0), (49.0, 3.0)])

    def test_butted_copy_is_read_as_a_copy(self):
        """复制粘贴：载荷按自己的原点解释（一份副本），不做任何重定基。"""
        scene = FakeScene()
        FakeStrip("A", left=1.0, right=101.0, scene=scene,
                  props=pair_props(WHOLE, (0,), DECLARED))
        copy = FakeStrip("A.001", left=101.0, right=201.0, scene=scene,
                         props=pair_props(WHOLE, (0,), DECLARED))
        self.assertEqual(STORE.get(copy).points, [(0.0, 1.0), (100.0, 3.0)])

    # ---------- 自描述区间--------- 

    def test_range_is_written_on_commit_and_dropped_on_forget(self):
        strip = FakeStrip("A", left=1.0, right=101.0)
        curve = STORE.get_or_create(strip)
        STORE.commit(strip, curve)
        self.assertEqual([float(v) for v in strip[RANGE]], [1.0, 101.0])
        STORE.forget(strip)
        self.assertNotIn(RANGE, strip.keys())

    def test_range_written_after_a_resize_matches_the_strip(self):
        """改短之后落盘：区间写的是**当下**的条带区间（否则下次读会误判成划分）。"""
        strip = FakeStrip("A", left=1.0, right=101.0)
        curve = STORE.get_or_create(strip)
        strip.right_handle = 61.0
        curve.set_duration(60.0)
        STORE.commit(strip, curve)
        self.assertEqual([float(v) for v in strip[RANGE]], [1.0, 61.0])

    def test_corrupt_range_attribute_degrades_to_unknown(self):
        strip = FakeStrip("A", left=1.0, right=50.0,
                          props={PROP: list(WHOLE), TYPES: [0], RANGE: [1.0]})
        self.assertIsNone(STORE.read_range(strip))
        self.assertEqual(STORE.get(strip).points, [(0.0, 1.0), (49.0, 3.0)])


class CutAfterlifeTest(unittest.TestCase):
    """切断之后的日子：监听器不许写回，真实编辑时整组物化（ADR-0008 修订 R1）。

    症状链（真机证据：``.scratch/cut-regress/report_cut_watch.json``）：切断当刻
    两半都正确，但**下一次依赖图更新**里 ``watch`` 把左半的切片连同新区间写回
    属性 → 两半载荷不再逐字相同 → 划分链断 → 右半退回"整份载荷按自己长度钳回"
    ＝ 用户看到的"复制了前半段的线"。真机上它表现为"按 K 就坏"，还会多出一个
    隐形撤销步（一次 Ctrl+Z 只修好、两次才撤回切断）。
    """

    def setUp(self):
        STORE.invalidate()
        watch_mod._TRACKED.clear()
        self.commits = []
        self._real_commit = watch_mod._commit
        watch_mod._commit = self._fake_commit

    def tearDown(self):
        watch_mod._commit = self._real_commit
        STORE.invalidate()
        watch_mod._TRACKED.clear()

    def _fake_commit(self, strip, curve, reanchor=True, write_prop=True):
        """替身照真路径走：``write_prop`` 为真才动属性。"""
        self.commits.append((strip, [tuple(p) for p in curve.points],
                             reanchor, write_prop))
        if write_prop:
            STORE.commit(strip, curve)

    def _drawn_then_cut(self):
        """真实会话的现场：线已经画好（跟踪表已有切前区间），然后切一刀。"""
        scene = FakeScene()
        left = FakeStrip("A", left=1.0, right=101.0, scene=scene,
                         props=pair_props(WHOLE, (0,), DECLARED))
        watch_mod.observe_scene(scene)
        self.assertEqual(watch_mod._TRACKED[("场景", "A")], (1.0, 101.0))
        right = FakeStrip("A.001", left=50.0, right=101.0, scene=scene,
                          props=pair_props(WHOLE, (0,), DECLARED))
        left.right_handle = 50.0
        return scene, left, right

    def test_observer_never_writes_a_division_members_payload(self):
        """监听器只落地、不写载荷：写回是那个隐形撤销步，也是断链的刀。"""
        scene, left, right = self._drawn_then_cut()
        before_left = [float(v) for v in left[PROP]]
        before_right = [float(v) for v in right[PROP]]
        self.commits.clear()

        watch_mod.observe_scene(scene)

        self.assertTrue(self.commits, "区间变了总要落一次地（承载 / 映射表）")
        for strip, _points, _reanchor, write_prop in self.commits:
            self.assertFalse(write_prop,
                             "%r 是被切出来的一半：写回载荷会毁掉划分链"
                             % strip.name)
        self.assertEqual([float(v) for v in left[PROP]], before_left)
        self.assertEqual([float(v) for v in right[PROP]], before_right)
        STORE.invalidate()
        self.assertEqual(STORE.get(right).points, [(0.0, 1.98), (51.0, 3.0)],
                         "右半必须仍是接缝处起算的忠实切片")

    def _edit_last_point(self, strip, speed=2.8):
        """在 ``strip`` 上做一次真实编辑（拖末点的速度）并落盘。"""
        curve = STORE.get(strip)
        curve.move_point(1, speed=speed)
        return STORE.commit(strip, curve)

    def test_editing_one_half_leaves_the_other_alone(self):
        """编辑一半不许把另一半拉成压缩形状（接缝 2.0 直接跳 3.0 的那个 bug）。"""
        left, right = cut_pair()
        self.assertTrue(self._edit_last_point(right))
        STORE.invalidate()
        self.assertEqual(STORE.get(left).points, [(0.0, 1.0), (49.0, 1.98)],
                         "左半必须一动不动")
        self.assertEqual(STORE.get(right).points, [(0.0, 1.98), (51.0, 2.8)])

    def test_a_three_way_cut_materializes_every_other_member(self):
        """连着切两刀：编辑第三段时，前两段也必须各自拿到**自己的**切片。

        这条钉的是"先算齐、再统一写"：边算边写时，写完第一段链就断了，第二段
        会算成"整份载荷按自己长度钳回"（`[(0,1.0),(30,3.0)]`，见下）。
        """
        scene = FakeScene()
        strips = [FakeStrip("A", left=1.0, right=40.0, scene=scene,
                            props=pair_props(WHOLE, (0,), DECLARED)),
                  FakeStrip("A.001", left=40.0, right=70.0, scene=scene,
                            props=pair_props(WHOLE, (0,), DECLARED)),
                  FakeStrip("A.002", left=70.0, right=101.0, scene=scene,
                            props=pair_props(WHOLE, (0,), DECLARED))]
        self._edit_last_point(strips[2], speed=2.9)
        STORE.invalidate()

        first, second, third = [STORE.get(s).points for s in strips]
        self.assertAlmostEqual(first[-1][1], 1.78, places=6, msg=first)
        self.assertAlmostEqual(second[0][1], 1.78, places=6, msg=second)
        self.assertAlmostEqual(second[-1][1], 2.38, places=6, msg=second)
        self.assertAlmostEqual(third[0][1], 2.38, places=6, msg=third)
        self.assertAlmostEqual(third[-1][1], 2.9, places=6, msg=third)

    def test_edited_group_is_self_describing(self):
        """一次真实编辑之后，同组每半都自带区间，不再依赖划分链。"""
        left, right = cut_pair()
        self._edit_last_point(right)
        self.assertIsNone(STORE.division_chain(left), "物化之后不再靠划分链")
        for strip in (left, right):
            self.assertEqual([float(v) for v in strip[RANGE]],
                             [float(strip.left_handle), float(strip.right_handle)],
                             "%r 应已自描述" % strip.name)
        STORE.invalidate()
        self.assertEqual(STORE.get(left).points, [(0.0, 1.0), (49.0, 1.98)])


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class RealSplitTest(unittest.TestCase):
    """真机：``strip.split()`` 之后两半都是忠实切片，且属性侧一字未动。"""

    def setUp(self):
        reset_scene()
        STORE.invalidate()
        watch_mod._TRACKED.clear()
        self.strip = make_strip(length=100)

    def tearDown(self):
        STORE.invalidate()
        watch_mod._TRACKED.clear()

    def test_real_split_yields_faithful_halves(self):
        curve = STORE.get_or_create(self.strip)
        curve.move_point(1, rel_frame=100.0, speed=3.0)
        self.assertTrue(STORE.commit(self.strip, curve))
        self.assertEqual([float(v) for v in self.strip[RANGE]], [1.0, 101.0])
        payload_before = [float(v) for v in self.strip[PROP]]

        right = self.strip.split(frame=51, split_method="SOFT")
        editor = self.strip.id_data.sequence_editor
        halves = sorted([s for s in editor.strips if s.type == self.strip.type],
                        key=lambda s: s.left_handle)
        self.assertEqual(len(halves), 2, "切断后应有两半")
        # 切点归右半：左半 1..51、右半 51..101（真机实测，与 RNA 探针一致）
        self.assertEqual(([float(s.left_handle) for s in halves],
                          [float(s.right_handle) for s in halves]),
                         ([1.0, 51.0], [51.0, 101.0]))
        STORE.invalidate()  # 模拟"下一次冷读"（真机上缓存可能还在）

        left_curve = STORE.get(halves[0])
        right_curve = STORE.get(halves[1])
        # 线性 1.0x → 3.0x 铺满 0..100：切点（相对帧 50）处的值 = 2.0
        self.assertAlmostEqual(left_curve.points[-1][0], 50.0, places=6)
        self.assertAlmostEqual(left_curve.points[-1][1], 2.0, places=6)
        self.assertAlmostEqual(right_curve.points[0][0], 0.0, places=6)
        self.assertAlmostEqual(right_curve.points[0][1], 2.0, places=6)
        self.assertAlmostEqual(right_curve.points[-1][1], 3.0, places=6)
        self.assertAlmostEqual(left_curve.line_speed_at(50.0),
                               right_curve.line_speed_at(0.0), places=9)

        # [决定 6] 修补只在内存里：属性一字未动（右半拿到的是原样复制的一份）
        self.assertEqual([float(v) for v in self.strip[PROP]], payload_before)
        self.assertEqual([float(v) for v in right[PROP]], payload_before)
        self.assertTrue(left_curve.is_valid() and right_curve.is_valid())

    def test_real_split_survives_one_observer_pass(self):
        """真机路径：切完跑一次观察（GUI 每次重绘都会来一发），两半不许被改写。

        这是 v1.6.5 修的那个 bug 的真机形态——替身层的
        ``CutAfterlifeTest::test_observer_never_writes_a_division_members_payload``
        钉的是判定矩阵，这条钉的是真条带上的载荷与曲线。
        """
        curve = STORE.get_or_create(self.strip)
        curve.move_point(1, rel_frame=100.0, speed=3.0)
        self.assertTrue(STORE.commit(self.strip, curve))
        scene = self.strip.id_data
        watch_mod.observe_scene(scene)  # 画线途中：跟踪表记下切前区间

        self.strip.split(frame=51, split_method="SOFT")
        halves = sorted([s for s in scene.sequence_editor.strips
                         if s.type == self.strip.type],
                        key=lambda s: s.left_handle)
        before = [[float(v) for v in s[PROP]] for s in halves]

        STORE.invalidate()
        watch_mod.observe_scene(scene)
        STORE.invalidate()

        self.assertEqual([[float(v) for v in s[PROP]] for s in halves], before,
                         "监听器不许把切片写回载荷")
        left_curve, right_curve = STORE.get(halves[0]), STORE.get(halves[1])
        self.assertAlmostEqual(left_curve.points[-1][1], 2.0, places=6)
        self.assertAlmostEqual(right_curve.points[0][1], 2.0, places=6)


if __name__ == "__main__":
    unittest.main()
