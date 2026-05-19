# -*- coding: utf-8 -*-
# 导出函数伪代码（支持 CLI / GUI 双模式，single / multi）
# CLI:
#   idat.exe -A -T -S"export_all_funcs.py <output_dir> [single|multi]" <binary>
# GUI:
#   File -> Script File -> 选择本脚本

import ida_auto
import ida_hexrays
import ida_kernwin
import ida_loader
import ida_funcs
import ida_lines
import ida_name
import idaapi
import idautils
import ida_pro
import idc
import time
import os
import re

# 运行模式判断
argv = idc.ARGV
IS_GUI_MODE = len(argv) <= 1

# 参数解析（CLI / GUI 两种模式）
EXPORT_DIR = None
EXPORT_MODE = "single"

if not IS_GUI_MODE:
    # ---------------- CLI 模式 ----------------
    if len(argv) >= 2:
        EXPORT_DIR = os.path.abspath(argv[1])
    else:
        idb_path = idaapi.get_path(idaapi.PATH_TYPE_IDB)
        EXPORT_DIR = os.path.join(os.path.dirname(idb_path), "export")

    if len(argv) >= 3 and argv[2].lower() in ("single", "multi"):
        EXPORT_MODE = argv[2].lower()

else:
    # ---------------- GUI 模式 ----------------
    idb_path = idaapi.get_path(idaapi.PATH_TYPE_IDB)
    base_dir = os.path.dirname(idb_path)
    default_dir = os.path.join(base_dir, "export")

    path = ida_kernwin.ask_str(
        default_dir,
        0,
        "请输入导出目录路径"
    )

    if path is None:
        print("[!] 用户取消了导出，脚本已退出")
        raise SystemExit
    elif path.strip() == "":
        EXPORT_DIR = default_dir
    else:
        EXPORT_DIR = os.path.abspath(path.strip())

    mode = ida_kernwin.ask_buttons(
        "单文件（all_functions.c）",
        "多文件（每函数一个 .c）",
        "",
        0,
        "请选择导出模式"
    )

    if mode == ida_kernwin.ASKBTN_CANCEL:
        print("[!] 用户取消了导出模式选择，脚本已退出")
        raise SystemExit
    elif mode == ida_kernwin.ASKBTN_YES:
        EXPORT_MODE = "single"
    elif mode == ida_kernwin.ASKBTN_NO:
        EXPORT_MODE = "multi"

    print(f"[*] GUI 选择的导出模式: {EXPORT_MODE}")

os.makedirs(EXPORT_DIR, exist_ok=True)

print(f"[+] 运行模式      : {'GUI' if IS_GUI_MODE else 'CLI'}")
print(f"[+] 导出模式      : {EXPORT_MODE}")
print(f"[+] 导出目录      : {EXPORT_DIR}")

if EXPORT_MODE == "single":
    OUT_FILE = os.path.join(EXPORT_DIR, "all_functions.c")
    print(f"[+] 输出文件      : {OUT_FILE}")

# 工具函数
def sanitize_filename(name):
    name = ida_name.demangle_name(name, ida_name.MNG_SHORT_FORM) or name
    return re.sub(r'[\\/:*?"<>| ]+', "_", name)

def is_bad_function(func):
    if func.flags & (ida_funcs.FUNC_THUNK | ida_funcs.FUNC_LIB):
        return True

    name = ida_funcs.get_func_name(func.start_ea)
    if name.startswith((
        "__imp_", "_imp_", "j_", "nullsub_", "__stub_", "__plt_"
    )):
        return True

    return False

def get_full_pseudocode(func):
    cfunc = ida_hexrays.decompile(func.start_ea)
    if not cfunc:
        return None
    return [ida_lines.tag_remove(sl.line) for sl in cfunc.get_pseudocode()]

# 等待自动分析
print("[*] Waiting for auto-analysis...")
ida_auto.auto_wait()
print("[+] Auto-analysis finished")

print("[*] Initializing Hex-Rays...")
if not ida_hexrays.init_hexrays_plugin():
    print("[!] Hex-Rays unavailable")
    if not IS_GUI_MODE:
        ida_pro.qexit(1)
    raise SystemExit

# 强制 Hex-Rays 稳定
print("[*] Forcing Hex-Rays refinement cycles...")
for i in range(5):
    ida_kernwin.process_ui_action("Empty", 0)
    ida_auto.auto_wait()
    time.sleep(1)
    print(f"    [+] refinement round {i + 1}/5")

# 枚举函数
print("[*] Collecting functions...")
good_funcs = []

for f_ea in idautils.Functions():
    func = ida_funcs.get_func(f_ea)
    if not func:
        continue
    if is_bad_function(func):
        continue
    good_funcs.append(func)

good_funcs.sort(key=lambda f: f.start_ea)
print(f"[+] {len(good_funcs)} valid functions found")

# 导出伪代码
print("[*] Exporting pseudocode...")

single_fp = None
if EXPORT_MODE == "single":
    single_fp = open(OUT_FILE, "w", encoding="utf-8")

for idx, func in enumerate(good_funcs, 1):
    name = ida_funcs.get_func_name(func.start_ea)
    print(f"[{idx}/{len(good_funcs)}] {name}")

    try:
        pseudo = get_full_pseudocode(func)
        if not pseudo:
            content = f"\n/* FAILED TO DECOMPILE {name} */\n"
        else:
            lines = []
            lines.append("\n/*=============================== */")
            lines.append(f"/* {name} @ 0x{func.start_ea:X} */")
            lines.extend(pseudo)
            lines.append("\n")
            content = "\n".join(lines)

        if EXPORT_MODE == "single":
            single_fp.write(content)
        else:
            safe_name = sanitize_filename(name)
            out_path = os.path.join(EXPORT_DIR, f"{safe_name}.c")
            with open(out_path, "w", encoding="utf-8") as fp:
                fp.write(content)

    except Exception as e:
        err = f"\n/* ERROR {name}: {e} */\n"
        if EXPORT_MODE == "single":
            single_fp.write(err)

if single_fp:
    single_fp.close()

print("[+] Export finished")

# 输出最终结果路径
if EXPORT_MODE == "single":
    print(f"[+] 导出完成，文件路径: {OUT_FILE}")
else:
    print(f"[+] 导出完成，目录路径: {EXPORT_DIR}")

# 保存并退出（GUI 模式不退出）
print("[*] Saving database...")
ida_loader.save_database(None, ida_loader.DBFL_COMP)

if IS_GUI_MODE:
    print("[+] 脚本执行完毕")
else:
    print("[+] 退出 IDA")
    ida_pro.qexit(0)
