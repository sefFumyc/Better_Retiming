# -*- coding: utf-8 -*-
"""[DEBUG-cr1] 播放闪退复现回路（真实 GUI，自动运行，不注入输入）。

用法（不要 --factory-startup：需要用户已启用 seqvel 插件，贴近真机）::

    blender.exe "C:/Users/sefFumyc/Desktop/5.blend" --python tools/crash_repro.py -- \
        --report %TEMP%/seqvel_crash_repro.txt --duration 90

用户报障场景：编辑会话已按 Esc 退出（条带仍选中 → overlay 播放时仍每帧画线），
播放几次就崩。本脚本复刻：选目标条带 → 循环「播放 6s / 停 1s」直到超时。

可选开关：
- ``--session``        额外进入 seqvel 编辑会话（不退出）再播放；
- ``--noedit``         跳过脚本化拖线落盘（只选中条带就播）；
- 环境变量 SEQVEL_REPRO_NO_OVERLAY=1
                       注销 overlay 绘制回调（差分：崩的是不是绘制路径）。

判定：报告末尾 ``RESULT: NO_CRASH`` → 绿；进程中途死亡 → 红。
GUI 进程 stdout 不接控制台，以报告文件与进程退出码为准。
"""

import os
import sys
import tempfile
import time

import bpy

REPORT = os.path.join(tempfile.gettempdir(), "seqvel_crash_repro.txt")
DURATION = 90.0
WITH_SESSION = False
PLAY_WINDOW = 6.0
PAUSE = 1.0
if "--report" in sys.argv:
    REPORT = sys.argv[sys.argv.index("--report") + 1]
if "--duration" in sys.argv:
    DURATION = float(sys.argv[sys.argv.index("--duration") + 1])
if "--session" in sys.argv:
    WITH_SESSION = True
NO_EDIT = "--noedit" in sys.argv

_T0 = time.time()
_DEADLINE = _T0 + DURATION
_STAGE = {"step": 0, "cycle": 0, "phase": None, "phase_t0": 0.0,
          "last_beat": 0.0}


def log(line):
    stamp = "%.1fs" % (time.time() - _T0)
    text = "[%s] %s" % (stamp, line)
    print("[crash_repro] %s" % text, flush=True)
    try:
        with open(REPORT, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except Exception:
        pass


def _find_area(kind):
    for area in bpy.context.screen.areas:
        if area.type == kind:
            return area
    return None


def _quit(code):
    log("RESULT: %s" % ("NO_CRASH" if code == 0 else "STOPPED(code=%d)" % code))
    try:
        bpy.ops.wm.quit_blender()
    except Exception:
        pass
    os._exit(code)


def _do_scripted_edit(scene, area, region):
    """复刻用户残留状态：进会话 → 拖线到 1.5x → 松手落盘 → 收尾（等效 Esc 后）。

    用 gui_smoke 的"借用操作符方法 + 替身 window_manager"路线：不需要真 modal
    （用户崩溃前 modal 已退出），但 commit 链路（STORE.commit + write_curve +
    retiming 显示）走的是真代码。
    """
    import types

    from seqvel import carrier, geometry, interaction, regions, status, store

    context = types.SimpleNamespace(
        scene=scene, region=region, area=area,
        window_manager=types.SimpleNamespace(modal_handler_add=lambda op: None))

    class _Event(object):
        def __init__(self, kind, x=0.0, y=0.0, value="PRESS"):
            self.type = kind
            self.value = value
            self.mouse_region_x = float(x)
            self.mouse_region_y = float(y)
            self.mouse_x = float(region.x + x)
            self.mouse_y = float(region.y + y)

    class _Op(object):
        pass

    for name in ("invoke", "execute", "_start", "modal", "_modal", "cancel",
                 "_begin_drag", "_end_action", "_apply_drag",
                 "_release_without_drag", "_insert_point", "_commit",
                 "_finish", "_cleanup", "_view2d", "_base_frame", "_channel",
                 "_mouse"):
        setattr(_Op, name, getattr(interaction.SEQVEL_OT_edit_speedline, name))
    op = _Op()
    op.report = lambda *args, **kwargs: None

    # 直接操纵（ADR-0005）：按下必须带真实鼠标位置，且要按在速度线上才起拖
    editor = scene.sequence_editor
    strip0 = next((s for s in editor.strips if getattr(s, "select", False)), None)
    if strip0 is None:
        log("edit: 没有选中的目标条带")
        return False
    view2d0 = regions.view2d(context)
    if view2d0 is None:
        log("edit: 拿不到时间轴 view2d")
        return False
    base0 = float(store.strip_base_frame(strip0))
    chan0 = float(store.strip_channel(strip0))
    dur0 = float(strip0.right_handle - strip0.left_handle)
    click = geometry.point_xy(view2d0, base0, chan0, min(50.0, dur0 * 0.5), 1.0)

    result = op.invoke(context, _Event("LEFTMOUSE", *click))
    if result != {"RUNNING_MODAL"}:
        log("edit: invoke 未进入拖动 %r" % (result,))
        return False
    strip = op.strip
    view2d = op._view2d(context)
    base = float(strip.left_handle)
    channel = float(strip.channel)
    duration = float(strip.right_handle - strip.left_handle)

    def px(frame, speed):
        return geometry.point_xy(view2d, base, channel, frame, speed)

    target = px(min(50.0, duration * 0.5), 1.5)
    op.modal(context, _Event("LEFTMOUSE", *click))
    op.modal(context, _Event("MOUSEMOVE", *target, value="NOTHING"))
    op.modal(context, _Event("LEFTMOUSE", *target, value="RELEASE"))
    op._cleanup(context)
    log("edit: 已拖到 1.5x 并落盘 strip=%r stored=%r"
        % (strip.name, bool(strip.get(store.PROP_KEY))))
    return True


def _setup():
    """阶段 1：定位区域/目标 → 选条带 → 拖线落盘 → （可选）进会话。"""
    import seqvel

    log("blender=%s seqvel=%s background=%s no_overlay=%s"
        % (bpy.app.version_string, getattr(seqvel, "VERSION_TAG", "?"),
           bpy.app.background, os.environ.get("SEQVEL_REPRO_NO_OVERLAY")))

    if os.environ.get("SEQVEL_REPRO_NO_OVERLAY") == "1":
        from seqvel import overlay
        overlay.unregister()
        log("overlay 绘制回调已注销（差分模式）")

    area = _find_area("SEQUENCE_EDITOR")
    region = next((r for r in area.regions if r.type == "WINDOW"), None) \
        if area else None
    if area is None or region is None:
        log("FAIL: 找不到 VSE 区域 %r/%r" % (area, region))
        _quit(1)
        return False

    scene = bpy.context.sequencer_scene or bpy.context.scene
    editor = scene.sequence_editor
    log("序列场景=%r 活动场景=%r" % (scene.name, bpy.context.scene.name))
    if editor is None:
        log("FAIL: 序列场景没有 sequence_editor")
        _quit(1)
        return False

    from seqvel import carrier
    target, notes = carrier.scan_targets(bpy.context)
    if target is None:
        log("FAIL: 没有可编辑目标 %r" % (notes,))
        _quit(1)
        return False
    log("目标条带=%r 类型=%s" % (target.name, target.type))

    for s in editor.strips_all:
        try:
            s.select = False
        except Exception:
            pass
    target.select = True
    editor.active_strip = target

    if not NO_EDIT:
        try:
            if not _do_scripted_edit(scene, area, region):
                log("edit: 脚本化编辑未完成（继续播放）")
        except Exception as exc:
            import traceback

            log("edit: 异常 %s" % traceback.format_exc())
            _quit(1)
            return False

    if WITH_SESSION:
        window = bpy.context.window
        with bpy.context.temp_override(window=window, screen=bpy.context.screen,
                                       area=area, region=region):
            try:
                result = bpy.ops.seqvel.edit_speedline()
                log("edit_speedline -> %r" % (result,))
            except Exception as exc:
                log("FAIL: edit_speedline 异常 %r" % (exc,))
                _quit(1)
                return False
    return True


def _toggle_play():
    window = bpy.context.window
    screen = bpy.context.screen
    try:
        with bpy.context.temp_override(window=window, screen=screen):
            bpy.ops.screen.animation_play()
    except Exception as exc:
        log("播放开关异常 %r" % (exc,))
        return False
    return True


def tick():
    """定时器：播放/暂停循环 + 心跳。"""
    step = _STAGE["step"]
    now = time.time()
    if step == 0:
        if not _setup():
            return None  # 已退出
        _STAGE["step"] = 1
        _STAGE["phase"] = "play"
        _STAGE["phase_t0"] = now
        _toggle_play()
        log("第 1 轮播放开始（循环：播 %.0fs / 停 %.0fs，限时 %.0fs）"
            % (PLAY_WINDOW, PAUSE, DURATION))
        return 0.5

    if now >= _DEADLINE:
        if _STAGE["phase"] == "play":
            _toggle_play()
        log("正常到点：共 %d 轮播放，未崩溃" % _STAGE["cycle"])
        _quit(0)
        return None

    if now - _STAGE["last_beat"] >= 2.0:
        _STAGE["last_beat"] = now
        try:
            playing = bpy.context.screen.is_animation_playing
        except Exception:
            playing = "?"
        log("heartbeat frame=%s playing=%s"
            % (bpy.context.scene.frame_current, playing))

    phase_t0 = _STAGE["phase_t0"]
    if _STAGE["phase"] == "play" and now - phase_t0 >= PLAY_WINDOW:
        _toggle_play()
        _STAGE["phase"] = "pause"
        _STAGE["phase_t0"] = now
        return 0.5
    if _STAGE["phase"] == "pause" and now - phase_t0 >= PAUSE:
        _STAGE["cycle"] += 1
        _STAGE["phase"] = "play"
        _STAGE["phase_t0"] = now
        _toggle_play()
        return 0.5
    return 0.5


bpy.app.timers.register(tick, first_interval=2.0)
