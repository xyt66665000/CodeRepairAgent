# export_all_funcs_full.py
# 使用 decompile_many 进行批量反编译，过滤thunk函数和库函数，导出到单个.c文件中，包含所有普通函数的伪代码和函数、变量的声明
# 使用方法: idat.exe -A -T -S"export_all_funcs_full.py <output_dir>" <binary>

import ida_auto
import ida_hexrays
import ida_kernwin
import ida_loader
import idaapi
import ida_pro
import idc
import idautils
import os
import time
import sys
import ida_funcs

def is_bad_function(func):
    # thunk / library
    if func.flags & (ida_funcs.FUNC_THUNK | ida_funcs.FUNC_LIB):
        return True
    name = ida_funcs.get_func_name(func.start_ea)
    if not name:
        return True
    # wrapper / import / stub
    if name.startswith((
        "__imp_", "_imp_", "j_", "nullsub_"
    )):
        return True

    return False

def main():
    # parse export dir from args
    argv = idc.ARGV
    is_gui_mode = len(argv) <= 1
    export_dir = None

    if not is_gui_mode:
    # ---------------- CLI mode ----------------    
        if len(argv) >= 2:
            export_dir = os.path.abspath(argv[1])
        else:
            # default: next to idb
            idb_path = idaapi.get_path(idaapi.PATH_TYPE_IDB)
            export_dir = os.path.join(os.path.dirname(idb_path), "export")
    else:
    # ---------------- GUI mode ----------------
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
            export_dir = default_dir
        else:
            export_dir = os.path.abspath(path.strip())

    print("[*] Waiting for auto-analysis to finish...")
    ida_auto.auto_wait()
    print("[+] Auto-analysis finished")

    print("[*] Initializing Hex-Rays...")
    if not ida_hexrays.init_hexrays_plugin():
        print("[!] Hex-Rays decompiler not available. Exiting.")
        # exit with non-zero
        ida_pro.qexit(1)

    # Run a few UI/refinement cycles to mimic what GUI does.
    print("[*] Running refinement cycles...")
    for i in range(6):
        try:
            # schedule a trivial UI action so internal pass queue runs
            ida_kernwin.execute_sync(lambda: None, ida_kernwin.MFF_WRITE)
        except Exception:
            # fallback to process_ui_action if execute_sync not allowed
            try:
                ida_kernwin.process_ui_action("Empty", 0)
            except Exception:
                pass
        ida_auto.auto_wait()
        time.sleep(0.5)
        print(f"[+] refinement {i+1}/6 done")

    # Optionally: ensure all functions are present
    funcs = list(idautils.Functions())
    total = len(funcs)
    print(f"[*] Found {total} functions")

    os.makedirs(export_dir, exist_ok=True)

    out_file = os.path.join(export_dir, "all_functions.c")
    print(f"[*] Batch decompiling to: {out_file}")

    # Use decompile_many which is a batch decompiler routine.
    flags = ida_hexrays.VDRUN_SILENT | ida_hexrays.VDRUN_CMDLINE

    ok = ida_hexrays.decompile_many(out_file, None, flags)
    if not ok:
        print("[!] decompile_many failed; falling back to iterative decompile.")

        exported = 0
        failed = 0
        with open(out_file, "w", encoding="utf-8") as fout:
            fout.write("/* exported by decompile fallback */\n\n")
            for ea in funcs:
                func = ida_funcs.get_func(ea)
                if not func:
                    continue
                if is_bad_function(func):
                    continue
                try:
                    cf = ida_hexrays.decompile(func.start_ea)
                    if not cf:
                        failed += 1
                        continue
                    fout.write(str(cf))
                    fout.write("\n\n")
                    try:
                        cf.save_user_iflags()
                        cf.save_user_cmts()
                        cf.save_user_labels()
                        cf.save_user_numforms()
                    except Exception:
                        pass

                    cf.release()
                    exported += 1

                except Exception:
                    failed += 1

        print(f"[+] fallback exported {exported}, failed {failed}")

    else:
        print("[+] decompile_many succeeded, saved to", out_file)

    if not is_gui_mode:
        # Save matured packed database so the i64 contains as much as we forced
        try:
            print("[*] Saving packed database (DBFL_COMP)...")
            ida_loader.save_database(None, ida_loader.DBFL_COMP)
            print("[+] Saved packed database.")
        except Exception as e:
            print(f"[!] Failed to save database: {e}")

        # Exit cleanly (0)
        ida_pro.qexit(0)

if __name__ == "__main__":
    main()
