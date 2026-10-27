# -*- coding: utf-8 -*-
"""取证工具：场景源条带在 VSE 渲染时被以什么时间求值（是否带子帧）。

不看画面（本机渲染输出不可靠），看的是数据：源场景里放一个带 Python 驱动器的
物体，驱动器把收到的 ``frame`` 记下来——主场景渲染（源条带 + Speed Control 承载）
时只要源场景真被求值，驱动器就会触发。**取帧小数若被传到，记录就带小数；
若 Raw 缓存把时间取整后命中缓存，源场景根本不会被再次求值——记录骤减。**

背景（上游缺陷，见 ``research/source-image-cache-subframe.md`` 与 ADR-0002）：
``cache/source_image_cache.cc`` 的 get/put 先 ``math::round(timeline_frame)`` 再建键，
Speed Control 恰好把小数源位置放在 ``timeline_frame`` 里 → 同一整数源帧内的输出帧
撞同一个键 → 掉帧。原生 Retiming 的小数产生在 ``give_frame_index``（取整之后），
所以它不中招。

用法（在 Blender 里跑）::

    blender --factory-startup --background --python tools/probe_scene_strip_time.py -- \
        [--cache on|off|both] [--frames 10]

`--cache both`（默认）会各跑一遍：off 应逐帧命中 ``1.0 / 1.25 / …`` 的期望序列
（真·无级）；on 预期求值次数骤减（掉帧复现）。退出码：0 = 与预期一致，
1 = 与预期不符（要么缺陷被上游修了、要么场景行为变了——都值得重看结论）。
"""

import os
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from seqvel import carrier  # noqa: E402
from seqvel.curve import Curve  # noqa: E402


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    cache = "both"
    frames = 10
    if "--cache" in argv:
        cache = argv[argv.index("--cache") + 1]
    if "--frames" in argv:
        frames = int(argv[argv.index("--frames") + 1])
    if cache not in ("on", "off", "both"):
        raise SystemExit("--cache 只接受 on / off / both")
    return cache, frames


def log(*parts):
    print("[probe]", *parts, flush=True)


logged = []


def seqprobe(frame):
    logged.append(round(float(frame), 4))
    return 0.0


def build(cache_on, frame_count):
    """搭场景：源场景（驱动器当记录仪）+ 主场景（场景条带 + 0.25x 承载）。"""
    bpy.app.driver_namespace["seqprobe"] = seqprobe

    src = bpy.data.scenes.new("SRC")
    probe = bpy.data.objects.new("Probe", None)
    src.collection.objects.link(probe)
    driver = probe.driver_add("location", 0).driver
    driver.expression = "seqprobe(frame)"

    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("C"))
    src.collection.objects.link(cam)
    src.camera = cam
    src.frame_start, src.frame_end = 1, 60
    src.render.engine = "CYCLES"
    src.cycles.samples = 1
    src.render.resolution_percentage = 10

    master = bpy.data.scenes["Scene"]
    bpy.context.window.scene = master
    editor = master.sequence_editor_create()
    editor.use_cache_raw = cache_on
    editor.use_cache_final = cache_on
    editor.use_prefetch = cache_on

    master.render.engine = "CYCLES"
    master.cycles.samples = 1
    master.render.resolution_percentage = 10
    master.render.use_sequencer = True
    master.render.filepath = os.path.join(ROOT, ".scratch", "_probe", "probe_")
    master.frame_start, master.frame_end = 1, frame_count

    strip = editor.strips.new_scene(name="SRC_STRIP", scene=src,
                                    channel=1, frame_start=1)
    strip.frame_final_duration = 30
    diag = carrier.write_curve(strip, Curve([(0.0, 0.25), (29.0, 0.25)], 30.0))
    if diag["errors"]:
        raise RuntimeError("承载落地失败：%s" % diag["errors"])
    car = carrier.carrier_of(strip)
    log("承载条带：%s mode=%s interp=%s keys=%s"
        % (car.name, car.speed_control, car.use_frame_interpolate,
           len(carrier.read_keys(car) or [])))
    log("缓存状态：use_cache_raw=%r use_cache_final=%r use_prefetch=%r"
        % (editor.use_cache_raw, editor.use_cache_final, editor.use_prefetch))
    return master


def run(cache_on, frame_count):
    del logged[:]
    build(cache_on, frame_count)
    result = bpy.ops.render.render(animation=True)
    log("cache=%s render 返回 = %r" % ("on" if cache_on else "off", result))
    seq = []
    for value in logged:
        if not seq or seq[-1] != value:
            seq.append(value)
    expected = [1.0 + 0.25 * i for i in range(frame_count)]
    log("SRC 被求值的 frame 序列（去重相邻）：", seq)
    log("期望（0.25x 慢放，逐帧 +0.25）：", expected)
    if cache_on:
        # 缓存键是 round(源位置)：每个整数桶只渲染一次，且渲染时刻是"第一个
        # 撞进这个桶的小数时间"——桶里其余输出帧全部复用那张图 = 掉帧。
        # 只要求值次数 < 输出帧数，就说明小数在键处丢了；取整方式若被上游改成
        # floor 等变体，本判定照样成立（具体取整方式看上面打印的序列）。
        ok = len(seq) < frame_count
        log("结论：%s（%d 个输出帧只求值 %d 次；>= %d 才算缺陷未复现）"
            % ("复现：子帧在缓存键处被丢弃" if ok else "缺陷未复现（上游可能已修）",
               frame_count, len(seq), frame_count))
    else:
        ok = seq == expected
        log("结论：%s（关缓存应逐帧命中期望 = 无级）"
            % ("符合无级" if ok else "与期望不符"))
    return ok


def main():
    cache, frame_count = parse_args()
    cases = {"on": [True], "off": [False], "both": [False, True]}[cache]
    results = [run(cache_on, frame_count) for cache_on in cases]
    print("[probe] done %s" % ("OK" if all(results) else "MISMATCH"), flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
