# -*- coding: utf-8 -*-
"""原生承载层：把影子模型落成 Speed Control 效果条带的关键帧。

承载机制（调研结论）：``SpeedControlStrip`` 的 ``speed_control='MULTIPLY'`` 是
唯一能连续表达任意速度曲线、且 Python 可全量读写的原生通道——C 侧会把
``speed_factor`` 关键帧**逐帧累加**成源帧映射，语义正好是"速度的积分"。

本模块的职责边界：只与 bpy 数据打交道，不碰绘制与交互。

写入顺序（重要）：``取/建承载条带 → 重置输入条带 Retiming → 重新对齐 →
清旧关键帧 → 逐点写 → 设 LINEAR 插值``。重置放在对齐之前，因为重置可能
改变输入条带的时长。
"""

import re

import bpy

from .curve import to_abs_frame
from . import VERSION_TAG, i18n, rules, regions
from .store import STORE, strip_base_frame, strip_channel, strip_duration

#: 从 fcurve 的 RNA 路径里提取归属条带名（``sequence_editor.strips_all["X"].speed_factor``）
_CURVE_OWNER_RE = re.compile(r'\["([^"]+)"\]\.speed_factor$')

#: 承载条带名前缀
CARRIER_PREFIX = "SPD_"
#: 只有 MULTIPLY 模式读取 speed_factor 关键帧
SPEED_MODE = "MULTIPLY"
#: 帧精度策略：**永不**做相邻帧交叉淡化（即"混合帧"）。
#: 混合帧是拿 floor / ceil 两帧按小数权重叠出来（``do_speed_effect`` 里就是一次
#: cross 溶解），观感即重影。而场景条带的源时间本来就是连续的——插值关时小数原样
#: 传给输入条带，场景渲染器吃 ``RE_RenderFrame(..., subframe)`` 做子帧渲染，
#: 所以对场景素材关掉混合**零代价**，纯粹去掉重影。movie / image 素材关掉后按
#: 取整选帧，非整数倍速会有重复帧——这是与原生 Retiming 同构的固有代价，
#: 不用混合去掩盖它。
#: 坑：``render.cc`` 里 ``Only convert to int when interpolation is not used.``
#: 这句注释与它下面的判断条件**写反了**（实际是"打开插值时才 floor"）。
USE_FRAME_INTERPOLATE = False
#: 关键帧插值：与"分段线性"的影子模型一致，保证所见即所得
KEY_INTERPOLATION = "LINEAR"
#: 会吞掉子帧的缓存开关（``SequenceEditor`` 上的 RNA 属性，需读 / 写）。
#: 上游缺陷（``cache/source_image_cache.cc``）：get / put **先
#: ``math::round(timeline_frame)`` 再建键**，而这个键本是给"条带自己的整数时间"
#: 设计的。Speed Control 恰好把小数源位置放在 ``timeline_frame`` 里
#: （``strip_speed_effect_target_frame_get`` 的返回值），于是同一个整数源帧内的
#: 输出帧全部撞同一个键 —— 源场景被以整数时间求值，观感就是掉帧。
#: 原生 Retiming 的小数产生在 ``give_frame_index``、即取整**之后**，所以它不中招；
#: 这是本插件唯一一处"在自身范围内修不好"的问题。原因与取舍见
#: ``docs/adr/0002-speed-carrier-despite-raw-cache.md``。
RAW_CACHE_FLAG = "use_cache_raw"


# --------------------------------------------------------------------------
# 查询 / 创建 / 对齐
# --------------------------------------------------------------------------

def editor_of(strip):
    scene = getattr(strip, "id_data", None)
    return getattr(scene, "sequence_editor", None)


def iter_carriers(strip):
    """遍历绑定到该目标的承载条带（按 **input_1 绑定**，不按名字）。"""
    editor = editor_of(strip)
    if editor is None:
        return
    try:
        candidates = list(editor.strips)
    except Exception:
        return
    for candidate in candidates:
        if getattr(candidate, "type", "") != "SPEED":
            continue
        try:
            if getattr(candidate, "input_1", None) == strip:
                yield candidate
        except Exception:
            continue


def carrier_of(strip):
    """取回该目标的承载条带（同一目标恒一条）。"""
    for carrier in iter_carriers(strip):
        return carrier
    return None


def align_carrier(strip, carrier):
    """对齐承载条带：通道 = 目标 + 1，位置 / 长度 = 目标区间。"""
    if carrier is None:
        return False
    changed = False
    try:
        if carrier.channel != strip.channel + 1:
            carrier.channel = strip.channel + 1
            changed = True
    except Exception:
        pass
    try:
        if carrier.frame_start != strip.left_handle:
            carrier.frame_start = strip.left_handle
            changed = True
    except Exception:
        pass
    try:
        if carrier.right_handle != strip.right_handle:
            carrier.right_handle = strip.right_handle
            changed = True
    except Exception:
        pass
    return changed


def is_aligned(strip, carrier):
    """承载条带是否已对齐到目标（通道 = 目标 + 1，位置 / 长度 = 目标区间）。"""
    if carrier is None or strip is None:
        return False
    try:
        return (carrier.channel == strip.channel + 1
                and carrier.frame_start == strip.left_handle
                and carrier.right_handle == strip.right_handle)
    except Exception:
        return False


def _new_diag(carrier, created):
    """两条落地路径（轻量 ``relay_span`` / 全量 ``write_curve``）共用的诊断骨架。

    面板只认这几个键（``ui`` 里那行"上次写入"），两条路径给同一形状，用户
    看到的行才不会因为走了哪条路而缺字段、显示成 ``?``。
    ``points`` / ``written`` 由各自补齐——轻量路径**确实一个关键帧都不写**，
    如实报 0 才说明"这条路没重写关键帧"。
    ``invalidated`` 同样两条路都得有：它是"收尾有没有让 Blender 重算速度映射表"
    的如实汇报（ADR-0004 修订 R2）。预置 ``False`` 才能把"这一步漏了"与
    "这一步失败了"分开——漏了会稳定报 False，而不是整个键不见。
    """
    return {
        "carrier": None if carrier is None else carrier.name,
        "created": created,
        "retiming_reset": False,
        "adjusted": False,
        "aligned": False,
        "invalidated": False,
        "points": 0,
        "written": 0,
        "errors": [],
    }


def _note_alignment(diag, strip, carrier):
    """落地之后再体检对齐：不一致既进 ``diag`` 也上面板（面板是唯一信息出口）。

    注意 ``align_carrier`` 对效果条带的 ``frame_start`` / ``right_handle`` 写
    **从来就是失败的**（只读属性），位置一直靠 Blender 自己跟随；所以这里报的
    "未对齐"是现状陈述，不是"刚才那一步没生效"。
    """
    diag["aligned"] = is_aligned(strip, carrier)
    if not diag["aligned"]:
        diag["errors"].append(i18n.T("carrier.not_aligned"))
    return diag


def ensure_carrier(strip):
    """取回或新建承载条带，并保证模式 / 取帧策略正确。

    取帧策略是**无条件覆盖写**的：旧 ``.blend`` 里可能还留着开着混合帧的承载条带，
    每次落地都按当前策略重写一遍，等于顺带治愈（策略不是曲线数据，不进影子模型）。
    """
    editor = editor_of(strip)
    if editor is None:
        return None
    carrier = carrier_of(strip)
    if carrier is None:
        channel = strip.channel + 1
        length = max(1, int(round(strip_duration(strip))))
        try:
            carrier = editor.strips.new_effect(
                name=CARRIER_PREFIX + strip.name,
                type="SPEED",
                channel=channel,
                frame_start=int(strip.left_handle),
                length=length,
                input1=strip,
            )
        except Exception:
            return None
    try:
        carrier.speed_control = SPEED_MODE
    except Exception:
        pass
    # 恒定写 False（不提供开关）：没有"要不要混合"这个选择，理由见常量注释。
    try:
        carrier.use_frame_interpolate = USE_FRAME_INTERPOLATE
    except Exception:
        pass
    return carrier


def delete_carriers(strip):
    """删除绑定到该目标的所有承载条带（先摘掉 fcurve，避免留下孤儿动画）。"""
    editor = editor_of(strip)
    if editor is None:
        return 0
    removed = 0
    for candidate in list(iter_carriers(strip)):
        purge_keys(candidate)
        try:
            editor.strips.remove(candidate)
            removed += 1
        except Exception:
            pass
    return removed


def invalidate_speed_map(strip):
    """强制 Blender 重建承载条带的速度映射表（``SpeedControlVars.frameMap``）。

    **为什么非要有这一步**（真机实测 + 上游源码，证据在
    ``.scratch/carrier-relaunch/evidence.md``）：那张表是**惰性构建、且建一次就不再更新**的。
    ``strip_effect_speed_frame_map_ensure`` 只在 ``frameMap == nullptr`` 时才建，
    长度写死为**构建那一刻**的 ``right_handle − left_handle``；而求值时
    ``target_frame = s->frameMap[frame_index]`` **没有边界检查**。唯一的重建入口
    ``relations_invalidate_cache()`` 只对"**自己就是 SPEED 条带**"的条带生效。

    于是目标条带被拉长 / 左裁时——Blender 只把承载条带的 ``enddisp`` 跟着改了
    （``strip_time_effect_range_set``），**没有任何代码连带失效它的映射表**——
    新长出来的那一段落在旧表之外，越界读到未初始化内存：画面在开头冻住或乱跳，
    直到某次恰好触发重建才"突然自己好了"（用户报告的原话）。

    手段：重写 ``speed_control``。它的 RNA update 是
    ``rna_Strip_invalidate_raw_update`` → ``relations_invalidate_cache_raw`` →
    ``relations_invalidate_cache`` → ``strip_effect_speed_rebuild_map``，链路在
    探针 02 里逐条验过（``channel`` / ``use_frame_interpolate`` 同样有效）；
    **同值写也会触发**——RNA 更新回调不做等值短路。

    两条看似更直接的路**走不通**，别退回去：条带的 ``left_handle`` /
    ``right_handle`` 对效果条带是**只读**的（``AttributeError``，实测），
    ``channel`` 会牵动原生重叠重排（承载条带与邻居同通道时会把别人挪走）。

    返回**失败原因**：成功是空串，失败给 ``"类型: 消息"``（与 ``cache_raw_state``
    同一格式）。不返回布尔——那条异常原文是用户唯一能得到的信息，吞掉它
    ``errors`` 里就只剩一句"重建没成功"，等于把"不静默"这条纪律丢在这一层。
    读不到承载条带同样算失败（给一句说明），由调用方决定要不要报。
    """
    carrier = carrier_of(strip)
    if carrier is None:
        return i18n.T("carrier.no_carrier")
    try:
        carrier.speed_control = SPEED_MODE
    except Exception as exc:
        return "%s: %s" % (type(exc).__name__, exc)
    return ""


# --------------------------------------------------------------------------
# 原生 Retiming 重置（重置重建）
# --------------------------------------------------------------------------

class _SoleSelect(object):
    """临时只选中目标条带（retiming ops 作用于"选中条带"，必须独占选择）。"""

    def __init__(self, strip):
        self.strip = strip
        self.editor = editor_of(strip)
        self.saved = []

    def __enter__(self):
        if self.editor is None:
            return self
        try:
            strips = list(self.editor.strips)
        except Exception:
            strips = []
        for other in strips:
            try:
                self.saved.append((other, bool(other.select)))
                other.select = other == self.strip
            except Exception:
                pass
        self.saved_active = getattr(self.editor, "active_strip", None)
        try:
            self.editor.active_strip = self.strip
        except Exception:
            pass
        return self

    def __exit__(self, *_exc):
        for other, flag in self.saved:
            try:
                other.select = flag
            except Exception:
                pass
        if self.editor is not None:
            try:
                self.editor.active_strip = self.saved_active
            except Exception:
                pass
        return False


def reset_retiming(strip):
    """重置输入条带上的原生 Retiming，避免与 Speed Control 叠乘。

    只在条带真有原生 Retiming 数据时才动手（无数据时 ops 是空操作，
    却会白改一次选择状态）。返回是否真的执行了重置。
    """
    keys = getattr(strip, "retiming_keys", None)
    if keys is None:  # 该条带类型没有 retiming 数据
        return False
    try:
        if len(keys) == 0:
            return False
    except Exception:
        return False

    try:
        with _SoleSelect(strip):
            bpy.ops.sequencer.retiming_reset()
        if len(getattr(strip, "retiming_keys", ())) == 0:
            return True
    except Exception:
        pass

    # 兜底：逐个 remove（首末 key 删不掉，但至少尽力清中间）
    removed = False
    for key in list(getattr(strip, "retiming_keys", ())):
        try:
            key.remove()
            removed = True
        except Exception:
            pass
    return removed


# --------------------------------------------------------------------------
# 关键帧读写
# --------------------------------------------------------------------------

def locate_speed_curve(carrier):
    """定位承载条带 ``speed_factor`` 的 fcurve 与所属 channelbag（5.x 分层 action）。

    返回 ``(fcurve, channelbag)``；结构不可用时返回 ``(None, None)``。
    """
    scene = getattr(carrier, "id_data", None)
    action = getattr(getattr(scene, "animation_data", None), "action", None)
    if action is None:
        return None, None
    try:
        layers = action.layers
    except Exception:
        return None, None
    needle = '["%s"]' % carrier.name
    fallback = []
    for layer in layers:
        for layer_strip in getattr(layer, "strips", ()):
            for bag in getattr(layer_strip, "channelbags", ()):
                for fcurve in getattr(bag, "fcurves", ()):
                    if not fcurve.data_path.endswith("speed_factor"):
                        continue
                    if needle in fcurve.data_path:
                        return fcurve, bag
                    fallback.append((fcurve, bag))
    # 条带改过名时 RNA 路径可能没跟上：**仅当**那条曲线写着的条带名不属于任何
    # 现存 SPEED 条带（真孤儿）才认领。否则新建第二个承载条带首次写入时（自己
    # 还没有 fcurve），会把第一个承载的曲线当自己的清空——片段 2 一编辑，
    # 片段 1 的速度关键帧就被抹掉（回归测试：test_seqvel_two_carriers）。
    if len(fallback) == 1 and _is_orphan_speed_curve(carrier, fallback[0][0]):
        return fallback[0]
    return None, None


def _is_orphan_speed_curve(carrier, fcurve):
    """这条 speed_factor 曲线是不是"无主"的（主人已不存在或已改名）。

    保守判定：认不出主人时一律返回 False——宁可放弃认领，也不冒抢错的风险。
    """
    match = _CURVE_OWNER_RE.search(getattr(fcurve, "data_path", "") or "")
    if match is None:
        return False
    owner = match.group(1)
    scene = getattr(carrier, "id_data", None)
    editor = getattr(scene, "sequence_editor", None)
    if editor is None:
        return False
    try:
        strips = list(editor.strips)
    except Exception:
        return False
    for strip in strips:
        if getattr(strip, "type", "") == "SPEED" and getattr(strip, "name", "") == owner:
            return False  # 曲线有主：是别的承载条带的，绝不能碰
    return True


def speed_curve(carrier):
    """承载条带的 ``speed_factor`` fcurve；找不到返回 None。"""
    return locate_speed_curve(carrier)[0]


def read_keys(carrier):
    """读回关键帧 ``[(帧, 数值, 插值), ...]``；结构不可用时返回 None。"""
    fcurve = speed_curve(carrier)
    if fcurve is None:
        return None
    try:
        return [(float(point.co[0]), float(point.co[1]), str(point.interpolation))
                for point in fcurve.keyframe_points]
    except Exception:
        return None


def clear_keys(carrier):
    """清空承载条带上的全部 speed_factor 关键帧，返回清掉的个数。"""
    fcurve = speed_curve(carrier)
    if fcurve is None:
        return 0
    removed = 0
    try:
        points = fcurve.keyframe_points
        while len(points):
            points.remove(points[0])
            removed += 1
        fcurve.update()
    except Exception:
        pass
    return removed


def purge_keys(carrier):
    """清空关键帧并摘掉 fcurve（删除承载条带时用，避免留下孤儿空曲线）。"""
    fcurve, bag = locate_speed_curve(carrier)
    if fcurve is None:
        return False
    clear_keys(carrier)
    if bag is None:
        return True
    try:
        bag.fcurves.remove(fcurve)
        return True
    except Exception:
        return False


def insert_keys(carrier, base_frame, curve):
    """把曲线写成 ``speed_factor`` 关键帧；返回写入个数。

    **线性段只写端点，非线性（缓动）段逐帧写**——帧序列由
    ``Curve.keyframe_frames()`` 给出。原因：Blender 的关键帧之间也是直线插值，
    只写两端点的话缓动形状根本落不了地（预览与导出都会变回直线）。
    速度值一律取 ``Curve.line_speed_at()``，与画出来的线同源。
    """
    written = 0
    for rel_frame in curve.keyframe_frames():
        frame = to_abs_frame(base_frame, rel_frame)
        speed = curve.line_speed_at(rel_frame)
        try:
            carrier.speed_factor = float(speed)
            carrier.keyframe_insert(data_path="speed_factor", frame=frame)
            written += 1
        except Exception:
            break
    return written


def set_linear_interpolation(carrier):
    """把承载条带的关键帧设为 LINEAR。

    与影子模型一致：**缓动的形状靠"逐帧关键帧"表达**，而不是靠关键帧之间的插值
    模式——所以这里恒定 LINEAR 就够，且与"画出来的线就是实际速度"一致。
    """
    fcurve = speed_curve(carrier)
    if fcurve is None:
        return False
    try:
        for point in fcurve.keyframe_points:
            point.interpolation = KEY_INTERPOLATION
        fcurve.update()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 子帧可用性（上游缺陷：Raw 缓存会把场景条带的源时间取整）
# --------------------------------------------------------------------------

def cache_raw_state(editor):
    """读本场景的 Raw 缓存开关：``(value, error)``；读不到时 ``value`` 为 ``None``。

    读不到**绝不当成"没问题"**：把异常原文交出去，由面板显示（不静默吞异常）。
    """
    if editor is None:
        return None, i18n.T("carrier.no_editor")
    try:
        return bool(getattr(editor, RAW_CACHE_FLAG)), None
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def set_cache_raw(editor, value):
    """写本场景的 Raw 缓存开关：``(ok, error)``。"""
    if editor is None:
        return False, i18n.T("carrier.no_editor")
    try:
        setattr(editor, RAW_CACHE_FLAG, bool(value))
        return True, None
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


# --------------------------------------------------------------------------
# 落地总入口
# --------------------------------------------------------------------------

def relay_span(strip, curve):
    """区间变了、但关键帧的**绝对帧**没变时的轻量重落地（ADR-0004 修订 R1）。

    只做三件该做的事：对齐承载条带、强制重建速度映射表（见
    :func:`invalidate_speed_map`）——**不重写关键帧**。适用于"只动了右端"的
    区间变化（拉长 / 收短但没吞点）：末点不动、尾段是常量外推，key 的值与绝对
    位置一个都没变，只缺一张按新长度重算的映射表；拖右端的每一 tick 都全量重写
    key 会卡（长缓动曲线要写几百个点）。

    调用方负责 ``STORE.commit``（载荷区间跟着当下区间走，是 ``store`` 的职责，
    本模块不碰影子模型的持久化）。承载条带已被删掉时返回 ``None``，调用方应退回
    全量 :func:`write_curve`（它会 ``ensure_carrier`` 把条带补回来）。

    **左端变了不能走这条路**：关键帧是按 ``left_handle + 相对帧`` 写的绝对帧
    （见 :func:`insert_keys`），左端一动就得重锚，那是 ``write_curve`` 的活。

    ``curve`` 只用来把 ``points`` 如实填进诊断（面板那行"上次写入"）；``written``
    是 0——轻量路径**一个关键帧都不写**，这个 0 就是它的证据。
    """
    carrier = carrier_of(strip)
    if carrier is None:
        return None
    diag = _new_diag(carrier, created=False)
    diag["adjusted"] = align_carrier(strip, carrier)
    _note_alignment(diag, strip, carrier)
    diag["points"] = len(curve.keyframe_frames())
    reason = invalidate_speed_map(strip)
    diag["invalidated"] = not reason
    if reason:
        diag["errors"].append(i18n.T("carrier.invalidate_failed", reason))
    return diag


def write_curve(strip, curve, reset_retiming_first=True):
    """全量写入：模型是唯一事实源，单向写、不读回。

    收尾**必须**强制重建速度映射表（ADR-0004 修订 R2）——这是本函数据以正确的前提：
    写入是"清空 key → 逐个插 key"，而那张表除了显式重建之外**没有任何东西会刷新它**，
    中途被建起来的那一份会一直用下去。真机实测：曲线数据一个字不改，只走一遍本函数，
    条带 93 帧里 89 帧的画面与"重建过的"不同（``.scratch/tail-bug/findings.md``）。
    与 :func:`relay_span` 同形、同一条失败通路，两条落地路径都不许漏这一步。

    返回诊断字典（面板显示 / 测试断言用）。
    """
    diag = _new_diag(None, created=carrier_of(strip) is None)
    diag.update({"cleared": 0, "points": len(curve), "keys_expected": 0,
                 "linear": False})
    carrier = ensure_carrier(strip)
    if carrier is None:
        diag["errors"].append(i18n.T("carrier.create_failed"))
        return diag
    diag["carrier"] = carrier.name

    if reset_retiming_first:
        diag["retiming_reset"] = reset_retiming(strip)

    # 重置会还原条带时长（Retiming 拖末 key 会改变时间轴长度，reset 后复原），
    # 所以顺序是：先重置 → 再同步模型窗口 → 最后对齐承载条带。
    curve.set_duration(strip_duration(strip))
    diag["adjusted"] = align_carrier(strip, carrier)
    _note_alignment(diag, strip, carrier)
    diag["cleared"] = clear_keys(carrier)
    # 期望的关键帧数由模型给出：线性段只写端点，缓动段逐帧（见 insert_keys）
    diag["keys_expected"] = len(curve.keyframe_frames())
    diag["written"] = insert_keys(carrier, strip_base_frame(strip), curve)
    if diag["written"] != diag["keys_expected"]:
        diag["errors"].append(i18n.T("carrier.keys_missing",
                                     diag["written"], diag["keys_expected"]))
    diag["linear"] = set_linear_interpolation(carrier)
    if not diag["linear"]:
        diag["errors"].append(i18n.T("carrier.interpolation_failed"))
    # **最后一步**：表要按"最终 fcurve"重算，所以排在所有写之后。
    reason = invalidate_speed_map(strip)
    diag["invalidated"] = not reason
    if reason:
        diag["errors"].append(i18n.T("carrier.invalidate_failed", reason))
    return diag


def carrier_report(strip):
    """承载条带现状（面板显示用）。"""
    carrier = carrier_of(strip)
    if carrier is None:
        return None
    keys = read_keys(carrier)
    bound = getattr(carrier, "input_1", None)
    # 取帧策略按**承载实际绑定的输入**判断：被渲染的是它，不是当前选中的条带。
    source = bound if bound is not None else strip
    source_type = getattr(source, "type", "")
    sampling = rules.sampling_policy(source_type)
    # Raw 缓存开关读的是"装着这条承载条带"的序列编辑器——渲染由它发起。
    cache_raw, cache_error = cache_raw_state(editor_of(strip))
    limited = rules.subframe_limited(sampling, cache_raw)
    return {
        "name": carrier.name,
        "channel": carrier.channel,
        "start": carrier.left_handle,
        "end": carrier.right_handle,
        "mode": getattr(carrier, "speed_control", "?"),
        "interpolate": getattr(carrier, "use_frame_interpolate", None),
        "sampling": sampling,
        "sampling_note": rules.sampling_note(source_type),
        # 面板据下面三项决定要不要出"无级受限"那一段（含一键开关）。
        "subframe_governed": sampling == rules.SAMPLING_SUBFRAME,
        "subframe_limited": limited,
        "subframe_note": i18n.T("rules.note_raw_cache") if limited else None,
        "cache_raw": cache_raw,
        "cache_error": cache_error,
        "keys": None if keys is None else len(keys),
        "bound": getattr(bound, "name", None),
    }


__all__ = [
    "CARRIER_PREFIX",
    "RAW_CACHE_FLAG",
    "SPEED_MODE",
    "USE_FRAME_INTERPOLATE",
    "align_carrier",
    "audio_note",
    "cache_raw_state",
    "can_edit",
    "carrier_of",
    "carrier_report",
    "clear_keys",
    "delete_carriers",
    "ensure_carrier",
    "insert_keys",
    "invalidate_speed_map",
    "is_aligned",
    "iter_carriers",
    "locate_speed_curve",
    "no_target_message",
    "purge_keys",
    "read_keys",
    "relay_span",
    "reset_retiming",
    "scan_targets",
    "set_cache_raw",
    "set_linear_interpolation",
    "speed_curve",
    "strip_base_frame",
    "strip_channel",
    "strip_duration",
    "target_diagnosis",
    "target_strip",
    "write_curve",
]


def can_edit(strip):
    """条带是否可以作为速度线目标（包装纯规则模块，方便 bpy 侧调用）。"""
    if strip is None:
        return False, i18n.T("carrier.no_target_selected")
    return rules.can_edit_type(getattr(strip, "type", ""))


def audio_note(strip):
    """音频相关提示；无提示返回 None。"""
    if strip is None:
        return None
    return rules.audio_note_type(getattr(strip, "type", ""))


def scan_targets(context):
    """扫描当前上下文里的候选目标，同时收集给用户看的诊断行。

    返回 ``(strip, notes)``。``notes`` 是实时状态——**任何一次读取失败都必须
    落成文字**，绝不静默吞掉：一旦"没有目标"，用户唯一能依赖的就是这几行，
    而空白的诊断会把"没选中"和"读取失败"混成一团，问题就无从下手。
    """
    notes = []
    scene = regions.displayed_scene(context)  # VSE 显示的场景，不是活动场景
    if scene is None:
        notes.append(i18n.T("carrier.no_scene"))
        return None, notes

    area_type = getattr(getattr(context, "area", None), "type", None)
    if area_type is not None and area_type != "SEQUENCE_EDITOR":
        notes.append(i18n.T("carrier.wrong_area", area_type))

    editor = getattr(scene, "sequence_editor", None)
    if editor is None:
        notes.append(i18n.T("carrier.no_editor_data",
                            getattr(scene, "name", "?")))
        return None, notes

    active = None
    try:
        active = editor.active_strip
    except Exception as exc:
        notes.append(i18n.T("carrier.read_active_failed",
                            type(exc).__name__, exc))

    strips = None
    try:
        strips = list(editor.strips)
    except Exception as exc:
        notes.append(i18n.T("carrier.read_strips_failed",
                            type(exc).__name__, exc))

    candidates = []
    if active is not None:
        candidates.append(active)
    selected = 0
    for strip in strips or ():
        if getattr(strip, "select", False):
            selected += 1
            if strip not in candidates:
                candidates.append(strip)

    if strips:
        notes.append(i18n.T("carrier.scene_summary",
                            getattr(scene, "name", "?"), len(strips), selected,
                            getattr(active, "name", None) or i18n.T("carrier.none_value")))

    target = None
    for strip in candidates:
        if rules.can_edit_type(getattr(strip, "type", ""))[0]:
            target = strip
            break
    if target is None:
        target = active or (candidates[0] if candidates else None)

    if target is None:
        if strips is not None:
            if not strips:
                notes.append(i18n.T("carrier.no_strips"))
            elif not candidates:
                notes.append(i18n.T("carrier.click_strip"))
    else:
        allowed, reason = rules.can_edit_type(getattr(target, "type", ""))
        if not allowed:
            notes.append(i18n.T("carrier.target_uneditable",
                                getattr(target, "name", "?"), reason))
    return target, notes


def target_strip(context):
    """当前操作目标：优先活动条带，其次选中条带里第一个可编辑的源条带。"""
    return scan_targets(context)[0]


def target_diagnosis(context):
    """没有可用目标时解释原因（面板显示用）。"""
    return scan_targets(context)[1]


def no_target_message(context):
    """没有目标时的完整拒绝文案：实时状态 + 版本号。

    版本号是刻意加的：覆盖安装插件后不重启 Blender，内存里仍是旧模块，症状
    就是"明明修了却依然报同样的错"。文案里带版本，能立刻分辨跑的是哪一版。
    """
    notes = target_diagnosis(context)
    detail = notes[0] if notes else i18n.T("carrier.no_target")
    return i18n.T("carrier.select_first", detail, VERSION_TAG)
