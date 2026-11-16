# -*- coding: utf-8 -*-
"""桌面控制插件 · Python 本体入口（**私有 stdio 行协议**）。

用法
----
  python worker.py --dump-tools     打印一行 JSON：{"tools":[{name,description,inputSchema}, ...]}
  python worker.py                  常驻：每行一个请求 {"id":1,"name":"mouse_click","args":{...}}
                                    回一行 {"id":1,"content":[...],"isError":false} 或 {"id":1,"error":"..."}

设计
----
  · 本体＝desktop_core.py（截图＋动作；OCR/UIA/图标识别已整层删除）；
  · stdout 严格只放协议行：import 阶段允许 desktop_core 打印，import 完成后把 sys.stdout 指到 stderr，
    协议用一开始保存的真 stdout；
  · 工具面直接取自本体的 dispatch_message 输出。
"""
import sys
import os
import json
import threading

# ★Windows 上 Python 默认按本地代码页（GBK）编解码 stdin/stdout，而协议流一律 UTF-8：
#   · dump 出去的描述里有箭头/中文（如 ⇒），write 会抛 UnicodeEncodeError（实测就栽在这）；
#   · 读入的参数同理（send_keystrokes 的中文文本是 Node 按 UTF-8 写进来的）。
#   抓真 stdout 之前先统统改成 UTF-8；index.js 另设 PYTHONUTF8=1 双保险。
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_REAL_OUT = sys.stdout          # 真 stdout：只给协议用

sys.stdout = sys.stderr         # ★import 之前就换掉：desktop_core 模块级会往 stdout 打印（DPI 等），
                                #   那些行绝不能落进协议流；import 完成后一切 print 也都进 stderr。

import desktop_core as dc       # noqa: E402

_OUT_LOCK = threading.Lock()    # ★协议写出互斥：响应行与「后台 push 行」不许交错


def _tools():
    """工具面：走本体的最高入口 `dispatch_message` 的 `tools/list`。

    单一出处＝本体的 TOOLS（不在 JS/Python 各抄一份）。
    """
    resp = dc.dispatch_message({"id": 0, "method": "tools/list", "params": {}})
    tools = (((resp or {}).get("result") or {}).get("tools")) or []
    out = []
    for t in tools:
        out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "inputSchema": t.get("inputSchema", {"type": "object", "properties": {}}),
        })
    return out


def _send(obj):
    """协议行：**直接写 stdout 的二进制层**（UTF-8），绕开任何文本编码设置。"""
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _OUT_LOCK:
        buf = getattr(_REAL_OUT, "buffer", None)
        if buf is not None:
            buf.write(line.encode("utf-8", "replace"))
            buf.flush()
        else:                                # 兜底：没有二进制层就退回纯 ASCII 文本
            _REAL_OUT.write(json.dumps(obj, ensure_ascii=True) + "\n")
            _REAL_OUT.flush()


def _send_push(ev):
    """**后台结果推送**（无 id 的事件行）：后台截图完成后由本体调用，
    Node 壳收到后走统一注入通道（pushTo）自动注入上下文，不再要求调用方 `vision_action=pull`。"""
    try:
        line = json.dumps({"push": ev}, ensure_ascii=False) + "\n"
        with _OUT_LOCK:
            buf = getattr(_REAL_OUT, "buffer", None)
            if buf is not None:
                buf.write(line.encode("utf-8", "replace"))
                buf.flush()
            else:
                _REAL_OUT.write(json.dumps({"push": ev}, ensure_ascii=True) + "\n")
                _REAL_OUT.flush()
    except Exception:
        pass


# 装上推送口：本体后台线程（peek／概览拼图）完成时自动推给 Node
dc._PUSH_SINK = _send_push


def _main():
    if "--dump-tools" in sys.argv:
        _send({"tools": _tools()})
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _send({"id": None, "error": "bad json: %s" % e})
            continue
        rid = req.get("id")
        try:
            # **接最高层入口 `dispatch_message`**（`tools/call` → `_tool_call_result`）。
            #   worker 只当传输层，不碰语义：
            #   耗时标签／按住状态提醒／动作类回执补「窗口矩形」／动作类默认附图／
            #   自带图路径的**标尺行**，全由本体中央收尾层产出。
            #   ⚠ 之前直接调 `handle_tool_call`（底层执行体）把这一整层绕过了：
            #   capture_screenshot 回执里承诺的「物理坐标换算表」整行消失、动作类默认附图也不出现。
            resp = dc.dispatch_message({"id": rid, "method": "tools/call",
                                        "params": {"name": req.get("name"),
                                                   "arguments": req.get("args") or {}}})
            if not isinstance(resp, dict):
                raise RuntimeError("dispatch 无响应（name=%s）" % (req.get("name"),))
            if resp.get("error"):
                raise RuntimeError(str(resp.get("error")))
            res = resp.get("result") or {}
            _send({"id": rid,
                   "content": res.get("content", []),
                   "isError": bool(res.get("isError"))})
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            _send({"id": rid, "error": str(e)})


if __name__ == "__main__":
    _main()
