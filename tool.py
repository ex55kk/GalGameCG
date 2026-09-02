# -*- coding: utf-8 -*-
"""
图像小工具集（清屏刷新 + 无括号文案）
功能 1: 为图片批量添加前缀
- 支持把多个文件夹拖到脚本上
- 每个文件夹只处理当前层级图片文件
- 文件夹顺序采用名称自然排序（数字按数值、字母不区分大小写）
- 可设置连接符（默认 "."），支持 ".", "-", " - ", "、" 和自定义
- 可按数字顺序为文件夹自动设置前缀 1,2,3,...
- 可选择：每个文件夹单独设置前缀，或所有文件夹统一前缀
- 已含相同 前缀+连接符 的文件会跳过；重名冲突跳过不覆盖
"""

import sys
import os
import re
import shutil
import subprocess
import runpy
from pathlib import Path

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".webp", ".tif", ".tiff", ".heic", ".heif", ".avif"
}
INVALID_IN_WIN = set('\\/:*?"<>|')  # Windows 非法字符

# ========================= 公共工具 =========================
def clear_screen():
    """清屏刷新界面。"""
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
    except Exception:
        # 兜底 ANSI
        print("\033[2J\033[H", end='')

def natural_key(s: str):
    """名称自然排序键：数字按数值，字母不区分大小写。"""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r'(\d+)', s)]

def pause_if_console():
    try:
        if sys.stdin.isatty():
            wait_enter("\n按回车退出...")
    except Exception:
        pass

def wait_enter(msg="按回车继续..."):
    """仅接收回车，不允许输入其他内容；不回显。Windows 用 msvcrt，其他平台退化到 input。"""
    try:
        import msvcrt
        print(msg, end="", flush=True)
        while msvcrt.kbhit():
            try:
                msvcrt.getwch()
            except Exception:
                break
        while True:
            ch = msvcrt.getwch()
            if ch in ('\r', '\n'):
                break
    except Exception:
        try:
            input(msg)
        except Exception:
            pass

def collect_folders_from_args_or_input():
    """
    优先从拖拽参数收集文件夹；若没有参数，则自动使用脚本所在目录的全部一级子文件夹。
    返回 Path 列表。
    """
    targets = []
    if len(sys.argv) >= 2:
        for raw in sys.argv[1:]:
            s = raw.strip().strip('"')
            p = Path(s)
            if p.is_dir():
                targets.append(p)
            else:
                print(f"路径无效或不是文件夹: {s}")
    else:
        # 无拖拽参数：自动使用脚本所在目录的所有子文件夹
        root = Path(__file__).resolve().parent
        try:
            subs = [d for d in root.iterdir() if d.is_dir()]
        except Exception:
            subs = []
        clear_screen()
        if subs:
            print(f"未检测到拖拽 已使用根目录: {root}")
            print(f"发现文件夹数量: {len(subs)}\n")
            targets.extend(subs)
        else:
            print(f"根目录下没有可用的文件夹: {root}")

    return targets

def sanitize_input(s: str, field_name: str) -> str:
    """校验：不允许包含 Windows 非法字符；去除首尾空白。"""
    s = s.strip()
    if not s:
        return s
    if any(ch in INVALID_IN_WIN for ch in s):
        print(f"{field_name} 含有非法字符 \\ / : * ? \" < > | ，请重新输入")
        return ""
    return s

# ========================= 重命名核心 =========================
def process_folder_add_prefix(folder: Path, prefix: str, sep: str):
    """对单个文件夹进行重命名处理，返回统计信息字典。"""
    total = 0
    renamed = 0
    skipped_already = 0
    skipped_conflict = 0
    skipped_nonimage = 0
    failed = 0

    print(f"===== 处理文件夹: {folder} =====")
    print(f"前缀: {prefix}    连接符: {sep}\n")

    for entry in folder.iterdir():
        if not entry.is_file():
            continue

        ext = entry.suffix.lower()
        if ext not in IMAGE_EXTS:
            skipped_nonimage += 1
            continue

        total += 1

        head = f"{prefix}{sep}"
        if entry.name.startswith(head):
            skipped_already += 1
            continue

        new_name = f"{head}{entry.name}"
        dest = entry.with_name(new_name)

        if dest.exists():
            print(f"冲突 跳过: {entry.name} -> {new_name}")
            skipped_conflict += 1
            continue

        try:
            entry.rename(dest)
            print(f"重命名: {entry.name} -> {new_name}")
            renamed += 1
        except Exception as e:
            print(f"失败: {entry.name} -> {new_name}  原因: {e}")
            failed += 1

    stats = {
        "folder": str(folder),
        "total": total,
        "renamed": renamed,
        "skipped_already": skipped_already,
        "skipped_conflict": skipped_conflict,
        "skipped_nonimage": skipped_nonimage,
        "failed": failed,
    }
    print("\n—— 本文件夹汇总 ——")
    print(f"图片文件总数: {total}")
    print(f"成功重命名:   {renamed}")
    print(f"已有前缀跳过: {skipped_already}")
    print(f"重名冲突跳过: {skipped_conflict}")
    print(f"非图片跳过:   {skipped_nonimage}")
    print(f"失败:         {failed}")
    print()
    return stats

def summarize_overall(overall):
    print("===== 全部文件夹总汇总 =====")
    print(f"图片文件总数: {overall['total']}")
    print(f"成功重命名:   {overall['renamed']}")
    print(f"已有前缀跳过: {overall['skipped_already']}")
    print(f"重名冲突跳过: {overall['skipped_conflict']}")
    print(f"非图片跳过:   {overall['skipped_nonimage']}")
    print(f"失败:         {overall['failed']}")
    print()

# ========================= 设置页面/功能 1 =========================
def choose_separator(current_sep: str) -> str:
    while True:
        clear_screen()
        print("—— 设置连接符 ——")
        print("")
        print(f"当前连接符: {current_sep}\n")
        print("1. .")
        print("2. -")
        print("3.  - ")
        print("4. 、")
        print("5. 自定义")
        print("6. 删除连接符")
        print("0. 返回")
        choice = input("\n请输入编号: ").strip()

        mapping = {
            "1": ".",
            "2": "-",
            "3": " - ",
            "4": "、",
        }
        if choice in mapping:
            return mapping[choice]
        elif choice == "5":
            while True:
                custom = input("请输入自定义连接符: ").strip()
                custom = sanitize_input(custom, "连接符")
                if custom:
                    return custom
                print("连接符不能为空或含非法字符")
        elif choice == "6":
            return ""
        elif choice == "0":
            return current_sep
        else:
            print("未识别选项")

def feature_add_prefix_settings(uniq_folders):
    """
    添加前缀 设置页面
    - 设置连接符
    - 数字排序: 按文件夹顺序使用 1,2,3,... 作为前缀
    - 设置前缀: 单独为文件夹设置前缀 或 设置全部文件夹前缀
    """
    if not uniq_folders:
        print("没有可处理的文件夹")
        return

    sep = "."  # 默认连接符
    while True:
        clear_screen()
        print("===== 添加前缀 设置 =====")
        print("")
        print(f"目标文件夹数: {len(uniq_folders)}")
        print(f"当前连接符: {sep}\n")
        print("1. 设置连接符")
        print("2. 按照数字排序")
        print("3. 设置前缀")
        print("0. 返回主菜单")
        sel = input("\n请输入编号: ").strip()

        # 1) 设置连接符
        if sel == "1":
            sep = choose_separator(sep)
            continue

        # 2) 数字排序 立即执行
        elif sel == "2":
            clear_screen()
            overall = {"total": 0, "renamed": 0, "skipped_already": 0,
                       "skipped_conflict": 0, "skipped_nonimage": 0, "failed": 0}
            for idx, folder in enumerate(uniq_folders, start=1):
                prefix = str(idx)
                stats = process_folder_add_prefix(folder, prefix, sep)
                overall["total"]           += stats["total"]
                overall["renamed"]         += stats["renamed"]
                overall["skipped_already"] += stats["skipped_already"]
                overall["skipped_conflict"]+= stats["skipped_conflict"]
                overall["skipped_nonimage"]+= stats["skipped_nonimage"]
                overall["failed"]          += stats["failed"]
            summarize_overall(overall)
            wait_enter("处理完成 按回车返回主菜单...")
            break

        # 3) 设置前缀
        elif sel == "3" or sel == "3.1" or sel == "3.2":
            # 子选项
            sub = sel
            if sel == "3":
                clear_screen()
                print("—— 设置前缀 ——")
                print("")
                print("1. 单独为文件夹设置前缀")
                print("2. 设置全部文件夹前缀")
                print("0. 返回")
                sub = input("\n请输入编号: ").strip()
                if sub == "0":
                    continue

            # 3.1 单独设置
            if sub in ("1", "3.1"):
                clear_screen()
                overall = {"total": 0, "renamed": 0, "skipped_already": 0,
                           "skipped_conflict": 0, "skipped_nonimage": 0, "failed": 0}
                for folder in uniq_folders:
                    while True:
                        prefix = input(f"[{folder.name}] 请输入该文件夹的前缀: ").strip()
                        prefix = sanitize_input(prefix, "前缀")
                        if prefix:
                            break
                        print("前缀不能为空或含非法字符")
                    print()  # 行距
                    stats = process_folder_add_prefix(folder, prefix, sep)
                    overall["total"]           += stats["total"]
                    overall["renamed"]         += stats["renamed"]
                    overall["skipped_already"] += stats["skipped_already"]
                    overall["skipped_conflict"]+= stats["skipped_conflict"]
                    overall["skipped_nonimage"]+= stats["skipped_nonimage"]
                    overall["failed"]          += stats["failed"]
                    print("\n--------------------------------\n")
                summarize_overall(overall)
                wait_enter("处理完成 按回车返回主菜单...")
                break

            # 3.2 统一设置
            elif sub in ("2", "3.2"):
                clear_screen()
                while True:
                    prefix_all = input("请输入用于全部文件夹的统一前缀: ").strip()
                    prefix_all = sanitize_input(prefix_all, "前缀")
                    if prefix_all:
                        break
                    print("前缀不能为空或含非法字符")
                clear_screen()
                overall = {"total": 0, "renamed": 0, "skipped_already": 0,
                           "skipped_conflict": 0, "skipped_nonimage": 0, "failed": 0}
                for folder in uniq_folders:
                    stats = process_folder_add_prefix(folder, prefix_all, sep)
                    overall["total"]           += stats["total"]
                    overall["renamed"]         += stats["renamed"]
                    overall["skipped_already"] += stats["skipped_already"]
                    overall["skipped_conflict"]+= stats["skipped_conflict"]
                    overall["skipped_nonimage"]+= stats["skipped_nonimage"]
                    overall["failed"]          += stats["failed"]
                    print("\n--------------------------------\n")
                summarize_overall(overall)
                wait_enter("处理完成 按回车返回主菜单...")
                break
            else:
                # 未识别子选项，回到设置页
                continue

        elif sel == "0":
            break
        else:
            # 未识别，刷新设置页
            continue

# ========================= 功能 2：移动文件到根目录 =========================
def feature_move_files_to_root(targets):
    """将选择的每个文件夹内的所有文件移动到脚本根目录；只处理当前层级文件，遇到重名跳过。"""
    # 去重并按名称自然排序
    uniq = []
    seen = set()
    for f in targets:
        try:
            r = f.resolve()
        except Exception:
            r = f
        if r not in seen:
            seen.add(r)
            uniq.append(f)
    uniq.sort(key=lambda p: natural_key(p.name))

    root = Path(__file__).resolve().parent
    clear_screen()
    print("===== 移动文件到根目录 =====")
    print(f"根目录: {root}\n")

    total = moved = skipped_exist = failed = 0

    for folder in uniq:
        print(f"—— 处理文件夹: {folder}")
        try:
            entries = list(folder.iterdir())
        except Exception as e:
            print(f"无法读取，跳过 原因: {e}")
            continue

        for entry in entries:
            if not entry.is_file():
                continue
            total += 1
            dest = root / entry.name
            if dest.exists():
                print(f"冲突 跳过: {entry.name} 已存在于根目录")
                skipped_exist += 1
                continue
            try:
                shutil.move(str(entry), str(dest))
                print(f"移动: {entry.name} -> {dest}")
                moved += 1
            except Exception as e:
                print(f"失败: {entry.name} 原因: {e}")
                failed += 1
        print("")

    print("===== 移动汇总 =====")
    print(f"总文件数: {total}")
    print(f"成功移动: {moved}")
    print(f"重名跳过: {skipped_exist}")
    print(f"失败: {failed}\n")
    wait_enter("处理完成 按回车返回主菜单...")

# ========================= 功能 3：启动差分合成 GUI =========================
def feature_launch_diff_gui():
    """
    启动 gal_diff_batch_exporter.py（Tk GUI）
    - 优先用 subprocess 以隔离依赖；
    - 若子进程启动失败，退回 runpy 在本进程执行。
    """
    from pathlib import Path
    script = Path(__file__).resolve().parent / "差分合成工具.py"
    clear_screen()
    print("===== 差分合成工具（GUI） =====\n")
    if not script.exists():
        print(f"[错误] 未找到脚本：{script}")
        wait_enter("\n按回车返回主菜单...")
        return

    print(f"正在启动：{script}\n关闭 GUI 窗口后将回到本菜单……\n")
    try:
        # 打开子进程，GUI 关闭后返回
        subprocess.run([sys.executable, str(script)])
    except Exception as e:
        print(f"[提示] 子进程启动失败，改用本进程执行：{e}")
        try:
            runpy.run_path(str(script), run_name="__main__")
        except Exception as ee:
            print(f"[错误] 执行失败：{ee}")

    wait_enter("\n已返回主菜单，按回车继续...")


# ========================= 主菜单 =========================
def print_menu():
    clear_screen()
    print("===== 图像工具菜单 =====")
    print("")
    print("1. 添加前缀")
    print("2. 移动文件到根目录")
    print("3. 差分合成工具（GUI）")
    print("0. 退出")

def main():
    while True:
        print_menu()
        choice = input("\n请输入数字选择功能: ").strip()

        if choice == "1":
            # 收集目标文件夹
            targets = collect_folders_from_args_or_input()
            if not targets:
                wait_enter("\n没有可处理的文件夹 按回车返回...")
                continue

            # 去重并按名称自然排序
            uniq = []
            seen = set()
            for f in targets:
                try:
                    r = f.resolve()
                except Exception:
                    r = f
                if r not in seen:
                    seen.add(r)
                    uniq.append(f)
            uniq.sort(key=lambda p: natural_key(p.name))

            # 进入设置页面；返回后直接回到主菜单
            feature_add_prefix_settings(uniq)
            continue

        elif choice == "2":
            targets = collect_folders_from_args_or_input()
            if not targets:
                wait_enter("\n没有可处理的文件夹 按回车返回...")
                continue
            feature_move_files_to_root(targets)
            continue

        elif choice == "3":
            feature_launch_diff_gui()
            continue

        elif choice == "0":
            # 主菜单选择 0 为真正退出；如果你想让 0 也回主菜单，改成 continue 即可
            break

        else:
            # 未识别输入，刷新主菜单
            continue

if __name__ == "__main__":
    main()
