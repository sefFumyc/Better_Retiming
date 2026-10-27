**中文** | [English](README.md)

# Better Retiming — Vegas Pro 样式的 Velocity

![Vegas 速度线演示](docs/screenshot/1.png)

&#x20;在Blender视频序列编辑器（VSE）里使用像Vegas Pro一样的速度线，替代反直觉的Retiming。

纯 Python 插件，编辑结果写入 **Speed Control 效果条带** 的 `speed_factor` 关键帧，因此渲染、预览、
FFmpeg 导出走同一条原生管线，**不需要打开原生 Retiming 开关**。

原生的渲染、预览功能不受影响。

基于Blender \*\*5.2.1 \*\*编写和测试，不支持更低版本。

## 安装 / 更新

从 [Releases](https://github.com/sefFumyc/Better_Retiming/releases) 下载最新的
`seqvel-<版本>.zip`，然后：

Blender → 编辑 → 偏好设置 → 插件 → 安装 → 选该 zip → 勾选启用。

并重启Blender

## 使用

**没有"编辑模式"这回事**——任何时刻按住**看得见**的线就能拖

| 操作                    | 效果                                                 |
| --------------------- | -------------------------------------------------- |
| 按住某段线拖动               | 两个端点同步平移                                           |
| 在线段上双击                | 插入速度点                                              |
| 拖方块（速度点）              | 上下改速度、左右改时间                                        |
| **按住 Ctrl 拖动（任意目标）**  | **微调**：速度变化减半（光标挪 2px，点只挪 1px）；拖点时**冻结左右**（只能上下移动） |
| 右键按在方块上               | 弹出点菜单（目前只有「删除点」）                                   |
| 右键按在线上的某一段            | 弹出段菜单：线性 / 平滑 / 快速 / 慢速                            |
| **`Shift+B`**         | **内存预览**：把当前范围播一遍（禁用声音）并装进 VSE 缓存                  |
| Ctrl+Z / Ctrl+Shift+Z | 逐动作撤销/重做（拖一次、插一次、删一次各一步）                           |

### 内存预览（`Shift+B`）

像 Vegas 的 Build Dynamic RAM Preview：**按一下，把当前范围逐帧播一遍**，播完播放头
回到范围开头，之后你自己播这一段就是流畅的。

按快捷键P，鼠标左键框选范围。

默认情况下，Shift + B 会跟Blender的默认（放大）快捷键冲突，建议将Blender的行为改成Ctrl+B，避免冲突。

<br />

### 新建曲线的默认类型（面板下拉）

**默认 = 平滑**

想要直线倾斜就把下拉掰回「线性」）

如果已经修改过速度（SPD图层已创建），这个选项就没用了，需要先删除速度线再重新触发才会有效。

### 速度百分比什么时候出现

- **总是**→ 每个速度点旁常显百分比；
- **拖动时（默认）**→ 平时没有数字；**拖动时临时显示"本次正在改变的那些点"**

<br />

## 面板（别看，大部分都是debug内容）

侧边栏标签叫 **Velocity**（只留英文，Blender 不支持运行时改标签）。面板是唯一的
信息出口，**自上而下**：设置在前、诊断垫底——

1. **语言**：`English / 中文` 二选一，**出厂 English**。它存在插件偏好里（用户属性
   不是工程属性），跨 `.blend` 持久；切换即时生效，覆盖面板文案、右键菜单
   （含段菜单四档档名）、提示与报告（运行层）。**注册期固定**的文案
   （侧栏标签 `Velocity`、菜单标题、操作符与设置的悬停说明）Blender 不允许
   运行时切换，**常驻英中双语**；仅侧栏标签按用户要求只留英文。
2. **设置**：**显示**（编辑时隐藏原生 Retiming 显示、显示速度百分比）与
   **新建曲线**（默认段曲线类型、**显示范围上限** 300% / 1000% 二档）。
3. **目标条带**：活动条带（优先活动条带，其次选中的源条带）与删除按钮；没有可用
   目标时面板会说明原因（场景里有多少条带、选中几条、活动条带是谁），不会只显示
   「（无）」；读取条带列表/活动条带失败时会把异常原文写出来（不静默吞掉）。
   选中音频/效果条带等不可编辑的条带时，面板会直接写明原因。
4. **内存预览**：这一段的范围（含来源：预览范围 / 场景范围）、大概要占多少 MB 与
   当前上限、以及现在手上有什么（未构建 / 构建中百分比 / 已构建到第几帧与缓存体积 /
   内存已满 / 提前停止 / 已失效 / 中止）。
5. **状态警告**：编辑/音频提示、场景素材不匹配、子帧受限等。
6. **调试区（最下）**：编辑中常显 `时间轴区域 <尺寸> · 命中 <结果>`（用来一眼分清
   "根本没命中"还是"命中了却没动"）、承载条带现状、段类型分布、版本号。

承载绑定的输入是场景素材时，面板还会常显本场景 Raw 缓存状态：子帧被它吞掉时显示
「无级受限」并给一键开关（点了才改，可再点恢复）。

## 开发

分层（不要跨层绕行）：

```
seqvel/curve.py        纯 Python 影子模型（唯一事实源，零 bpy）
seqvel/geometry.py     条带坐标换算与命中检测（纯 Python）
seqvel/rules.py        条带类型规则（纯 Python）
seqvel/regions.py      定位时间轴区域（纯 Python）—— context.region 不可信
seqvel/editing.py      交互原语：双击检测、右键目标（纯 Python）
seqvel/ram.py         内存预览的规则层（范围 / 内存账 / 水位判定，零 bpy）
seqvel/store.py        模型缓存与持久化（条带自定义属性）
seqvel/carrier.py      落地映射：Speed Control + speed_factor 关键帧
seqvel/watch.py        条带区间监听：末端跟随 / 吞点、承载重落地（映射表重建）、内存预览失效（ADR-0004 修订 R1）
seqvel/preview.py      内存预览的构建 operator（Shift+B，ADR-0011）
seqvel/overlay.py      绘制 overlay（draw handler + gpu/blf）
seqvel/hover.py        悬停光标（MOUSEMOVE 键位 → HAND_POINT）
seqvel/interaction.py  直接操纵的 modal 操作与点/段菜单
seqvel/status.py       运行状态（面板文案与诊断的单一出口）
seqvel/ui.py           侧边栏面板与设置
```

`curve` / `geometry` / `rules` / `regions` / `editing` / `ram` 是零 bpy 层，`store` / `watch`
自带 `try: import bpy` 兜底——所以「快跑」那条命令能直接把这几层跑起来。对应的硬约束是
`seqvel/__init__.py` **模块级不得裸碰 bpy**（连装饰器也不行），否则纯 Python 下连
`import seqvel` 都过不去；挂 app handler 的 `persistent` 统一在 `register()` 期套。

<br />

## 文档

- `CONTEXT.md` —— 领域术语表（速度线 / 速度点 / 段 / 影子模型 / 承载条带 …）
- `docs/adr/` —— 关键决策记录（11 篇）
- `research/` —— 上游数据模型、源图缓存、绘制交互等调研笔记

## 许可证

GPL-2.0-or-later，见 [LICENSE](LICENSE)。
