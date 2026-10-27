# -*- coding: utf-8 -*-
"""Vegas 速度线 —— 用"速度线"编辑 VSE 条带的变速，结果走原生管线。

模块分层（不要跨层绕行）::

    curve      纯 Python 影子模型（唯一事实源，零 bpy）
    geometry   条带坐标换算与命中检测（纯 Python）
    rules      条带类型规则（纯 Python）
    regions    定位时间轴区域（纯 Python）—— ``context.region`` 不可信
    store      模型的内存缓存与持久化（条带自定义属性）
    carrier    落地映射：Speed Control 效果条带 + speed_factor 关键帧
    watch      条带端点监听：右端收短 → 末点跟随/吞点（ADR-0004）
    preview      内存预览（ADR-0011）：Shift+B 把范围"真的播一遍"装进 VSE 缓存
    overlay    绘制 overlay（draw handler + gpu/blf）
    interaction  直接操纵交互（ADR-0005）：按下即命中，命中才起短 modal
    hover      悬停光标（ADR-0005 修订 R3）：MOUSEMOVE 键位命中 → HAND_POINT
    ui         侧边栏面板与设置

承载机制见 ``docs`` 与调研笔记：影子模型单向写入 ``SpeedControlStrip`` 的
``speed_factor`` 关键帧（MULTIPLY 模式），预览 = 渲染 = FFmpeg 导出同一条
``seq_render`` 管线，因此不需要打开原生 Retiming 开关。

**模块级不得裸碰 ``bpy``**（票 04）：纯 Python 环境（单测、``tools/build_zip.py``）
下 ``bpy`` 是 ``None``，而测试全都写 ``from seqvel.xxx import ...`` —— 那会先执行
本文件，模块级任何未兜底的 ``bpy.``（**包括装饰器**）都会让导入期直接崩。
``store`` / ``watch`` 用自带的 ``try: import bpy`` + 空壳 ``persistent`` 在模块级
挂装饰；本文件的 handler 装饰推到 ``register()`` 里做
（``_install_load_post_handler``），所以不需要那层空壳。
"""

try:
    import bpy
except ImportError:  # 纯 Python 环境（单测）下只暴露子模块，不注册插件
    bpy = None

from . import i18n  # 纯 Python 文案表（运行层双语，ADR-0009）
from . import status  # 零 bpy 层，模块级导入安全；键位挂载的诊断出口

# 静态层注册元数据：bl_info.description 用户可见 → 常驻英中双语（ADR-0009 修订 R1）
bl_info = {
    "name": "Vegas Velocity Line",
    "author": "blendvelo",
    "version": (1, 6, 6),
    "blender": (5, 2, 0),
    "location": "Video Sequence Editor > Sidebar (N) > Velocity",
    "description": ("Vegas-style velocity line: drag to edit the speed curve; "
                    "the result is written to a Speed Control effect strip and "
                    "follows the native render/export pipeline. "
                    "Vegas 式速度线：拖动即可编辑速度曲线，结果写入 Speed "
                    "Control 效果条带，走原生渲染/导出管线"),
    "category": "Sequencer",
}

#: 版本标签。**刻意**出现在面板与所有拒绝文案里：覆盖安装插件后不重启
#: Blender，跑的还是内存里的旧模块（Python 不会因为磁盘文件变了就重新导入），
#: 于是"明明修好了却依然报同样的错"。有了这个标签，一眼就能判断当前跑的
#: 是哪一版——文案里没写 v1.0.5，就说明内存里是旧代码，需要重启 Blender。
VERSION_TAG = "v%d.%d.%d" % tuple(bl_info["version"])

#: 直接操纵键位（Sequencer，ADR-0005 修订 R2/R3/R9）。
#: - 没有键盘删点键位——菜单打开期间键位表停摆，删点统一走菜单项点击；
#: - hover_cursor 绑 MOUSEMOVE：Window 没有 mouse_x 可轮询（5.2.1 RNA
#:   实测），悬停只能靠事件驱动；
#: - 想加 TWEAK 兜底（原生选中抢走 PRESS 时拖动仍可进）不可行：5.2.1 的
#:   ``keymap_items.new`` 拒绝 TWEAK_L/EVT_TWEAK_L（枚举里没有，实测）；
#: - [修订 R9] Ctrl 微调需要**显式的 ctrl=True 变体**：键位表对修饰键有
#:   匹配优先级，Ctrl+按下会被原生同键项截走，没有变体微调就进不来。
KEYMAP_ITEMS = (
    ("seqvel.edit_speedline", "LEFTMOUSE", "PRESS", {}),
    ("seqvel.edit_speedline", "LEFTMOUSE", "PRESS", {"ctrl": True}),
    ("seqvel.point_menu", "RIGHTMOUSE", "PRESS", {}),
    ("seqvel.hover_cursor", "MOUSEMOVE", "ANY", {}),
    # [ADR-0011] Shift+B = Vegas 式内存预览。只挂编辑器键位表：工具键位表
    # 只对 edit_speedline 开放（下面按 idname 显式过滤），构建不需要参与工
    # 具模态——那里接管的话，timeline 上的每一次普通点击都要先过一遍它。
    ("seqvel.build_memory_preview", "B", "PRESS", {"shift": True}),
)
#: [修订 R8/R9] 工具键位表只挂左键编辑项（普通 + Ctrl 微调两条都要）。
#: 按 idname 显式选取而非切片：KEYMAP_ITEMS 重排时工具表不会静默跟着变。
TOOL_KEYMAP_ITEMS = tuple(item for item in KEYMAP_ITEMS
                          if item[0] == "seqvel.edit_speedline")
#: 键位表名
KEYMAP_NAME = "Sequencer"

#: [修订 R8] 工具键位表。真机 e2e 实锤（v1.2.6 排障）：**活动工具的键位表
#: 先于编辑器键位表求值**——"Select Box" 工具下按在**已选中条带**上时，
#: 原生 ``sequencer.select`` 以模态启动（内部等"点击 vs 框选"判定，
#: wait_to_deselect_others=1），把 PRESS 整个吃掉，我们的项连
#: "Sequencer" 表头都抢不过。解法：把编辑项同时插进工具键位表头——
#: press 永远先过我们：命中→拖线；未命中→PASS_THROUGH→原生照常。
TOOL_KEYMAP_NAMES = (
    "Sequencer Tool: Select Box",
    "Sequencer Tool: Select Box (fallback)",
)

_keymap = None  # [(keymap, items), ...]


def _purge_keymap_items(keymap):
    """删掉键位表里所有 seqvel.* 项（自愈：重复注册/上次失败不累积）。"""
    stale = [item for item in keymap.keymap_items
             if (item.idname or "").startswith("seqvel.")]
    for item in stale:
        try:
            keymap.keymap_items.remove(item)
        except Exception:
            pass


def _register_keymap():
    """挂直接操纵的键位（插到表头，压过原生同键）。

    两层挂载（修订 R8）：
    - 编辑器键位表 "Sequencer"：全部三项（拖线/点菜单/悬停）；
    - 工具键位表（TOOL_KEYMAP_NAMES）：只有左键编辑项——工具键位表
      先于编辑器键位表求值，原生 select 在"按在已选中条带上"时从那里
      以模态吃掉 PRESS（真机 e2e 实锤），不挂这里就抢不过。

    逐项容错：一项失败不许殃及其余。健康位写进面板（"键位 n/m"）：
    **注册流程整体挂掉也不能静默**。
    """
    global _keymap
    try:
        addons = bpy.context.window_manager.keyconfigs.addon
        if addons is None:
            status.note_keymap(i18n.T("ui.keymap_no_addon"))
            status.trace("keymap: keyconfigs.addon is None")
            return
        # keymaps.new 对同名同空间的键位表是"取既有"而非新建（实测），
        # 所以先清残留再挂新，天然幂等。
        plan = [(KEYMAP_NAME, KEYMAP_ITEMS)]
        for name in TOOL_KEYMAP_NAMES:
            plan.append((name, TOOL_KEYMAP_ITEMS))  # 工具表只挂左键编辑项（普通+Ctrl，R9）
        managed = []
        items_ok = []
        total = 0
        for km_name, defs in plan:
            try:
                keymap = addons.keymaps.new(name=km_name,
                                            space_type="SEQUENCE_EDITOR",
                                            region_type="WINDOW")
            except Exception as exc:
                status.trace("keymap: %s 取不到：%r" % (km_name, exc))
                total += len(defs)
                continue
            _purge_keymap_items(keymap)
            items = []
            for idname, key, value, props in defs:
                total += 1
                try:
                    # head=True：插到键位表头部。键位表按顺序取第一个
                    # 匹配项，排我们后面的原生同键项才抢不过我们。
                    items.append(keymap.keymap_items.new(idname, key, value,
                                                         head=True, **props))
                    short = "tool:%s" % idname.split(".")[-1] \
                        if km_name != KEYMAP_NAME else idname.split(".")[-1]
                    items_ok.append(short)
                    status.trace("keymap: [%s] +%s(%s/%s)"
                                 % (km_name, idname, key, value))
                except Exception as exc:
                    status.set_error(i18n.T("ui.keymap_mount_item",
                                            idname, key, km_name, exc))
                    status.trace("keymap: FAIL [%s] %s: %r"
                                 % (km_name, idname, exc))
            managed.append((keymap, items))
        _keymap = managed
        summary = "%d/%d (%s)" % (len(items_ok), total,
                                  "·".join(items_ok)
                                  if items_ok else i18n.T("ui.keymap_none"))
        status.note_keymap(summary)
        status.trace("keymap: %s" % summary)
    except Exception as exc:
        _keymap = None
        summary = i18n.T("ui.keymap_mount_failed")
        status.note_keymap(summary)
        status.set_error("%s: %r" % (summary, exc))
        status.trace("keymap: EXCEPTION %r" % (exc,))


def _reregister_keymap_on_load(_scene, _depsgraph=None):
    """文件加载后自愈一次键位挂载（R8）。

    工具键位表由工作区/工具系统按需创建，插件注册时可能还不存在——
    这时挂载会缺一环。加载后重跑一遍（purge 前置，天然幂等）。
    """
    _register_keymap()


def _install_load_post_handler():
    """把自愈处理器挂上 ``load_post``，并**在此刻**套上 ``persistent``。

    装饰刻意不在 ``def`` 上做：``@bpy.app.handlers.persistent`` 一挂到模块级，
    ``import seqvel`` 就需要 ``bpy``，纯 Python 下导入期直接崩（票 04）。
    ``persistent`` 只是给函数打一个标记属性并**原样返回同一个对象**（5.2.1
    实测），所以"append 之前套"与"def 上套"完全等价——真机行为不变。

    ``persistent`` 本身不可省：Blender 载入新文件时会清空所有**未标记**的 app
    handler，不标的话自愈就只剩"注册那一次"，R8 在打开工程后失效。
    """
    handler = bpy.app.handlers.persistent(_reregister_keymap_on_load)
    if handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(handler)


def _remove_load_post_handler():
    """摘掉自愈处理器；本来就没挂时静默（注销要处处可重入）。"""
    try:
        bpy.app.handlers.load_post.remove(_reregister_keymap_on_load)
    except ValueError:
        pass


def _unregister_keymap():
    """摘掉我们挂的键位项；再按自愈逻辑扫一遍（不依赖注册时的记录）。"""
    global _keymap
    managed = _keymap or []
    _keymap = None
    try:
        addons = bpy.context.window_manager.keyconfigs.addon
        if addons is None:
            return
        for keymap, _items in managed:
            _purge_keymap_items(keymap)
            try:
                addons.keymaps.remove(keymap)
            except Exception:
                pass  # 里面可能还有别的插件的项；空壳留着无害
        # 自愈兜底：不管注册时记录了什么，把所有托管表名扫一遍
        for name in (KEYMAP_NAME,) + TOOL_KEYMAP_NAMES:
            keymap = addons.keymaps.get(name)
            if keymap is not None:
                _purge_keymap_items(keymap)
    except Exception:
        pass


def register():
    from . import (carrier, hover, interaction, overlay, preview, status, store,
                   ui, watch)  # noqa: F401

    status.trace("=== seqvel register %s ===" % VERSION_TAG)
    store.register_handlers()
    watch.register_handlers()
    ui.register()
    interaction.register()
    overlay.register()
    hover.register()
    preview.register()
    _register_keymap()
    _install_load_post_handler()


def unregister():
    from . import hover, interaction, overlay, preview, status, store, ui, watch

    _remove_load_post_handler()
    _unregister_keymap()
    preview.unregister()
    hover.unregister()
    overlay.unregister()
    interaction.unregister()
    ui.unregister()
    watch.unregister_handlers()
    store.unregister_handlers()
    status.reset()
