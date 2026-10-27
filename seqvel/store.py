# -*- coding: utf-8 -*-
"""影子模型的存放与持久化（薄 bpy 层）。

- 内存缓存：编辑过程中的即时读写；
- 持久化：曲线写在**条带自己的自定义属性** ``seqvel_curve``（平坦浮点列表）上，
  随 .blend 一起保存，并天然进入 Blender 撤销栈；段类型另存一条，**载荷区间**
  （"这份载荷是在哪个绝对区间上写的"）再另存一条——三条同进同退；
- 撤销 / 重做 / 载入文件后清空缓存，强制从属性重读，避免"内存里的曲线"与
  "撤销回来的关键帧"对不上；
- **条带被切断**（Split）时在这里当场对账：划分链成立就按"纯划分"切片，
  读路径**只在内存里**（ADR-0008 决定 6）；等到用户**真正编辑**其中一半，才由
  ``commit`` 把整组物化成自描述切片，此后各半互不牵连（修订 R1）。

去重按条带标识（场景名 + 条带名）；条带被删掉时它的属性也一并消失，
因此不会留下孤儿数据。
"""

try:
    import bpy
    from bpy.app.handlers import persistent
except ImportError:  # 纯 Python 环境（单测）：模型缓存与属性读写不依赖 bpy
    bpy = None

    def persistent(func):  # noqa: F811 — bpy 缺席时原样返回
        return func

from .curve import Curve, normalize_type
from . import i18n, status

#: 曲线在条带自定义属性里的键名
PROP_KEY = "seqvel_curve"
#: 段曲线类型（整数列表）单独一条属性：旧 .blend 没有它 → 全部按线性读
TYPES_KEY = "seqvel_curve_types"
#: 载荷区间（``[左端, 右端]`` 绝对帧）单独一条属性：**载荷自描述"它属于哪一段"**
#: （ADR-0008 决定 5）。旧 .blend 没有它 → 区间未知 → 老语义，不猜、不迁移。
RANGE_KEY = "seqvel_curve_range"
#: 区间比较容差：远小于一帧，只用来吃掉句柄的浮点噪声
SPAN_EPS = 1e-3


def strip_key(strip):
    """内存缓存键（场景名, 条带名）。"""
    scene = getattr(strip, "id_data", None)
    return (getattr(scene, "name", "") or "", getattr(strip, "name", "") or "")


def strip_span(strip):
    """条带在时间轴上的绝对区间 ``(左端, 右端)``；读不到返回 None。"""
    try:
        return (float(strip.left_handle), float(strip.right_handle))
    except Exception:
        return None


def strip_duration(strip):
    """条带在时间轴上的长度（帧）。"""
    span = strip_span(strip)
    if span is None:
        return 0.0
    return max(0.0, span[1] - span[0])


def strip_base_frame(strip):
    """条带起点在时间轴上的帧（模型相对帧的原点）。"""
    span = strip_span(strip)
    return 1.0 if span is None else span[0]


def same_span(a, b):
    """两个绝对区间是否指向同一段（容差 ``SPAN_EPS``）。

    公开给 ``watch`` 用：它拿它判断"目标区间这一拍动没动"（ADR-0004 修订 R1）。
    """
    if a is None or b is None:
        return False
    return abs(a[0] - b[0]) <= SPAN_EPS and abs(a[1] - b[1]) <= SPAN_EPS


def strip_channel(strip):
    try:
        return float(strip.channel)
    except Exception:
        return 1.0


def default_segment_type(strip):
    """条带所属场景的「默认段曲线类型」（ADR-0007）；读不到一律退化为线性。

    **只在曲线诞生那一刻用得上**（``get_or_create`` / ``display_curve`` 建默认
    曲线、``get`` 的损坏载荷修复），且**只在这一处**读：
    拆段继承、并段取左都不看它，所以把默认设成平滑不会悄悄改掉既有曲线的形状。

    经 ``getattr`` 逐层取，不 import ``ui``（ui import 本模块，反向会成环）——
    与 ``ui.apply_retiming_display`` 是同一种取法。
    """
    settings = getattr(getattr(strip, "id_data", None), "seqvel", None)
    return normalize_type(getattr(settings, "default_curve_type", None))


class ModelStore(object):
    def __init__(self):
        self._cache = {}

    # ---------- 属性读写 ----------

    def read_prop(self, strip):
        """读取条带上的曲线数据；没有或损坏返回 None。"""
        try:
            flat = strip.get(PROP_KEY)
        except Exception:
            return None
        if flat is None:
            return None
        try:
            return [float(value) for value in flat]
        except (TypeError, ValueError):
            return None

    def read_types(self, strip):
        """读取段类型载荷；没有或损坏返回 None（= 调用方按全线性处理）。"""
        try:
            payload = strip.get(TYPES_KEY)
        except Exception:
            return None
        if payload is None:
            return None
        try:
            return [int(value) for value in payload]
        except (TypeError, ValueError):
            return None

    def read_range(self, strip):
        """读取载荷自描述的绝对区间；没有或损坏返回 None（= **区间未知**）。

        区间未知一律按老语义处理（ADR-0008 决定 5）：旧 .blend 里没有这条属性，
        不能因为"看不出来"就替用户改动既有曲线。
        """
        try:
            payload = strip.get(RANGE_KEY)
        except Exception:
            return None
        if payload is None:
            return None
        try:
            values = [float(value) for value in payload]
        except (TypeError, ValueError):
            return None
        if len(values) != 2:
            return None
        return (values[0], values[1])

    def write_prop(self, strip, curve):
        """点、段类型、载荷区间一起写；三条属性必须同进同退。

        区间写的是**当下**的条带区间——它是读路径判断"这份载荷是不是在这个区间上
        写的"的唯一依据；写失败不算致命（退回"区间未知"），但**不许静默**。
        """
        try:
            strip[PROP_KEY] = curve.to_flat()
        except Exception:
            return False
        try:
            strip[TYPES_KEY] = curve.types_to_flat()
        except Exception as exc:
            # 类型写不进去不算致命：点已经落盘，读回时按线性处理。
            # 但**不许静默**——用户会觉得"我明明设了平滑，重开怎么变直了"，
            # 所以留一条轨迹让排障能直接看到真正的原因。
            status.trace("store.write_prop 段类型写失败 strip=%r: %r"
                         % (getattr(strip, "name", None), exc))
        span = strip_span(strip)
        if span is not None:
            try:
                strip[RANGE_KEY] = [span[0], span[1]]
            except Exception as exc:
                status.trace("store.write_prop 载荷区间写失败 strip=%r: %r"
                             % (getattr(strip, "name", None), exc))
        return True

    def clear_prop(self, strip):
        try:
            for key in (PROP_KEY, TYPES_KEY, RANGE_KEY):
                if key in strip.keys():
                    del strip[key]
        except Exception:
            pass

    # ---------- 缓存 ----------

    def invalidate(self):
        self._cache.clear()

    def get(self, strip):
        """取曲线；没有返回 None（不创建）。

        **条带被切断**时在这里对账（ADR-0008）：划分链成立就把载荷按"纯划分"
        切成本条带该有的那一段——**只在内存里**，属性与关键帧一字不动；其余
        一律退回 ADR-0004 的老语义。读路径绝不猜"这份坐标属于谁"。
        """
        if strip is None:
            return None
        key = strip_key(strip)
        span = strip_span(strip)
        entry = self._cache.get(key)
        if entry is not None:
            curve, cached_span = entry
            if (span is not None and cached_span is not None
                    and not same_span(span, cached_span)):
                sliced = self._slice_for(strip, self.read_prop(strip))
                if sliced is not None:
                    self._cache[key] = (sliced, span)
                    sliced.set_duration(strip_duration(strip))
                    return sliced
            # 不是切断：身份与点都不动（ADR-0004 的"条带变化不带动模型"），
            # 只把印记推进到当下，免得每次读都重新对账。
            self._cache[key] = (curve, span if span is not None else cached_span)
            curve.set_duration(strip_duration(strip))
            return curve
        flat = self.read_prop(strip)
        if flat is None:
            return None
        curve = self._slice_for(strip, flat)
        if curve is None:
            curve = Curve.from_flat(flat, strip_duration(strip),
                                    types=self.read_types(strip),
                                    kind=default_segment_type(strip))
        self._cache[key] = (curve, span)
        curve.set_duration(strip_duration(strip))
        return curve

    def get_or_create(self, strip):
        """取曲线，没有就建默认 2 点曲线（只进内存，改动后才落属性）。"""
        curve = self.get(strip)
        if curve is None:
            curve = Curve.default(strip_duration(strip), default_segment_type(strip))
            self._cache[strip_key(strip)] = (curve, strip_span(strip))
        return curve

    def display_curve(self, strip):
        """绘制用曲线：没有模型时给一条默认 1.0x 线，保证"选中即见"。"""
        curve = self.get(strip)
        if curve is not None:
            return curve
        return Curve.default(strip_duration(strip), default_segment_type(strip))

    def has_model(self, strip):
        return self.get(strip) is not None

    def commit(self, strip, curve):
        """把曲线写进条带属性（真正发生编辑时才调用）。

        **一次真实编辑会顺带物化同组**（ADR-0008 修订 R1）：趁载荷还没被这条带
        改掉，把划分链上其它成员各自按自己的区间写成**自描述切片**。它落在这次
        编辑自己的撤销步里，不额外产生一步；写完每半都自带区间，不再互相牵连
        ——否则"编辑一半"会把另一半拉成压缩形状（接缝凭空跳一拍）。
        """
        if strip is None or curve is None:
            return False
        self.materialize_division(strip, skip=strip)
        return self._publish(strip, curve)

    def forget(self, strip, drop_prop=True):
        if strip is None:
            return
        self._cache.pop(strip_key(strip), None)
        if drop_prop:
            self.clear_prop(strip)

    # ---------- 切断（ADR-0008） ----------

    def _peers(self, strip):
        """同场景的其它条带（划分判据要拿它们比对）；拿不到就是空表。"""
        editor = getattr(getattr(strip, "id_data", None), "sequence_editor", None)
        strips = getattr(editor, "strips", None)
        if strips is None:
            return []
        try:
            return [other for other in strips if other is not strip]
        except Exception:
            return []

    def same_payload(self, a, b):
        """两条条带的载荷是否**逐字相同**：自描述区间 + 点 + 段类型三条全等。

        "逐字"是刻意的：切断是原生对属性的**原样复制**，所以"贴合且逐字相同"
        是它留下的指纹之一。裁剪、整体移动、改名都不会在贴合处留下逐字相同的一份。
        """
        if a is None or b is None:
            return False
        declared = self.read_range(a)
        if declared is None or declared != self.read_range(b):
            return False
        points = self.read_prop(a)
        if points is None or points != self.read_prop(b):
            return False
        return self.read_types(a) == self.read_types(b)

    def division_chain(self, strip):
        """本条带所在的**划分链**（含自己，按区间升序）；不成立返回 None。

        判据（`CONTEXT.md`「划分链」/ ADR-0008 决定 6）：沿时间轴**首尾贴合**、
        载荷**逐字相同**、各段区间**无重叠地恰好拼回**载荷声明的区间的一组条带。
        只有划分会把一条载荷复制成拼得回去的两半——裁剪、复制粘贴、改名都不满足，
        所以它是"切断"留在数据里的专属证据（切断不上报任何通知，读路径只能靠它）。
        """
        declared = self.read_range(strip)
        span = strip_span(strip)
        if declared is None or span is None or self.read_prop(strip) is None:
            return None
        if same_span(span, declared):
            return None  # 区间一致：没有要对账的东西
        peers = self._peers(strip)
        chain = [strip]
        for side in ("left", "right"):
            cursor = strip
            while True:
                found = None
                for peer in peers:
                    if peer in chain or not self.same_payload(peer, strip):
                        continue
                    edge = strip_span(peer)
                    if edge is None:
                        continue
                    touching = (abs(edge[1] - cursor.left_handle) if side == "left"
                                else abs(cursor.right_handle - edge[0]))
                    if touching <= SPAN_EPS:
                        found = peer
                        break
                if found is None:
                    break
                chain.append(found)
                cursor = found
        if len(chain) < 2:
            return None
        chain.sort(key=lambda item: strip_span(item)[0])
        first, last = strip_span(chain[0])[0], strip_span(chain[-1])[1]
        if abs(first - declared[0]) > SPAN_EPS or abs(last - declared[1]) > SPAN_EPS:
            return None  # 拼不回声明的区间
        for before, after in zip(chain, chain[1:]):
            if abs(strip_span(before)[1] - strip_span(after)[0]) > SPAN_EPS:
                return None  # 链上有缝
        return chain

    def _slice_for(self, strip, flat):
        """划分成立时返回**切片后**的曲线；否则 None（= 不是切断，走老语义）。

        切片基准是**载荷自己声明的那个区间**：先按它把载荷还原成"切前的那条曲线"
        （时长取声明区间，绝不让构造函数先把点钳掉），再切出本条带的那一段。

        ``kind`` 与冷读路径取同一个来源（ADR-0007 决定 1）：它只在"载荷不可用 →
        建一条新曲线"这条修复路径上起作用，两处若取不同的档，同一份损坏载荷
        会因"走没走切断对账"而得到不同形状。
        """
        if flat is None:
            return None
        declared = self.read_range(strip)
        span = strip_span(strip)
        if declared is None or span is None:
            return None
        if self.division_chain(strip) is None:
            return None
        payload = Curve.from_flat(flat, max(0.0, declared[1] - declared[0]),
                                  types=self.read_types(strip),
                                  kind=default_segment_type(strip))
        sliced = payload.slice_between(span[0] - declared[0], span[1] - declared[0])
        status.trace("store 切断对账 strip=%r 声明区间=%r 本条带=%r → 切片 %r"
                     % (getattr(strip, "name", None), declared, span, sliced.points))
        return sliced

    def is_division_member(self, strip):
        """本条带是不是**划分链上的一员**（只吃盘上证据，不新增内存状态）。

        ``watch`` 用它决定要不要把模型写回属性：被切开的一半**写回就等于把划分
        的指纹擦掉**——载荷与兄弟不再逐字相同，链当场断，兄弟退回"整份载荷按
        自己长度钳回"（ADR-0008 修订 R1）。
        """
        if strip is None:
            return False
        return self.division_chain(strip) is not None

    def materialize_division(self, strip, skip=None):
        """把同组各半写成**自描述切片**，让它们不再依赖划分链（ADR-0008 修订 R1）。

        只在**真实编辑**（``commit``）时调用，读路径绝不调用它：读路径写回正是
        决定 6 禁止的事（会多出一个隐形撤销步）。

        切片必须**先全部算完再统一写**：写掉一个成员链就断了，之后再去算别人的
        切片，拿到的是"整份载荷按它自己的长度钳回"的错误形状。``skip`` 是调用方
        自己接着要写的那条带（它的曲线是用户刚编辑的那份）——按条带标识比，不按
        对象身份比（求值副本会换个对象）。
        """
        chain = self.division_chain(strip)
        if chain is None:
            return
        skip_key = strip_key(skip) if skip is not None else None
        pending = []
        for member in chain:
            if skip_key is not None and strip_key(member) == skip_key:
                continue
            span = strip_span(member)
            if span is None:
                continue
            sliced = self._slice_for(member, self.read_prop(member))
            if sliced is not None:
                pending.append((member, sliced, span))
        for member, sliced, span in pending:
            if not self._publish(member, sliced, span):
                # 写不进去不许静默：曲线还在缓存里、盘上仍是旧载荷，界面上看不出来。
                status.set_error(i18n.T("store.materialize_failed",
                                        getattr(member, "name", "?")))
                continue
            status.trace("store 物化划分成员 strip=%r 区间=%r 点=%r"
                         % (getattr(member, "name", None), span, sliced.points))

    def _publish(self, strip, curve, span=None):
        """同值落地：时长对齐 → 推进缓存印记 → 写三条属性。

        三条属性**同进同退**，所以"缓存里那条曲线"与"盘上那份载荷"必须一起换；
        返回 ``write_prop`` 的结果，失败由调用方负责**可见地**处理。
        """
        curve.set_duration(strip_duration(strip))
        if span is None:
            span = strip_span(strip)
        self._cache[strip_key(strip)] = (curve, span)
        return self.write_prop(strip, curve)


#: 全局单例
STORE = ModelStore()


@persistent
def _invalidate_cache(*_args, **_kwargs):
    """撤销 / 重做 / 载入后清模型缓存。

    **必须标 persistent**：Blender 默认在加载新文件时清空未标 persistent
    的处理器——不标的话打开 .blend 后缓存清理也一起死，跨文件的同名
    （场景名 + 条带名）条带会串到旧曲线。
    """
    STORE.invalidate()


#: 需要清缓存的时机（签名各异，统一用 *args 兼容）
_HANDLER_NAMES = ("undo_post", "redo_post", "load_post")


def register_handlers():
    if bpy is None:
        return
    for name in _HANDLER_NAMES:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is None or _invalidate_cache in handlers:
            continue
        handlers.append(_invalidate_cache)


def unregister_handlers():
    if bpy is None:
        return
    for name in _HANDLER_NAMES:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is None:
            continue
        while _invalidate_cache in handlers:
            handlers.remove(_invalidate_cache)
