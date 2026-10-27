# -*- coding: utf-8 -*-
"""内存预览的纯规则层（**零 bpy**）：渲染范围、内存账、构建水位判定。

这些判定全是"给几个数 → 给一个结论"，放进 operator 就要开 Blender 才能测；
放这里用假对象测又快又稳。常量上挂着的是真机标定
（``.scratch/ram-preview/probe_006/008``）换来的事实，别当魔法数改掉。

五个被实测纠正过、不许"顺手简化"的点：

- ``cache_final_size`` 单位是 MB，但它**不按帧线性增长**——整段条带会一次
  性进缓存（播过两三帧，它就从 0 跳到覆盖整段的值）。因此它只能当"有没有
  东西进了缓存 / 顶没顶到上限"的信号，**不能当帧计数器**，更不能用字节数
  反推"构建完了多少帧"——**构建水位一律取播放头**。
- 内存不足引发的中止是**提前中止**；主判据永远是"播放头走到范围末尾"。
- **装不下时保留的是开头**（probe 013 实测：8 层时间线渲完 900 帧、缓存顶
  在第 74 帧就不再长了，事后第 1/50/150 帧重绘 3.5~4.5 ms（命中），而冷测
  同帧要 20~30 ms；第 300 帧之后回落到十几毫秒）。也就是 Blender 到顶后只是
  **不再收新的**，并不把先来的挤出去——这正是 Vegas 的"渲到哪里算哪里"，所以
  面板说"保留到第 N 帧"是诚实的，别改成"最后 N 帧"。
- "单位是 MB、1080p 约 7.9MB/帧"是比对出来的：RGBA8 理论值 7.91MB，实测
  "24 帧的时间线 → 189MB"与之吻合。
- 顶没顶到上限认两条门槛（比例 **或** 绝对空隙，取宽松的那个），见
  ``HEADROOM_MB``：Blender 留的空隙是几百 MB 的绝对值，不是一个固定比例。
"""

#: 每帧字节数：VSE 的 final cache 存 RGBA8（实测与理论吻合）。
BYTES_PER_PIXEL = 4
BYTES_PER_MB = 1024 * 1024

#: 出厂默认上限（MB）。真值以用户偏好为准，这里只作"上限读不到"时的兜底。
DEFAULT_CACHE_LIMIT_MB = 4096

#: 连续多少次采样没长进才算"不再增长"。缓存体积是大块跳变增长的，采样太密
#: 会把"还没记进来"误判成停滞，所以窗口给得比直觉宽。
STALL_WINDOW = 6

#: 占满到什么程度算"贴到上限"（**比例**）。见下面的 HEADROOM_MB：这一条单独
#: 用不够，必须与"绝对空隙"并用，两条取更宽松的那个。
NEAR_LIMIT_RATIO = 0.90

#: 离上限还剩多少 MB 也算"贴到上限"（**绝对空隙**）。真机测绘（probe 011/012）：
#: 上限 4096 MB 时最终水位停在 **3860 MB（94.2%）** 与 **3978 MB（97.1%）**
#: 两个不同位置——Blender 留的是几百 MB 的**绝对**空隙，不是固定比例。只认
#: 比例会出两种漏判：
#:
#: - 门槛取 95% 时，3860 那一次永远等不到"满"（probe 011 实红：宣称整段构建
#:   完成，其实后面的帧根本没留下）；
#: - 上限被调小（1~2 GB）时，几百 MB 的空隙占更大比重，水位落在比例门槛之下。
#:
#: 300 MB 覆盖两次实测（236 MB / 118 MB）并留了一点余量。代价要说清：装得下
#: 却恰好停在 90% 以上的范围会被误判成"装不下"，于是少渲一点。这个方向是刻意
#: 选的——**少渲一点顶多少一段，而谎称"已构建到第 N 帧"是最坏的错**。
HEADROOM_MB = 300

#: 两条门槛算出来的天花板最多低到上限的这么多为止（**地板**，见
#: ``MemoryWatch.at_limit`` 的说明）。再低就不是"留了点空隙"，而是"缓存刚动
#: 就喊满了"——小上限的用户会立刻看到构建在第 1 帧就停。
CEILING_FLOOR_RATIO = 0.5

#: resolve_range 的两种来源（source 字段）
SOURCE_PREVIEW = "preview"
SOURCE_SCENE = "scene"


def _frame(value):
    """场景帧属性 → int；读不到 / 非数值 → None（宁可不给，也不猜一个数）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def frame_mb(width, height, bytes_per_pixel=BYTES_PER_PIXEL):
    """一帧成品画面在缓存里的大小（MB）。

    纯估算即可：它只用于按下 Shift+B 那一刻的"大约需要多少"预告，
    不是计费。读不到尺寸返回 0（调用方按"估算不可用"处理，不中止构建）。
    """
    try:
        return max(0.0, float(width) * float(height)
                   * float(bytes_per_pixel) / BYTES_PER_MB)
    except (TypeError, ValueError):
        return 0.0


def estimate_mb(frames, width, height):
    """这段范围大概要占多少 MB（帧数 × 单帧）。"""
    try:
        count = max(0, int(frames))
    except (TypeError, ValueError):
        return 0.0
    return frame_mb(width, height) * count


def resolve_range(scene):
    """取内存预览的渲染范围：``(start, end, source)``，取不到则全 None。

    **预览范围优先，否则场景渲染范围**（ADR-0011 决定 4）——对应 Vegas 的
    "有循环区就渲循环区，没有就渲整个工程"。这是用户在 Blender 里自己选的
    起止，插件不自造第三个范围概念。

    端点整帧化：Blender 的帧是真整数，带小数的场景属性一律截断。
    ``start >= end``（空范围 / 反了）返回 None——空范围里起一次播放没有
    意义，交给调用方报告。
    """
    if scene is None:
        return (None, None, None)
    if getattr(scene, "use_preview_range", False):
        start = _frame(getattr(scene, "frame_preview_start", None))
        end = _frame(getattr(scene, "frame_preview_end", None))
        source = SOURCE_PREVIEW
    else:
        start = _frame(getattr(scene, "frame_start", None))
        end = _frame(getattr(scene, "frame_end", None))
        source = SOURCE_SCENE
    if start is None or end is None or end <= start:
        return (None, None, source)
    return (start, end, source)


def frame_count(start, end):
    """范围内的帧数（含首尾）；非法范围给 0。"""
    if start is None or end is None:
        return 0
    return max(0, int(end) - int(start) + 1)


def passed_end(current, end):
    """播放头是否已走到 / 越过范围末尾（构建完工的判据）。"""
    if current is None or end is None:
        return False
    try:
        return float(current) >= float(end)
    except (TypeError, ValueError):
        return False


def progress(start, end, current):
    """构建进度 0.0~1.0（按**播放头**算，不按缓存体积）。"""
    if start is None or end is None:
        return 0.0
    try:
        span = float(end) - float(start)
        if span <= 0:
            return 0.0
        value = (float(current) - float(start)) / span
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, value))


class MemoryWatch(object):
    """盯着缓存体积，判"内存已经装不下，别再渲了"。

    只看两件事：**有没有贴到上限**、**是不是连续若干次没再涨**。两者同时
    成立才建议中止——单独"没涨"可能是正常的帧间空隙（体积本就是大块跳变），
    单独"贴上限度"则可能恰恰是最后一帧刚好填满。

    上限读不到（``limit_mb`` 为 0 / None）时**永不建议中止**：宁可让用户看
    到"渲完了但可能已被挤出"，也不要在人家机器还有内存的时候自作主张停。
    """

    def __init__(self, limit_mb=None, stall_window=STALL_WINDOW,
                 near=NEAR_LIMIT_RATIO, headroom_mb=HEADROOM_MB):
        # 只有上限来自 Blender 偏好（属性不在时是 None，得兜）；其余三项则由
        # 模块常量和测试递进来，不需要防御。
        try:
            self.limit_mb = max(0, int(limit_mb or 0))
        except (TypeError, ValueError):
            self.limit_mb = 0
        self.stall_window = max(1, int(stall_window))
        self.near = float(near)
        self.headroom_mb = max(0, int(headroom_mb))
        self._peak = 0
        self._stalls = 0

    @property
    def peak(self):
        """见过的最大缓存体积（MB）。只有测试会读——生产路径只看 observe 的返回值。"""
        return self._peak

    @property
    def stalls(self):
        """当前连续未增长次数。同上：给测试和排错看的。"停滞"是中间量，对外
        的结论只在 ``observe`` 的返回值里。
        """
        return self._stalls

    def at_limit(self, size):
        """当前体积是否已贴到上限：比例与绝对空隙**取更宽松的那个**。

        理由见 ``HEADROOM_MB`` 的注释：Blender 留的空隙是绝对量，比例门槛在
        小上限下够不着。        地板是上限的一半——不是装饰：上限被调到 300MB 以下时，
        ``limit - headroom`` 会算成负数，什么都还没装就会喊"满了"。
        """
        if self.limit_mb <= 0:
            return False
        ceiling = min(self.limit_mb * self.near,
                      max(self.limit_mb - self.headroom_mb,
                          self.limit_mb * CEILING_FLOOR_RATIO))
        return size >= ceiling

    def observe(self, size_mb):
        """喂一次采样；返回 True = 该中止了（贴上限且已停止增长）。"""
        try:
            size = max(0, int(size_mb or 0))
        except (TypeError, ValueError):
            size = 0
        if size > self._peak:
            self._peak = size
            self._stalls = 0
            return False
        self._stalls += 1
        if self._stalls < self.stall_window:
            return False
        return self.at_limit(self._peak)


# ---------------------------------------------------------------------------
# 构建循环的判定与临时接管（鸭子类型，真 Scene/SequenceEditor 与假对象通用）
# ---------------------------------------------------------------------------

#: decide 的四种下一步；modal 只照着做，自己不发明判据。
ACTION_WAIT = "wait"
ACTION_DONE = "done"
ACTION_FULL = "full"
ACTION_STOPPED = "stopped"


def decide(playhead, end, cache_mb, watch, playing):
    """构建循环的一步判定 → 下一步动作（modal 按它执行，不再自己发明判据）。

    顺序有讲究：

    1. **播放头走到 / 越过末尾 → done**（ADR-0011 决定 7 的主判据）。内存
       水位 Watch **不能抢在它前面**：末帧刚好填满缓存是常见情形，那不是
       "装不下"，是刚好装完。
    2. 仍然播放中但内存已装满且不再增长 → full（提前中止，已构建部分保留）。
    3. 播放自己停了却没到末尾 → stopped（外部中止，已构建部分一样保留）。

    ``watch.observe`` 每次都先喂一次：上面三条无论走哪条，采样序列都是完
    整的，不会攒下一次丢失的算法记忆。
    """
    stalled = watch.observe(cache_mb)
    if passed_end(playhead, end):
        return ACTION_DONE
    if stalled:
        return ACTION_FULL
    if not playing:
        return ACTION_STOPPED
    return ACTION_WAIT


#: 构建期间接管的场景 / 编辑器设置（ADR-0011 决定 8）
BUILD_CACHE_FINAL = True
BUILD_SYNC_MODE = "NONE"
BUILD_AUDIO = False

_MISSING = object()


def _read(obj, name):
    """读属性；不存在返回 ``_MISSING``——缺了就别回去写一个假的默认值。"""
    return getattr(obj, name, _MISSING)


def capture_settings(editor, scene):
    """存旧值：``{"cache_final":…, "sync_mode":…, "audio":…}``。

    读不到的项一律不进字典——还原时照着字典写，**缺席的项不去捏一个假的
    默认值写回用户场景**（那是插件最常伤人的地方）。
    """
    saved = {}
    for key, obj, name in (("cache_final", editor, "use_cache_final"),
                           ("sync_mode", scene, "sync_mode"),
                           ("audio", scene, "use_audio")):
        if obj is None:
            continue
        value = _read(obj, name)
        if value is not _MISSING:
            saved[key] = value
    return saved


def apply_build_settings(editor, scene, saved=None):
    """临时接管：开 final 缓存、断音画同步、静音；返回存下的旧值。"""
    saved = capture_settings(editor, scene) if saved is None else saved
    if editor is not None and "cache_final" in saved:
        editor.use_cache_final = BUILD_CACHE_FINAL
    if scene is not None:
        if "sync_mode" in saved:
            scene.sync_mode = BUILD_SYNC_MODE
        if "audio" in saved:
            scene.use_audio = BUILD_AUDIO
    return saved


def clear_final_cache(editor):
    """清空 final 缓存：`use_cache_final` 拨 False 再拨回来是唯一手段。

    副作用要说清：**它清的是 Blender 自己攒的所有 final 缓存**，不只是一段
    范围里我们那几帧（ADR-0011 决定 6 认领的代价）。
    """
    if editor is None:
        return False
    try:
        editor.use_cache_final = False
        editor.use_cache_final = BUILD_CACHE_FINAL
    except Exception:
        return False
    return True


def restore_settings(editor, scene, saved):
    """还原接管项 → ``{"final_left_on": 是否被迫把 final 缓存留在开}``。

    **唯一不照抄旧值的是 ``use_cache_final``**：旧值为 False 时照抄
    （=关缓存）会把刚刚构建出来的东西当场清空，等于一次完整的白干。
    所以这种情形宁折不还原——如实告诉用户"本场景 final 缓存已替你打开，
    否则这段内存预览无处可存"，由用户决定要不要再关。其余两项一律精确还原。
    """
    report = {"final_left_on": False}
    saved = saved or {}
    try:
        if scene is not None:
            if "sync_mode" in saved:
                scene.sync_mode = saved["sync_mode"]
            if "audio" in saved:
                scene.use_audio = saved["audio"]
        if editor is not None and "cache_final" in saved:
            if saved["cache_final"]:
                editor.use_cache_final = True
            else:
                report["final_left_on"] = bool(getattr(
                    editor, "use_cache_final", True))
    except Exception:
        pass  # 还原失败不许盖掉构建结论：走到了这里，成果已经产出了
    return report


def limit_of(prefs):
    """内存上限（MB）；读不到退回出厂默认。"""
    try:
        system = getattr(prefs, "system", None)
        value = int(getattr(system, "memory_cache_limit", 0) or 0)
    except (TypeError, ValueError):
        return DEFAULT_CACHE_LIMIT_MB
    return value if value > 0 else DEFAULT_CACHE_LIMIT_MB


def resolution_of(scene):
    """预览大约按多大画面存：``(宽, 高)``；读不到给 (0, 0)。

    只用于按下 Shift+B 那一刻的"大约需要多少"预告。VSE 预览是否吃渲染百分比
    尚无实测结论，这里按"吃"来算，估错了也只影响一行预告。
    """
    try:
        render = scene.render
        pct = float(getattr(render, "resolution_percentage", 100) or 0) / 100.0
        return (int(float(render.resolution_x) * pct),
                int(float(render.resolution_y) * pct))
    except Exception:
        return (0, 0)


# ---------------------------------------------------------------------------
# 运行时状态（仍然零 bpy —— 这样 watch 与单测都能直接驱动它）
# ---------------------------------------------------------------------------

#: 结果语义。面板按它挑选文案；**不是错误码**，别当成异常用。
OUTCOME_IDLE = "idle"
OUTCOME_FINISHED = "finished"
#: **提前停止**：Esc / 播放自己停了 / Blender 单方面取消，三者同一语义——
#: 没走到范围末尾，但已构建的那一截照样留着（ADR-0011 决定 6）。名字叫
#: "stopped" 不叫 "cancelled"，是因为前两种根本不是用户点了取消，叫
#: cancelled 会让读代码的人以为它一定源自一次取消动作。
OUTCOME_STOPPED = "stopped"
OUTCOME_FULL = "full"
OUTCOME_ABORTED = "aborted"
OUTCOME_INVALIDATED = "invalidated"

#: 哪些结果**留下已构建的那半截**（其余一律当作什么都没留下）。
#: ui / preview 两边都按它判断，别各写一份——"成果算不算数"是一个判定，
#: 不是两个 UI 各自的口味。
OUTCOMES_THAT_KEEP = frozenset(
    (OUTCOME_FINISHED, OUTCOME_FULL, OUTCOME_STOPPED))

STATE = {
    #: 是否正在构建。**构建期间作废一律忽略**——那时 UI 是锁的，用户改不了
    #: 东西，能来的更新只可能是我们自己改设置激起的（ADR-0011 决定 5）。
    "building": False,
    "start": None,
    "end": None,
    "source": None,
    #: 成果属于哪个场景（多场景工程里区分"谁动的手"，见 ``invalidate``）
    "scene": None,
    #: 构建进行到哪一阵（面板用的进度）
    "frame": None,
    #: 构建水位：完工/中止时播放头所在的那一帧。**取播放头，不取字节换算**
    "watermark": None,
    "cache_mb": 0,
    "estimate_mb": 0.0,
    "limit_mb": 0,
    "built": False,
    "outcome": OUTCOME_IDLE,
}


def reset():
    """整体归零（撤销 / 重做 / 载入：旧的内存预览对新时间线没有意义）。"""
    STATE["building"] = False
    STATE["start"] = None
    STATE["end"] = None
    STATE["source"] = None
    STATE["scene"] = None
    STATE["frame"] = None
    STATE["watermark"] = None
    STATE["cache_mb"] = 0
    STATE["estimate_mb"] = 0.0
    STATE["limit_mb"] = 0
    STATE["built"] = False
    STATE["outcome"] = OUTCOME_IDLE


def begin(start, end, source, estimate_mb=0.0, limit_mb=0, scene_name=None):
    """开始一次构建：记范围与预估，旧成果作废。

    ``scene_name`` 记下这份成果属于哪条时间线——多场景工程里它是失效判定的
    一半（另一半见 ``invalidate``）。
    """
    STATE["building"] = True
    STATE["start"] = start
    STATE["end"] = end
    STATE["source"] = source
    STATE["scene"] = scene_name
    STATE["frame"] = start
    STATE["watermark"] = None
    STATE["cache_mb"] = 0
    STATE["estimate_mb"] = estimate_mb
    STATE["limit_mb"] = limit_mb
    STATE["built"] = False
    STATE["outcome"] = OUTCOME_IDLE


def note_progress(frame, cache_mb):
    """构建中的一次心跳（每 tick 喂一次：当前播放头 + 当前缓存体积）。

    体积在这里只是"观察值"，**不参与任何判定**——见本模块开头那条不允许
    简化的警告。上层别拿它反推进度。
    """
    STATE["frame"] = frame
    STATE["cache_mb"] = cache_mb


def finish(outcome, watermark):
    """收摊：记录结果语义与构建水位（水位＝那时的播放头）。

    **提前停止（``OUTCOME_STOPPED``）保留已构建的那半截**——用户说的是"我不想
    看了"（Esc）或者播放自己断了，都不是"我改了时间线"（ADR-0011 决定 6）。
    真正让成果消失的只有 ``invalidate``。只有 ABORTED（自家起手就失败，一帧
    没渲）才没得留。
    """
    STATE["building"] = False
    STATE["outcome"] = outcome
    STATE["watermark"] = watermark
    STATE["frame"] = watermark
    STATE["built"] = bool(watermark is not None
                          and outcome in OUTCOMES_THAT_KEEP)
    return STATE["built"]


def invalidate(scene_name=None):
    """**那个序列场景**有改动：整段作废（ADR-0011 决定 5）。

    刻意保守——凡是进了序列场景依赖图更新的事件都算数，**包括纯选中变化**。
    理由：显示过期画面比白渲一次严重得多，而省一次重渲省不出多少。

    ``scene_name`` 指明"谁动的手"：

    - 与构建时记录的场景一致 → 作废；
    - 是别的场景 → **不作废**。多场景工程里 A 场景的成果不该被 B 场景的编辑
      带走（同一个"指出是别人"的纪律，见 ADR-0003 与 ``preview`` 里的场景名
      校验）；
    - 为 None（调用方拿不到 / 没说）→ 按最保守处理，**照样作废**：说不清是谁
      动的手时，宁可白渲一次，也不要留过期画面。

    构建期间直接忽略（见 ``STATE["building"]`` 的说明）。
    """
    if STATE["building"]:
        return False
    if not STATE["built"]:
        return False
    mine = STATE.get("scene")
    if scene_name is not None and mine is not None and scene_name != mine:
        return False
    STATE["built"] = False
    STATE["outcome"] = OUTCOME_INVALIDATED
    STATE["watermark"] = None
    STATE["frame"] = None
    return True


def cached_range():
    """当前内存里那一段 ``(start, watermark)``；没有成果给 None。"""
    if not STATE["built"] or STATE["start"] is None or STATE["watermark"] is None:
        return None
    return (STATE["start"], STATE["watermark"])
