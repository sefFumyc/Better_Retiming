# -*- coding: utf-8 -*-
"""速度曲线影子模型（纯 Python，零 bpy 依赖）。

模型 = 有序点集 ``[(相对条带起点的帧, 速度), ...]``，是插件的单一事实源：
编辑界面读写它、落地映射（``carrier``）从它采样、绘制（``overlay``）按它画线。

不变量
------
1. 至少 2 个点；
2. 首点帧恒为 0.0（速度线始终覆盖素材开头）；
3. 按帧升序（允许同帧，同帧处取左侧速度，即"硬切"）；
4. 速度 ∈ ``[SPEED_MIN, SPEED_CEIL]``（显示范围上限只管显示与拖动钳制，
   不约束已落库的数据）；
5. ``len(types) == len(points) - 1``，且每个类型都是 ``CURVE_TYPES`` 之一。

语义要点
--------
- **线上的速度** = 按**段曲线类型**插值（``LINEAR`` / ``SMOOTH`` / ``FAST`` / ``SLOW``）；
  越界外推 = 取最近端点的速度，因此"末点左移后其右侧自动延伸水平线"是模型自带语义
  （``tail_extension_end``）；
- **类型挂在段上，不在点上**：``types[i]`` 是第 ``i`` 段（点 ``i`` 与点 ``i+1`` 之间）的形状，
  长度恒为 ``len(points) - 1``；类型只改段内形状，**不改两端点值**，相邻段允许斜率突变；
- 拖动会话用**快照锚定**：``端点速度 = 快照 + scale × (光标当前速度 − 锚点速度)``，
  绝不"在当前值上叠加差值"（叠加会让线比鼠标快、钳制后无法反向）。
  ``scale=1.0`` 即 1:1 跟手；``FINE_SCALE``（0.5）即 Ctrl 微调（ADR-0005 修订 R9）。
  锚点取"会话建立瞬间的光标速度"，所以建立零跳变。拖动钳制走会话 ``cap``
  （按下那一刻的显示范围上限）。
"""

import math

#: 纵轴标尺的**默认**满量程（速度上限）。标尺 ``0 ~ 上限`` 铺满**标尺区间**
#: （见 ``SCALE_BOTTOM`` / ``SCALE_TOP``；绘制区下沿之上再留 4% 底部留白），
#: 所以这个值同时决定"1.0x 基线画在哪"
#: ——3.0 之下 1.0x 落在标尺 1/3 处（0.02~2.0 时代它落在正中）。抬高上限会让
#: 1.0x 下移、每像素速度增量变大。
#: v1.5.0 起它是**场景设置**（「显示范围上限」，两档 300% / 1000%）：
#: 本常量只是出厂默认，运行时经 ``set_active_cap`` 由 UI 层同步（见下）。
SPEED_MAX = 3.0
#: 速度下限（钳制下界；0.02 → 0.01，v1.5.0 审计改定）
SPEED_MIN = 0.01
#: 数据硬顶：任何落进影子模型的速度都不许超过它（「显示范围上限」的两档
#: 300%/1000% 里最大就是 10.0）。加载 / 撤销读回按它钳——**不按**当前显示
#: 上限钳：上限调小后"超限点数据不动"，见 CONTEXT.md「显示范围上限」。
SPEED_CEIL = 10.0
#: 条带绘制区（ADR-0010）：Blender 画素材主体时上下各收回通道高度的
#: ``STRIP_OFSBOTTOM / STRIP_OFSTOP = 0.05 / 0.95``（DNA_sequence_types.h，
#: 2.80 起从未变过），条带只占通道高的 90%。速度标尺 ``0 ~ 上限`` 铺的是
#: 这个矩形而**不是**整个通道带：上限速度的线正好贴素材上缘。
#: **这两个数是 Blender 的常量，不是插件的魔法数**——任何调整必须以
#: Blender 源码为准，"顺手改回 0..1"会让极限速度的线重新溢出素材边界
#: （ADR-0010「后人不该简化回去的理由」）。
DRAW_BOTTOM = 0.05
#: 底部留白（ADR-0010 修订 R1）：标尺下界在绘制区下沿之上**再**让出 4% 通道
#: 高，整条曲线因此略扁、``SPEED_MIN`` 不再糊在素材下缘。**顶端不放**——
#: "贴到上缘 = 到达上限"是刻意保留的读数，别"对称地"给顶端也加一份。单位是
#: **通道比例、不是像素**：VSE 一个通道的屏幕像素高随编辑器高度变化（实测
#: 88.5 ↔ 51.25 px），按像素留白会让"扁的程度"随窗口高矮漂移。
BOTTOM_MARGIN = 0.04
#: 标尺的合法区间（0.09~0.95）：下界 = Blender 内缩 5% ＋ 插件留白 4%；上界就是
#: Blender 的 ``STRIP_OFSTOP``。**顶端故意不设「留白」常量**——贴到上缘 = 到达
#: 上限，别为了对称给顶端也加一份。
SCALE_BOTTOM = DRAW_BOTTOM + BOTTOM_MARGIN
SCALE_TOP = 0.95
#: 标尺跨度（0.86）：映射对里反复出现的跨度，抽出来免得三处各写一遍。
SCALE_SPAN = SCALE_TOP - SCALE_BOTTOM
MIN_POINTS = 2
#: 插点去重阈值（帧）：与已有点距离小于它就不插
MIN_GAP = 0.5
#: "同一帧"的判据（帧）：只用来吃掉浮点噪声——切片时切点恰好落在一个已有点上，
#: 两半共享它而不各造一个。与 ``store.SPAN_EPS`` 不是一回事：那个比的是**区间**。
FRAME_EPS = 1e-9

#: 当前生效的显示范围上限（场景设置驱动）。零 bpy 的本模块只存这个数：
#: UI/绘制/交互在各自的入口（draw 回调、locate、modal）用
#: ``ui.sync_active_cap`` 按显示场景同步它；映射函数（``speed_to_yfrac`` /
#: ``yfrac_to_speed``）一律吃这个值，所以"画出来的标尺"与"拖动的钳制"
#: 永远同一档。出厂 = ``SPEED_MAX``。
ACTIVE_CAP = SPEED_MAX


def set_active_cap(value):
    """同步当前显示范围上限（浮点；非法/越界值回退默认或硬顶）。"""
    global ACTIVE_CAP
    value = _safe_float(value, SPEED_MAX)
    ACTIVE_CAP = max(SPEED_MAX, min(SPEED_CEIL, value))
    return ACTIVE_CAP


def active_cap():
    """当前显示范围上限（浮点）。"""
    return ACTIVE_CAP

#: 段曲线类型（Vegas 四档）。键是持久化用的稳定短串，不要改。
LINEAR = "linear"
SMOOTH = "smooth"
FAST = "fast"
SLOW = "slow"
CURVE_TYPES = (LINEAR, SMOOTH, FAST, SLOW)
#: 面板 / 菜单显示名——**源文案 = 英文**（ADR-0009：静态常量恒英文；
#: 运行层显示经 ``i18n.type_label`` 随语言模式切换，中文在文案表里）。
TYPE_LABELS = {LINEAR: "Linear", SMOOTH: "Smooth", FAST: "Fast", SLOW: "Slow"}
#: 持久化编码（自定义属性里存整数）
TYPE_CODES = {LINEAR: 0, SMOOTH: 1, FAST: 2, SLOW: 3}
_CODES_TO_TYPE = {code: name for name, code in TYPE_CODES.items()}

#: Ctrl 微调档（ADR-0005 修订 R9）：速度增量乘这个系数。只作用在**增量**上，
#: 不引入固定步进吸附；钳制仍走 ``[SPEED_MIN, 会话 cap]``。
FINE_SCALE = 0.5


def normalize_type(value):
    """任意输入 → 合法类型名；不认识的退化为 ``LINEAR``。"""
    if isinstance(value, str) and value in TYPE_CODES:
        return value
    if isinstance(value, bool):
        return LINEAR
    try:
        code = int(value)
    except (TypeError, ValueError):
        return LINEAR
    return _CODES_TO_TYPE.get(code, LINEAR)


def ease_ratio(kind, ratio):
    """段内归一化位置 ``t`` → 形状位置 ``t'``（``v = v0 + (v1-v0)·t'``）。

    命名约定（`docs/adr/0006-curve-type-names-describe-the-start.md`）：
    ``FAST`` / ``SLOW`` 描述的是**速度变化集中在段内的哪一端**，与段是升是降
    无关——「快速」= 起步那半段就把大部分变化吃掉，「慢速」= 起步平缓、后段
    才把变化吃掉。所以线上凸向会随升降方向自然**翻转**：上升段的「快速」上凸、
    下降段的「快速」下凸，两者是同一个形状，**凸向不是判据**。

    - ``LINEAR``：恒等（匀速变化）
    - ``SMOOTH``：``3t² − 2t³``（S 形，两端切线水平；变化集中在中间）
    - ``FAST``：``1 − (1-t)²``（起步急促、收尾平缓）
    - ``SLOW``：``t²``（起步平缓、收尾急促）
    """
    t = 0.0 if ratio < 0.0 else (1.0 if ratio > 1.0 else float(ratio))
    if kind == SMOOTH:
        return t * t * (3.0 - 2.0 * t)
    if kind == FAST:
        return 1.0 - (1.0 - t) * (1.0 - t)
    if kind == SLOW:
        return t * t
    return t


def _decode_types(payload):
    """把持久化载荷（整数列表 / 任意垃圾）解成类型序列；不可用返回 None（=全线性）。"""
    if payload is None or isinstance(payload, (str, bytes)):
        return None
    try:
        values = list(payload)
    except TypeError:
        return None
    if not values:
        return None
    return [normalize_type(value) for value in values]


def _normalize_types(types, segment_count):
    """类型序列对齐到段数：多了截断、少了补 ``LINEAR``、非法值退化为 ``LINEAR``。"""
    count = segment_count if segment_count > 0 else 0
    values = list(types) if types is not None else []
    out = [normalize_type(value) for value in values[:count]]
    while len(out) < count:
        out.append(LINEAR)
    return out


def clamp_speed(value, cap=None):
    """把任意输入钳到 ``[SPEED_MIN, cap]``；非法输入退化为 1.0。

    ``cap`` 缺省 = **数据硬顶** ``SPEED_CEIL``——加载 / 撤销读回的老数据不许
    被"当前显示上限"吃掉（上限调小后超限点数据不动，CONTEXT.md「显示范围
    上限」）。拖动会话与光标映射各自显式传当前显示上限。
    """
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return 1.0
    if speed != speed or speed in (float("inf"), float("-inf")):
        return 1.0
    return max(SPEED_MIN, min(SPEED_CEIL if cap is None else cap, speed))


def speed_to_yfrac(speed, cap=None):
    """速度 → 通道垂直比例（0~上限 铺满**标尺区间**，ADR-0010 修订 R1）。

    返回值是加在 ``strip.channel`` 上的通道坐标偏移，合法区间是
    ``[SCALE_BOTTOM, SCALE_TOP]``（0.09~0.95：绘制区下沿 0.05 再抬 4% 底部
    留白，上沿仍贴 0.95）——不是 0..1。``cap`` 缺省 = 当前显示范围上限
    （``ACTIVE_CAP``）。
    **超限不钳值、只钳显示**：速度高于上限时比例钳到 ``SCALE_TOP``
    （画在素材上缘），数据原样。
    """
    limit = ACTIVE_CAP if cap is None else cap
    frac = SCALE_BOTTOM + SCALE_SPAN * float(speed) / limit
    return max(SCALE_BOTTOM, min(SCALE_TOP, frac))


def yfrac_to_speed(yfrac, cap=None):
    """标尺区间内的通道垂直比例 → 速度（钳制在 ``[SPEED_MIN, 上限]`` 内）。

    ``speed_to_yfrac`` 的逆映射：``速度 = (比例 − 0.09) / 0.86 × 上限``。
    ``cap`` 缺省 = 当前显示范围上限：光标映射天然被钳在**当前档**里，
    这正是"下次拖动才真正钳进去"的入口（拖到哪个值由当时的标尺决定）。
    这是"标尺"语义的映射：绝对吸附点拖、插点初值等要"点到光标处"的
    路径都吃它。
    """
    try:
        frac = float(yfrac)
    except (TypeError, ValueError):
        return 1.0
    if frac != frac:
        return 1.0
    limit = ACTIVE_CAP if cap is None else cap
    speed = (frac - SCALE_BOTTOM) / SCALE_SPAN * limit
    return max(SPEED_MIN, min(limit, speed))


def yfrac_to_speed_raw(yfrac, cap=None):
    """标尺区间内的比例 → **未钳**速度（[R11] 只喂锚定拖动会话）。

    与 :func:`yfrac_to_speed` 同一条线性标尺、同一个换算率，但**不做
    ``[SPEED_MIN, cap]`` 钳制**：标尺下界（0.09）以下为负、上沿以上超过
    cap。锚定会话（``SegmentDrag``）拿它算增量，光标越过素材边界后
    增量才能继续增长（ADR-0005 修订 R11「锚定输入去饱和」）；点最终
    落在哪仍由 ``clamp_speed(快照 + delta, cap)`` 收口。
    """
    try:
        frac = float(yfrac)
    except (TypeError, ValueError):
        return 1.0
    if frac != frac or frac in (float("inf"), float("-inf")):
        return 1.0
    limit = ACTIVE_CAP if cap is None else cap
    return (frac - SCALE_BOTTOM) / SCALE_SPAN * float(limit)


def to_abs_frame(base_frame, rel_frame):
    """相对帧 → 时间轴绝对帧（关键帧只能落在整帧上）。"""
    return int(round(float(base_frame) + float(rel_frame)))


def to_rel_frame(base_frame, abs_frame):
    """时间轴绝对帧 → 相对条带起点的帧。"""
    return float(abs_frame) - float(base_frame)


class Curve(object):
    """速度曲线（可变，点集为 ``(rel_frame, speed)`` 的列表，加段类型序列）。"""

    def __init__(self, points=None, duration=0.0, types=None):
        self.duration = max(0.0, _safe_float(duration, 0.0))
        cleaned = []
        for item in points or ():
            try:
                frame = float(item[0])
                speed = float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if frame != frame or speed != speed:
                continue
            cleaned.append((max(0.0, frame), clamp_speed(speed)))
        cleaned.sort(key=lambda p: p[0])
        if not cleaned:
            cleaned = [(0.0, 1.0), (self.duration, 1.0)]
        else:
            cleaned[0] = (0.0, cleaned[0][1])  # 不变量 2
        while len(cleaned) < MIN_POINTS:  # 不变量 1
            last_frame, last_speed = cleaned[-1]
            tail = self.duration if self.duration > last_frame else last_frame + 1.0
            cleaned.append((tail, last_speed))
        self.points = cleaned
        # 类型序列对齐到段数（不变量 5）；形状变化（排序 / 补点）后长度以点为准
        self.types = _normalize_types(types, len(cleaned) - 1)
        if self.duration > 0.0:
            # 历史脏数据静默钳回（ADR-0004）：读取路径（旧文件 / 撤销后重读）
            # 把越过条带末端的点收敛回窗口内——"末点跃出边界"自此不出现。
            # 时长读数不可信（异常退化 0）时绝不动数据。
            self.carry_tail(self.duration)

    # ---------- 构造 / 序列化 ----------

    @classmethod
    def default(cls, duration, kind=LINEAR):
        """默认 2 点曲线：整段 1.0x，那唯一一段取 ``kind``。

        ``kind`` 缺省线性——**纯层不带策略**："新曲线的默认档"由场景设置决定，
        而"从哪里读它"是 store 的事（本模块零 bpy，也不该知道场景）。
        两端都是 1.0x 时档位看不出来：它第一次显形是"拖其中一个端点"那一刻
        （线性 = 直线倾斜、平滑 = S 形收尾）。见
        `docs/adr/0007-default-segment-type-read-once.md`。
        """
        span = max(0.0, _safe_float(duration, 0.0))
        return cls([(0.0, 1.0), (span, 1.0)], span, [normalize_type(kind)])

    def copy(self):
        clone = Curve(self.points, self.duration, list(self.types))
        return clone

    def to_flat(self):
        """序列化为平坦浮点列表（供自定义属性持久化，避免嵌套数组）。

        **只管点，不管类型**：类型单独存一条属性（``types_to_flat``），
        这样旧版本读到的仍是它认识的点对序列，旧数据缺类型时也天然退化为全线性。
        """
        flat = []
        for frame, speed in self.points:
            flat.append(float(frame))
            flat.append(float(speed))
        return flat

    def types_to_flat(self):
        """类型序列化为整数列表（长度 = 段数 = 点数 − 1）。"""
        return [TYPE_CODES.get(kind, 0) for kind in self.types]

    @classmethod
    def from_flat(cls, flat, duration, fallback=None, types=None, kind=LINEAR):
        """从平坦浮点列表还原；数据不可用时返回 ``fallback`` 或默认曲线。

        ``types`` 可缺省（旧 .blend 里没这条属性）→ 全部按 ``LINEAR`` 读：
        默认档是"诞生值"不是"兜底值"，旧载荷**不因默认而变形状**。
        ``kind`` 只作用于"载荷不可用 → 建一条新曲线"这条修复路径——那也是一条
        新曲线（两端都是 1.0x，形状上无从观察），照诞生处理（ADR-0007 决定 1）。
        """
        try:
            values = [float(v) for v in flat]
        except (TypeError, ValueError):
            values = []
        if len(values) < 2 * MIN_POINTS or len(values) % 2:
            return fallback if fallback is not None else cls.default(duration, kind)
        points = list(zip(values[0::2], values[1::2]))
        return cls(points, duration, _decode_types(types))

    # ---------- 段类型 ----------

    def segment_type(self, index):
        """第 ``index`` 段的类型；下标越界返回 ``LINEAR``。"""
        if 0 <= index < len(self.types):
            return self.types[index]
        return LINEAR

    def set_segment_type(self, index, kind):
        """设第 ``index`` 段类型；返回是否发生变化（下标越界返回 False）。"""
        if index < 0 or index >= len(self.types):
            return False
        value = normalize_type(kind)
        if self.types[index] == value:
            return False
        self.types[index] = value
        return True

    def type_counts(self):
        """各类型出现次数（面板显示用，四档全列出）。"""
        counts = {kind: 0 for kind in CURVE_TYPES}
        for kind in self.types:
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def keyframe_frames(self):
        """承载需要写关键帧的帧序列（升序）。

        线性段的形状由相邻两个关键帧的线性插值天然表达；**非线性的段必须逐帧采样**，
        否则 Blender 侧（LINEAR 关键帧之间也是直线）画不出缓动。

        例外：**恒速**的非线性段（两端速度相等）同样只需两端点——`v0 == v1` 时
        `v(t) = v0 + (v1 − v0)·ease(t) = v0`，逐帧写与两端点写在数学上完全等价。
        判据是**精确相等**（两端速度由同一套映射算出，相等可精确命中），刻意不引入
        容差魔数。见 `docs/adr/0007-default-segment-type-read-once.md` 决定 6——
        默认段曲线类型=平滑之后"平坦的平滑段"是新曲线的常态，少了这条，在平坦的新线上
        插一个点就会给 120 帧素材写出 ~120 个全是 1.0x 的关键帧。
        """
        frames = set()
        for frame, _speed in self.points:
            frames.add(round(float(frame), 6))
        for index, (start, end) in enumerate(zip(self.points, self.points[1:])):
            if self.segment_type(index) == LINEAR:
                continue
            if start[1] == end[1]:
                continue
            for frame in self._interior_frames(start[0], end[0]):
                frames.add(round(frame, 6))
        return sorted(frames)

    @staticmethod
    def _interior_frames(start, end):
        """``start..end`` 之间的**整数**帧（不含两端）。

        全仓只有这一处"往一段里撒整数帧"的实现：``keyframe_frames`` 给非线性段
        写 key 用它，``_frame_samples`` 展开被切点劈开的段也用它。两边必须逐帧
        重合——否则"画出来的线"与"真正播出来的速度"会在切断处当场分家。
        """
        return [float(frame)
                for frame in range(int(math.floor(start)) + 1, int(math.ceil(end)))
                if start < frame < end]

    def sampled_points(self, frame_step=1.0):
        """**绘制与命中共用**的采样：返回 ``(samples, owners)``。

        - ``samples``：``(rel_frame, speed)`` 升序序列，**含全部模型点**；非线性的段
          被细分成不大于 ``frame_step`` 帧的小段（线性段不细分——直线两点就够）。
        - ``owners``：长度 ``len(samples) - 1``，第 ``j`` 项是 ``samples[j] → samples[j+1]``
          这一小段所属的**模型段下标**（命中时用它决定拖哪一段）。

        画线与命中必须吃同一份采样，否则"线看得见、弯的地方抓不到"会以新形式复发。
        """
        step = _safe_float(frame_step, 1.0)
        if step is None or step <= 0.0:
            step = 1.0
        points = self.points
        samples = [(points[0][0], points[0][1])]
        owners = []
        for index in range(len(points) - 1):
            (f0, s0), (f1, _s1) = points[index], points[index + 1]
            span = f1 - f0
            pieces = 1
            if self.segment_type(index) != LINEAR and span > step:
                pieces = int(math.ceil(span / step))
            for piece in range(1, pieces):
                sub_frame = f0 + span * (piece / float(pieces))
                samples.append((sub_frame, self.line_speed_at(sub_frame)))
                owners.append(index)
            samples.append((f1, _s1))
            owners.append(index)
        return samples, owners

    # ---------- 基本访问 ----------

    def __len__(self):
        return len(self.points)

    def __iter__(self):
        return iter(tuple(self.points))

    def __eq__(self, other):
        return (
            isinstance(other, Curve)
            and self.points == other.points
            and self.types == other.types
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        body = ", ".join("(%.1f, %.3f)" % p for p in self.points)
        return "Curve([%s], duration=%.1f)" % (body, self.duration)

    def speeds(self):
        return [speed for _frame, speed in self.points]

    def frames(self):
        return [frame for frame, _speed in self.points]

    def is_valid(self):
        """不变量自检（测试与调试用）。"""
        frames = self.frames()
        speeds = self.speeds()
        return (
            len(self.points) >= MIN_POINTS
            and frames[0] == 0.0
            and all(frames[i] <= frames[i + 1] for i in range(len(frames) - 1))
            and all(SPEED_MIN <= s <= SPEED_CEIL for s in speeds)
            and len(self.types) == len(self.points) - 1
            and all(kind in CURVE_TYPES for kind in self.types)
        )

    # ---------- 几何查询 ----------

    def set_duration(self, duration):
        """跟随条带长度变化（只影响钳制范围，不改动已有点）。

        **刻意**不带动点：左端裁剪 / 缓存命中路径的行为必须保持不变
        （ADR-0004）。右端收短时的末端跟随 / 吞点由 ``watch`` 监听器驱动
        ``carry_tail`` 完成，不在这里做。
        """
        self.duration = max(0.0, _safe_float(duration, self.duration))

    def line_speed_at(self, rel_frame):
        """线上速度：按**段曲线类型**插值（``ease_ratio``）；越界外推取最近端点速度。"""
        rel = _safe_float(rel_frame, 0.0)
        points = self.points
        if rel <= points[0][0]:
            return points[0][1]
        if rel >= points[-1][0]:
            return points[-1][1]
        index = self.segment_index_at(rel)
        (f0, s0), (f1, s1) = points[index], points[index + 1]
        if f1 <= f0:
            return s0  # 同帧 = 硬切，与类型无关
        ratio = (rel - f0) / (f1 - f0)
        return s0 + (s1 - s0) * ease_ratio(self.segment_type(index), ratio)

    def segment_index_at(self, rel_frame):
        """``rel_frame`` 落在第几段（段 = 点 ``i`` 与点 ``i+1`` 之间）。

        越界取最近段；同帧（硬切）取左侧段。与 ``line_speed_at`` 的分段查找
        **同源**（同一处实现），两处各写一套必然在某个边界上分家。
        """
        rel = _safe_float(rel_frame, 0.0)
        points = self.points
        if rel <= points[0][0]:
            return 0
        if rel >= points[-1][0]:
            return len(points) - 2
        for index, ((f0, _s0), (f1, _s1)) in enumerate(zip(points, points[1:])):
            if f0 <= rel <= f1:
                return index
        return len(points) - 2

    def slice_between(self, low, high):
        """[ADR-0008 纯划分] 切出相对帧 ``low..high`` 这一段并**重定基**到 ``low``。

        切断不改变坐标的含意，所以这不是"裁剪"：

        - 两端点取**原曲线在该帧的值**（按该段类型插值）——因此接缝两侧速度恒相等；
        - **整段落在切区里**的段原样保留（连段类型一起）——同样的两端点、同样的
          区间长度 ⇒ 同样的一档缓动，逐帧完全相等；
        - **被切点劈开**的那一段改用**逐帧展开 + 线性小段**：缓动段的一截（例如
          「S 形的前六个百分点」）不在四档里，任何一档都表示不了它——若让该档把缩短
          后的区间重新铺满，形状会变，而**承载侧是按绝对帧逐帧写 key 的**（
          ``keyframe_frames``），于是"画出来的线"与"真正播出来的速度"当场分家。
          逐帧展开正是承载侧在做的事，两边的每一帧因此严格相等；
        - 区间与载荷没有交集时（曲线整体在切点之前），退化为该区间上的**外推
          水平线**——模型自带的"越界取最近端点速度"。

        判定"这确实是一次划分"是调用方（``store``）的事：本函数只管几何，
        给一段区间就切一段，不猜意图。
        """
        low = _safe_float(low, 0.0)
        high = _safe_float(high, low)
        if high < low:
            low, high = high, low
        # 先把**绝对帧**上的切片端点攒起来，最后一步才重定基到 low。
        absolute_points = [(low, self.line_speed_at(low))]
        types = []

        def append_point(frame, kind):
            """接一个重定基前的端点；与上一个点同帧就跳过（两半共享该点）。"""
            if abs(frame - absolute_points[-1][0]) <= FRAME_EPS:
                return
            absolute_points.append((frame, self.line_speed_at(frame)))
            types.append(kind)

        for index in range(len(self.points) - 1):
            (f0, _s0), (f1, _s1) = self.points[index], self.points[index + 1]
            if f1 <= low or f0 >= high:
                continue  # 整段在切区之外
            kind = self.segment_type(index)
            if low <= f0 and f1 <= high:
                append_point(f1, kind)  # 整段都在切区里：连类型一起原样保留
                continue
            if kind == LINEAR or _s0 == _s1:
                # 线性段（以及恒速段）被切一刀**不丢形状**：两端一取，切出来的
                # 那一截与原来逐帧相同，段类型照旧。
                append_point(f1 if f1 < high else high, kind)
                continue
            for frame in self._frame_samples(max(f0, low), min(f1, high)):
                append_point(frame, LINEAR)  # 被劈开的一截：逐帧展开成线性小段
        append_point(high, LINEAR)  # 尾端贴住本条带的右端（尾段延长线的那一端）
        return Curve([(frame - low, speed) for frame, speed in absolute_points],
                     high - low, types)

    @staticmethod
    def _frame_samples(start, end):
        """``start..end`` 的逐帧采样：两端 + 中间的所有整数帧。

        中间的整数帧必须与 ``keyframe_frames`` 给非线性段写的**同一套**
        （见 ``_interior_frames``），所以逐帧展开出来的折线与承载侧的 key
        逐帧重合。
        """
        return [start] + Curve._interior_frames(start, end) + [end]

    def tail_extension_end(self, duration=None):
        """末点左侧于时长时，返回尾段水平延长线的终点帧；否则 None。"""
        span = self.duration if duration is None else max(0.0, _safe_float(duration, self.duration))
        last_frame = self.points[-1][0]
        return span if last_frame < span - 0.5 else None

    def carry_tail(self, tail_frame):
        """末端跟随 / 吞点（ADR-0004）：把末点带到 ``tail_frame``（相对帧）。

        仅当条带右端收短越过末点时由监听器（``watch``）调用：

        - 末点被带到 ``tail_frame``（速度不变，位置贴住条带末端）；
        - 途中撞上倒数第二点时，删除的是**跟随中的末点本身**（吞点），
          由倒数第二点升格为新末点继续跟随——其左侧曲线一点不动，无跳变；
          删除瞬间升格者恰好位于末端，曲线在数学上连续；
        - 保底 ``MIN_POINTS``：只剩首末两点时末点无条件跟随；
        - ``tail_frame`` 在末点右侧（拉长）或正好处在末点时**不动**：
          尾段水平延长线自然变长；
        - 被吞的点不恢复（之后再拉长，它就是没了）；
        - **并段取左段类型**：被吞掉的末点两侧的两段并成一段，新段沿用**左**段的类型。

        返回是否有变化。
        """
        tail = _safe_float(tail_frame, None)
        if tail is None:
            return False
        tail = max(0.0, tail)
        changed = False
        while len(self.points) > MIN_POINTS and self.points[-2][0] >= tail:
            self.points.pop()  # 吞点：删跟随者，不删挡路者（挡路者会令前段跳变）
            self.types.pop()  # 并段：丢掉被吞点右侧那一段的类型 → 留下左段类型
            changed = True
        last_frame, last_speed = self.points[-1]
        if last_frame > tail:
            self.points[-1] = (tail, last_speed)
            changed = True
        return changed

    # ---------- 编辑操作 ----------

    def insert_point(self, rel_frame, speed=None):
        """插点（保持升序、避开已有点）。返回新点下标；未插入返回 None。

        - 初值取**该帧处缓动曲线上的值**（"点插上去，线不动"）；
        - 被劈开的那一段变成两段，**两段都继承原段类型**（形状不跳变）。
        """
        rel = _safe_float(rel_frame, None)
        if rel is None:
            return None
        rel = max(0.0, min(self.duration, rel))
        for frame, _speed in self.points:
            if abs(frame - rel) < MIN_GAP:
                return None
        value = self.line_speed_at(rel) if speed is None else clamp_speed(speed)
        index = len(self.points)
        for i, (frame, _speed) in enumerate(self.points):
            if frame > rel:
                index = i
                break
        self.points.insert(index, (float(rel), value))
        # 劈开第 (index-1) 段（index==0 时点插在首点之前，段号就是 0）
        split = max(0, min(index - 1, len(self.types) - 1))
        if self.types:
            inherited = self.types[split]
            self.types.insert(split, inherited)
        else:
            self.types.insert(0, LINEAR)
        return index

    def can_delete(self, index):
        """首点不可删（速度线须从素材起点开始），且至少保留 2 个点。"""
        return 0 < index < len(self.points) and len(self.points) > MIN_POINTS

    def delete_point(self, index):
        """删点；被拒绝（首点 / 保底 2 个）返回 False。

        **并段取左段类型**：删掉点 ``index`` 会让它左右两段并成一段，新段沿用左段类型。
        """
        if not self.can_delete(index):
            return False
        self.points.pop(index)
        if index < len(self.types):
            self.types.pop(index)  # 丢掉右段类型 → 合并段留在原位，用的就是左段类型
        elif self.types:
            self.types.pop()  # 删的是末点：两段并成一段，同样留在左段类型上
        return True

    def move_point(self, index, rel_frame=None, speed=None):
        """拖点：x 被相邻点钳制（首点 x 恒 0），y 钳制在值域内。返回是否变化。"""
        if index < 0 or index >= len(self.points):
            return False
        old_frame, old_speed = self.points[index]
        new_frame = old_frame if rel_frame is None else _safe_float(rel_frame, old_frame)
        if index == 0:
            new_frame = 0.0
        else:
            low = self.points[index - 1][0]
            high = self.points[index + 1][0] if index + 1 < len(self.points) else self.duration
            if high < low:
                high = low
            new_frame = max(low, min(high, new_frame))
        new_speed = old_speed if speed is None else clamp_speed(speed)
        if (new_frame, new_speed) == (old_frame, old_speed):
            return False
        self.points[index] = (float(new_frame), new_speed)
        return True

    # ---------- 段拖动（快照锚定） ----------

    def begin_segment_drag(self, index, grab_frame, scale=1.0, cap=None):
        """按住某段线：只挪这一段（两个端点同步平移）。

        ``scale`` 传入 ``FINE_SCALE`` 即按下时就在微调档（Ctrl 起拖，R9）。
        ``cap`` = 本次拖动的显示范围上限（缺省 = 数据硬顶）：拖动钳制走
        **按下那一刻的档**，拖动中改设置不追改会话。
        """
        if index < 0 or index + 1 >= len(self.points):
            raise IndexError("段下标越界：%r" % (index,))
        snapshot = [self.points[index][1], self.points[index + 1][1]]
        return SegmentDrag(self, (index, index + 1), snapshot,
                           self.line_speed_at(grab_frame), scale, cap=cap)

    def begin_tail_drag(self, grab_frame, scale=1.0, cap=None):
        """按住尾段延长线：只挪末点（``scale``/``cap`` 语义同 ``begin_segment_drag``）。"""
        last = len(self.points) - 1
        return SegmentDrag(self, (last,), [self.points[last][1]],
                           self.line_speed_at(grab_frame), scale, cap=cap)

    def begin_point_drag(self, index, anchor_speed, scale=1.0, cap=None):
        """[R9] 点拖的**锚定**会话：只挪该点的速度，帧一概不动。

        常规点拖是"绝对吸附"（速度 = 光标速度、帧跟光标，走 ``move_point``），
        不经过这里；微调模式下点拖没有锚定就谈不了"减半"，锚点语义与段拖
        **同构**：``速度 = 快照 + scale × (光标速度 − 锚点速度)``，锚取生效
        瞬间的光标速度 → 生效零跳变。帧冻结是微调对点拖的真实新增约束
        （段拖 / 尾段拖本就只改速度）。``cap`` 语义同 ``begin_segment_drag``。
        """
        if index < 0 or index >= len(self.points):
            raise IndexError("点下标越界：%r" % (index,))
        return SegmentDrag(self, (index,), [self.points[index][1]],
                           anchor_speed, scale, cap=cap)


class SegmentDrag(object):
    """拖动会话：快照锚定，1:1 跟手、可随时反向、钳制不粘连。

    ``scale`` 是增量系数（ADR-0005 修订 R9）：1.0 = 常规 1:1；
    ``FINE_SCALE`` = Ctrl 微调（光标挪 0.2x，点只挪 0.1x）。
    ``cap`` 是本次拖动的显示范围上限（缺省 = 数据硬顶 ``SPEED_CEIL``）：
    被拖端点钳在 ``[SPEED_MIN, cap]``，拖动中用户改设置不影响本会话。

    [R11] 输入语义：锚点与 ``apply`` 的光标值都是**未钳**的原始速度
    （``yfrac_to_speed_raw`` 的产物，下沿以下为负、上沿以上超 cap）——
    增量 ``scale × (光标 − 锚点)`` 因此**不随光标贴边而饱和**；点最终
    落在哪由 ``clamp_speed(快照 + delta, cap)`` 收口（值域边界是唯一的
    墙）。1:1 会话的可见结果与旧"输入也钳"的实现逐值一致。
    """

    def __init__(self, curve, indices, snapshot, anchor_speed, scale=1.0, cap=None):
        self.curve = curve
        self.indices = tuple(indices)
        self.snapshot = list(snapshot)
        # [R11] 锚点只消毒不钳标尺（_safe_float 的 1.0 兜底沿用既有约定）：
        # 钳制是值域边界的事（clamp_speed 管目标值），不是输入的事。
        self.anchor_speed = _safe_float(anchor_speed, 1.0)
        self.scale = max(0.0, float(scale))
        self.cap = SPEED_CEIL if cap is None else max(SPEED_MIN, float(cap))

    def reanchor(self, cursor_speed, scale=None):
        """[R9] 修饰键实时开关的重锚：快照 = 当前值、锚 = 当前光标速度。

        切换瞬间增量从零重新起步 → **零跳变**；``scale`` 不传就是只重锚
        不换档（回 1:1 时才传回 1.0）。[R11] 锚与输入同吃原始值，光标在
        界外时重锚同样零跳变。
        """
        self.snapshot = [self.curve.points[index][1] for index in self.indices]
        self.anchor_speed = _safe_float(cursor_speed, 1.0)  # [R11] 消毒不钳
        if scale is not None:
            self.scale = max(0.0, float(scale))
        return self

    def apply(self, cursor_speed):
        """按当前光标速度重算被拖动端点。返回是否发生变化。

        [R11] 光标值**不再预钳**：增量按原始值算，越过标尺边界后继续
        增长；钳制只在最终目标值上发生（``clamp_speed``，值域边界）。
        """
        delta = self.scale * (_safe_float(cursor_speed, 1.0) - self.anchor_speed)
        changed = False
        for index, original in zip(self.indices, self.snapshot):
            frame, current = self.curve.points[index]
            target = clamp_speed(original + delta, self.cap)
            if target != current:
                self.curve.points[index] = (frame, target)
                changed = True
        return changed


def _safe_float(value, fallback):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    if result != result or result in (float("inf"), float("-inf")):
        return fallback
    return result
