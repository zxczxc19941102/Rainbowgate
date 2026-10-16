# -*- coding: utf-8 -*-
"""一键打包 EXE（PyInstaller + UPX 压缩）

产物（dist/ 目录）：
    RainbowGateCheckin.exe   图形界面版（无控制台窗口，双击即用）
    RainbowGateDaemon.exe    自动签到守护进程（控制台版）
    RainbowGateCli.exe       命令行版（--status / --force 等）

用法：
    .venv\\Scripts\\python.exe build_exe.py           # 默认启用 UPX 压缩
    .venv\\Scripts\\python.exe build_exe.py --noupx   # 不使用 UPX

说明：
    * EXE 运行时的 config.json（账号/状态/会话）与 checkin.log
      都保存在 EXE 所在目录（checkin_core._resolve_base_dir 已做处理）。
    * 打包完成后会把当前目录的 config.json 复制到 dist/，方便直接运行。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import checkin_core

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"
BUILD_DIR = BASE_DIR / "build"

TARGETS = [
    {"script": "checkin_gui.py", "name": "RainbowGateCheckin", "windowed": True},
    {"script": "checkin_daemon.py", "name": "RainbowGateDaemon", "windowed": False},
    {"script": "checkin_core.py", "name": "RainbowGateCli", "windowed": False},
]


def find_upx() -> str | None:
    """返回 upx 可执行文件所在目录，找不到时返回 None。"""
    exe = shutil.which("upx")
    return str(Path(exe).resolve().parent) if exe else None


def human_size(num_bytes: int) -> str:
    """把字节数格式化成易读文本。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def build_target(target: dict, upx_dir: str | None, use_upx: bool) -> Path:
    """用 PyInstaller 打包单个目标，返回生成的 EXE 路径。"""
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        target["name"],
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
        "--specpath",
        str(BUILD_DIR),
    ]
    if target["windowed"]:
        cmd.append("--windowed")
    else:
        cmd += ["--console", "--exclude-module", "tkinter", "--exclude-module", "_tkinter"]

    if use_upx and upx_dir:
        cmd += ["--upx-dir", upx_dir]
    else:
        cmd.append("--noupx")

    cmd.append(str(BASE_DIR / target["script"]))

    print(f"\n[打包] {target['name']}  <-  {target['script']}")
    subprocess.run(cmd, check=True, cwd=BASE_DIR)

    exe_path = DIST_DIR / f"{target['name']}.exe"
    if not exe_path.exists():
        raise FileNotFoundError(f"未生成 {exe_path}")
    return exe_path


def upx_final(exe_path: Path, upx_exe: str) -> str:
    """对最终 EXE 再做一次 UPX 压缩，返回结果说明。

    PyInstaller 的引导程序（bootloader）带 CFG 保护，UPX 默认拒绝压缩这类 PE
    （CantPackException: GUARD_CF）。此时保留 PyInstaller 原生体积即可——内层
    的 DLL / pyd 已由 PyInstaller 调用 UPX 压缩过了。
    """
    result = subprocess.run(
        [upx_exe, "--best", "--quiet", str(exe_path)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode == 0:
        return "外层已压缩"
    if "GUARD_CF" in (result.stderr or "") or "GUARD_CF" in (result.stdout or ""):
        return "外层跳过（引导程序带 CFG 保护，内层已压缩）"
    return f"外层跳过（{result.returncode}）"


def main(argv: list[str] | None = None) -> int:
    """构建入口。"""
    checkin_core.setup_console_utf8()

    parser = argparse.ArgumentParser(description="打包 Rainbow Gate 签到工具为 EXE")
    parser.add_argument("--noupx", action="store_true", help="不使用 UPX 压缩")
    parser.add_argument("--no-final-upx", action="store_true", help="不用 UPX 压缩最终 EXE 外层")
    parser.add_argument(
        "--only",
        nargs="+",
        choices=[t["name"] for t in TARGETS],
        metavar="NAME",
        help="只构建指定目标（可多个），默认构建全部",
    )
    args = parser.parse_args(argv)

    targets = [t for t in TARGETS if not args.only or t["name"] in args.only]

    upx_dir = find_upx()
    use_upx = not args.noupx and upx_dir is not None

    print("=" * 56)
    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"UPX         : {'已找到 -> ' + upx_dir if upx_dir else '未找到（本次不压缩）'}")
    print(f"输出目录    : {DIST_DIR}")
    print(f"本次目标    : {', '.join(t['name'] for t in targets)}")
    print("=" * 56)

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    # 只清理待生成的 EXE，保留 dist 里已有的 config.json / checkin.log 等
    # 运行时文件，避免覆盖用户正在使用的数据
    locked: list[str] = []
    for target in targets:
        old_exe = DIST_DIR / f"{target['name']}.exe"
        if not old_exe.exists():
            continue
        try:
            old_exe.unlink()
            print(f"[清理] 移除旧文件 {old_exe.name}")
        except PermissionError:
            locked.append(target["name"])
            print(f"[跳过] {old_exe.name} 正在运行中，无法覆盖；请关闭后再重新打包")

    started = time.time()
    results: list[tuple[Path, int]] = []

    for target in targets:
        if target["name"] in locked:
            continue
        exe_path = build_target(target, upx_dir, use_upx)
        if use_upx and not args.no_final_upx:
            print(f"[UPX] {exe_path.name}: {upx_final(exe_path, str(Path(upx_dir) / 'upx.exe'))}")
        results.append((exe_path, exe_path.stat().st_size))

    # 把账号配置复制到 dist，EXE 可直接使用
    config_src = BASE_DIR / "config.json"
    if config_src.exists():
        shutil.copy2(config_src, DIST_DIR / "config.json")
        print("\n[配置] 已复制 config.json 到 dist/")

    print("\n" + "=" * 56)
    print(f"构建完成，用时 {time.time() - started:.1f} 秒，UPX：{'启用' if use_upx else '未启用'}")
    for exe_path, size in results:
        print(f"  {exe_path.name:<26} {human_size(size):>10}")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
