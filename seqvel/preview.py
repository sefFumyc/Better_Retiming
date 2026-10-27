# -*- coding: utf-8 -*-
"""内存预览：Shift+B 把渲染范围**真的播一遍**（ADR-0011）。

机制一句话：**Blender 的 final cache 才是我们的"内存"，我们一根像素都不碰。**
而想让 final cache 装上东西，只有"真的播"（``screen.animation_play``）这一
条路——逐帧 ``frame_set`` + 重绘实测一个字节都写不进去（见 ADR-0011 背景）。
所以本模块看起来粗陋地"启动一次播放并盯着播放头"，那不是偷懒，是唯一正解。

Vegas 的行为（用户确认）：按下 Shift+B 之后用户动不了任何东西，只能看着播
放头扫、预览窗跟着动；播完回到范围开头，等用户自己再播。因此本 operator 在
构建期间**吞掉一切事件**——这在本插件里是有风险的（ADR-0005 修订 R7：模态
回到 ``PASS_THROUGH`` 却不结束 ⇒ 僵尸 operator 吞掉所有鼠标事件 ⇒ 时间线
冻结），所以收摊纪律写得比别处死：

- 结束条件有**六条**，全写在 ``_modal`` 里，每条都必须 ``FINISHED``：到
  末尾 / Esc / 内存装满 / 播放自己停了 / 播放压根没起来 / 跑表超时；包裹层
  （``modal``）还有一条兜底——任何异常都按"中断"收摊，绝不把 modal 留在半路；
- ``_cleanup`` 幂等，且是本文件唯一写"还原"的地方；
- **自我激起的东西必须在 ``ram.finish`` 之前走完**——挪播放头、改设置都会
  激起依赖图更新，而 ``ram.invalidate`` 在 ``building`` 期间一律忽略。换句话
  说 ``ram.finish()`` 是 cleanup 的**最后一行**，顺序反过来就是"刚建完就被
  自己作废"。
"""

import time
import traceback

import bpy
from bpy.app.handlers import persistent

from . import VERSION_TAG, i18n, ram, regions, status

#: 轮询间隔（秒）。缓存体积是大块跳变增长的，采得更密没有信息量。
TICK_SECONDS = 0.25

#: 起播后给播放头的宽限：断了音画同步的头几次采样还在原地不代表挂了。
START_GRACE_SECONDS = 2.0

#: 跑表上限的两截（秒）：固定宽限 + 每帧额度。这是**兜底**，真正的出路是
#: Esc——它永远有效。
GRACE_SECONDS = 30.0
SECONDS_PER_FRAME = 2.0

#: 构建不产生可撤销的用户改动，**故意不挂 UNDO**（ADR-0011 决定 8）——占了
#: 撤销栈会把用户上一个真编辑挤掉。
BL_OPTIONS = {"REGISTER"}


def sequence_area(context):
    """找 VSE 窗口 / 屏幕 / 区域；找不到就退回上下文里的那个。

    ``animation_play`` 拼上下文需要这三个东西：真机进 VSE 时都在，脚本直调
    （e2e 探针）常常缺，所以两边都得能活。
    """
    window = getattr(context, "window", None)
    if window is None:
        manager = getattr(context, "window_manager", None)
        windows = getattr(manager, "windows", None) or ()
        window = windows[0] if windows else None
    screen = getattr(window, "screen", None) if window is not None else None
    if screen is None:
        screen = getattr(context, "screen", None)
    fallback = getattr(context, "area", None)
    for candidate in getattr(screen, "areas", ()) or ():
        if getattr(candidate, "type", "") == "SEQUENCE_EDITOR":
            return window, screen, candidate
    return window, screen, fallback


def play_override(context):
    """给 ``animation_play`` 拼一个上下文：优先落在 VSE 区域上。

    场景必须是**序列场景**（ADR-0003），不能用 ``context.scene``（活动场景）：
    多场景工程里两者可以不是同一个，那样会把别的场景播一遍——播放头扫得
    欢天喜地，缓存里装的全是另一个时间线的画面。
    """
    window, screen, area = sequence_area(context)
    override = {}
    if window is not None:
        override["window"] = window
    if screen is not None:
        override["screen"] = screen
    if area is not None:
        override["area"] = area
        for region in getattr(area, "regions", ()) or ():
            override["region"] = region
            break
    scene = regions.displayed_scene(context) or getattr(context, "scene", None)
    if scene is not None:
        override["scene"] = scene
    return override


def toggle_playback(context):
    """起播 / 停播（同一个 ops，靠屏幕状态区分）；失败返回 False 且不抛。"""
    try:
        with bpy.context.temp_override(**play_override(context)):
            bpy.ops.screen.animation_play()
        return True
    except Exception as exc:
        status.trace("preview: animation_play failed %r" % (exc,))
        return False


def is_playing(context):
    """这个屏幕是否在播放中（读不到按"没在播"处理）。"""
    try:
        return bool(getattr(context.screen, "is_animation_playing", False))
    except Exception:
        return False


def _redraw(context):
    """让界面立刻重画（构建状态变了却看不见，最容易被当成没生效）。"""
    window = getattr(context, "window", None)
    screens = (getattr(window, "screen", None), getattr(context, "screen", None))
    for screen in screens:
        for area in getattr(screen, "areas", ()) or ():
            try:
                area.tag_redraw()
            except Exception:
                pass


class SEQVEL_OT_build_memory_preview(bpy.types.Operator):
    # 静态层常驻英中双语（ADR-0009 修订 R1）：bl_label/bl_description 注册期固定
    bl_idname = "seqvel.build_memory_preview"
    bl_label = "Build RAM Preview 构建内存预览"
    bl_description = ("Play the render range once so Blender's VSE final "
                      "cache holds it, then bring the playhead back to the "
                      "start of the range. Interaction is locked during the "
                      "build; Esc stops it and keeps what was built. "
                      "把渲染范围真的播一遍，装进 Blender 的 VSE final 缓存，"
                      "再把播放头送回范围开头。构建期间锁交互；Esc 中止，已构建"
                      "的部分保留")
    bl_options = BL_OPTIONS

    # ---------------------------------------------------------------- 进入
    def invoke(self, context, event):
        status.trace("preview.invoke building=%s" % ram.STATE["building"])
        try:
            return self._enter(context)
        except Exception:
            status.set_error(i18n.T(
                "preview.start_failed",
                traceback.format_exc().strip().splitlines()[-1]))
            status.trace("preview.invoke EXCEPTION\n" + traceback.format_exc())
            return {"CANCELLED"}

    def execute(self, context):
        """脚本 / e2e 直调（没有事件）走同一条路：构建仍然是 modal 收摊。"""
        try:
            return self._enter(context)
        except Exception:
            status.set_error(i18n.T(
                "preview.start_failed",
                traceback.format_exc().strip().splitlines()[-1]))
            return {"CANCELLED"}

    def _prepare(self, context):
        """读出这次构建的全部输入；返回 False 时 ``self._why`` 是拒绝原因。"""
        scene = regions.displayed_scene(context)
        if scene is None:
            self._why = "preview.no_scene"
            return False
        start, end, source = ram.resolve_range(scene)
        if start is None or end is None:
            self._why = "preview.no_range"
            return False
        editor = getattr(scene, "sequence_editor", None)
        if editor is None:
            self._why = "preview.no_editor"
            return False
        width, height = ram.resolution_of(scene)
        limit = ram.limit_of(getattr(context, "preferences", None))
        self.scene = scene
        self.editor = editor
        self.source = source
        self.start = start
        self.end = end
        self.limit_mb = limit
        self.estimate_mb = ram.estimate_mb(ram.frame_count(start, end),
                                           width, height)
        self.watch = ram.MemoryWatch(limit_mb=limit)
        self.peak = start          # 见过的最高播放头帧（起播位置）
        self.saved = {}
        self.timer = None
        self.handler = None
        self.done = False
        self.playing_seen = False
        return True

    def _enter(self, context):
        if not self._prepare(context):
            self.report({"ERROR"}, i18n.T(self._why, VERSION_TAG))
            return {"CANCELLED"}
        if ram.STATE["building"]:
            # 已在构建：不许叠第二层接管（第二层的还原会把第一层的设置写乱）。
            self.report({"ERROR"}, i18n.T("preview.already_building",
                                          VERSION_TAG))
            return {"CANCELLED"}

        # 每次 Shift+B 都是从零开始（用户明确要求）：旧成果连同 Blender 自己
        # 攒的 final 缓存一起清空（全局副作用，ADR-0011 决定 6 认领的代价）。
        ram.reset()
        self.saved = ram.capture_settings(self.editor, self.scene)
        # 从"接管用户设置"那一刻起就有得可丢了：往后任何一步抛异常都必须走
        # cleanup 把 sync_mode / use_audio 还原回去，否则用户会莫名其妙地
        # 断着音画同步、静着音留在那儿。
        try:
            try:
                self.scene.frame_current = self.start
            except Exception:
                pass
            ram.apply_build_settings(self.editor, self.scene, self.saved)
            ram.clear_final_cache(self.editor)
            self._warn_if_too_big()

            now = time.monotonic()
            self.started_at = now
            self.deadline = (now + GRACE_SECONDS
                             + ram.frame_count(self.start, self.end)
                             * SECONDS_PER_FRAME)
            ram.begin(self.start, self.end, self.source,
                      estimate_mb=self.estimate_mb, limit_mb=self.limit_mb,
                      scene_name=getattr(self.scene, "name", None))
            self._install_frame_handler()
            self.timer = context.window_manager.event_timer_add(
                TICK_SECONDS, window=getattr(context, "window", None))
            context.window_manager.modal_handler_add(self)
        except Exception as exc:
            status.trace("preview: setup failed, rolling back %r" % (exc,))
            self._cleanup(context, ram.OUTCOME_ABORTED, None)
            return {"CANCELLED"}
        if not toggle_playback(context):
            status.trace("preview: playback did not start")
            self._cleanup(context, ram.OUTCOME_ABORTED, None)
            return {"CANCELLED"}
        self.playing_seen = True
        status.set_hint(i18n.T("preview.building_hint"))
        _redraw(context)
        return {"RUNNING_MODAL"}

    def _warn_if_too_big(self):
        """按下的那一刻给一句预告：一眼看出这段大概率装不下。"""
        if self.estimate_mb and self.limit_mb \
                and self.estimate_mb > self.limit_mb:
            status.set_error(i18n.T("preview.will_not_fit",
                                    int(self.estimate_mb), self.limit_mb))
        else:
            status.clear_error()

    # ------------------------------------------------------------ 帧跟踪
    def _install_frame_handler(self):
        """装一次性的 ``frame_change_post``：它是构建水位的行踪记录仪。

        为什么不能只读瞬时帧：断了同步的播放可能在一个轮询间隔里扫完短范围
        并（有预览范围时）绕回去——那样"播放头走到末尾"那一瞬间根本不会被
        轮询到，构建就永远等不到完工。

        标 ``@persistent`` 是照 repo 的硬规矩走（Blender 载入文件会清空所有
        未标的 handler）：构建的寿命比一次 modal 短，摘的时候清单早换过了，
        挂下去时它已不在原列表里——这里的收益只是"不用多想"，不是救了某个
        真实故障。
        """
        self.handler = self._on_frame_change
        handlers = bpy.app.handlers.frame_change_post
        if self.handler not in handlers:
            handlers.append(self.handler)

    def _remove_frame_handler(self):
        if self.handler is None:
            return
        try:
            bpy.app.handlers.frame_change_post.remove(self.handler)
        except Exception:
            pass
        self.handler = None

    @persistent
    def _on_frame_change(self, scene, _depsgraph=None):
        """每一帧都留痕（app handler 里抛异常会污染整个 Blender）。

        只认我们自己那个场景：多场景工程里别的场景挪播放头也会走到这里，
        不校验的话构建水位会被别人的帧数带跑。
        """
        try:
            # 场景名在同一个文件里唯一；直接比对象不一定可靠（RNA 包装可能不同）。
            mine = getattr(self.scene, "name", None)
            theirs = getattr(scene, "name", None)
            if mine is not None and theirs is not None and mine != theirs:
                return
            frame = int(getattr(scene, "frame_current", 0) or 0)
            if frame > self.peak:
                self.peak = frame
        except Exception:
            pass

    # ---------------------------------------------------------------- 事件
    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            status.set_error(i18n.T(
                "preview.interrupted",
                traceback.format_exc().strip().splitlines()[-1]))
            status.trace("preview.modal EXCEPTION\n" + traceback.format_exc())
            self._cleanup(context, ram.OUTCOME_ABORTED, self.peak)
            return {"FINISHED"}

    def _modal(self, context, event):
        etype = getattr(event, "type", "")
        # 构建期间是 Vegas 语义：**吞掉一切**。与 ADR-0005 修订 R7 那次僵尸
        # 事故的区别是——这里的每条出口都一律 FINISHED，绝不放 PASS_THROUGH。
        if etype == "ESC" and getattr(event, "value", "") == "PRESS":
            status.trace("preview: esc cancel at f=%s" % self.peak)
            self._cleanup(context, ram.OUTCOME_STOPPED, self.peak)
            return {"FINISHED"}
        if etype != "TIMER":
            return {"RUNNING_MODAL"}

        playing = is_playing(context)
        if playing:
            self.playing_seen = True
        frame = int(getattr(self.scene, "frame_current", 0) or 0)
        if frame > self.peak:
            self.peak = frame
        cache_mb = int(getattr(self.editor, "cache_final_size", 0) or 0)
        ram.note_progress(self.peak, cache_mb)
        elapsed = time.monotonic() - self.started_at
        if elapsed > START_GRACE_SECONDS:
            if not self.playing_seen:
                status.trace("preview: playback never really started")
                self._cleanup(context, ram.OUTCOME_ABORTED, None)
                return {"FINISHED"}
            action = ram.decide(self.peak, self.end, cache_mb,
                                self.watch, playing)
            if action == ram.ACTION_DONE:
                self._cleanup(context, ram.OUTCOME_FINISHED, self.end)
                return {"FINISHED"}
            if action == ram.ACTION_FULL:
                status.trace("preview: cache full at f=%s cf=%sMB"
                             % (self.peak, cache_mb))
                self._cleanup(context, ram.OUTCOME_FULL, self.peak)
                return {"FINISHED"}
            if action == ram.ACTION_STOPPED:
                status.trace("preview: playback stopped at f=%s" % self.peak)
                self._cleanup(context, ram.OUTCOME_STOPPED, self.peak)
                return {"FINISHED"}
        if time.monotonic() > self.deadline:
            status.trace("preview: deadline hit at f=%s" % self.peak)
            self._cleanup(context, ram.OUTCOME_ABORTED, self.peak)
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        """Blender 单方面取消（切文件 / 注销插件等）：同样必须还原。"""
        self._cleanup(context, ram.OUTCOME_STOPPED, self.peak)

    # ---------------------------------------------------------------- 收摊
    def _cleanup(self, context, outcome, watermark):
        """唯一的收摊出口：停播 → 送回开头 → 还原设置 → 摘 handler / 计时器
        → **最后**才 ``ram.finish``（顺序即纪律，见本文件开头）。
        """
        if getattr(self, "done", True):
            return
        self.done = True
        scene = getattr(self, "scene", None)
        start = getattr(self, "start", None)
        if watermark is not None and start is not None \
                and watermark <= start and outcome != ram.OUTCOME_FINISHED:
            watermark = None  # 一步都没走动：别宣称"构建到第 N 帧"
        try:
            if is_playing(context):
                toggle_playback(context)
        except Exception:
            pass
        try:
            if scene is not None and start is not None:
                scene.frame_current = start
        except Exception:
            pass
        report = ram.restore_settings(self.editor, self.scene,
                                      getattr(self, "saved", {}))
        self._remove_frame_handler()
        if self.timer is not None and context is not None:
            try:
                context.window_manager.event_timer_remove(self.timer)
            except Exception:
                pass
            self.timer = None
        built = ram.finish(outcome, watermark)
        self._report(outcome, watermark, built, report)
        status.set_hint(status.hint_idle())
        _redraw(context)

    def _report(self, outcome, watermark, built, report):
        """把结果讲清楚：构建到哪一帧、为什么停、顺手打开了什么。"""
        try:
            if outcome == ram.OUTCOME_ABORTED:
                status.set_error(i18n.T("preview.aborted", VERSION_TAG))
                return
            if not built:
                return
            if report.get("final_left_on"):
                status.set_error(i18n.T("preview.final_left_on"))
            if outcome == ram.OUTCOME_FINISHED:
                self.report({"INFO"}, i18n.T("preview.built_full",
                                             watermark, VERSION_TAG))
            elif outcome == ram.OUTCOME_FULL:
                self.report({"WARNING"}, i18n.T("preview.built_part",
                                                watermark, self.limit_mb,
                                                VERSION_TAG))
            else:
                self.report({"INFO"}, i18n.T("preview.built_stopped",
                                             watermark, VERSION_TAG))
        except Exception:
            pass


_CLASSES = (SEQVEL_OT_build_memory_preview,)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
