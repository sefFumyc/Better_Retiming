# -*- coding: utf-8 -*-
"""条带区间监听：素材端点一变就把承载重新落地（末端跟随 / 映射表重建）。

挂 ``depsgraph_update_post``：原生裁剪条带 → 依赖图更新 → 扫描带速度线的
源条带，比较**目标区间** ``(左端句柄, 右端句柄)``：

- 右端收窄越过末点 → ``Curve.carry_tail``（越过倒数第二点时吞掉跟随中的末点）；
- 区间变了 → 重新落地：左端变了 / 吞了点就全量重写关键帧（key 是按
  ``left_handle + 相对帧`` 写的绝对帧，左端一动必须重锚），只动右端就只
  对齐承载 + **强制重建速度映射表**（``carrier.invalidate_speed_map``）。

拖动全程所见即所得，不存在"点悬在条带外"或"拉长出来的那一段不跟着变"的中间态。

同一个回调还兼一件事：**内存预览的失效**（``observe_memory_preview``，
ADR-0011 决定 5）。序列场景里任何动静都让已构建的内存预览整段作废——放在
这里而不是另起一个回调，是因为"改动"这件事只有一个来源，分成两个监听器
只会多一处可能漏事件的地方。

边界（ADR-0004 定死，不要"顺手补全"）：

- **模型不随条带变化**这条语义只管"点集与速度值不改"（拉长时末点原地不动、
  尾段水平延长线自然变长）。它**不**等于"拉长什么都不用做"——承载层必须跟着
  重落地，见修订 R1；
- 拉长**不带动末点**：末点位置与速度都原地不动；
- 只管**已落盘**的速度线（条带上有 ``seqvel_curve`` 属性）：编辑会话中
  尚未首次落盘的内存默认曲线不属于监听对象，避免把默认曲线"替用户提交"；
- **被切断的半段只落地、不写模型载荷**（ADR-0008 修订 R1）：写回会擦掉划分链的
  证据（两半载荷不再逐字相同），兄弟当场退回"整份载荷按自己长度钳回"，还会多
  出一个隐形撤销步。判据是 ``STORE.is_division_member``，只吃盘上证据；
- 首次见到只**重建映射表与对齐**，不写**模型载荷属性**（``seqvel_curve`` /
  ``seqvel_curve_types`` / ``seqvel_curve_range``）：撤销 / 重做 / 载入后
  跟踪表被清空，重见时补一次自愈就够，例行自愈不该去动用户的曲线数据。
  **别读成"一个属性都不写"**——对齐与显示同步照旧同值重写（承载的
  ``channel`` / ``speed_control``、目标的 ``show_retiming_keys``），那是
  派生状态的幂等恢复，不是模型数据；至于那些写会不会翻起 Blender 的
  "未保存"标记，脚本层量不到，别写死结论；
- 落盘复用手动编辑的 ``STORE.commit`` + ``carrier`` 的落地入口，但
  ``reset_retiming_first=False``：depsgraph 处理器里调 ops 不可靠，而
  Retiming 重置对正常条带本来就是空操作，交给下一次显式编辑即可；
- 撤销 / 重做 / 载入时清空跟踪表（曲线缓存由 ``store`` 的同名机制清），
  跟随产生的改动发生在原生裁剪操作过程中，与裁剪合为一步撤销。
- **依赖图更新清单里的 id 是求值副本**（``depsgraph.updates[i].id``）：经它做的
  每一处 RNA 写都会被静默丢弃，所以挑目标场景时必须归一到 ``.original``
  （修订 R3，见 :func:`_original_of`）；归不到就跳过并报错，**不许拿副本凑合**。
  回调入参 ``scene`` 本来就是原始数据块，只作末级兜底；
"""

try:
    import bpy
    from bpy.app.handlers import persistent
except ImportError:  # 纯 Python 环境（单测）：核心逻辑用假对象驱动
    bpy = None

    def persistent(func):  # noqa: F811 — bpy 缺席时原样返回
        return func

from . import i18n, ram, rules, status
from .store import (SPAN_EPS, STORE, same_span, strip_base_frame, strip_key,
                    strip_span)

#: 条带标识 → 上次见到的目标区间 ``(左端句柄, 右端句柄)`` 绝对帧
_TRACKED = {}

#: 出错文案只打印一次（同一异常反复刷屏没有信息量）
_last_error = None


@persistent
def reset(*_args, **_kwargs):
    """撤销 / 重做 / 载入后清空跟踪表（签名各异，统一 *args 兼容）。

    **必须标 persistent**：Blender 默认在加载新文件时清空所有未标
    persistent 的处理器（官方文档明文），不标的话用户一打开 .blend
    监听器就全灭——"末端跟随真机无效"的根因。

    同一时刻也要作废内存预览（ADR-0011 决定 5）：撤销掉的可能正是当年
    建缓存时的那次编辑，留着旧画面等于让面板撒谎。
    """
    _TRACKED.clear()
    ram.reset()


def _commit(strip, curve, reanchor=True, write_prop=True):
    """区间变化后的落地：属性（可选）+ 承载（轻 / 重两条路）。

    - ``write_prop``：要不要写**模型载荷属性**（``STORE.commit`` 负责的
      ``seqvel_curve`` / ``seqvel_curve_types`` / ``seqvel_curve_range``）。
      **首次见到时是 False**——读取路径的例行自愈不该动用户的曲线数据，而
      "刷新载荷区间"只该在真的观测到区间变化时发生（也守住 ADR-0008"旧文件
      区间未知时不猜、不迁移"）。它对承载层与显示同步**不生效**：那两件的
      写发生在本函数末段，是幂等恢复。
    - ``reanchor``：要不要重写关键帧。关键帧是按 ``left_handle + 相对帧`` 写的
      **绝对帧**（``carrier.insert_keys``），所以**左端一动就必须重锚**；末点被
      吞掉时点集变了，同样得重写。只动右端则不必——那是本修订要修的病灶：
      代码里写着"拉长不带动模型"，于是拉长时**什么都没做**，连按新长度重建
      速度映射表都没做。

    ``carrier`` / ``ui`` 都是 bpy 层，延迟到这里导入——纯 Python 单测会
    替换掉整个 ``_commit``，不会走到这条路径。
    """
    from . import ui
    from .carrier import relay_span, write_curve

    if write_prop:
        STORE.commit(strip, curve)
    diag = None
    if not reanchor:
        diag = relay_span(strip, curve)  # 承载条带没了会返回 None → 退全量
    if diag is None:
        diag = write_curve(strip, curve, reset_retiming_first=False)
    status.note_write(diag)
    if diag.get("errors"):
        status.set_error("；".join(diag["errors"]))
    ui.apply_retiming_display(strip)


def _heal_first_sight(strip, curve):
    """首次见到一条已落盘的速度线：补一次"只重建映射表"的落地（修订 R1）。

    撤销 / 重做 / 载入都会清空跟踪表，重见时条带可能已经被改过区间而没人接手
    ——速度映射表的长度、承载对齐都可能对不上当下。**不写模型载荷属性**：见
    ``_commit`` 的 ``write_prop``。

    返回恒为 ``False``，但**它确实落了地**（重建映射表 + 对齐）——只是这不算
    "观测到一次区间变化"，所以不计入 ``observe_*`` 的落盘次数（那两处的计数
    语义是"因变化而落地"）。别把这个 False 读成"什么都没做"。
    """
    try:
        _commit(strip, curve, reanchor=False, write_prop=False)
    except Exception as exc:
        status.set_error(i18n.T("watch.write_failed", type(exc).__name__, exc))
    return False


def observe_strip(strip, tracker=None):
    """对单条条带执行区间监听；返回是否**因观测到区间变化**而落地。

    首次见到会补一次自愈落地却**返回 False**（那不叫变化），所以调用方拿到的
    True 一律意味着"区间真的变了"。判定矩阵（ADR-0004 修订 R1）：
    无属性 / 非源类型 / 区间读不到 → 不管；
    首次见到 → 收编基准并补一次轻落地；**区间没动 → 不管**；区间动了 →
    先按需要执行末端跟随（只在右端收窄时），再按"关键帧要不要重锚"选一条
    落地路径：左端变了或吞了点 → 全量重写；只动右端 → 轻量重建映射表。
    """
    if strip is None:
        return False
    if tracker is None:
        tracker = _TRACKED
    if getattr(strip, "type", "") not in rules.SOURCE_TYPES:
        return False
    if STORE.read_prop(strip) is None:
        return False  # 没有已落盘的速度线：不监听，更不替用户提交默认曲线
    curve = STORE.get(strip)
    if curve is None:
        return False
    span = strip_span(strip)
    if span is None:
        return False
    key = strip_key(strip)
    last = tracker.get(key)
    if last is None:
        tracker[key] = span  # 首次看到：收编基准（历史脏数据已在读取时钳回）
        return _heal_first_sight(strip, curve)
    if same_span(span, last):
        return False
    carried = False
    if span[1] < last[1]:  # 右端收窄才谈得上末端跟随；拉长时末点原地不动
        carried = curve.carry_tail(span[1] - strip_base_frame(strip))
    reanchor = carried or abs(span[0] - last[0]) > SPAN_EPS
    # **先推进基准再落地**：落地会写 RNA（重建映射表、对齐承载），可能再激起
    # 一次 depsgraph 更新；重入时看到的必须已经是新区间，否则同一次变化落两遍。
    tracker[key] = span
    # 被切开的一半**只落地、不写载荷**（ADR-0008 修订 R1）：写回会毁掉划分链的
    # 证据（载荷不再与兄弟逐字相同），兄弟当场退回"整份载荷按自己长度钳回"——
    # 用户在 GUI 里看到的就是"按 K 之后右半变成前半段的复制"。它还会多出一个
    # 隐形撤销步（真机症状：Ctrl+Z 一次只修好、两次才撤回切断）。
    # 证据：`.scratch/cut-regress/evidence.md`。
    write_prop = not STORE.is_division_member(strip)
    try:
        _commit(strip, curve, reanchor=reanchor, write_prop=write_prop)
    except Exception as exc:
        # 落盘失败也推进基准：carry 已生效且幂等，保留信号只会每次更新空转；
        # 真实原因写进面板（write_curve 自身的错误走 diag，也会落到 status）。
        status.set_error(i18n.T("watch.write_failed",
                                type(exc).__name__, exc))
    return True


def observe_scene(scene, tracker=None):
    """扫描一个场景的顶层条带，处理区间变化（右端收窄 / 拉长 / 左裁 / 移动）。

    返回**因变化而落地**的条带条数（首次见到的自愈不计，见 ``observe_strip``）。
    """
    editor = getattr(scene, "sequence_editor", None)
    if editor is None:
        return 0
    if tracker is None:
        tracker = _TRACKED
    # list(strips) 失败不在这里吞：交给 _on_depsgraph 的统一错误出口
    # （打印一次 + status 写真实原因），否则整场监听静默失效。
    strips = list(editor.strips)
    seen = set()
    writes = 0
    for strip in strips:
        seen.add(strip_key(strip))
        if observe_strip(strip, tracker):
            writes += 1
    scene_name = getattr(scene, "name", "") or ""
    for key in [k for k in tracker if k[0] == scene_name and k not in seen]:
        del tracker[key]  # 条带已删：跟踪记录一并清掉（防名字复用串线）
    return writes


def observe_memory_preview(scene, targets=()):
    """内存预览失效判定（ADR-0011 决定 5）：**动到的那个**序列场景让成果作废。

    刻意比 Blender 自己的失效保守——实测 Blender 只会部分失效（挪通道时
    45→43MB，改名毫无反应），指望它清干净等于留下过期画面。代价是多渲一次，
    但"显示过期画面"比"白渲一次"严重得多。

    场景的名字要一路递到 ``ram.invalidate``：多场景工程里成果只属于它被构建
    的那条时间线，A 场景的成果不该被 B 场景的编辑带走。

    构建期间由 ``ram.invalidate`` 自己忽略（那时 UI 锁着，能来的更新只可能
    是我们自己改设置激起的），这里不需要知道这个细节。
    """
    candidates = list(targets or ())
    if getattr(scene, "sequence_editor", None) is not None:
        candidates.append(scene)
    for target in candidates:
        if getattr(target, "sequence_editor", None) is None:
            continue  # 没有序列编辑器的场景改动与预览无关
        if ram.invalidate(getattr(target, "name", None)):
            status.trace("watch: ram preview invalidated by scene %r"
                         % getattr(target, "name", "?"))
            return True
    return False


def _original_of(obj):
    """把依赖图给的 id 还原成**原始数据块**；还原不了返回 ``None``（修订 R3）。

    ``depsgraph.updates[i].id`` 装的是**求值副本**（copy-on-write），不是用户正在
    编辑的那个数据块。经副本做的每一处 RNA 写都会被**静默丢弃**——映射表重建只写
    一个结构体字段、``clear_keys`` / ``insert_keys`` 动的是副本的 fcurve、自定义属性
    （模型载荷）在副本上是另一份——**而 ``diag`` 照样报 ``invalidated: True``**，
    面板与既有用例一律说"成功"。

    真机症状（2026-10-01 实测）：拉长素材右端 → 新长出来那一段永远是黑的；用户在
    控制台对**原始**条带跑一次裸 ``invalidate_speed_map`` 当场就好。整条证据链见
    ``.scratch/extend-blackout/evidence.md``。

    拿不到 ``.original`` 时**返回 None 交给调用方**，绝不返回副本凑合："拿副本当原始
    用"正是这次故障的形态，宁可跳过并报错（不静默）。
    """
    if not getattr(obj, "is_evaluated", False):
        return obj
    return getattr(obj, "original", None)


@persistent
def _on_depsgraph(scene, depsgraph):
    """depsgraph 回调：只扫本次更新涉及的场景；自身异常绝不冒泡到 Blender。

    更新清单里装的是**求值副本**，挑目标时必须先归一到 ``.original``（修订 R3）；
    归不到的不动它、并把原因写到面板——写副本等于没写。
    """
    global _last_error
    try:
        targets = []
        seen = set()
        unresolved = []
        for update in getattr(depsgraph, "updates", ()) or ():
            obj = getattr(update, "id", None)
            if obj is None:
                continue
            original = _original_of(obj)
            if original is None:
                unresolved.append(getattr(obj, "name", "?"))
                continue
            if id(original) in seen:
                continue
            # 只有场景有 sequence_editor：借它把 Scene 数据块挑出来，
            # 不依赖"更新报告的 id 一定是 context.scene"（ADR-0003 的坑），
            # 也**不能**是它的求值副本（ADR-0004 修订 R3）。
            if getattr(original, "sequence_editor", None) is not None:
                seen.add(id(original))
                targets.append(original)
        if unresolved:
            status.set_error(i18n.T("watch.evaluated_target",
                                    ", ".join(unresolved)))
        if not targets and getattr(scene, "sequence_editor", None) is not None:
            targets.append(scene)  # 更新清单拿不到场景时退回回调入参（入参本就是原始）
        observe_memory_preview(scene, targets)
        for target in targets:
            observe_scene(target)
    except Exception:
        import traceback

        error = traceback.format_exc()
        if error != _last_error:
            print("[seqvel] 端点监听错误:\n" + error)
        _last_error = error
        brief = error.strip().splitlines()[-1]
        status.set_error(i18n.T("watch.listener_error", brief))
        return
    _last_error = None


_HANDLERS = (
    ("depsgraph_update_post", _on_depsgraph),
    ("undo_post", reset),
    ("redo_post", reset),
    ("load_post", reset),
)


def register_handlers():
    if bpy is None:
        return
    for name, callback in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is None or callback in handlers:
            continue
        handlers.append(callback)


def unregister_handlers():
    if bpy is None:
        return
    for name, callback in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is None:
            continue
        while callback in handlers:
            handlers.remove(callback)
