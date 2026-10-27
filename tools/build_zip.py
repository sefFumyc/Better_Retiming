# -*- coding: utf-8 -*-
"""打包插件为可安装 zip（Blender > 编辑 > 偏好设置 > 插件 > 安装）。

用法::

    python tools/build_zip.py            # 产出 dist/seqvel-<版本>.zip
    python tools/build_zip.py --check    # 只校验 zip 内容与源码一致
"""

import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "seqvel"
DIST = os.path.join(ROOT, "dist")
SKIP_DIRS = {"__pycache__"}


def version():
    """从 __init__.py 抓版本号（不 import bpy）。"""
    path = os.path.join(ROOT, PACKAGE, "__init__.py")
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip().startswith('"version"'):
                numbers = line.split("(")[1].split(")")[0]
                return tuple(int(part.strip()) for part in numbers.split(","))
    raise RuntimeError("未找到 bl_info 版本号")


def sources():
    for base, dirs, files in os.walk(os.path.join(ROOT, PACKAGE)):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(base, name)
            yield full, os.path.join(PACKAGE, os.path.relpath(full, os.path.join(ROOT, PACKAGE)))


def build():
    tag = ".".join(str(part) for part in version())
    target = os.path.join(DIST, "%s-%s.zip" % (PACKAGE, tag))
    os.makedirs(DIST, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for full, arcname in sources():
            archive.write(full, arcname.replace(os.sep, "/"))
    print("已生成 %s" % target)
    return target


def check(target):
    with zipfile.ZipFile(target) as archive:
        packed = {name: archive.read(name) for name in archive.namelist()}
    bad = []
    for full, arcname in sources():
        key = arcname.replace(os.sep, "/")
        if key not in packed:
            bad.append("缺少 %s" % key)
            continue
        with open(full, "rb") as handle:
            if handle.read() != packed[key]:
                bad.append("内容不一致：%s" % key)
    if bad:
        print("校验失败：")
        for line in bad:
            print("  - %s" % line)
        return 1
    print("校验通过：zip 与源码一致（%d 个文件）" % len(packed))
    return 0


if __name__ == "__main__":
    built = build()
    sys.exit(check(built) if "--check" in sys.argv else 0)
