# ida_pseudocode_server.py

import json
import sys
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import threading
import traceback
import time

import ida_funcs
import ida_hexrays
import ida_lines
import ida_kernwin
import ida_idaapi
import ida_name
import ida_pro
import idautils
import idautils
import ida_dbg

# ================= core helpers =================
PORT = int(os.environ.get("IDA_PSEUDO_PORT", "8888"))

def _ensure_hexrays():
    if not ida_hexrays.init_hexrays_plugin():
        raise RuntimeError("Hex-Rays decompiler not available")

def _get_pseudocode_context_core(ea, context=2):
    _ensure_hexrays()

    func = ida_funcs.get_func(ea)
    if not func:
        raise RuntimeError(f"0x{ea:X} not in any function")

    cfunc = ida_hexrays.decompile(func.start_ea)
    if not cfunc:
        raise RuntimeError("decompile failed")

    citem = cfunc.body.find_closest_addr(ea)
    parent = cfunc.body.find_parent_of(citem)
    if parent:
        citem = parent

    _, line_no = cfunc.find_item_coords(citem)
    pseudo = cfunc.get_pseudocode()

    start = max(0, line_no - context)
    end = min(len(pseudo), line_no + context + 1)

    return {
        "function": ida_funcs.get_func_name(func.start_ea),
        "line_number": line_no,
        "target_line": ida_lines.tag_remove(pseudo[line_no].line),
        "context": [ida_lines.tag_remove(pseudo[i].line) for i in range(start, end)],
    }

def _get_pseudocode_by_line_core(func_name, line, before, after):
    _ensure_hexrays()

    ea = ida_name.get_name_ea(ida_idaapi.BADADDR, func_name)
    func = ida_funcs.get_func(ea)
    cfunc = ida_hexrays.decompile(func.start_ea)
    pseudo = cfunc.get_pseudocode()

    idx = min(max(0, line), len(pseudo) - 1)
    start = max(0, idx - before)
    end = min(len(pseudo), idx + after + 1)

    return {
        "function": func_name,
        "line_number": idx,
        "target_line": ida_lines.tag_remove(pseudo[idx].line),
        "context": [ida_lines.tag_remove(pseudo[i].line) for i in range(start, end)],
    }

def _get_full_pseudocode_core(func_name):
    _ensure_hexrays()

    ea = ida_name.get_name_ea(ida_idaapi.BADADDR, func_name)
    func = ida_funcs.get_func(ea)
    cfunc = ida_hexrays.decompile(func.start_ea)
    return [ida_lines.tag_remove(sl.line) for sl in cfunc.get_pseudocode()]

def is_bad_function(func):
    if func.flags & (ida_funcs.FUNC_THUNK | ida_funcs.FUNC_LIB):
        return True
    name = ida_funcs.get_func_name(func.start_ea)
    if name.startswith(("__imp_", "_imp_", "j_", "nullsub_")):
        return True
    return False

def _list_functions_core():
    res = []
    for f_ea in idautils.Functions():
        f = ida_funcs.get_func(f_ea)
        if not f:
            continue
        if is_bad_function(f):
            continue   # 跳过 thunk / 外部函数
        res.append({
            "name": ida_funcs.get_func_name(f_ea),
            "ea": f"0x{f_ea:X}"
        })
    return res

# ================= HTTP handler =================

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)

        def run(job):
            holder = {}
            def task():
                try:
                    holder["res"] = job()
                except Exception as e:
                    holder["error"] = e
                    holder["traceback"] = traceback.format_exc()
            success = ida_kernwin.execute_sync(task, ida_kernwin.MFF_READ)
            if not success:
                raise RuntimeError("execute_sync failed to execute the job in the main thread")
            if "error" in holder:
                raise Exception(holder["error"], holder["traceback"])  # 传递异常信息
            return holder["res"]

        try:
            if parsed.path == "/":
                self._send(run(lambda: _get_pseudocode_context_core(
                    int(q["ea"][0], 0),
                    int(q.get("n", [3])[0])
                )), 200)

            elif parsed.path == "/by_line":
                self._send(run(lambda: _get_pseudocode_by_line_core(
                    q["func"][0],
                    int(q["line"][0]),
                    int(q.get("before", [3])[0]),
                    int(q.get("after", [3])[0])
                )), 200)

            elif parsed.path == "/full":
                self._send(run(lambda: {
                    "function": q["func"][0],
                    "pseudocode": _get_full_pseudocode_core(q["func"][0])
                }), 200)

            elif parsed.path == "/list_functions":
                self._send(run(_list_functions_core), 200)

            elif parsed.path == "/shutdown":
                self._send({"ok": True}, 200)
                
                # 延迟退出，确保响应发送完成
                def delayed_exit():
                    time.sleep(0.5)  # 等待500ms
                    ida_pro.qexit(0)  # 0表示正常退出
                
                # 在新线程中执行退出，不阻塞当前响应
                threading.Thread(target=delayed_exit, daemon=True).start()

            else:
                self._send({"error": "unknown path"}, 404)

        except Exception as e:
            tb = traceback.format_exc()
            self._send({"error": str(e), "traceback": tb}, 500)

    def _send(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

# ================= start server =================

def start_server(port=8888, host="0.0.0.0"):
    server = HTTPServer((host, port), RequestHandler)
    print("[+] IDA pseudocode server listening on http://{}:{}".format(host, port))
    server.serve_forever()

threading.Thread(target=start_server, kwargs={"port": PORT}, daemon=True).start()
