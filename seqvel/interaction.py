# -*- coding: utf-8 -*-
"""编辑交互：直接操纵（ADR-0005）——没有"编辑会话"这个东西。

交互模型（docs/adr/0005-direct-manipulation-no-edit-session.md）：

- **按下即判定**：在 VSE 时间轴上按下（左键 / 右键）先做命中检测——按在
  **可见**的速度线/点/尾段延长线上才开始拖动（一个随松手结束的短 modal），
  没按中一律 ``PASS_THROUGH``，原生选择、框选、空格播放、移动播放头从不
  被打断。鼠标路径**任何失败原因都不许返回 CANCELLED**——那会把点击吃掉
  （历史 bug：点条带反而选不中）。
- **可见 = 可抓**：命中集合就是绘制集合（``rules.visible_strips``），
  不存在"看不见却能抓住的线"；同屏多条线重叠时点 > 线、近 > 远
  （``geometry.pick_hit``）。
- **不碰选择态**（修订 R1）：拖动不设置条带的选中/活动——Vegas 式"拖包络
  不选素材"。拖动链路已核实无选中依赖；面板"活动条带"在拖动时与编辑对象
  脱节，由头部提示兜底。
- **右键按命中分流**：按在速度点上弹出点菜单（「删除点」，菜单项点击
  删除）；没按在点上穿透给原生右键。**没有键盘删点键位**（修订 R2：菜单
  打开期间键位表停摆，菜单加速键对插件键位不可靠，``D`` 已移除）——
  ``X`` 留给原生"删除条带"。
- **拖动期间吞事件**（修订 R4）：按下瞬间的穿透纪律只管**按下**；拖动一旦
  开始，modal 兜底必须是 ``RUNNING_MODAL``（吞掉一切），只显式放行导航与
  空格——否则按住拖动时 TWEAK 类事件漏给原生键位，原生开始拖条带、曲线
  冻住（v1.2.0 真机 bug）。TWEAK/INBETWEEN 显式当作拖动移动处理。
- **Ctrl = 微调拖动**（修订 R9）：按住 Ctrl 拖动时速度增量减半（光标挪
  0.2x，点只挪 0.1x），点拖同时**冻结帧**（仅 Y 移动）；修饰键**实时开关**
  ——每次翻转瞬间重锚（快照 = 当前值、锚 = 当前光标速度，零跳变），点拖
  退出微调时帧恢复跟随光标。命中、按下分流、悬停、双击插点一概不受
  Ctrl 影响；一次拖动无论开关多少次记一步撤销。键位侧需要 ctrl=True
  变体（编辑器表 + 工具表，R8 同款纪律）。
- **撤销按动作**：每次拖动/插点/删除各记一步（``REGISTER``+``UNDO``）。

三个入口（工具栏工具 / 面板按钮 / Alt+S）已随会话一起移除；唯一入口就是
时间轴上的直接按下。面板按钮路径走 ``execute`` 时只做礼貌拒绝——Blender
的 UI 按钮不携带事件，而直接操纵必须有真实鼠标位置，按钮没有。

命中与坐标换算一律走 ``regions``（显式定位时间轴 ``WINDOW`` 区域），绝不
使用 ``context.region.view2d``：按键派发虽然只在时间轴上发生，但
``bpy.ops`` 直调（脚本 / 测试）拿到的 ``context.region`` 可能是任何区域。

写盘时机：只在真正改动的动作上写（拖动松手、插入、删除各写一次）。
"""

import time
import traceback

import bpy

from . import carrier, curve, editing, geometry, i18n, overlay, regions, status, store, ui
from .curve import (CURVE_TYPES, FINE_SCALE, LINEAR, MIN_POINTS,
                    clamp_speed, normalize_type)
from .store import STORE, strip_base_frame, strip_channel, strip_duration, strip_key

#: 允许在拖动中穿透的导航事件（平移 / 缩放时间轴；空格见 _modal）
_NAVIGATION_EVENTS = {
    "MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "WHEELINMOUSE", "WHEELOUTMOUSE",
    "TRACKPADPAN", "TRACKPADZOOM", "NDOF_MOTION",
}

#: 拖动移动类事件：按住左键移动时，Blender 可能派发 MOUSEMOVE，也可能
#: 派发 TWEAK / INBETWEEN——三者都要当拖动处理，一个都不能漏。
_DRAG_MOVE_EVENTS = {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE", "EVT_TWEAK_L"}

#: 模块级双击检测：一次双击 = 两个独立的短 modal，状态必须跨运行存活
_CLICKS = editing.DoubleClick()

#: 右键选中的待删除点（模块级；点菜单项凭它定位）
_POINT_TARGET = None

#: 右键选中的段（模块级；段菜单项凭它定位）。与点目标同一种新鲜期纪律。
_SEGMENT_TARGET = None


def locate(context, event, note_hit=True):
    """对所有可见（=可抓）条带做命中检测并择近；返回 ``(strip, hit)``。

    悬停光标（``hover``）与按下分流共用本函数，保证"画的、悬的、抓的"
    永远是同一个集合。全失败（没有时间轴区域 / 没有鼠标坐标 / 全没命中）
    返回 ``(None, Hit(NONE))``。最近一次命中的结果写进 ``status``：面板靠
    它区分"根本没命中"和"命中了却没动"——``note_hit=False`` 供悬停轮询
    传参：0.1s 一次的轮询不许冲刷这个诊断位（那是"按下"的语义）。
    """
    view2d = regions.view2d(context)
    mouse = regions.mouse(event, context)
    if view2d is None or mouse is None:
        if note_hit:
            status.note_hit(geometry.HIT_NONE)
        return None, geometry.Hit(geometry.HIT_NONE, None, None)
    # 显示范围上限按显示场景同步（CONTEXT.md「显示范围上限」）：命中检测与
    # 绘制（overlay._draw_all 同样同步）永远同一档，"画的"才"抓得到"。
    ui.sync_active_cap(context)
    candidates = []
    for strip in overlay.visible_strips(context):
        try:
            hit = geometry.hit_test(view2d, carrier.strip_base_frame(strip),
                                    carrier.strip_channel(strip),
                                    carrier.strip_duration(strip),
                                    STORE.display_curve(strip), mouse)
        except Exception:
            continue  # 单条带坏数据只跳过它（与绘制同一条容错纪律）
        candidates.append((strip, hit))
    strip, hit = geometry.pick_hit(candidates)
    if strip is None:
        hit = geometry.Hit(geometry.HIT_NONE, None, None)
    if note_hit:
        status.note_hit(hit.kind)
    return strip, hit


def _set_point_target(strip, index, made_at=None):
    """记录右键选中的点（删除动作的定位依据）。"""
    global _POINT_TARGET
    _POINT_TARGET = editing.PointTarget(strip.name, index,
                                        time.monotonic() if made_at is None
                                        else made_at)


def _take_point_target(now=None):
    """取出并清除点目标；返回 ``(目标或 None, 是否过期)``。

    ``(None, False)`` = 没有右键目标（无删除意图，调用方静默穿透）；
    ``(None, True)`` = 有过但已过期（调用方必须说明原因，绝不静默）。
    """
    global _POINT_TARGET
    if _POINT_TARGET is None:
        return None, False
    target = _POINT_TARGET
    _POINT_TARGET = None
    now = time.monotonic() if now is None else now
    used = target.use(now)
    return used, used is None


def _set_segment_target(strip, index, made_at=None):
    """记录右键选中的段（段菜单项凭它定位）。"""
    global _SEGMENT_TARGET
    _SEGMENT_TARGET = editing.PointTarget(strip.name, index,
                                         time.monotonic() if made_at is None
                                         else made_at)


def _take_segment_target(now=None):
    """取出并清除段目标；返回 ``(目标或 None, 是否过期)``（语义同点目标）。"""
    global _SEGMENT_TARGET
    if _SEGMENT_TARGET is None:
        return None, False
    target = _SEGMENT_TARGET
    _SEGMENT_TARGET = None
    now = time.monotonic() if now is None else now
    used = target.use(now)
    return used, used is None


def _peek_segment_target(now=None):
    """**不消费**地读段目标（菜单画勾选态用）。"""
    target = _SEGMENT_TARGET
    if target is None:
        return None
    now = time.monotonic() if now is None else now
    return target.use(now)


def _peek_segment_type(context):
    """右键那一段当前的类型（菜单画勾用）；读不到返回 None（谁都不打勾）。"""
    target = _peek_segment_target()
    if target is None:
        return None
    strip_name, index = target
    strip = _find_strip(context, strip_name)
    if strip is None:
        return None
    curve = STORE.get(strip)
    if curve is None:
        # 还没模型：屏幕上那条默认线就是唯一一段，且它是线性
        return LINEAR
    return curve.segment_type(index)


def _delete_point_from(curve, index):
    """删除校验 + 删除；返回 ``(是否成功, 拒绝文案)``（文案走运行层 i18n）。"""
    if index == 0:
        return False, i18n.T("interaction.first_point_protected")
    if len(curve) <= MIN_POINTS:
        return False, i18n.T("interaction.min_points")
    if not curve.delete_point(index):
        return False, i18n.T("interaction.point_vanished")
    return True, ""


def _persist(context, strip, curve):
    """落盘三层：影子模型 → 条带自定义属性 → 原生 Speed Control 关键帧。

    拖动松手与删点共用；诊断写进面板，错误不吞。
    """
    STORE.commit(strip, curve)
    diag = carrier.write_curve(strip, curve)
    status.note_write(diag)
    if diag.get("errors"):
        status.set_error("；".join(diag["errors"]))
    ui.apply_retiming_display(strip)
    overlay.redraw(context)


def _restore_ours(strip_name, before):
    """[修订 R6/R7] 回收"恰好是我们这条线"的选择/激活翻转。

    条件不满足（用户自己的选择）一律不动：只回收"选中集合 ⊆ 快照集合
    ∪ {本条带}，或激活条带是本条带（快照里不是它）"的翻转。模块级
    （不依赖 operator实例）：按下时与松手后的延迟定时器共用同一套规则，
    两处行为必须严格一致。全程不抛（回收失败不能连累交互）。
    """
    if not strip_name or not before:
        return
    try:
        scene = bpy.data.scenes.get(before.get("scene") or "")
        se = getattr(scene, "sequence_editor", None) if scene else None
        if se is None:
            return
        strip = None
        for s in se.strips_all:
            if s.name == strip_name:
                strip = s
                break
        if strip is None:
            return
        before_sel = set(before.get("selected") or ())
        before_active = before.get("active")
        selected = {s.name for s in se.strips_all if getattr(s, "select", False)}
        active = getattr(se, "active_strip", None)
        active_name = getattr(active, "name", None)
        ours_only = selected <= (before_sel | {strip_name})
        active_ours = (active_name == strip_name
                       and before_active != strip_name)
        if not (ours_only or active_ours):
            return
        status.trace("selection restore: sel=%r active=%r -> %r"
                     % (sorted(selected), active_name, before_active))
        if strip_name not in before_sel and getattr(strip, "select", False):
            strip.select = False
        if active_ours:
            target = None
            if before_active:
                for s in se.strips_all:
                    if s.name == before_active:
                        target = s
                        break
            if target is not None:
                se.active_strip = target
            else:
                try:
                    se.active_strip = None
                except Exception:
                    pass
    except Exception as exc:
        status.trace("selection restore failed: %r" % (exc,))


def _schedule_selection_restore(strip_name, before, delay=0.15):
    """[修订 R7] 松手后**延迟**回收：等可能到达的合成 CLICK（若存在）
    落完再校验。用 ``bpy.app.timers`` 一次性任务——modal 已在松手时
    FINISHED，不能再靠 modal 生命周期（v1.2.5 的吞咬窗口用 PASS_THROUGH
    收摊 = 僵尸 modal，冻结时间线，真机 e2e 实锤后已废除）。
    """
    def _do():
        _restore_ours(strip_name, before)
        return None  # 返回 None = 一次性任务

    try:
        bpy.app.timers.register(_do, first_interval=delay)
    except Exception as exc:
        status.trace("schedule restore failed: %r" % (exc,))


class SEQVEL_OT_edit_speedline(bpy.types.Operator):
    # 静态层常驻英中双语（ADR-0009 修订 R1）：bl_label/bl_description 注册期固定
    bl_idname = "seqvel.edit_speedline"
    bl_label = "Drag Speed Line 拖动速度线"
    bl_description = ("Hold the speed line/point and drag to edit; the result is "
                      "written to native Speed Control. Missing the line passes "
                      "the click through untouched - selection and playback are "
                      "never disturbed. "
                      "按住速度线/速度点直接拖动编辑，结果写入原生 Speed Control；"
                      "没按到线一律原样穿透——选择与播放从不被打断")
    bl_options = {"REGISTER", "UNDO"}

    # ---------------------------------------------------------------- 进入
    def invoke(self, context, event):
        # [诊断 v1.2.3] 每次"按下"都留痕：分不清"键位没派发"和"派发了
        # 却没命中"是真机排障的头号难题，轨迹文件把这两层拆开。
        status.trace("edit.invoke %s/%s (%.0f,%.0f) %s"
                     % (getattr(event, "type", "?"), getattr(event, "value", "?"),
                        getattr(event, "mouse_x", -1.0),
                        getattr(event, "mouse_y", -1.0),
                        status.sel_snapshot(context)))
        try:
            return self._start(context, event)
        except Exception:
            status.set_error(i18n.T(
                "interaction.start_failed",
                traceback.format_exc().strip().splitlines()[-1]))
            status.trace("edit.invoke EXCEPTION\n"
                         + traceback.format_exc())
            return {"PASS_THROUGH"}  # 任何失败都不许吃掉点击

    def execute(self, context):
        """面板按钮已移除；F3/脚本直调时的礼貌拒绝。

        直接操纵必须有真实鼠标位置，而走 ``execute`` 的调用方没有事件——
        什么都不做就是"点了没反应"，所以必须说明白。
        """
        self.report({"INFO"}, i18n.T("interaction.execute_hint"))
        return {"CANCELLED"}

    def _start(self, context, event):
        strip, hit = locate(context, event)
        status.trace("edit.locate strip=%r hit=%s idx=%s"
                     % (getattr(strip, "name", None), hit.kind,
                        getattr(hit, "index", None)))
        if strip is None or hit.kind == geometry.HIT_NONE:
            return {"PASS_THROUGH"}  # 铁律：没按中，点击原样交还
        allowed, message = carrier.can_edit(strip)
        if not allowed:
            status.set_error(message)
            status.trace("edit.can_edit拒绝: %s" % message)
            return {"PASS_THROUGH"}  # 可见集合已按类型过滤，这条纯属防御

        status.trace("edit.start-can_edit %s" % status.sel_snapshot(context))
        self.strip = strip
        self.curve = STORE.get_or_create(strip)
        # [修订 R6] 按下即激活的翻转发生在键位派发之前，invoke 里现抓的
        # 快照必然是翻转后的——基准用**按下前最后一次悬停**的快照
        # （hover 常驻缓存），悬停还没跑过（触屏/脚本直调）才退回现抓。
        from . import hover as _hover
        data_now = status.sel_data(context)
        before = getattr(_hover, "last_sel_data", None)
        if (before is None or data_now is None
                or before.get("scene") != data_now.get("scene")):
            before = data_now
        self._sel_before = before
        # [修订 R6] 真机实锤（e2e 用户文件环境）：按下速度线所在条带的
        # 瞬间，原生层（键位表之前）就会把它选中+激活——违反 ADR-0005
        # "拖包络不选素材"的承诺，也是"激活后线被素材交互抢占"的根源。
        # 按下即按基准恢复，吞咬窗口关闭时再校验一次；只回收"恰好是
        # 我们这条线"的翻转，用户自己的选择绝不碰。
        self._restore_selection(context)
        status.trace("edit.start-model %s" % status.sel_snapshot(context))
        self.mode = "IDLE"
        self.index = None
        self.drag = None
        self.fine = False  # [修订 R9] Ctrl 微调开关（事件级实时跟踪）
        self.action_changed = False
        self._traced_idle_move = False  # [诊断 v1.2.4]

        self._begin_drag(context, event, hit)
        status.trace("edit.start-begun %s" % status.sel_snapshot(context))

        status.set_editing(strip.name)
        status.set_mode(self.mode)
        status.set_hint(status.hint_edit())
        status.clear_error()
        context.window_manager.modal_handler_add(self)
        overlay.set_header_text(context, self._header_text())
        overlay.redraw(context)
        return {"RUNNING_MODAL"}

    # ---------------------------------------------------------------- 事件
    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            # [诊断 v1.2.4] 模态异常之前只进面板（一行摘要），轨迹里没有
            # 全貌——真机排障时 traceback 原文比一行摘要有用得多。
            status.set_error(i18n.T(
                "interaction.interrupted",
                traceback.format_exc().strip().splitlines()[-1]))
            status.trace("modal EXCEPTION\n" + traceback.format_exc())
            self._cleanup(context)
            return {"FINISHED"}

    def _modal(self, context, event):
        # 修订 R4：拖动一旦开始，这里就是"全吞"模式。v1.2.0 的兜底是
        # PASS_THROUGH，按住拖动时 TWEAK 类事件漏给原生键位 → 原生开始拖
        # 条带、曲线冻住（真机 bug）。只有导航与空格显式放行。
        etype = event.type
        if etype in _NAVIGATION_EVENTS or etype == "SPACE":
            return {"PASS_THROUGH"}
        if etype in _DRAG_MOVE_EVENTS:
            if self.mode != "IDLE":
                self._apply_drag(context, event)
            elif not getattr(self, "_traced_idle_move", False):
                # [诊断 v1.2.4] 每个会话的第一条 mousemove 留痕一次（事件
                # 级留痕会刷屏，这条只证明"modal 还活着"）。
                self._traced_idle_move = True
                status.trace("modal move-IDLE %s" % status.sel_snapshot(context))
            return {"RUNNING_MODAL"}
        if etype == "LEFTMOUSE" and event.value == "RELEASE":
            # [诊断 v1.2.4] 松手前后各留一份选择/激活快照：真机报障
            # "拖动会激活底下的素材"——这两行能定位翻转发生在落盘前
            # （我们自己改的）还是落盘后（事件层漏出去被原生捡走）。
            status.trace("modal release-before %s" % status.sel_snapshot(context))
            self._end_action(context, event)
            status.trace("modal release-after %s" % status.sel_snapshot(context))
            # 修订 R7：**松手立刻收摊（FINISHED）**。v1.2.5 的"吞咬窗口"
            # 用 PASS_THROUGH 收摊——PASS_THROUGH 不会结束 modal，operator
            # 变僵尸吞掉一切鼠标事件，时间线冻结到狂敲 ESC（真机 e2e
            # 实锤：拖完后零 HOVER、第二次拖动死亡）。松手后可能到达的
            # 合成 CLICK 造成的选中翻转，改由一次性定时器按 R6 规则
            # 延迟回收（等 CLICK 落完再校验）。
            _schedule_selection_restore(self.strip.name
                                        if self.strip is not None else None,
                                        getattr(self, "_sel_before", None))
            return {"FINISHED"}
        if etype in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            # 拖动中的 Esc/右键 = 结束本次拖动（已拖出的改动照常落盘）
            status.trace("modal %s end-before %s"
                         % (etype, status.sel_snapshot(context)))
            self._finish(context)
            return {"FINISHED"}
        # 其余未知事件（TIMER、各种功能键……）一律吞掉：放行等于把拖动
        # 期间的控制权交给原生键位，正是真机接管 bug 的形态。
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        # 每次动作都已落盘，这里只需收尾
        self._cleanup(context)

    # ------------------------------------------------------------ 选择恢复
    def _capture_selection(self, context):
        """[修订 R6] 结构化选择/激活快照（悬停缓存不可用时的回退）。"""
        return status.sel_data(context)

    def _restore_selection(self, context):
        """[修订 R6] 把"恰好是我们这条线"的选择/激活翻转收回去。

        规则见模块级 ``_restore_ours``：只在"当前选中集合 ⊆ 快照集合
        ∪ {本条带}，且激活条带是本条带（快照里不是它）"时动手——
        用户自己的选择绝不碰。
        """
        _restore_ours(self.strip.name if self.strip is not None else None,
                      getattr(self, "_sel_before", None))

    # ---------------------------------------------------------------- 动作
    def _begin_drag(self, context, event, hit):
        """按下即开拖：按命中类型进入点/段/尾段拖动。

        [修订 R9] Ctrl 按住起拖 = 一开始就处于微调：点拖从"绝对吸附"换成
        **锚定会话**（帧冻结 + 增量减半，锚 = 按下瞬间的光标速度，零跳变）；
        段 / 尾段拖的会话照旧建（本就只改速度），减半由 ``_retune`` 统一换档。
        """
        self.press_px = self._mouse(context, event)
        grab_rel = None
        cursor_speed = None
        if self.press_px is not None:
            # 一次映射同时拿到相对帧（段/尾段拖的抓取点）与光标速度（微调锚点）。
            # [R11] 会话输入吃**未钳**映射（锚定增量不随光标贴边饱和）；
            # 非微调点拖走的 move_point 自己钳值，语义不变。
            mapped = geometry.mouse_to_rel_speed_raw(
                self._view2d(context), self._base_frame(), self._channel(),
                self.press_px)
            if mapped is not None:
                grab_rel, cursor_speed = mapped
        self.fine = bool(getattr(event, "ctrl", False)) and cursor_speed is not None
        # 本次拖动的钳制档 = 按下那一刻的显示范围上限（locate 已同步 ACTIVE_CAP；
        # 拖动中改设置不追改会话，CONTEXT.md「显示范围上限」）。[R11] 存在
        # 实例上：拖动中才开启微调的重锚 / 补建会话必须沿用**同一档**，
        # 不能退回数据硬顶。
        cap = self._cap = curve.active_cap()
        if hit.kind == geometry.HIT_POINT:
            self.mode, self.index, self.drag = "POINT", hit.index, None
            if self.fine:
                self.drag = self.curve.begin_point_drag(hit.index, cursor_speed,
                                                        FINE_SCALE, cap=cap)
        elif hit.kind == geometry.HIT_SEGMENT and grab_rel is not None:
            self.mode, self.index = "SEGMENT", hit.index
            self.drag = self.curve.begin_segment_drag(
                hit.index, grab_rel, FINE_SCALE if self.fine else 1.0, cap=cap)
        elif hit.kind == geometry.HIT_TAIL and grab_rel is not None:
            self.mode, self.index = "TAIL", None
            self.drag = self.curve.begin_tail_drag(
                grab_rel, FINE_SCALE if self.fine else 1.0, cap=cap)
        else:
            self.mode, self.index, self.drag = "IDLE", None, None
        if self.fine:
            status.trace("edit.start fine=on mode=%s" % self.mode)
        status.set_mode(self.mode)
        # [修订 R10] 公布"这次拖动会改变哪些点"：绘制层据此在未勾选百分比时
        # 临时把它们亮出来。按下即公布（不需要先移动过——按下就是激活）。
        status.set_drag_target(strip_key(self.strip), self._drag_indices())

    def _drag_indices(self):
        """[修订 R10] 本次拖动会改变的点下标。

        **不能**从拖动会话取：POINT 模式在非微调下 ``self.drag`` 是 None（走
        ``move_point`` 的绝对吸附），会话里没有下标。段拖是两端同步平移，
        尾段延长线只有末点一个会动的端。
        """
        if self.curve is None:
            return ()
        if self.mode == "POINT":
            return (self.index,) if self.index is not None else ()
        if self.mode == "SEGMENT":
            return (self.index, self.index + 1) if self.index is not None else ()
        if self.mode == "TAIL":
            count = len(self.curve.points)
            return (count - 1,) if count else ()
        return ()

    def _header_text(self):
        """拖动中的头部横幅（修订 R9：微调生效时带后缀，"不静默"纪律）。"""
        hint = status.hint_edit()
        if getattr(self, "fine", False):
            hint += i18n.T("status.fine_suffix")
        name = getattr(self.strip, "name", None) if self.strip is not None else None
        return i18n.T("status.header_editing", name, hint)

    def _retune(self, context, cursor_speed):
        """[修订 R9] 微调开关翻转：重锚，保证切换瞬间零跳变。

        - 点拖进入微调：新建锚定会话（快照 = 当前点速度、锚 = 当前光标速度）；
        - 点拖退出微调：回到绝对吸附（帧恢复跟随光标，跳到光标当前帧属预期）；
        - 段 / 尾段拖：原会话 ``reanchor`` 换档（微调 ×0.5 / 回 1:1）。
        """
        if self.mode == "POINT":
            if self.fine:
                # [R11] 重锚补建沿用按下那一刻的档（``self._cap``），不退回硬顶。
                self.drag = self.curve.begin_point_drag(
                    self.index, cursor_speed, FINE_SCALE, cap=self._cap)
            else:
                self.drag = None
        elif self.drag is not None:
            self.drag.reanchor(cursor_speed, FINE_SCALE if self.fine else 1.0)
        status.trace("fine %s mode=%s" % ("on" if self.fine else "off", self.mode))
        overlay.set_header_text(context, self._header_text())

    def _end_action(self, context, event):
        if self.action_changed:
            self._commit(context)
            _CLICKS.forget()  # 拖过的松手不算连击的第一次
        else:
            self._release_without_drag(context, event)
        self.mode, self.index, self.drag = "IDLE", None, None
        status.set_mode("IDLE")
        self._cleanup(context)

    def _release_without_drag(self, context, event):
        """没拖动的松手：参与双击检测，构成双击则插点。"""
        strip_id = strip_key(self.strip)
        px = self._mouse(context, event)
        if not _CLICKS.click(strip_id, px):
            return
        self._insert_point(context, event)

    def _apply_drag(self, context, event):
        mouse = self._mouse(context, event)
        if mouse is None:
            return
        ui.sync_active_cap(context)  # 光标映射按当前显示范围上限
        # [R11] 拖动输入吃**未钳**映射：微调锚定增量在光标越过条带边界后
        # 继续增长；非微调点拖的 move_point 自己钳值，1:1 语义不变。
        mapped = geometry.mouse_to_rel_speed_raw(
            self._view2d(context), self._base_frame(), self._channel(), mouse)
        if mapped is None:
            return
        rel_frame, cursor_speed = mapped
        # [修订 R9] 微调开关是**事件级实时**的：每次移动都比对 Ctrl 状态，
        # 翻转瞬间重锚（快照 = 当前值、锚 = 当前光标速度，零跳变）。
        fine_now = bool(getattr(event, "ctrl", False))
        if fine_now != self.fine:
            self.fine = fine_now
            self._retune(context, cursor_speed)
        if self.mode == "POINT":
            if self.fine:
                # 锚定会话缺席（按下时光标映射失败等）：现场补建，锚=当前光标；
                # [R11] 档沿用按下那一刻的 ``self._cap``，不退回数据硬顶。
                if self.drag is None:
                    self.drag = self.curve.begin_point_drag(
                        self.index, cursor_speed, FINE_SCALE, cap=self._cap)
                changed = self.drag.apply(cursor_speed)
            else:
                # [R11] 绝对吸附不走会话：光标值在此显式钳回当前档（会话侧
                # 的去饱和只属于锚定增量，不能让"点到光标处"越过显示档）。
                changed = self.curve.move_point(
                    self.index, int(round(rel_frame)),
                    clamp_speed(cursor_speed, curve.active_cap()))
        else:
            if self.drag is None:
                return
            changed = self.drag.apply(cursor_speed)
        if not changed:
            return
        self.action_changed = True
        overlay.redraw(context)

    def _insert_point(self, context, event):
        mouse = self._mouse(context, event)
        if mouse is None:
            return False
        mapped = geometry.mouse_to_rel_speed(
            self._view2d(context), self._base_frame(), self._channel(), mouse)
        if mapped is None:
            return False
        if self.curve.insert_point(int(round(mapped[0]))) is None:
            status.set_error(i18n.T("interaction.insert_busy"))
            return False
        self._commit(context)
        return True

    # ---------------------------------------------------------------- 落盘
    def _commit(self, context):
        _persist(context, self.strip, self.curve)
        self.action_changed = True

    def _finish(self, context):
        if self.action_changed:
            self._commit(context)
        self._cleanup(context)

    def _cleanup(self, context):
        overlay.set_header_text(context, None)
        self.mode, self.index, self.drag = "IDLE", None, None
        status.set_mode("IDLE")
        status.set_editing("")
        status.set_hint(status.hint_idle())
        # [修订 R10] 临时读数必须"松手即隐"，所以这里**自己补一次重绘**：松手
        # 路径里有一条既不落盘也不重绘的（按下没拖动 → 只判双击插点，不走
        # `_persist` 里那次 `overlay.redraw`），搭它的顺风车会漏掉那条路径。
        status.clear_drag_target()
        try:
            overlay.redraw(context)
        except Exception:
            pass

    # ---------------------------------------------------------------- 工具
    def _view2d(self, context):
        """时间轴的 view2d —— 经 ``regions`` 显式定位，**不是** ``context.region``。

        ``bpy.ops`` 直调（脚本/测试）时 ``context.region`` 可能是任何区域，
        而这些区域的 view2d 坐标系都不是"帧 / 通道"（真机实测，见 ``regions``
        模块）。拿它换算，命中检测必然落空。
        """
        return regions.view2d(context)

    def _base_frame(self):
        return strip_base_frame(self.strip)

    def _channel(self):
        return strip_channel(self.strip)

    def _mouse(self, context, event):
        """鼠标位置（时间轴区域坐标）；没有事件返回 None。"""
        return regions.mouse(event, context)


class SEQVEL_OT_point_menu(bpy.types.Operator):
    bl_idname = "seqvel.point_menu"
    # 静态层常驻英中双语（ADR-0009 修订 R1）
    bl_label = "Velocity Context Menu 速度线右键菜单"
    bl_description = ("Right-click a speed point = point menu, right-click a "
                      "line segment = segment curve type menu; misses pass "
                      "through. "
                      "右键速度点=点菜单，右键线上的某一段=段曲线类型菜单；"
                      "没命中一律穿透")
    # 不挂 UNDO：这个操作只记录"右键选中了哪个点/哪一段"，不改任何数据，
    # 压进撤销栈只会污染历史（真正的删除/改类型由各自的 operator 记一步）。
    bl_options = set()

    def invoke(self, context, event):
        status.trace("menu.invoke %s/%s"
                     % (getattr(event, "type", "?"), getattr(event, "value", "?")))
        try:
            return self._start(context, event)
        except Exception:
            status.set_error(i18n.T("interaction.menu_start_failed",
                                    traceback.format_exc().strip().splitlines()[-1]))
            status.trace("menu.invoke EXCEPTION\n" + traceback.format_exc())
            return {"PASS_THROUGH"}  # 任何失败都不许吃掉右键

    def _start(self, context, event):
        strip, hit = locate(context, event)
        status.trace("menu.locate strip=%r hit=%s idx=%s"
                     % (getattr(strip, "name", None), hit.kind,
                        getattr(hit, "index", None)))
        if strip is None:
            return {"PASS_THROUGH"}
        if hit.kind == geometry.HIT_POINT:
            _set_point_target(strip, hit.index)
            _open_menu(context, "SEQVEL_MT_point_menu",
                       i18n.T("interaction.point_menu_title"))
            # 目标已记录 = 右键的语义已达成（这个点被选中，菜单项凭它删除），
            # 菜单弹不弹得出是锦上添花；弹不出时面板有说明，但点击不算白吃。
            return {"FINISHED"}
        if hit.kind == geometry.HIT_SEGMENT:
            # ADR-0005 修订 R5：命中集合从"点"扩到"线的某一段"。
            # 代价是速度线附近那条窄带不再出原生右键菜单——已接受。
            _set_segment_target(strip, hit.index)
            _open_menu(context, "SEQVEL_MT_segment_menu",
                       i18n.T("interaction.segment_menu_title"))
            return {"FINISHED"}
        # HIT_TAIL（末点右侧的水平延长线不是段）与 HIT_NONE：原样交还原生右键
        return {"PASS_THROUGH"}


def _open_menu(context, menu_name, label):
    """弹出指定菜单；弹不出返回 False（目标仍保留，菜单项凭它定位）。

    **必须先确认有窗口**：后台模式（无窗口上下文）调用 ``wm.call_menu``
    会让 Blender 直接崩溃（实测 EXCEPTION_ACCESS_VIOLATION，连 Python
    异常都不给）——这不是"弹不出"而是"进程没了"，唯一防御就是不调。
    模块级函数：不依赖 operator 实例（替身/测试直调不踩鸭子类型坑）。
    """
    if getattr(context, "window", None) is None:
        status.set_error(i18n.T("interaction.menu_no_window", label))
        return False
    try:
        bpy.ops.wm.call_menu("INVOKE_DEFAULT", name=menu_name)
        return True
    except Exception as exc:
        # 弹不出菜单也不能假装成功：把真实原因写进面板；目标随新鲜期
        # 自动失效，不会留下可误触的动作。
        status.set_error(i18n.T("interaction.menu_invoke_failed", label, exc))
        return False


class SEQVEL_MT_point_menu(bpy.types.Menu):
    """速度点右键菜单：第一版只有「删除点」，后续设置项往这里加。"""

    bl_idname = "SEQVEL_MT_point_menu"
    # 静态层常驻英中双语（ADR-0009 修订 R1）
    bl_label = "Speed point 速度点"

    def draw(self, context):
        layout = self.layout
        layout.operator("seqvel.delete_point",
                        text=i18n.T("interaction.menu_delete_point"), icon="X")


class SEQVEL_MT_segment_menu(bpy.types.Menu):
    """段曲线类型菜单（ADR-0005 修订 R5）：只放四档，当前类型打勾。

    删点不在这里——那是点菜单的事（一个菜单一件事）。
    档名走运行层 ``i18n.type_label``：跟随语言切换。**不许**直接引用
    ``curve.TYPE_LABELS``——那是个未导入的名字，v1.5.0 曾因此让整个菜单
    在 draw 时 NameError（真机表现为"右键线段弹不出菜单"）。
    """

    bl_idname = "SEQVEL_MT_segment_menu"
    # 静态层常驻英中双语（ADR-0009 修订 R1）
    bl_label = "Segment shape 这段的形状"

    def draw(self, context):
        layout = self.layout
        current = _peek_segment_type(context)
        for kind in CURVE_TYPES:
            item = layout.operator("seqvel.set_segment_type",
                                   text=i18n.type_label(kind),
                                   icon="CHECKMARK" if kind == current else "NONE")
            item.kind = kind


class SEQVEL_OT_set_segment_type(bpy.types.Operator):
    bl_idname = "seqvel.set_segment_type"
    # 静态层常驻英中双语（ADR-0009 修订 R1）
    bl_label = "Set Segment Curve Type 设置段曲线类型"
    bl_description = ("Set the right-clicked segment to the given curve type "
                      "(linear/smooth/fast/slow) "
                      "把右键选中的那一段设为指定曲线类型（线性/平滑/快速/慢速）")
    bl_options = {"REGISTER", "UNDO"}

    kind: bpy.props.StringProperty(default=LINEAR)

    def execute(self, context):
        target, expired = _take_segment_target()
        if target is None and not expired:
            # 没有右键目标：没有改类型的意图（F3/脚本直调等）——静默穿透。
            return {"PASS_THROUGH"}
        if expired:
            status.set_error(i18n.T("interaction.segment_expired",
                                    editing.POINT_TARGET_TTL))
            return {"PASS_THROUGH"}
        strip_name, index = target
        strip = _find_strip(context, strip_name)
        if strip is None:
            self.report({"ERROR"}, i18n.T("interaction.strip_missing", strip_name))
            return {"CANCELLED"}
        # 还没有模型时顺手建一个：屏幕上那条默认 1.0x 直线就是"唯一一段"，
        # 否则"看得见的线改不了类型"就破了"可见=可抓"（ADR-0005）。
        curve_model = STORE.get_or_create(strip)
        if index < 0 or index >= len(curve_model.types):
            self.report({"ERROR"}, i18n.T("interaction.segment_gone"))
            return {"CANCELLED"}
        label = i18n.type_label(normalize_type(self.kind))
        if not curve_model.set_segment_type(index, self.kind):
            status.clear_error()
            self.report({"INFO"}, i18n.T("interaction.segment_is", label))
            return {"FINISHED"}
        _persist(context, strip, curve_model)
        status.clear_error()
        self.report({"INFO"}, i18n.T("interaction.segment_set", label))
        return {"FINISHED"}


class SEQVEL_OT_delete_point(bpy.types.Operator):
    # 静态层常驻英中双语（ADR-0009 修订 R1）
    bl_idname = "seqvel.delete_point"
    bl_label = "Delete Speed Point 删除速度点"
    bl_description = ("Delete the right-clicked speed point (at least 2 points "
                      "kept; the first point is protected) "
                      "删除右键选中的速度点（至少保留 2 个，首点受保护）")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        target, expired = _take_point_target()
        if target is None and not expired:
            # 没有右键目标：没有删除意图（F3/脚本直调等）——静默穿透。
            return {"PASS_THROUGH"}
        if expired:
            # 有过目标但已过期：绝不能静默（"不静默"纪律），说明原因后放行。
            status.set_error(i18n.T("interaction.point_expired",
                                    editing.POINT_TARGET_TTL))
            return {"PASS_THROUGH"}
        strip_name, index = target
        strip = _find_strip(context, strip_name)
        if strip is None:
            self.report({"ERROR"}, i18n.T("interaction.strip_missing", strip_name))
            return {"CANCELLED"}
        curve_model = STORE.get(strip)
        if curve_model is None or index >= len(curve_model):
            self.report({"ERROR"}, i18n.T("interaction.point_gone"))
            return {"CANCELLED"}
        ok, message = _delete_point_from(curve_model, index)
        if not ok:
            status.set_error(message)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        _persist(context, strip, curve_model)
        status.clear_error()
        self.report({"INFO"}, i18n.T("interaction.point_deleted"))
        return {"FINISHED"}


def _find_strip(context, strip_name):
    """按名字在时间轴场景里找条带（模块级：不依赖 operator 实例）。"""
    scene = regions.displayed_scene(context)
    editor = getattr(scene, "sequence_editor", None)
    if editor is None:
        return None
    for strip in getattr(editor, "strips", ()):
        if strip.name == strip_name:
            return strip
    return None


class SEQVEL_OT_delete_speedline(bpy.types.Operator):
    bl_idname = "seqvel.delete_speedline"
    # 静态层常驻英中双语（ADR-0009 修订 R1）
    bl_label = "Delete Speed Line 删除速度线"
    bl_description = ("Delete this strip's speed line and remove its Speed "
                      "Control carrier strip "
                      "删除该条带的速度线，并移除承载用的 Speed Control 效果条带")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        strip = carrier.target_strip(context)
        if strip is None:
            self.report({"ERROR"}, carrier.no_target_message(context))
            return {"CANCELLED"}
        STORE.forget(strip)
        removed = carrier.delete_carriers(strip)
        ui.restore_retiming_display(strip)
        status.note_write(None)
        status.clear_error()
        overlay.redraw(context)
        self.report({"INFO"}, i18n.T("interaction.line_deleted", removed))
        return {"FINISHED"}


_CLASSES = (SEQVEL_OT_edit_speedline, SEQVEL_OT_point_menu,
            SEQVEL_MT_point_menu, SEQVEL_MT_segment_menu,
            SEQVEL_OT_set_segment_type, SEQVEL_OT_delete_point,
            SEQVEL_OT_delete_speedline)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
