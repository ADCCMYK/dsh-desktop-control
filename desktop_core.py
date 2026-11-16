# ══════════════════════════════════════════════════════════════════════════════
# 桌面控制内核：截图、窗口枚举与聚焦、键鼠注入、序列与后台观察。
# 工具面定义在 TOOLS；分发入口 handle_tool_call。
# ══════════════════════════════════════════════════════════════════════════════






import json, sys, os, time, re, threading, subprocess, tempfile, shutil, traceback, atexit

# ══════════════════════════════════════════════════════════════════════════════
# 后台结果 → 宿主**统一注入通道**：插件形态下 worker.py 把 `_PUSH_SINK` 装上，
# 后台线程完成时这里推一条事件，由 Node 壳走统一注入函数 `pushTo` 注入上下文。
# **没有推送通道时不装 sink**（`_PUSH_SINK` 恒 None）⇒ 行为与原来逐字一致（仍靠 vision_action=pull）。
_PUSH_SINK = None


def _push_event(kind, payload=None):
    """把后台完成事件推给宿主。**异常绝不外溢**（后台线程不能因为推送失败而死）。

    ★0.1.88：注入的是**拼图/单拍**（不是图内坐标轴图）⇒ 作废当前坐标轴，
    避免模型拿拼图上的位置当图内坐标去点；需要操作请重新整图截图。"""
    try:
        if kind in ("peek", "watch", "frame"):
            try:
                _axis_reset("最近附图是后台注入的拼图/单拍，不是坐标轴图——请重新截图")
            except Exception:
                pass
        if _PUSH_SINK:
            ev = {"kind": kind}
            if payload:
                ev.update(payload)
            _PUSH_SINK(ev)
    except Exception:
        pass


def _b64_of_png(png_bytes):
    """PNG 字节 → base64 文本（推送用；失败返回空串，调用方照旧走 pull 兜底）。"""
    try:
        import base64 as _b64
        return _b64.b64encode(bytes(png_bytes or b"")).decode("ascii")
    except Exception:
        return ""
import argparse, secrets, socket
import math, random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
# 已删除：`from concurrent.futures import ThreadPoolExecutor, as_completed`
# —— 它唯一的用处是「分条并行跑 N 个 tesseract 子进程」，随分条一起退场（现全文件已无使用者）。
# 注：别处的"并行"是「UIA 与 OCR 各起一个线程」，用的是 threading，不是这个，**那些保留不动**。
import mss
import mss.tools
# 已删除：`import pytesseract` —— OCR 引擎唯一＝RapidOCR（不再保留 tesseract 通道）
# 已删除：`import uiautomation as auto`（UIA 整层不存在）。
import ctypes
from ctypes import wintypes

# ══ 显式声明 DPI 意识 ══════════════════════════════════════════════════════════════
# 目标：无论系统缩放调到多少，都能由图片算出任意位置的**物理坐标**。
# 事实（实测）：本机显示缩放 **125%**；python.exe 默认 **DPI-unaware**（虚拟化坐标：
#   GetSystemMetrics 报 1536×864、GetWindowRect 也被缩），而 `import uiautomation` 会**顺手**
#   把进程设成 per-monitor（实测 0→2）——**这是它模块的副作用，不能依赖**。
# ⇒ 在任何窗口/UI 调用之前**显式**声明：per-monitor v2（Win10 1703+）→ 退 shcore 2 → 退 system。
#   此后 窗口矩形 / 鼠标坐标 / 截图尺寸 全在**物理像素**空间，标尺公式算出的就是物理坐标。
def _init_dpi_awareness():
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
        return "per-monitor"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "unaware"


def _dpi_awareness_code():
    """0=unaware / 1=system / 2=per-monitor（读不到给 -1）。"""
    try:
        v = ctypes.c_uint()
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(v))
        return int(v.value)
    except Exception:
        return -1


_DPI_MODE = _init_dpi_awareness()
_DPI_AWARENESS = _dpi_awareness_code()

# ========== SendInput 内联模块 ==========
# 替代 sendinput.py 子进程调用，直接操作 Windows SendInput API

user32_si = ctypes.windll.user32
user32_si.GetDC.argtypes = [wintypes.HWND]
user32_si.GetDC.restype = wintypes.HDC
user32_si.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32_si.ReleaseDC.restype = ctypes.c_int

INPUT_MOUSE    = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE      = 0x0001
MOUSEEVENTF_LEFTDOWN  = 0x0002
MOUSEEVENTF_LEFTUP    = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP   = 0x0010
MOUSEEVENTF_ABSOLUTE  = 0x8000
MOUSEEVENTF_WHEEL     = 0x0800
# 中键常量：`sequence` 的 click 步与工具级 `mouse_click` 一样支持 middle。
#   （补齐前序列 click 只认 left/right，middle 直接抛错。）
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP   = 0x0040
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000   # WM_MOUSEMOVE 不合并（默认会合并）——相对移动的可选项，默认不用

user32_si.mouse_event.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
user32_si.mouse_event.restype = None
user32_si.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32_si.SetCursorPos.restype = ctypes.c_bool
KEYEVENTF_KEYDOWN = 0x0000
KEYEVENTF_KEYUP   = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_UNICODE  = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001   # E0 扩展键标志（WIN 家族等）

# SendInput 结构体
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]
class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
class INPUT(ctypes.Structure):
    _anonymous_ = ("_",)
    _fields_ = [("type", ctypes.c_ulong), ("_", _INPUT_UNION)]

user32_si.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32_si.SendInput.restype = ctypes.c_uint

_SI_VK_MAP = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    # WIN 家族与左右修饰键（原来只有 "win"，右WIN/Apps/右Ctrl 等发不出来）
    "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D, "menu": 0x5D,
    "lctrl": 0xA2, "rctrl": 0xA3, "lalt": 0xA4, "ralt": 0xA5,
    "lshift": 0xA0, "rshift": 0xA1,
    "printscreen": 0x2C, "numlock": 0x90, "scroll": 0x91, "pause": 0x13, "capslock": 0x14,
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09,
    "backspace": 0x08, "delete": 0x2E, "space": 0x20,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    # 数字
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    # 符号（VK_OEM_*）
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE,
    "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}


def _si_parse_key(key_string):
    ks_lower = key_string.lower()
    if ks_lower in _SI_VK_MAP:
        return _SI_VK_MAP[ks_lower]
    if len(key_string) == 1 and key_string.isascii():
        return ord(key_string.upper())
    return None




_last_click_timing = {}          # 最近一次点击的时序（回执自证用：落下停顿/按下-抬起各多少 ms）


# 全局**按住状态账本**（鼠标键按 button 名、键盘键按 token 名，**跨工具调用可见**）。
#   三处用途：① 每次工具回执末尾若仍处于按住状态 ⇒ 明写一行提醒（含"怎么松开"）；
#            ② 下一次工具调用**入口**若发现残留的鼠标按住（异常/超时/被强杀留下的）⇒ **自动松开并如实告警**；
#            ③ 鼠标"按下→抬起"全部包 `try/finally`（拖拽那条原来**没有** finally，中途异常就会把桌面拖住）。
_HELD_LOCK = threading.Lock()
_HELD = {"mouse": set(), "keys": set(), "intentional": set()}   # intentional＝hold 模式**刻意保持按住**的鼠标键
_HELD_AUTO_NOTE = ""      # 本轮"入口自动松开残留按住"的说明（每轮重置，由回执带出）


def _held_mark_btn(button, down):
    with _HELD_LOCK:
        b = str(button or "left").lower()
        if down:
            _HELD["mouse"].add(b)
        else:
            _HELD["mouse"].discard(b)
            _HELD["intentional"].discard(b)


def _held_mark_intentional(button, on):
    """hold 模式：**刻意保持按住**（入口的"残留自动松开"要放过它，否则跨调用 hold 活不过下一轮）。"""
    with _HELD_LOCK:
        b = str(button or "left").lower()
        if on:
            _HELD["intentional"].add(b)
        else:
            _HELD["intentional"].discard(b)


def _held_mark_key(key, down):
    with _HELD_LOCK:
        k = str(key)
        if down:
            _HELD["keys"].add(k)
        else:
            _HELD["keys"].discard(k)


def _held_desc():
    """当前仍按住的东西（没人按住 ⇒ 空串）。hold 模式刻意保持的会特别标注（怎么松开）。"""
    with _HELD_LOCK:
        ms = sorted(_HELD["mouse"])
        ks = sorted(_HELD["keys"])
        intentional = set(_HELD["intentional"])
    names = {"left": "鼠标左键", "right": "鼠标右键", "middle": "鼠标中键"}
    parts = []
    if ms:
        segs = []
        for b in ms:
            nm = names.get(b, "鼠标 " + b + " 键")
            if b in intentional:
                segs.append(nm + "（**hold 模式·刻意保持按住**；松开用 `mouse_drag(release=true)`）")
            else:
                segs.append(nm + "（按住中，还没松开）")
        parts.append("、".join(segs))
    if ks:
        parts.append("键盘 " + "、".join(ks) + "（按住中，还没松开）")
    return "；".join(parts)


def _held_release_mouse_all(stale_only=False):
    """把鼠标按住状态强制松开（返回松开了哪些；没有可松开的返回空串）。

    `stale_only=True`：只松**非刻意**的残留（入口安全网用）——hold 模式刻意保持的不动，
    否则"按住不松手"活不过下一轮工具调用。"""
    with _HELD_LOCK:
        btns = sorted(b for b in _HELD["mouse"]
                      if (not stale_only) or (b not in _HELD["intentional"]))
    if not btns:
        return ""
    up_map = {"left": MOUSEEVENTF_LEFTUP, "right": MOUSEEVENTF_RIGHTUP, "middle": MOUSEEVENTF_MIDDLEUP}
    names = {"left": "鼠标左键", "right": "鼠标右键", "middle": "鼠标中键"}
    done = []
    for b in btns:
        u = up_map.get(b)
        if u:
            try:
                user32_si.mouse_event(u, 0, 0, 0, 0)
                done.append(names.get(b, "鼠标 " + b + " 键"))
            except Exception:
                pass
        _held_mark_btn(b, False)
    return "、".join(done)


def _mouse_event_click(x, y, button="left", click_count=1, human=False, speed=None, precise=False,
                       pre_click_hook=None, dwell_ms=None):
    """mouse_event 点击：不受 UIPI 限制，MoveTo + 点击。

    接近方式：某些窗口/控件对"鼠标瞬移到位后立刻点击"会丢弃这一击，因此不直接瞬移点击——
    光标先落到目标旁 3px，再用 `_glide_to()` 走完最后一小步，让目标窗口先收到"鼠标走过来"的过程。
    档位与 `mouse_move`／`mouse_drag` 同形：`human=True` ⇒ 人影档（≈2500 px/s，夹 0.14~0.60s）；
    默认 False ⇒ 快档（≈4000 px/s，夹 0.08~0.40s）。档位只影响"接近路要走多久"，
    落下停顿/按住时长那套人手化 `_human_pause` 不动。

    `dwell_ms`：**落点后、按下前的停顿**，自由传（0~2000ms）。
      不传 ⇒ 老行为（随机 30~100ms，中位 50ms）；远控/RDP 建议 200~300，游戏串流 300~500。"""
    glide = _glide_to(x, y, human=human, speed=speed, precise=precise)   # 人手化走到目标（统一入口）★两档＋可调速度从这里生效
    _dv = _clamp_dwell_ms(dwell_ms)
    if _dv is None:
        settle = _human_pause(0.03, 0.10, 0.05)     # 落下后先停一下再按（真人点之前会"瞄"一下）
    else:
        settle = _dv / 1000.0
    # 纯像素生效判定的「前帧」钩子——放在**接近路走完、按下之前**
    #   （否则指针扫过控件产生的 hover 变化会被误算成"这次点击生效了"）。默认 None ⇒ 老路一字节不变。
    if pre_click_hook is not None:
        try:
            pre_click_hook()
        except Exception:
            pass
    if button == "left":
        down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    else:
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    holds = []
    for _ in range(click_count):
        user32_si.mouse_event(down, 0, 0, 0, 0)
        _held_mark_btn(button, True)
        try:
            holds.append(_human_pause(0.05, 0.20, 0.085))  # 按下→抬起：真人 50~200ms（原固定 20ms 是指纹）
        finally:
            # **按下→抬起必须成对**——中间抛异常也先抬起（否则桌面会被一直按住）
            user32_si.mouse_event(up, 0, 0, 0, 0)
            _held_mark_btn(button, False)
        _human_pause(0.04, 0.12, 0.06)                 # 连击之间
    global _last_click_timing
    _last_click_timing = {"settle_ms": round(settle * 1000),
                          "glide_steps": glide.get("steps"),
                          "glide_elapsed": glide.get("elapsed"),
                          "hold_ms": [round(h * 1000) for h in holds]}


def _clamp_dwell_ms(v):
    """`dwell_ms` 夹取：None/非法 ⇒ None（走老随机停顿）；否则夹到 0~2000ms。"""
    if v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n:          # NaN
        return None
    return max(0.0, min(2000.0, n))


def _mouse_event_scroll(clicks):
    """mouse_event 滚轮：不受 UIPI 限制。"""
    user32_si.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, clicks * 120, 0)


# ══════════════════════════════════════════════════════════════════════════════
# 相对输入：相对移动 ＋ 输入序列
#   为什么需要：① **相对移动**——只认 raw delta 的应用（典型是游戏）；老路的 `SetCursorPos` 一个输入事件都不产生
#   ⇒ 现象是「点得到、视角转不动」；② 把「同时按键盘 ＋ 动鼠标」一次发出去（宏），跑完回一张截图。
#   实现方式：**不套用工具级包装**（那会带来每步 `_ensure_foreground` 与每次点击的无条件
#   `enum_all_windows()` ⇒ 时间成本太大），而是直接组合底层原语
#   （`mouse_event`／`_si_key_down`／`_si_key_up`／`_si_hold_key`／`_glide_to`／`_mouse_event_scroll`），
#   形成一个薄执行器。
#   边界：**纯新增** —— 老的 `_glide_to`/`_me_start_move`/`_si_move_abs`/`_mouse_event_click`/
#   `_mouse_event_scroll`/`_key_event`/`_do_send_keystrokes` **一行未动**；
#   不调用新工具 `sequence` 即回到原行为。
#
#   复用与差异：`wait_with_cancel`（分片睡眠＋ESC 可取消）替换裸 `time.sleep`；
#   其余直接复用现有件：`_require_focus_target`／`_ensure_foreground`／`_glide_to`／`_mouse_event_scroll`／
#   `_si_key_down`／`_si_key_up`／`_show_indicator`／`_prune_tracked_popups`／`_win_snapshot_light`＋
#   `_win_delta_text`／`_status_block`／附图（`_want_image` 恒 True）。
#   三处刻意不同（各有理由，改动前先看）：
#     ① `KBD_KEY_HOLD`（3~6ms）＝**为打字**调的值 —— 游戏「按一下」要被帧循环采到，需 30~50ms ⇒ 本处默认 40ms；
#     ② `_si_hold_key` 的两步（down→up）照用，但睡眠换成 `_sleep`：只有 `_sleep` 同时受
#        「总超时」约束且「中途可取消」，而 `_si_hold_key` 内部是裸 `time.sleep`；
#     ③ `_human_pause`——相对位移路径要**确定性**，不做 20~200ms 的随机抖动（绝对移动步仍走
#        `_glide_to`，那条路原本就是人手化，未改）。
# ══════════════════════════════════════════════════════════════════════════════

def _mouse_event_move_rel(dx, dy, nocoalesce=False):
    """**相对位移**（`MOUSEEVENTF_MOVE`，**不带 ABSOLUTE**）—— 视角类操作要的 delta 语义。

    选它的三条依据：
      · 实测：`mouse_event` 相对位移**进 `WM_INPUT`**（112 次调用 → 224 个 raw 事件）；
        而 `SetCursorPos`（`_me_start_move`／`_glide_to`）**一个输入事件都不产生**（336 次 → 0）
        ⇒ 视角类操作里表现为「点得到、视角转不动」。
      · 只有它是 **delta 语义**；且**无屏幕边界**（绝对坐标转两下就贴边，增量可以一直转）。
      · 本文件本来就在用 `mouse_event`（点击/滚轮）⇒ 与 `_mouse_event_scroll` 同形，纯接入。
    ⚠ 官方文档（MOUSEINPUT）：相对位移受**指针速度与两个阈值**影响，**可能被放大约 4 倍**
      ⇒ 回执只报「请求位移」，**不承诺视角转了多少**（不去改系统设置——那是红线）。
    `nocoalesce=True` 时附加 `MOUSEEVENTF_MOVE_NOCOALESCE`（只影响 `WM_MOUSEMOVE` 消息合并，
      **不影响 raw input**）；默认 False。"""
    flags = MOUSEEVENTF_MOVE | (MOUSEEVENTF_MOVE_NOCOALESCE if nocoalesce else 0)
    return user32_si.mouse_event(flags, int(dx), int(dy), 0, 0)


def _rel_move_paced(dx, dy, speed, interval_ms=None, nocoalesce=False, sleep_fn=None,
                    human=False, drag=False):
    """**相对位移的"调速/人手化"**：把一次增量切成小块、按时间铺开送出。

    人手化档（默认）：把它当成"从 (0,0) 位移到 (dx,dy)"，用 `_human_trace()` 生成点位
      （**那个函数一字节未动**），再按点间差发 raw delta；整数累加
      ⇒ **末块精确落在 (dx,dy)**（与下面直线铺时同一条契约）。**默认路径**。
      `human=False`（老路）＝原直线切块铺时，**保留不动**；快速档 `precise=true` 的"一次直发"
      **不走本函数**（显式 opt-in）。

    为什么需要：`mouse_event(MOUSEEVENTF_MOVE, dx, dy)` 是**瞬时增量**，OS 层面**没有"用多久"
      这个参数**（`human`/`speed` 改的是 `_glide_to` 的轨迹时长，对相对位移天然不适用）。
      而相对位移的"快/慢"＝**单位时间转多少** ⇒ 拆成 n 块、块间留 `interval_ms` 即可。
      这就是序列里手写 `{"move":[8,0],"times":12,"gap_ms":16}` 的**"按速度自动版"**。

    参数：`speed`（像素/秒，人影档上限已由调用方夹好）；`interval_ms` 默认 `REL_PACING_INTERVAL_MS`（≈一帧）；
      `sleep_fn` 传序列的 `_sleep` ⇒ **受总超时与 ESC 取消约束**（序列里必须传）。
    安全：总时长仍受 `HUMAN_SEG_MAX_SEC` 防呆 —— 超了就把**有效速度**抬到"这段最多 5 秒"，并在 note 里写明。
    返回：steps/step_px/interval_ms/elapsed/speed_requested/speed_effective/pace_note。
    """
    _sleep = sleep_fn or time.sleep
    dx, dy = int(dx), int(dy)
    dist = math.hypot(dx, dy)
    if human:
        # 人手化相对位移（默认路径）：轨迹算法＝`_human_trace`。
        req = max(1.0, float(speed))
        _lo, _hi = (DRAG_MIN_SEC_HUMAN, DRAG_MAX_SEC_HUMAN) if drag else (HUMAN_SEG_MIN_SEC, HUMAN_SEG_MAX_SEC)
        _dur = max(_lo, min(_hi, dist / req)) if dist > 0 else _lo
        pts, _meta = _human_trace(0, 0, dx, dy, _dur)
        t0 = time.perf_counter()
        sx = sy = 0
        for (tx, ty, d) in pts:
            cxx, cyy = int(tx) - sx, int(ty) - sy
            if cxx or cyy:                     # 整数累加：末块精确落在 (dx,dy)，不漂移
                _mouse_event_move_rel(cxx, cyy, nocoalesce=nocoalesce)
                sx, sy = int(tx), int(ty)
            _sleep(d)
        _n = max(1, len(pts))
        return {"steps": _n, "step_px": round(dist / _n, 2),
                "interval_ms": round(_dur / _n * 1000.0, 1),
                "elapsed": round(time.perf_counter() - t0, 3),
                "speed_requested": req, "speed_effective": round(req, 1), "pace_note": "",
                "humanized": True, "duration": round(_dur, 3),
                "jitter": {k: _meta.get(k) for k in
                           ("segs", "burst_segs", "pauses", "bow_px", "und_px",
                            "settle_steps", "overshoot_px")}}
    interval_ms = float(REL_PACING_INTERVAL_MS if interval_ms is None else interval_ms)
    req = max(1.0, float(speed))
    eff, note = req, ""
    if dist > 0 and dist / req > HUMAN_SEG_MAX_SEC:
        eff = dist / HUMAN_SEG_MAX_SEC
        note = ("⚠ 请求 %g px/s ⇒ 这段要走 %.1fs，超过单段防呆上限 %gs ⇒ 有效速度抬到 %g px/s"
                % (req, dist / req, HUMAN_SEG_MAX_SEC, eff))
    step_px = max(1.0, eff * interval_ms / 1000.0)
    n = max(1, int(math.ceil(dist / step_px))) if dist > 0 else 1
    t0 = time.perf_counter()
    sx = sy = 0
    for i in range(1, n + 1):
        tx, ty = int(round(dx * i / n)), int(round(dy * i / n))
        cxx, cyy = tx - sx, ty - sy
        if cxx or cyy:                       # 整数累加：末块精确落在 (dx,dy)，不漂移
            _mouse_event_move_rel(cxx, cyy, nocoalesce=nocoalesce)
            sx, sy = tx, ty
        if i < n:
            _sleep(interval_ms / 1000.0)
    return {"steps": n, "step_px": round(step_px, 2), "interval_ms": interval_ms,
            "elapsed": round(time.perf_counter() - t0, 3),
            "speed_requested": req, "speed_effective": round(eff, 1), "pace_note": note}


def _mouse_event_click_here(button="left", click_count=1, hold_ms=30, gap_ms=40):
    """**在当前光标处**按下＋抬起（**不移动光标**）—— 不移动光标的「就地点击」（挖掘/攻击/放置）。

    写法与 `_mouse_event_click` 里那两行 `mouse_event(DOWN/UP)` 一致，
    但去掉两样**就地点击不需要**的东西：① 接近路 `_glide_to`（那会 `SetCursorPos` 动光标）
    ② 人手化随机等待（就地点击要**确定性**，不要 50~200ms 的随机按住）。
    支持 left/right/middle；middle 沿用同批新增的 `MOUSEEVENTF_MIDDLEDOWN/MIDDLEUP` 常量。
    ⚠ 只支持 left/right/middle（其它 ⇒ 抛错，不静默降级）。"""
    pair = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}.get(str(button or "left"))
    if pair is None:
        raise ValueError("就地点击只支持 button=left / right / middle（收到 %r）" % (button,))
    down, up = pair
    n = max(1, int(click_count or 1))
    # ⚠ 下面两处**刻意用裸 `time.sleep`**（不接 `wait_with_cancel`）：按住只有 3~200ms，要的是**确定性**
    #   （游戏按帧采样，随机化或 0.15s 粒度的分片睡眠只会削掉精度）；取消在**步边界**就会生效。
    held = max(0.003, float(hold_ms or 30) / 1000.0)
    gap = max(0.0, float(gap_ms or 40) / 1000.0)
    for i in range(n):
        user32_si.mouse_event(down, 0, 0, 0, 0)
        _held_mark_btn(button, True)
        try:
            time.sleep(held)
        finally:
            # 按下→抬起成对（异常也抬）
            user32_si.mouse_event(up, 0, 0, 0, 0)
            _held_mark_btn(button, False)
        if i + 1 < n and gap:
            time.sleep(gap)


def _seq_parse_seconds(v):
    """把「裸值停顿」解析成秒：`0.5`｜`"0.5"`｜`"0.5s"`｜`"0.5秒"`｜`"500ms"`｜`"500毫秒"`。
    认不出 ⇒ 抛错（**绝不静默当成 0**）。"""
    if isinstance(v, bool):
        raise ValueError("停顿不接受 true/false")
    if isinstance(v, (int, float)):
        return max(0.0, float(v))
    s = str(v).strip().lower().replace(" ", "")
    for suf, div in (("ms", 1000.0), ("毫秒", 1000.0), ("s", 1.0), ("秒", 1.0)):
        if s.endswith(suf) and len(s) > len(suf):
            try:
                return max(0.0, float(s[:-len(suf)]) / div)
            except ValueError:
                break
    try:
        return max(0.0, float(s))          # 裸数字字符串 ＝ 秒
    except ValueError:
        raise ValueError("认不出这个停顿：%r（写法：0.5 ／ \"0.5s\" ／ \"500ms\"）" % (v,))


# ══════════════════════════════════════════════════════════════════════════════
# 序列的**嵌套块**与**多轨同时**
#   鼠标与键盘是两条独立轨道，可以并行。
#   两条实现原则（都是"最小改动"）：
#     ① **嵌套块 → 先展开成扁平步**（`_seq_expand`）⇒ **执行器一行未改**、老形状行为逐字不变；
#        `repeat`/`gap_ms` 借用现成的「裸值＝停顿」语义（gap_ms 秒化后当裸值插进去）。
#     ② **多轨 → 每轨一个线程跑同一个扁平执行器**：两轨**不共享任何状态**（各自的 `pressed`
#        账本、各自的预算与取消轮询）⇒ **一把锁都不需要**；合并在回执层做。
#        老形状（数组）**不启线程**，走的还是原来那条路。
#   边界：老路径（数组）＝**逐字不变**。
# ══════════════════════════════════════════════════════════════════════════════
_SEQ_MAX_DEPTH = 5          # 嵌套深度上限（防递归失控）
_SEQ_MAX_STEPS = 2000       # 展开后总步数上限


def _seq_expand(items, depth=0):
    """把**嵌套块**展开成扁平步列表（执行器不用改一行）。

    块的写法：`{"repeat": 3, "gap_ms": 16, "steps": [ ...子步... ]}`
      · `steps` 必填（子步数组，可再嵌套）；子步可以是**任何既有步型**，也可以是**一个时间值**；
      · `repeat`（默认 1）＝整块重复几遍；`gap_ms`（默认 0）＝**遍与遍之间**的间隔（毫秒）；
      · 时间值在**块内**（子步里直接写 0.5 ／ 「500ms」）与**块之间**（块外、或 gap_ms）都生效。
    判据：展开后总步数超过 `_SEQ_MAX_STEPS`、或深度超过 `_SEQ_MAX_DEPTH` ⇒ **抛错**（绝不静默截断）。
    """
    out = []
    for st in items:
        if isinstance(st, dict) and isinstance(st.get("steps"), list):
            if depth + 1 > _SEQ_MAX_DEPTH:
                raise ValueError("嵌套太深（上限 %d 层）" % _SEQ_MAX_DEPTH)
            try:
                reps = int(st.get("repeat", 1) or 1)
            except Exception:
                raise ValueError("块里的 repeat 必须是整数，收到：%r" % (st.get("repeat"),))
            if reps < 1:
                reps = 1
            try:
                gap = max(0.0, float(st.get("gap_ms", 0) or 0) / 1000.0)
            except Exception:
                raise ValueError("块里的 gap_ms 必须是数字（毫秒），收到：%r" % (st.get("gap_ms"),))
            body = _seq_expand(st["steps"], depth + 1)
            for _r in range(reps):
                out.extend(body)
                if gap and _r + 1 < reps and body:
                    out.append(gap)          # ← 裸值＝停顿（沿用现成语义）
        else:
            out.append(st)
        if len(out) > _SEQ_MAX_STEPS:
            raise ValueError("展开后步数超过上限 %d（现 %d）——序列写太大了"
                             % (_SEQ_MAX_STEPS, len(out)))
    return out


def _run_input_sequence_flat(steps, timeout_ms=60000, relative=True, human=False, speed=None,
                             precise=False, abort_check=None, progress=None, ime_ascii=True, ime_mode="temp",
                             unicode_text=False):
    """**输入序列**（短序列／一次性宏）：把「同时按键盘 ＋ 动鼠标」按时间轴一次跑完。

    ✓ 形参 `ime_ascii`（**带默认值 ⇒ 老调用逐字不变**）：**纯 ASCII 文字打字前要不要
      走输入法守卫**（把目标窗切英文、打完恢复）。默认 **True**＝与单发 `send_keystrokes` 同规则；
      **要故意用输入法打字（拼音→汉字）就传 False**——这是序列级默认，步内仍可逐条覆盖。

    ✓ 形参 `abort_check`（**带默认值 ⇒ 老调用逐字不变**）：外部中止位的可调用对象，
      每步开头查一次，为真 ⇒ 当场停手（`aborted="abort"`）并在 finally 里抬键。`sequence` 的后台队列
      就是靠它实现「**覆盖**」与「停」；**传 None（默认）时这一支完全不进**，老路一个字节没变。

    ✓ 形参 `progress`（同样**带默认值 ⇒ 老调用逐字不变**）：每步开头回调一次
      `progress(i, n, st)`，给指示条报「在跑第几步／哪一步」用。**传 None（默认）时完全不进**；
      回调抛异常一律吞掉——**不许影响执行**。

    短序列（实时操作）与长序列（一次性宏）＝**同一条时间轴的两种长度**：
      短序列＝边看边发；长序列＝宏 —— 交给这里一次跑完，中间不参与，跑完回一张截图。

    **步**（三种元素靠**类型**分开，不靠猜：对象＝动作／数字＝停顿／字符串＝文字输入）：
      {"key_down": ["w","shift"]}                     按下不放 —— 之后一直按着（「同时」靠它）
      {"key_up":   ["w"]}                             松开
      {"key":      ["e"]}                             按一下（按下→抬起，间隔 ms 可调）
      {"hold":     ["space"], "ms": 120}              按住 N ms 再松
      {"combo":    ["ctrl","c"], "ms": 60}            组合键：按给定顺序按下 → 等 → **逆序**抬起
      {"move":     [dx,dy], "times": 1, "gap_ms": 0}  移动；步内可写 "relative": true/false 覆盖默认
      {"click":    "left"|"right", "count": 1, "hold_ms": 30}   在当前光标处点击（**不移动光标**）
      {"wheel":    clicks}                            滚轮（正上负下）
      {"wait_ms":  n}                                 等 n 毫秒
      **数字 0.5**                                      ← **停顿 0.5 秒**（时间参数的**唯一**写法）
      **字符串 "{ctrl+c}" ／ "w" ／ "hello" ／ "0.5"**    ← **文字输入**（同 send_shortcut 的 {} 语言）
                                                          ——**字符串永远不是时间**：写 "0.5" 就**打这三个字**（不当停顿）；回执会留痕
                                                          ★纯 ASCII 的字符串**自动走输入法守卫**：
                                                            打之前把该窗切英文、打完恢复，免得 `r3` 被中文输入法吃掉
    ⇒ 于是可以随意组合：`[{"move":[10,0]}, 0.5, {"click":"left"}, "{ctrl+e}", 0.3, {"move":[-10,0]}]`

    **新增步型（与鼠标族/键盘族已实现的能力对齐）**——全部"不写就是老行为"：
      {"drag": [x,y]|[dx,dy], "from":[x,y]?, "button":"left|right", "duration":s?, "relative":bool?,
       "human":bool?, "speed":px/s?, "grip_ms":130, "settle_ms":90, "nocoalesce":bool?}
                    拖拽（对齐工具级 `mouse_drag`，但抓握/落定**确定性**、无随机停顿）
      {"move": [dx,dy], "duration": s, "nocoalesce": bool}     ← 补上 `mouse_move` 这两个能力
      {"click": "left|right|middle", "count":n, "hold_ms":ms, "gap_ms":ms}   ← 补 middle
      {"wheel": clicks, "mode": "foreground|background", "x":…, "y":…}       ← 补后台通道
      {"text": "…", "interval":s?, "typing_interval":s?, "human":bool?, "speed":字/秒?,
       "mode":"foreground|background", "press_enter":bool?, "ime_ascii":bool?, "ime_mode":"temp|perm|off"?}
                    文字输入的完整形态（对齐 `send_keystrokes`／`send_shortcut`：节奏/后台/回车/输入法守卫）
                     ★`ime_ascii`＝**默认开**：纯 ASCII 文本注入前把该窗切英文、
                       打完恢复。不守的实测后果（真机当场）：目标窗中文态下 `r3 alpha ` 打出来是
                       「加 alpha 加 beta」——`r3` 被当拼音吃掉了。要关就显式写 `"ime_ascii": false`；
                       `mode=background` 走 WM_CHAR、**不经过输入法**，那条不守。
                    ★`speed`＝**本步的打字速度（字/秒）**，与鼠标那个同形：只有 `"human": true` 可用，
                      夹在 [0.5, 人影档上限 10]；超出夹住**并在回执里报**；逐字间隔**带对数正态抖动**。
      {"mouse_pos": true}                          只读：记下当前光标位置（对齐 `get_mouse_position`）
      {"shot": "window|fullscreen", "rect":"x,y,w,h"?, "save_path":"…"?}
                    **执行途中截图**：截下并登记进最新帧 ⇒ 收尾附图走复用通道、不重复截

    参数：`timeout_ms`（默认 60000，可传更大）＝**总超时**（防卡死；**不是「时长参数」**——
      时长由你放了多少步决定）；`relative`（默认 True）＝步里没写 relative 时用哪个
      （**True＝增量**；False＝绝对，走老路 `_glide_to` 人手化轨迹）。
      `human`（默认 False）＝**只对绝对步有效**的鼠标档位（与 `mouse_move`／`mouse_drag`／`mouse_click`
      同形：True＝人影档 ≈2500 px/s，False＝快档 ≈4000 px/s）；**步内可写 `"human": true` 单独覆盖**。
      `speed`（像素/秒，**只有 human=true 时可传**）＝人影档内自选速度（例 300 慢／600 正常／1000 快），
      上限＝人影档上限；**步内可写 `"speed": 600` 单独覆盖**。
      ★**相对步同样能调速**：给了 `speed` 且没显式写 `times`/`gap_ms` ⇒ 按速度**自动切块铺时间**
      （块间≈一帧，睡眠走本序列的 `_sleep` ⇒ 受总超时/ESC 约束）；显式写 `times`/`gap_ms` 就是手动版。
      `ime_ascii`（默认 True）＝**打字前的输入法守卫**（纯 ASCII 文字注入前把该窗切英文、打完恢复）：
      与单发 `send_keystrokes` 同规则；**要故意用输入法打字（拼音→汉字）就传 False**；步内可逐条覆盖。
      `ime_mode`（`temp`默认／`perm`／`auto`／`off`）＝**输入法怎么切**：**temp＝临时切换**（打完还原成切换前那个模式）｜**perm＝永久切换**（切了不还原）｜**auto＝读着判断**（读得到中文态才切、读不到就一个字不碰；切了按 temp 规则还原）｜**off＝一个字都不碰**。★**只做「防被吃」一个方向**（`cn`＝切中文会混乱，已撤掉）。
      步内可写 `"ime_mode": "perm"` 覆盖。
    安全：① 超时／ESC／异常 ⇒ **立刻停**，并把本次按下的键**全部抬起**
      （否则卡住的 W 会让人物一直往前走）；② 回执只报「发了什么」；**结果看附图**。"""
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps 必须是非空数组，形如 [{\"key_down\": [\"w\"]}, \"0.5s\", {\"click\": \"left\"}]")
    t0 = time.perf_counter()
    budget = max(0.05, float(timeout_ms) / 1000.0)
    pressed, released, counts, paces = set(), [], {}, []
    typed, timeish = [], []          # 字符串＝文字输入（打了什么 / 其中像时间的）
    shots, poses, texts = [], [], []  # 途中截图账 / 只读光标位 / text 步的输入账
    matches, waits_cond, transfers = [], [], []   # 模板匹配步 / 条件等待步 / 搬移步的账
    hovers, focuses, sysnotes, wins_snaps = [], [], [], []   # 悬停 / 聚焦 / 系统类(set_window/open) / 窗口清单
    kbd_notes = []                     # 打字速度被夹的记账（"夹了必须报"）——由回执打出来
    done, moved, waits, aborted = 0, [0, 0], 0.0, None

    def _left():
        return budget - (time.perf_counter() - t0)

    def _lst(v):
        return list(v) if isinstance(v, (list, tuple)) else [v]

    def _pair(v):
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return int(v[0]), int(v[1])
        raise ValueError("move 必须是 [dx, dy] 两项，收到：%r" % (v,))

    def _sleep(sec):
        nonlocal waits, aborted
        want = max(0.0, float(sec))
        room = max(0.0, _left())
        if want > room:
            # 被总超时截断时**必须如实记成超时中止**——
            #   否则"要等 5s 只等了 0.2s"会被回执说成「✓ 跑完」，等于静默缩短。
            aborted = aborted or "timeout"
        sec = min(want, room)
        if sec > 0:
            # 复用现有件 `wait_with_cancel`：它分片睡眠并轮询
            #   ESC（interval 默认 0.15s）⇒ **长停顿中途也能取消**；裸 `time.sleep` 做不到。
            _t0 = time.perf_counter()
            _cancelled = not wait_with_cancel(sec, interval=min(0.15, max(0.02, sec)))
            waits += time.perf_counter() - _t0
            if _cancelled:
                aborted = aborted or "esc"

    try:
        for i, st in enumerate(steps):
            if _left() <= 0:
                aborted = "timeout"
                break
            if poll_cancel():
                aborted = "esc"
                break
            if progress:                 # 给指示条报进度（None ⇒ 这一支完全不进）
                try:
                    progress(i, len(steps), st)
                except Exception:
                    pass
            # （后台队列专用）：**外部中止位**——`abort_check is None` 时这一支完全不进
            #   ⇒ 老调用（不传新形参）行为逐字不变。`sequence action=overwrite`／`stop` 就靠它在
            #   **步边界**停手（不会把一次 move/一次停顿劈开；停下后 finally 照旧抬键）。
            if abort_check is not None and abort_check():
                aborted = "abort"
                break
            # ── 类型区分：对象＝动作／数字＝停顿／字符串＝文字输入 ──
            #   · **数字** = 停顿（秒）——时间参数的唯一写法
            #   · **字符串** = **文字输入**（走现成件 `_parse_sendinput_text`，与 send_shortcut 同形的
            #     {} 语言）；**它不再是时间** —— 长得像时间的字符串（0.5s/500ms/2秒）当场报错，
            #     绝不静默当文本打出去（宁可报错，也不要"以为是等一等、结果真打了一串字"）。
            #   · **对象** = 动作 / 轨 / 块
            if isinstance(st, (int, float)) and not isinstance(st, bool):
                _sleep(float(st))
                counts["wait"] = counts.get("wait", 0) + 1
                done += 1
                continue
            if isinstance(st, str):
                _s = st.strip()
                if not _s:
                    counts["wait"] = counts.get("wait", 0) + 1
                    done += 1
                    continue
                # 这一支**只干文字输入**：
                #   · `"0.5"` 这种**数字文本照打**（既不变成停顿、也不报错）；
                #   · 「看着像时间」的只**留痕**（进回执的 text_looks_like_time），**不改语义**。
                if _seq_looks_like_time(_s):
                    timeish.append(_s)
                typed.append(_s)
                # 直发非 ASCII 默认不允许（与工具级同一规则）——序列里也要**当场报错**，
                #   绝不静默丢字（`_send_combo` 对"本布局打不出"的字符是跳过的）。
                if not unicode_text and not _text_is_ascii(_s):
                    raise ValueError(
                        "第 %d 步字符串含非 ASCII（直发默认不允许）：要发就传 unicode_text=true"
                        "（序列级，或把该步写成 {\"text\": ..., \"unicode_text\": true}）；"
                        "中文也可以走 ime_mode ＋ 拼音。" % (i + 1))
                # 文字输入走现成件 `_parse_sendinput_text`（普通字符逐字 ＋ {} 块的组合键）；
                # interval/typing_interval 传 0 ⇒ **不打打字节奏**（时间完全由序列里的数字决定，
                # 也不让这条通道偷偷睡过去而绕过总超时）。
                # **输入法守卫**：纯 ASCII 打之前把该窗切英文、
                #   打完恢复；不守的实测后果：目标窗中文态下 `r3 alpha ` 打出来是「加 alpha 加 beta」
                #   （`r3` 被输入法当拼音吃掉）。与另两条打字路共用同一个闸，不再各写一份。
                _smode_ime = "off" if not ime_ascii else str(ime_mode or "temp").lower()
                _sguard = _ime_guard_typed(_s, mode=_smode_ime) if _smode_ime != "off" else False
                # 混排：非 ASCII 走 Unicode 注入、ASCII 段走键盘 ⇒ 先切英文（同款的混排闸）
                if not _sguard and unicode_text and not _text_is_ascii(_s):
                    _sguard = _ime_mixed_guard(mode=_smode_ime)
                try:
                    _parse_sendinput_text(_s, interval=0.0, typing_interval=0.0, unicode_text=unicode_text)
                finally:
                    if _sguard:
                        _ime_guard_restore()
                counts["keys_text"] = counts.get("keys_text", 0) + 1
                done += 1
                continue
            if not isinstance(st, dict) or not st:
                raise ValueError("第 %d 步既不是动作对象、也不是时间值：%r" % (i + 1, st))
            if "key_down" in st:
                for k in _lst(st["key_down"]):
                    _si_key_down(str(k)); pressed.add(str(k))
                kind = "key_down"
            elif "key_up" in st:
                for k in _lst(st["key_up"]):
                    _si_key_up(str(k)); pressed.discard(str(k))
                kind = "key_up"
            elif "key" in st:
                ks = [str(k) for k in _lst(st["key"])]
                _si_key_down(*ks)
                _sleep(float(st.get("ms", 40) or 40) / 1000.0)
                _si_key_up(*ks)
                kind = "key"
            elif "hold" in st:
                # 按下→睡→抬起：两步与 `_si_hold_key` 相同，但睡眠走 `_sleep`
                # （只有它同时受总超时约束、且中途可被 ESC 取消；`_si_hold_key` 内部是裸 sleep）。
                ks = [str(k) for k in _lst(st["hold"])]
                _si_key_down(*ks)
                _sleep(float(st.get("ms", 100) or 100) / 1000.0)
                _si_key_up(*ks)
                kind = "hold"
            elif "combo" in st:
                ks = [str(k) for k in _lst(st["combo"])]
                _si_key_down(*ks)
                _sleep(float(st.get("ms", 60) or 60) / 1000.0)
                _si_key_up(*reversed(ks))          # 逆序抬起（与 _send_combo 的收尾顺序一致）
                kind = "combo"
            elif "move" in st:
                mx, my = _pair(st["move"])
                rel = bool(st.get("relative", relative))
                # 两个与工具级 `mouse_move` 对齐的参数：
                #   · `duration`——**仅绝对步**有意义（0=瞬移、>0=指定秒数）；相对步没有"时长"概念，
                #     与工具级同规则**忽略**（`mouse_move` 的描述里就是这么写的），不静默改语义。
                #   · `nocoalesce`——**仅相对步**有意义（附加 MOVE_NOCOALESCE：官方语义只抑制
                #     WM_MOUSEMOVE 消息的合并，**不影响 raw input**）。
                _dur = st.get("duration")
                _noco = bool(st.get("nocoalesce", False))
                if _dur is not None and rel:
                    _dur = None
                # 两档（与 mouse_move/mouse_drag/click 同形）：
                #   **绝对步**＝人手化轨迹（`_glide_to`，human 决定快/人影档）；**相对步**＝raw delta。
                _hm = bool(st.get("human", human))
                # `speed`（像素/秒，只有人影档可传）：步内可覆盖默认。
                #   · 绝对步 ⇒ 走 `_glide_to(..., speed=)`（轨迹时长按速度算）
                #   · **相对步 ⇒ 走 `_rel_move_paced`（切块铺时间）**——相对位移同样要能调速。
                _spd = st.get("speed", speed)
                times = max(1, int(st.get("times", 1) or 1))
                gap = max(0.0, float(st.get("gap_ms", 0) or 0) / 1000.0)
                # 两档（人手化档默认走 `_human_trace()` 轨迹；
                #   快速档可传 precise=true 要"直接精确、不走抖动"；**人影档不许 precise**）：
                _pc = bool(st.get("precise", precise))
                if rel and _pc and _hm:
                    raise ValueError("人影档不允许 precise=true：人影档必须走人手化轨迹（抖动）；"
                                     "要精确/瞬移请用快速档（human=false）")
                if rel:
                    _spd_px = (_mouse_speed(_hm, False, _spd)[0] if _spd is not None
                               else _mouse_speed(_hm, False, None)[0])
                    if _pc:
                        # 快速档·精确：原样（一次直发；显式 times/gap_ms ⇒ 手动 raw 循环）
                        kind = "move_rel"
                        for _n in range(times):
                            if _left() <= 0:
                                aborted = "timeout"
                                break
                            _mouse_event_move_rel(mx, my, nocoalesce=_noco)
                            moved[0] += mx; moved[1] += my
                            if gap and _n + 1 < times:
                                _sleep(gap)
                    else:
                        # ★默认（两档都是）：相对位移也走人手化轨迹（切块＋抖动＋弧线），末块精确落在 (dx,dy)；
                        #   睡眠走 `_sleep` ⇒ 受总超时与 ESC 取消约束。
                        kind = "move_rel_humanized"
                        for _n in range(times):
                            if _left() <= 0:
                                aborted = "timeout"
                                break
                            _pm = _rel_move_paced(mx, my, _spd_px, nocoalesce=_noco,
                                                  sleep_fn=_sleep, human=True)
                            moved[0] += mx; moved[1] += my
                            paces.append(_pm)
                            if gap and _n + 1 < times:
                                _sleep(gap)
                else:
                    kind = "move_abs"
                    for _n in range(times):
                        if _left() <= 0:
                            aborted = "timeout"
                            break
                        _glide_to(mx, my, duration=(None if _dur is None else float(_dur)),
                                  human=_hm, precise=_pc,
                                  speed=(None if _spd is None else float(_spd)))   # 绝对＝老路（★两档＋可调速度＋时长）
                        if gap and _n + 1 < times:
                            _sleep(gap)
                if aborted:
                    break
            elif "click" in st:
                # 对齐工具级 `mouse_click`：`at`＝先走到这个点再点（图内坐标，_axis_apply 已换算）；
                #   `dwell_ms`＝落点后、按下前的驻留（远控/串流必给，否则第一下会被吞）。
                _cat = st.get("at")
                if _cat is not None:
                    _cax, _cay = _seq_pair(_cat, "click.at")
                    _cahm = bool(st.get("human", human))
                    _capc = bool(st.get("precise", precise))
                    _caspd = st.get("speed", speed)
                    if _capc and _cahm:
                        raise ValueError("click 步：人影档不允许 precise=true")
                    _glide_to(_cax, _cay, human=_cahm,
                              speed=(None if _caspd is None else float(_caspd)), precise=_capc)
                _cbtn = st["click"]
                if isinstance(_cbtn, dict):
                    raise ValueError(
                        "click 步写法不对：`at`／`count`／`hold_ms`／`dwell_ms` 都是 **click 的同级键**，"
                        '不是 click 的子对象 —— 正确写法：{"click": "left", "at": [640, 480], "count": 2}'
                        "（at＝先走到该点再点，图内坐标；count＝连点次数）。收到 %r" % (_cbtn,))
                _cdwell = max(0, min(2000, int(st.get("dwell_ms", 0) or 0)))
                if _cdwell > 0:
                    _sleep(_cdwell / 1000.0)
                _mouse_event_click_here(str(_cbtn or "left"),
                                        int(st.get("count", 1) or 1),
                                        float(st.get("hold_ms", 30) or 30),
                                        float(st.get("gap_ms", 40) or 40))
                kind = ("click_at" if _cat is not None else "click")
            elif "drag" in st:
                # **拖拽**——工具级 `mouse_drag` 那套能力的序列版。内部两步与它一致
                #   （抓取点 `_glide_to` → DOWN → 按住那段再 `_glide_to(drag=True)` → UP），
                #   **不调它本人**（套工具级函数每步要多付
                #   `_require_focus_target`／`_ensure_foreground`／`_show_indicator` 三笔调度开销）。
                #   与 `mouse_drag` 的**两处刻意差异**：
                #     ① `_human_pause` 的**随机**抓握/落定（20~200ms）⇒ 换成**确定性** `grip_ms`/
                #        `settle_ms`（默认取原中位 130／90）——相对路径要确定性；
                #     ② 睡眠走本序列的 `_sleep` ⇒ 受总超时／ESC／队列中止位约束。
                dx2, dy2 = _pair(st["drag"])
                _frm = st.get("from")
                _brel = bool(st.get("relative", relative))
                _bhm = bool(st.get("human", human))
                _bspd = st.get("speed", speed)
                _bnoco = bool(st.get("nocoalesce", False))
                _bdur = None if _brel else st.get("duration")
                # 两档：快速档可 precise=true（不走抖动/可瞬移）；**人影档不许 precise**。
                _bpc = bool(st.get("precise", precise))
                if _brel and _bpc and _bhm:
                    raise ValueError("人影档不允许 precise=true：人影档必须走人手化轨迹（抖动）；"
                                     "要精确/瞬移请用快速档（human=false）")
                if _frm is not None:
                    # `from` **绝对步与相对步都生效**（原来对相对步**静默忽略**——本线大忌）。
                    #   语义＝先用现成的绝对移动走到抓取点，再按住做（相对或绝对）拖拽；
                    #   与工具级 `mouse_drag` 的"先走到起点"一致。不传 `from` ⇒ 完全不动光标。
                    _fx, _fy = _pair(_frm)
                    _glide_to(_fx, _fy, human=_bhm, precise=_bpc,
                              speed=(None if _bspd is None else float(_bspd)))   # 人手化走到抓取点
                # **不认识的键要抛错、不许静默降级**——
                #   原来写成 `== "right" ? right : left`，于是 `button="middle"` 会**悄悄当左键拖**
                #   （工具级 `mouse_drag` 也是这个静默形状；别顺手改它）。
                _bbtn = str(st.get("button") or "left")
                if _bbtn not in ("left", "right"):
                    raise ValueError("drag 步的 button 只认 left / right（收到 %r）" % (st.get("button"),))
                _bt_down, _bt_up = ((MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if _bbtn == "right"
                                    else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
                user32_si.mouse_event(_bt_down, 0, 0, 0, 0)
                _held_mark_btn(_bbtn, True)
                try:
                    _sleep(float(st.get("grip_ms", 130) or 0) / 1000.0)          # 抓稳（确定性，原中位 130）
                    if _brel:
                        if _bpc:
                            _mouse_event_move_rel(dx2, dy2, nocoalesce=_bnoco)   # 快速档·精确：原样一次直发
                        else:
                            _spd2 = (_mouse_speed(_bhm, True, _bspd)[0] if _bspd is not None
                                     else _mouse_speed(_bhm, True, None)[0])
                            _pm = _rel_move_paced(dx2, dy2, _spd2, nocoalesce=_bnoco,
                                                  sleep_fn=_sleep, human=True, drag=True)
                            paces.append(_pm)
                        moved[0] += dx2; moved[1] += dy2
                    else:
                        _glide_to(dx2, dy2, duration=(None if _bdur is None else float(_bdur)),
                                  human=_bhm, speed=(None if _bspd is None else float(_bspd)),
                                  drag=True, precise=_bpc)     # ★拖拽档上限（1600）
                    _sleep(float(st.get("settle_ms", 90) or 0) / 1000.0)         # 落定（确定性，原中位 90）
                finally:
                    # ★中止/异常也**必须抬键**——与"卡住的 W"同族：按住不放的鼠标键会把整个桌面拖住
                    #   （比卡键更坏）。这是本分支自带的小 finally，不依赖外层那个只抬键盘的账本。
                    user32_si.mouse_event(_bt_up, 0, 0, 0, 0)
                    _held_mark_btn(_bbtn, False)
                kind = "drag_rel" if _brel else "drag_abs"
            elif "wheel" in st:
                # 对齐工具级 `scroll` 的自动分流：`mode=background` ⇒ 走后台
                #   PostMessage 那条现成路（`_post_scroll`）；不给 mode ＝ **前台老路，逐字不变**。
                _wmode = str(st.get("mode") or "foreground").lower()
                if _wmode == "background":
                    _post_scroll(_resolve_target(), int(st["wheel"] or 0),
                                 st.get("x"), st.get("y"))
                    kind = "wheel_bg"
                else:
                    _mouse_event_scroll(int(st["wheel"] or 0))
                    kind = "wheel"
            elif "text" in st:
                # **文字输入的完整形态**——与工具级 `send_keystrokes`／`send_shortcut` 对齐
                #   （打字节奏 / 后台通道 / 回车 / 输入法 ASCII 守卫），复用
                #   `_do_send_keystrokes` 的内部通道，不调它本人（那会带上 UIA 树遍历与每次 `_ensure_foreground`）。
                #   与**字符串步**的分工（两个都保留，语义不重叠）：字符串步＝"原样打出去、最快、前台"；
                #   本步＝"要节奏/要后台/要回车/要输入法守卫"时才用。
                _tv = str(st.get("text") or "")
                _tuni = bool(st.get("unicode_text", unicode_text))   # 本步是否允许直发非 ASCII
                if _tv and not _tuni and not _text_is_ascii(_tv):
                    raise ValueError(
                        "第 %d 步 text 含非 ASCII（直发默认不允许）：该步写 \"unicode_text\": true，"
                        "或序列级传 unicode_text=true；中文也可以走 ime_mode ＋ 拼音。" % (i + 1))
                _thuman = bool(st.get("human", False))
                _tint = st.get("interval")
                _tyint = st.get("typing_interval")
                _tcps = st.get("speed")          # 本步的**打字速度**（字/秒）——与鼠标那个同形
                if _tcps is not None:
                    # 只有人影档可调；超出**夹住并把夹的结果记进回执**；快档传它 ⇒ 当场抛错（与鼠标规则一致）
                    _kt, _tkn = _kbd_gap_speed(_thuman, _tcps)
                    _tyint = _kt          # 传**整条抖动带**（lo,hi,中位）⇒ 底层走 `_human_pause`，**有波动**
                    if _tkn:
                        kbd_notes.append("第 %d 步打字：%s" % (i + 1, _tkn))
                if _thuman:                      # 人影档打字：与 `send_keystrokes(human=true)` 同规则 ≈90ms/字
                    _tint = 0.45 if _tint is None else float(_tint)
                    if _tyint is None:
                        _tyint = 0.09
                else:                            # 不给参数＝沿用字符串步的"最快"规则（时间只由序列里的数字决定）
                    _tint = 0.0 if _tint is None else float(_tint)
                    _tyint = 0.0 if _tyint is None else float(_tyint)
                _tmode = str(st.get("mode") or "foreground").lower()
                if _tmode not in ("foreground", "background"):
                    raise ValueError("text 步的 mode 只认 foreground / background（收到 %r）" % (st.get("mode"),))
                # **默认就守**——与单发
                #   `send_keystrokes` 同规则；要关就显式写 `"ime_ascii": false`。后台通道
                #   （`mode=background`）**不守**：那条走 `_post_text` 的 WM_CHAR，**根本不经过输入法**，
                #   去切输入法状态纯属多余的副作用。
                _tmode_ime = "off" if st.get("ime_ascii", ime_ascii) is False \
                    else str(st.get("ime_mode", ime_mode) or "temp").lower()
                _tguard = False
                _tauto_done = False
                if _tmode_ime != "off" and _tmode != "background" and _tv:
                    _tguard = _ime_guard_typed(_tv, mode=_tmode_ime)
                # 混排：非 ASCII 走 Unicode 注入、ASCII 走键盘 ⇒ 先切英文（同款的混排闸）
                #   （混排闸本来就没有 auto 分支，auto 在这里按 temp 处理＝插件原有行为）
                if not _tguard and _tuni and not _text_is_ascii(_tv) and _tmode != "background":
                    _tguard = _ime_mixed_guard(mode=("temp" if _tmode_ime == "auto" else _tmode_ime))
                if _tmode == "background":
                    # ★后台统一流：正文 WM_CHAR（发**文本输入子控件**）＋ `{KEY}` 按位置发键，
                    #   press_enter ＝ 末尾追加换行（同一 PostMessage 队列 ⇒ 顺序天然有序）。
                    _tgt = _text_input_child(_resolve_target()) or _resolve_target()
                    _tb = _tv + ("\n" if st.get("press_enter") else "")
                    if _tb:
                        _post_text_tokens(_tgt, _tb)
                else:
                    if _tv and not _tauto_done:
                        _parse_sendinput_text(_tv, interval=_tint, typing_interval=_tyint, unicode_text=_tuni)
                    if st.get("press_enter"):
                        _parse_sendinput_text("{ENTER}", interval=0.0, typing_interval=0.0)
                if _tguard:
                    _ime_guard_restore()          # 用完把该窗恢复成中文态（与 `_ime_guard_typed` 成对）
                texts.append({"text": _tv, "mode": _tmode, "typing_interval": _tyint,
                              "interval": _tint, "press_enter": bool(st.get("press_enter")),
                              "ime_ascii": _tguard})
                kind = "text"
            elif "mouse_pos" in st:
                # 只读步（对齐工具级 `get_mouse_position`）——宏里顺手记一笔光标位，
                #   用于事后对账"视角/光标有没有飘"。成本≈0，不改任何状态。
                _mpt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(_mpt))
                poses.append({"x": _mpt.x, "y": _mpt.y})
                kind = "mouse_pos"
            elif "shot" in st:
                # **提交后台单拍、不阻塞宏**——宏继续跑；截图随后台线程抓，
                # 结果用 `vision_action="pull"` + `vision_id=<peek id>` 取（一次性、不自动续）。
                _sreg = str(st.get("shot") or "window").lower()
                if _sreg not in ("window", "fullscreen"):
                    raise ValueError("shot 只认 window / fullscreen（收到 %r）" % (st.get("shot"),))
                if int(st.get("frames", 1) or 1) > 1 or int(st.get("watch_ms", 0) or 0) > 0:
                    raise ValueError("shot 步的抽帧拼图已取消（会阻塞）；shot 步只提交后台单拍")
                _srect = st.get("rect")
                _spath = st.get("save_path") or ""
                _safter = max(0, min(600000, int(st.get("after_ms", 0) or 0)))   # 延迟单拍（毫秒）
                _pk = _peek_start(_sreg, st.get("window"), _srect, save_path=(_spath or None),
                                  after_ms=_safter)
                shots.append({"region": _sreg, "peek_id": _pk["id"], "saved": _spath,
                              "after_ms": _safter,
                              "at_s": round(time.perf_counter() - t0, 3), "async": True})
                kind = "shot"
            elif "wait_image" in st or "click_image" in st:
                # 模板匹配步型（0.1.91）：等某张图出现／识别到就过去点它——宏里"自动识别图片→过去拿"
                _is_click = "click_image" in st
                _spec = st.get("click_image" if _is_click else "wait_image")
                _wi = _spec if isinstance(_spec, dict) else {"template": _spec}
                _wt = _tmpl_load(_wi.get("template"))
                _wthr = float(_wi.get("threshold", 0.85) or 0.85)
                _wto = float(_wi.get("timeout_ms", 5000) or 0) / 1000.0
                _wt0 = time.time()
                _whit = None
                _wox = _woy = 0
                while True:
                    _wfr, _wox, _woy = _tmpl_search_frame(_wi.get("region"))
                    _whs = _tmpl_locate(_wfr, _wt, _wthr, 1)
                    if _whs:
                        _whit = _whs[0]
                        break
                    if time.time() - _wt0 >= _wto:
                        break
                    _sleep(0.1)
                if not _whit:
                    raise ValueError("%s 超时：%.1fs 内没找到模板（阈值 %.2f）"
                                     % ("click_image" if _is_click else "wait_image", _wto, _wthr))
                _wsx = _wox + _whit[1] + _whit[3] // 2
                _wsy = _woy + _whit[2] + _whit[4] // 2
                if _is_click:
                    _glide_to(_wsx, _wsy, human=human, speed=speed, precise=precise)
                    _mouse_event_click_here(str(_wi.get("button") or "left"), 1)
                    kind = "click_image"
                else:
                    kind = "wait_image"
            elif "focus" in st:
                # 对齐工具级 `focus_window`：中途换操作面，之后所有步都作用在它上面。
                _fv = st["focus"]
                _fspec = (_fv.get("window") if isinstance(_fv, dict) else _fv)
                if not _fspec:
                    raise ValueError("focus 步要给窗口（标题子串/hwnd）：{\"focus\": \"标题\"} 或 {\"focus\": {\"window\": \"标题\"}}")
                _fhw = _window_arg_target(_fspec)
                if not _fhw:
                    raise ValueError("focus 步找不到窗口：%r" % (_fspec,))
                _focus_and_remember(_fhw)
                focuses.append({"window": str(_fspec)[:80], "hwnd": int(_fhw),
                                "at_s": round(time.perf_counter() - t0, 3)})
                _sleep(0.15)          # 聚焦后首个输入前留稳定时间（与 window= 前置同规则）
                kind = "focus"
            elif "hover" in st:
                # 对齐工具级 `hover`：移过去＋停留（不点击）；睡眠走 `_sleep` ⇒ 受总超时/ESC 约束。
                _hx, _hy = _seq_pair(st["hover"], "hover")
                _hdwell = max(0, min(5000, int(st.get("dwell_ms", 600) or 0)))
                _hhm = bool(st.get("human", human))
                _hpc = bool(st.get("precise", precise))
                _hspd = st.get("speed", speed)
                if _hpc and _hhm:
                    raise ValueError("hover 步：人影档不允许 precise=true")
                _glide_to(_hx, _hy, human=_hhm,
                          speed=(None if _hspd is None else float(_hspd)), precise=_hpc)
                _sleep(_hdwell / 1000.0)
                hovers.append({"x": int(_hx), "y": int(_hy), "dwell_ms": _hdwell})
                kind = "hover"
            elif "transfer" in st:
                # 对齐工具级 `mouse_transfer`（drag/click/auto）：模式沿用同一套原语。
                _tv = st["transfer"]
                if not isinstance(_tv, dict):
                    raise ValueError("transfer 步要写成 {\"transfer\": {\"from\": [x,y], \"to\": [x,y], \"mode\": \"drag|click\"}}")
                _tfx, _tfy = _seq_pair(_tv.get("from"), "transfer.from")
                _ttx, _tty = _seq_pair(_tv.get("to"), "transfer.to")
                _tmode = str(_tv.get("mode") or "drag").strip().lower()
                if _tmode not in ("drag", "click", "auto"):
                    raise ValueError("transfer 步的 mode 只认 drag / click / auto（收到 %r）" % (_tv.get("mode"),))
                _tbtn = str(_tv.get("button") or "left").strip().lower()
                if _tbtn not in ("left", "right"):
                    raise ValueError("transfer 步的 button 只认 left / right")
                _tver = bool(_tv.get("verify", False))
                if _tmode == "auto" and not _tver:
                    raise ValueError("transfer 步的 mode=auto 依赖像素判定：显式 \"verify\": true 再用，或直接选 drag / click")
                _thm = bool(_tv.get("human", human))
                _tpc = bool(_tv.get("precise", precise))
                _tspd = _tv.get("speed", speed)
                if _tpc and _thm:
                    raise ValueError("transfer 步：人影档不允许 precise=true")
                _tres = mouse_transfer((_tfx, _tfy), (_ttx, _tty), mode=_tmode, button=_tbtn,
                                       human=_thm, speed=(None if _tspd is None else float(_tspd)),
                                       precise=_tpc, verify_effect=_tver,
                                       settle_ms=int(_tv.get("settle_ms", _PX_SETTLE_MS) or 0),
                                       safe_park=_tv.get("safe_park") or "",
                                       undo=_tv.get("undo") or "auto")
                transfers.append({"from": [int(_tfx), int(_tfy)], "to": [int(_ttx), int(_tty)],
                                  "mode": _tmode, "mode_used": _tres.get("mode_used"),
                                  "changed": bool(_tres.get("changed")), "verify": _tver})
                kind = "transfer"
            elif "find_click" in st:
                # 把「寻找点击」整条能力搬进宏：找位置/按图点/按两张图搬（模板三种来源同工具）。
                #   步内默认不附图（宏只回一张总图），要步内图就显式 "include_image": true。
                _fc = st["find_click"]
                if isinstance(_fc, dict):
                    _fc = dict(_fc)
                else:
                    _fc = {"action": "click", "template": _fc}
                if "include_image" not in _fc:
                    _fc["include_image"] = False
                _fc_res = _find_click_do(_fc)
                _fc_err = bool(_fc_res.get("isError"))
                _fc_txt = ""
                for _cc in (_fc_res.get("content") or []):
                    if isinstance(_cc, dict) and _cc.get("type") == "text":
                        _fc_txt = _cc.get("text") or ""
                        break
                matches.append({"action": _fc.get("action") or "find", "ok": (not _fc_err),
                                "text": _fc_txt[:800]})
                if _fc_err:
                    raise ValueError("find_click 步失败：%s" % ((_fc_txt.split("\n") or [""])[0][:200],))
                kind = "find_click"
            elif "wait" in st:
                # 条件等待进宏：数字＝纯等；对象＝{seconds, conditions, match}，多条件 any/all。
                _wv = st["wait"]
                if isinstance(_wv, (int, float)) and not isinstance(_wv, bool):
                    _sleep(float(_wv))
                    kind = "wait"
                elif isinstance(_wv, dict):
                    _wsecs = float(_wv.get("seconds", 5) or 5)
                    _wconds = _wait_norm_conditions(_wv.get("conditions"))
                    _wmatch = str(_wv.get("match") or "any").strip().lower()
                    if _wmatch not in ("any", "all"):
                        raise ValueError("wait 步的 match 只认 any / all（收到 %r）" % (_wv.get("match"),))
                    if not _wconds:
                        _sleep(min(max(0.0, _wsecs), max(0.0, _left())))
                        kind = "wait"
                    else:
                        _wsecs = min(max(0.05, _wsecs), max(0.05, _left()))
                        _ok, _win2, _info2 = _wait_race(
                            _wconds, _wsecs, match=_wmatch,
                            abort_check=lambda: bool(poll_cancel() or _left() <= 0
                                                     or (abort_check is not None and abort_check())))
                        waits_cond.append({"match": _wmatch, "seconds": round(_wsecs, 2),
                                           "ok": bool(_ok),
                                           "winner": (_win2 or {}).get("mode"),
                                           "elapsed": (_info2 or {}).get("elapsed"),
                                           "states": (_info2 or {}).get("states")})
                        if not _ok:
                            if (_info2 or {}).get("aborted"):
                                aborted = aborted or "timeout"
                                break
                            if str(_wv.get("on_timeout") or "abort").lower() == "continue":
                                kind = "wait"
                            else:
                                _st_txt = "；".join("%s=%s" % (_m2, _s2)
                                                   for _m2, _s2 in ((_info2 or {}).get("states") or []))
                                raise ValueError("wait 步超时（%.2fs，match=%s）：%s"
                                                 % (_wsecs, _wmatch, _st_txt or "条件没有达成"))
                        else:
                            kind = "wait"
                else:
                    raise ValueError("wait 步要给秒数或 {seconds, conditions, match} 对象（收到 %r）" % (_wv,))
            elif "set_window" in st:
                # 对齐工具级 `set_window`：移动/改大小（**位置与尺寸是屏幕值**，不是图内坐标）。
                _sv = st["set_window"]
                if not isinstance(_sv, dict):
                    raise ValueError("set_window 步要写成 {\"set_window\": {\"x\":…, \"y\":…, \"width\":…, \"height\":…}}")
                _sres = set_window(_sv.get("x"), _sv.get("y"), _sv.get("width"), _sv.get("height"))
                sysnotes.append({"step": "set_window",
                                 "result": (_sres if isinstance(_sres, str) else str(_sres))[:300]})
                kind = "set_window"
            elif "open" in st:
                # 对齐工具级 `open_desktop_item`：打开桌面/开始菜单项，或扫描清单。
                _ov = st["open"]
                _okw = (_ov.get("keyword") if isinstance(_ov, dict) else _ov)
                _omode = str((_ov.get("mode") if isinstance(_ov, dict) else None) or "open").strip().lower()
                if _omode == "scan":
                    _scan = _shell_scan_desktop()
                    sysnotes.append({"step": "open_scan", "count": len(_scan),
                                     "names": [it.get("name") for it in _scan][:40]})
                elif _omode == "open":
                    if not _okw:
                        raise ValueError("open 步要给 keyword（桌面/开始菜单项名字）")
                    _overb = str((_ov.get("verb") if isinstance(_ov, dict) else None) or "open")
                    _ores = _shell_open_desktop_item(str(_okw), _overb)
                    sysnotes.append({"step": "open", "keyword": str(_okw)[:60],
                                     "found": bool(_ores.get("found")), "item": _ores.get("item")})
                    if not _ores.get("found"):
                        raise ValueError("open 步没找到包含 %r 的桌面项" % (_okw,))
                else:
                    raise ValueError("open 步的 mode 只认 open / scan（收到 %r）" % (_ov.get("mode"),))
                kind = "open"
            elif "windows" in st:
                # 对齐工具级 `list_windows`（只读）：把当前窗口清单记进回执，便于宏后对账。
                _wlist = enum_all_windows(False)
                wins_snaps.append({"count": len(_wlist),
                                   "windows": [{"hwnd": w.get("hwnd"),
                                                "title": str(w.get("title") or "")[:60],
                                                "process": w.get("process")} for w in _wlist][:60]})
                kind = "windows"
            elif "wait_ms" in st:
                _sleep(float(st["wait_ms"] or 0) / 1000.0)
                kind = "wait"
            else:
                raise ValueError(
                    "第 %d 步认不出：%s —— 只认 key_down/key_up/key/hold/combo/move/click/drag/transfer"
                    "/hover/wheel/text/shot/mouse_pos/focus/set_window/open/windows/find_click/wait/wait_ms"
                    "（动作）、**数字**（停顿，秒）、"
                    "**字符串**（文字输入，即**原样打出去的字**，如「{ctrl+c}」／「0.5」）；"
                    "并行＝「track」容器，重复＝「steps+repeat+gap_ms」块。"
                    "或直接给一个时间值（0.5 ／ \"0.5s\" ／ \"500ms\"）" % (i + 1, sorted(st.keys())))
            counts[kind] = counts.get(kind, 0) + 1
            done += 1
    finally:
        # 中止/异常也必须把键抬起：按住的 W 卡住＝人物一直往前走
        released = sorted(pressed)
        for _k in released:
            try:
                _si_key_up(_k)
            except Exception:
                pass
        pressed.clear()
    return {"steps_total": len(steps), "steps_done": done, "kinds": counts,
            "requested_move": {"dx": moved[0], "dy": moved[1]},
            "paces": paces,
            "typed_text": typed, "text_looks_like_time": timeish,
            # 新增三键（老键一字未动 ⇒ 老形状回执逐字不变；新键只在用了新步型时有内容）
            "shots": shots, "mouse_pos": poses, "texts": texts, "kbd_notes": kbd_notes,
            "matches": matches, "wait_conditions": waits_cond, "transfers": transfers,
            "hovers": hovers, "focuses": focuses, "sys": sysnotes, "window_snaps": wins_snaps,
            "waited_s": round(waits, 3),
            "elapsed": round(time.perf_counter() - t0, 3), "timeout_ms": int(timeout_ms),
            "aborted": aborted, "keys_released_on_exit": released}


def _seq_looks_like_time(s):
    """判定一个**字符串**是不是"长得像时间"（`0.5s` ／ `500ms` ／ `2秒` ／ `0.5`）。

    为什么要它：序列里**数字**才是停顿、**字符串**是**文字输入** ⇒ 遇到"看着像
    时间"的字符串**照打**（**语义一个字不动**），只在**回执里留痕**（`text_looks_like_time`）——
    这样"把 0.5 打进聊天框"与"等 0.5 秒"再也不会互相冒充。
    """
    try:
        return re.match(r"^\d+(\.\d+)?\s*(ms|毫秒|s|秒)?$", str(s or "").strip().lower()) is not None
    except Exception:
        return False


def _seq_split(steps):
    """把 `steps` 拆成**轨道表**（内部唯一形状；外面只有一种容器）。

    **对外只认一种容器**：`{"track": 轨名, "steps": [步骤...]}` ⇒ **一条轨**（轨与轨**同时跑**）：
      · 数组里**不出现**这种容器 ⇒ 全部步归入**一条隐式轨 `main`**（＝老行为，逐字不变）；
      · 轨内另有**块**：`{"steps": [...], "repeat": n, "gap_ms": m}`（**不带 `track`**）＝**就地重复**
        的步骤组 ⇒ **「轨」与「块」用 `track` 一个字区分，不靠猜**。
      · **要严格顺序 ⇒ 别写多条轨**（把步骤都写进同一条轨里就是顺序执行）。
    判据：`track` 的 `steps` 必须非空数组；轨名重复 ⇒ 抛错（不合并、不覆盖）。
    """
    if not isinstance(steps, list) or not steps:
        raise ValueError('steps 必须是步骤数组（要并行就放 {"track": 轨名, "steps": [...]}）')
    tracks, rest = {}, []
    for el in steps:
        if isinstance(el, dict) and el.get("track"):
            nm = str(el["track"])
            body = el.get("steps")
            if not isinstance(body, list) or not body:
                raise ValueError("轨 %s 的 steps 必须是非空数组" % nm)
            if nm in tracks:
                raise ValueError("轨名重复：%s（同名轨请并成一条）" % nm)
            tracks[nm] = body
        else:
            rest.append(el)
    if rest or not tracks:
        tracks.setdefault("main", rest)
    return tracks


def _run_input_sequence(steps, timeout_ms=60000, relative=True, human=False, speed=None,
                        precise=False, abort_check=None, progress=None, ime_ascii=True, ime_mode="temp",
                        unicode_text=False):
    """序列统一入口（鼠标和键盘是两条独立轨道）。

    **只有一种形状要记 —— `steps` 永远是数组**；数组里**只有一种容器**：
      · `{"track": 轨名, "steps": [...]}` ⇒ **一条轨**（轨与轨**同时跑**，各自从 0 开始）；
      · **不带 `track` 但带 `steps`** ⇒ **块**（就地重复的步骤组，可 `repeat` / `gap_ms`，可再嵌块）；
      · **一个 `track` 都没有** ⇒ 一条隐式轨 `main` ⇒ **＝老行为，逐字不变**（回执也逐字相同）。
    **步骤**三种元素（判据一眼可辨）：**数字＝停顿**（0.5 ／ 「500ms」）｜**字符串＝文字输入**｜**带 `steps`＝容器
      （轨或块）**｜**其余对象＝动作**（key_down/key_up/key/hold/combo/move/click/wheel/wait_ms）。
    **要严格顺序 ⇒ 别写多条轨**（把步骤写进同一条轨里就是顺序）；时间值在**块内**与**块之间**都生效。
    回执：单轨 ⇒ 与改前逐字相同；多轨 ⇒ 老键全保留，另把每轨步型并进 `kinds`（前缀＝轨名，如
      `mouse.move_rel`），并附 `tracks` / `tracks_count`。
    安全：超时/ESC **按轨各自生效**（同一个 `timeout_ms` 数值 ⇒ 两轨同时到点停）；抬键在**每条轨**的
      finally 里各自做（谁也不替谁欠账）。
    """
    tracks = _seq_split(steps)
    if len(tracks) == 1:
        _only = next(iter(tracks.values()))
        return _run_input_sequence_flat(_seq_expand(_only), timeout_ms=timeout_ms,
                                        relative=relative, human=human, speed=speed,
                                        precise=precise, abort_check=abort_check, progress=progress,
                                        ime_ascii=ime_ascii, ime_mode=ime_mode, unicode_text=unicode_text)
    res, err = {}, {}

    def _one(nm):
        try:
            res[nm] = _run_input_sequence_flat(_seq_expand(tracks[nm]), timeout_ms=timeout_ms,
                                               relative=relative, human=human, speed=speed,
                                               precise=precise, abort_check=abort_check,
                                               ime_ascii=ime_ascii, ime_mode=ime_mode,
                                               unicode_text=unicode_text)
        except Exception as e:          # 一轨出错，也要让另一轨正常收尾（它自己的 finally 会抬键）
            err[nm] = e

    _ths = [threading.Thread(target=_one, args=(nm,), daemon=True) for nm in tracks]
    for _t in _ths:
        _t.start()
    _join = max(0.05, float(timeout_ms) / 1000.0) + 1.0
    for _t in _ths:
        _t.join(timeout=_join)
    if err:
        raise list(err.values())[0]
    _kinds, _paces = {}, []
    _shots, _poses, _texts = [], [], []
    for _nm, _r in res.items():
        for _k, _n in (_r.get("kinds") or {}).items():
            _kinds["%s.%s" % (_nm, _k)] = _n          # 轨名前缀 ⇒ 回执里一眼看出谁做了什么
        _paces.extend(_r.get("paces") or [])
        _shots.extend(_r.get("shots") or [])          # 新键也要按轨汇总（否则多轨时丢账）
        _poses.extend(_r.get("mouse_pos") or [])
        _texts.extend(_r.get("texts") or [])
    return {"steps_total": sum(_r.get("steps_total", 0) for _r in res.values()),
            "steps_done": sum(_r.get("steps_done", 0) for _r in res.values()),
            "kinds": _kinds,
            "requested_move": {"dx": sum(_r.get("requested_move", {}).get("dx", 0) for _r in res.values()),
                               "dy": sum(_r.get("requested_move", {}).get("dy", 0) for _r in res.values())},
            "paces": _paces,
            "shots": _shots, "mouse_pos": _poses, "texts": _texts,
            "typed_text": [_s for _r in res.values() for _s in (_r.get("typed_text") or [])],
            "text_looks_like_time": [_s for _r in res.values() for _s in (_r.get("text_looks_like_time") or [])],
            "waited_s": round(sum(_r.get("waited_s", 0.0) for _r in res.values()), 3),
            "elapsed": max(_r.get("elapsed", 0.0) for _r in res.values()),
            "timeout_ms": int(timeout_ms),
            "aborted": next((_r.get("aborted") for _r in res.values() if _r.get("aborted")), None),
            "keys_released_on_exit": sorted({_k for _r in res.values()
                                             for _k in (_r.get("keys_released_on_exit") or [])}),
            "tracks_count": len(res), "tracks": res}


# ══════════════════════════════════════════════════════════════════════════════
# 序列的**后台队列**：途中截图 ＋ 列表（含内容）＋ 追加/覆盖
#   为什么必须改成后台（机制，不是偏好）：`_HTTP_TOOL_LOCK` 把**整个工具面**锁住（HTTP handler 里
#     包住 `dispatch_message`）⇒ 同步跑一条 60s 的宏，期间**任何**工具调用（含截图）只能排队 ⇒
#     「执行途中截图」在结构上做不到。⇒ 把执行挪到**不持那把锁**的后台线程；工具面因此空闲，
#     随手 `action=shot` 就能看当前画面。
#   边界：
#     · **纯新增**：不传 `action`（＝`run`）时走**原封不动**的老同步路，回执逐字相同；
#     · 老 executor／老工具一行未改（只多了一个**有默认值**的中止位形参）；
#     · 队列**同一时刻只跑一条**（单 worker）⇒「两条序列同时往桌面注入输入」这件事不存在；
#     · 后台任务**只碰注入通道＋自己的账本**：不调 `_show_indicator`（那只在提交时一次），
#       收尾也不调 `_status_block`／`_win_snapshot_light` 那两句重活（后台线程里没人看它们，
#       它们还会去碰全局状态）——要看状态就用 `action=list`／`action=shot` 现取。
#   复用与差异：
#     · **接入的现成件**（一步没自造）：序列三件套本身（`_run_input_sequence`／`_seq_expand`／`_seq_split`）
#       ｜`wait_with_cancel`（步内睡眠，ESC 可取消）｜`_grab_png_bytes`＋`_pil_png_bytes`＋`_frame_set`
#       （截图与"最新帧"登记）｜`_post_scroll`／`_post_text`／`_post_key`（后台通道）｜`_parse_sendinput_text`
#       ｜`_ime_ascii_guard`＋`_set_ime_open`（输入法守卫，与 `_do_send_keystrokes` 的两步一致）｜`_resolve_target`
#       ｜`_parse_rect4`（rect 解析，**失败抛错**）｜`_ocr_cache_invalidate`＋`_prune_tracked_popups`（动作后作废快照）
#       ｜`_glide_to`／`_mouse_event_move_rel`／`_mouse_event_click_here`／`_rel_move_paced`／`_mouse_speed`。
#     · **三处刻意不同**（各有理由，写在这里免得下次被"统一"掉）：
#       ① **不套工具级函数**（`mouse_click`／`mouse_drag`／`scroll`／`send_keystrokes` 那层）——
#          每步要多付 `enum_all_windows`（控制台 ~175ms/次）＋抢前台＋整帧回读；
#       ② **拖拽不做随机停顿**（`_human_pause` 的 20~200ms）⇒ 换**确定性** `grip_ms`/`settle_ms`（默认取原中位
#          130／90）：相对路径要确定性；
#       ③ **不接 `verify` 回读**（跳过后变化校验）——整帧 OCR 是宏里最大的一笔省；要看结果就附图或 `action=shot`。
#     · **为什么队列必须在后台**（不是偏好，是机制）：`_HTTP_TOOL_LOCK` 包住 `dispatch_message`
#       ⇒ 同步跑一条 60s 的宏时，**其它工具（含截图）只能排队** ⇒ "执行途中截图"结构上做不到。
# ══════════════════════════════════════════════════════════════════════════════
_SEQ_MAX_JOBS = 32          # 队列上限（含已结束未清理的）：超了先清已结束的，仍满则**抛错**不静默丢
_SEQ_CONTENT_MAX = 800      # 列表回执里单条 steps 原文的截断长度（超了截断并给出原长）
_SEQ_ABORT_TAG = {"abort": "⛔ 被顶掉／Stop 中止", "esc": "⛔ 被取消（ESC+`）", "timeout": "⏱ 超时中止"}
_seq_jobs = {}              # id -> job（**有序**：dict 保插入序 ⇒ 天然 FIFO）
_seq_cond = threading.Condition()   # 队列锁＋唤醒
_seq_seq = [0]              # 自增号
_seq_cur = [None]           # 当前在跑的 job id
_seq_stops = {}             # id -> threading.Event（中止位）
_seq_worker = [None]        # worker 线程（懒起、空闲即退）


def _seq_new_id():
    _seq_seq[0] += 1
    return "sq%d" % _seq_seq[0]


def _seq_steps_brief(steps, limit=_SEQ_CONTENT_MAX):
    """steps 原文（列表里要能看出内容是什么）＋超长截断留痕。返回 (文本, 原长, 是否截断)。"""
    try:
        txt = json.dumps(steps, ensure_ascii=False)
    except Exception:
        txt = repr(steps)
    if len(txt) <= limit:
        return txt, len(txt), False
    return txt[:limit] + "…［截断］", len(txt), True


def _seq_shots_text(res):
    """`shot` 步的取图指引（**纯新增**，不改老回执）：`shot` 步只提交后台单拍、不阻塞宏，
    但回执里必须留下 peek id——否则调用方拿不回那张图。没有 shot 步 ⇒ 返回空串（老回执逐字不变）。"""
    _sh = (res or {}).get("shots") or []
    if not _sh:
        return ""
    return "".join(
        "\n  途中截图（后台单拍、不阻塞）：%s ｜ `vision_action=\"pull\"` + `vision_id=%s` 取图%s"
        % (s.get("region"), s.get("peek_id"),
           (" ｜ 已存 %s" % s.get("saved")) if s.get("saved") else " ｜ 未落盘（默认）")
        for s in _sh)


def _seq_job_line(j):
    """一条 job 的一行：**状态 ｜ 进度 ｜ 用时 ｜ 档位 ｜ 内容**。"""
    st = j.get("state")
    tag = {"queued": "排队", "running": "★在跑", "done": "✓ 跑完", "aborted": "⛔ 中止",
           "error": "✗ 出错", "dropped": "— 被顶掉"}.get(st, str(st))
    r = j.get("res") or {}
    prog = (" ｜ %s/%s 步" % (r.get("steps_done", "-"), r.get("steps_total", "-"))
            if st in ("running", "done", "aborted") else "")
    t1 = j.get("t1") or (time.time() if (st == "running" and j.get("t0")) else None)
    used = (" ｜ 用时 %.1fs" % (t1 - j["t0"])) if (t1 and j.get("t0")) else ""
    why = ""
    if st == "aborted":
        why = " ｜ " + _SEQ_ABORT_TAG.get(r.get("aborted") or "", "⛔ 已中止")
    elif st == "error":
        why = " ｜ " + str(j.get("err") or "")[:160]
    txt, n, cut = _seq_steps_brief(j.get("steps"))
    return ("%s [%s]%s%s%s\n      档位：relative=%s human=%s speed=%s timeout=%sms ｜ 提交 %s\n      内容：%s"
            % (j.get("id"), tag, prog, used, why, j.get("relative"), j.get("human"), j.get("speed"),
               j.get("timeout_ms"),
               time.strftime("%H:%M:%S", time.localtime(j.get("t_submit") or time.time())),
               txt + ("（原文 %d 字，已截断）" % n if cut else "") + _seq_shots_text(r)))


def _seq_jobs_text(with_steps=True):
    """`action=list` 的正文：**列表 ＋ 内容**（＋首行给"现在谁在跑"）。"""
    with _seq_cond:
        jobs = list(_seq_jobs.values())
    if not jobs:
        return "序列队列：空（没有在跑的、也没有排队的）"
    head = ("序列队列：共 %d 条（★在跑 %s ｜ 排队 %d ｜ 已结束 %d）\n"
            % (len(jobs), _seq_cur[0] or "无",
               sum(1 for j in jobs if j.get("state") == "queued"),
               sum(1 for j in jobs if j.get("state") in ("done", "aborted", "error", "dropped"))))
    return head + "\n".join(_seq_job_line(j) if with_steps
                            else "%s [%s] （内容略，with_steps=true 看全文）" % (j.get("id"), j.get("state"))
                            for j in jobs)


def _seq_trim_locked():
    """超上限时先清**已结束的最旧**（清单由调用方报出去＝不静默丢）；全是活任务 ⇒ 抛错。"""
    if len(_seq_jobs) < _SEQ_MAX_JOBS:
        return []
    cleaned = []
    for k in list(_seq_jobs.keys()):
        if _seq_jobs[k].get("state") in ("done", "aborted", "error", "dropped"):
            cleaned.append(k)
            _seq_jobs.pop(k, None)
            _seq_stops.pop(k, None)
            if len(_seq_jobs) < _SEQ_MAX_JOBS:
                break
    if len(_seq_jobs) >= _SEQ_MAX_JOBS:
        raise ValueError("序列队列已满（上限 %d 条，且全是活任务）——先 action=stop 或等它们跑完"
                         % _SEQ_MAX_JOBS)
    return cleaned


def _seq_step_text(st, limit=56):
    """把「一步」压成一行短文字（给指示条看）。不认识的一律退回，**不许抛错**。"""
    try:
        if isinstance(st, dict):
            if "wait_ms" in st:
                return "等待 %sms" % st.get("wait_ms")
            k = next(iter(st.keys()), "")
            return ("%s %s" % (k, st.get(k)))[:limit]
        if isinstance(st, str):
            return ("打字「%s」" % st)[:limit]
        return "等待 %ss" % st
    except Exception:
        return "…"


def _seq_indicator_show(jid, i, n, st, win=""):
    """序列跑的时候指示器常驻（`hold_s=None`）＋ 每步刷新成「第 i/n 步：跑的是哪一步」。

    收尾由 worker 的 finally 调 `_hide_indicator()`（成功／中止／异常三条路都到）。
    指示条是"给模型看"的旁路物 ⇒ 任何异常一律吞掉，**绝不许影响序列本身**。
    """
    try:
        _show_indicator(window=win or "",
                        action="序列 %s ｜ 第 %d/%d 步：%s" % (jid, i + 1, n, _seq_step_text(st)),
                        hold_s=None)
    except Exception:
        pass


def _seq_worker_loop():
    """常驻 worker：一次只跑一条（FIFO）；没活了就退出（下次提交再起）。"""
    while True:
        with _seq_cond:
            nxt = next((j for j in _seq_jobs.values() if j.get("state") == "queued"), None)
            if nxt is None:
                _seq_worker[0] = None
                return
            nxt["state"] = "running"
            nxt["t0"] = time.time()
            _seq_cur[0] = nxt["id"]
            ev = _seq_stops.setdefault(nxt["id"], threading.Event())
        try:
            _win = ""
            try:
                _win = _get_focus_title()
            except Exception:
                _win = ""
            try:      # ★常驻指示条：开跑先亮一次（之后每一步由 progress 刷新）
                _show_indicator(window=_win,
                                action="序列 %s ｜ 开跑（%d 步）" % (nxt["id"], len(nxt.get("steps") or [])),
                                hold_s=None)
            except Exception:
                pass
            res = _run_input_sequence(nxt.get("steps"), timeout_ms=nxt.get("timeout_ms", 60000),
                                      relative=bool(nxt.get("relative", True)),
                                      human=bool(nxt.get("human", False)),
                                      speed=nxt.get("speed"),
                                      precise=bool(nxt.get("precise", False)),
                                      abort_check=ev.is_set,
                                      ime_ascii=bool(nxt.get("ime_ascii", True)),
                                      ime_mode=str(nxt.get("ime_mode", "temp")),
                                      unicode_text=bool(nxt.get("unicode_text", False)),
                                      progress=lambda i, n, st: _seq_indicator_show(nxt["id"], i, n, st, _win))
            nxt["res"] = res
            nxt["state"] = "aborted" if res.get("aborted") else "done"
        except Exception as e:
            nxt["err"] = "%s: %s" % (type(e).__name__, e)
            nxt["state"] = "error"
        finally:
            nxt["t1"] = time.time()
            try:      # ★常驻指示条收尾（成功／中止／异常三条路都到这里）
                _hide_indicator()
            except Exception:
                pass
            # ★与同步路同规则的收尾（动作后作废快照：附图通道＋弹窗备忘＋两套缓存）
            try:
                _ocr_cache_invalidate(_popup_hwnd if (_popup_hwnd and _is_window_alive(_popup_hwnd))
                                      else _focus_hwnd)
            except Exception:
                pass
            try:
                _prune_tracked_popups()
            except Exception:
                pass
            with _seq_cond:
                _seq_cur[0] = None
                _seq_cond.notify_all()


def _seq_job_submit(steps, timeout_ms=60000, relative=True, human=False, speed=None, precise=False,
                    hwnd=None, overwrite=False, ime_ascii=True, ime_mode="temp", unicode_text=False):
    """入队（`action=append`）／**先顶掉旧的再入队**（`action=overwrite`）。

    返回 (job_id, 被顶掉或清掉的清单, 自动清掉的历史条数)。语义：
    追加 ⇒ 加在末尾｜覆盖 ⇒ 直接立刻把旧的去掉，重新用新的执行 ⇒ overwrite ＝
      ① 在跑那条 set 中止位（**在步边界停手**，随后抬键）② 清空排队的 ③ 本条入队并被立刻取走。
    """
    killed = []
    with _seq_cond:
        if overwrite:
            for j in list(_seq_jobs.values()):
                if j.get("state") == "queued":
                    killed.append(j["id"])
                    _seq_jobs.pop(j["id"], None)
                    _seq_stops.pop(j["id"], None)
                elif j.get("state") == "running":
                    killed.append(j["id"])
                    _seq_stops.setdefault(j["id"], threading.Event()).set()
        cleaned = _seq_trim_locked()
        jid = _seq_new_id()
        _seq_jobs[jid] = {"id": jid, "state": "queued", "steps": steps,
                          "timeout_ms": int(timeout_ms or 60000), "relative": bool(relative),
                          "human": bool(human), "speed": speed, "precise": bool(precise), "hwnd": hwnd,
                          "ime_ascii": bool(ime_ascii),
                          "ime_mode": str(ime_mode or "temp"),
                          "unicode_text": bool(unicode_text),
                          "t_submit": time.time(), "t0": None, "t1": None, "res": None, "err": None}
        _seq_stops[jid] = threading.Event()
        if _seq_worker[0] is None or not _seq_worker[0].is_alive():
            _seq_worker[0] = threading.Thread(target=_seq_worker_loop, name="seq-worker", daemon=True)
            _seq_worker[0].start()
        _seq_cond.notify_all()
    return jid, killed, cleaned


def _seq_job_stop(jid):
    """`action=stop`：`all`＝停全部（在跑的中止＋排队的移除）；否则只动那一条。"""
    with _seq_cond:
        if str(jid).lower() == "all":
            got = []
            for j in list(_seq_jobs.values()):
                if j.get("state") == "running":
                    _seq_stops.setdefault(j["id"], threading.Event()).set()
                    got.append(j["id"])
                elif j.get("state") == "queued":
                    j["state"] = "dropped"
                    got.append(j["id"])
            return "已停：%s" % (", ".join(got) or "（没有在跑/排队的任务）")
        j = _seq_jobs.get(str(jid))
        if not j:
            return "没有这条任务：%s（用 action=list 看现有什么）" % jid
        st = j.get("state")
        if st == "running":
            _seq_stops.setdefault(j["id"], threading.Event()).set()
            return "%s 已请求中止（在**步边界**停手并抬键）——过一两秒 action=list 看结果" % jid
        if st == "queued":
            j["state"] = "dropped"
            return "%s 已从队列移除（还没开跑 ⇒ 未产生任何输入）" % jid
        return "%s 已经结束了（状态 %s），无需停" % (jid, st)


# ── 宏槽：**单槽持久化**（刻意只存一套）─────────────────────────────
#   为什么只留一个槽：宏一多，调用方会倾向反复套用同一套固定动作、失去临场应变；
#   一个都不留又每次重写太慢。单槽＝有记忆点（跨重启不丢）又逼着按现场微调。
#   文件落 %LOCALAPPDATA%\dsh-desktop-control\macro.json（worker 重启/插件升级都不丢）。
_MACRO_DIR = os.path.join((os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")),
                          "dsh-desktop-control")
_MACRO_PATH = os.path.join(_MACRO_DIR, "macro.json")
_MACRO_MAX_STEPS = 2000
_MACRO_SETTING_KEYS = ("relative", "human", "precise", "speed", "timeout_ms",
                       "ime_ascii", "ime_mode", "unicode_text")


def _macro_load():
    """读宏槽：返回 (payload, 说明)；没有/坏了 payload=None。"""
    try:
        if not os.path.exists(_MACRO_PATH):
            return None, ("宏槽是空的（还没存过）。要存：action=save + steps；**只存一套**，下次 save 会覆盖旧的。")
        with open(_MACRO_PATH, "r", encoding="utf-8") as _f:
            _d = json.load(_f)
        if not isinstance(_d, dict) or not isinstance(_d.get("steps"), list) or not _d["steps"]:
            return None, ("宏槽文件存在但内容不对（缺 steps）——重新 action=save 一次即可覆盖：%s" % _MACRO_PATH)
        return _d, ""
    except Exception as _e:
        return None, "宏槽读取失败（%s）：%s" % (_MACRO_PATH, str(_e)[:160])


def _macro_save(payload):
    """写宏槽（**覆盖**——刻意只留一套）；原子写：先 .tmp 再 replace。返回 (ok, 说明)。"""
    try:
        os.makedirs(_MACRO_DIR, exist_ok=True)
        _tmp = _MACRO_PATH + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as _f:
            json.dump(payload, _f, ensure_ascii=False, indent=1)
        os.replace(_tmp, _MACRO_PATH)
        return True, _MACRO_PATH
    except Exception as _e:
        return False, str(_e)[:160]


def _macro_text(d):
    """宏槽正文（action=saved 回执）：备注/时间/设置 + steps 原文（超长截断留痕）。"""
    _steps = d.get("steps") or []
    _txt, _n, _cut = _seq_steps_brief(_steps, 6000)
    _cfg = d.get("settings") or {}
    return "\n".join([
        "宏槽：**已存 1 套**（%d 步）｜保存于 %s%s"
        % (len(_steps), d.get("saved_at") or "?",
           ("｜备注：" + str(d.get("note"))) if d.get("note") else ""),
        "文件：%s" % _MACRO_PATH,
        "设置：relative=%s human=%s precise=%s speed=%s timeout=%sms ime_ascii=%s ime_mode=%s unicode=%s"
        % (_cfg.get("relative"), _cfg.get("human"), _cfg.get("precise"), _cfg.get("speed"),
           _cfg.get("timeout_ms"), _cfg.get("ime_ascii"), _cfg.get("ime_mode"), _cfg.get("unicode_text")),
        "steps 原文（%d 字%s）：%s" % (_n, "，已截断" if _cut else "", _txt),
        "（run_saved 直接跑；要按现场改：把这里 steps 复制出来改几步，再 run_saved 传 steps＝改后的，**不覆盖槽位**）",
    ])


def _seq_crop_png(data, rect):
    """把 PNG 字节按 `rect` 裁一块（对齐工具面 `rect` 的语义：**裁，不是放大**）。

    **先解析、再裁**——`_parse_rect4` 认不出就**抛错**（不许静默返回原图：
    调用方以为裁了、其实没裁，是"静默降级"的典型形状）；只有 PIL 编解码失败才退回原字节。
    """
    x, y, w, h = _parse_rect4(rect)          # 认不出 ⇒ ValueError 直接往上抛（不吞）
    try:
        import io as _io
        from PIL import Image as _Image
        im = _Image.open(_io.BytesIO(data)).convert("RGB").crop((x, y, x + w, y + h))
        return _pil_png_bytes(im)
    except Exception:
        return data                          # 编解码层面的兜底（与工具面同策略）


def _seq_shot_bytes(region="window", rect=None):
    """`action=shot`／`shot` 步 取一帧（内存字节、**默认不落盘**）＋登记最新帧（附图复用通道）。"""
    data = _grab_png_bytes(region, None)
    if data and rect:
        data = _seq_crop_png(data, rect)
    if data:
        try:
            _frame_set(None, "window" if region == "window" else "screen", note="seq shot", data=data)
        except Exception:
            pass
    return data


def _me_start_move(x, y):
    """SetCursorPos 移动到屏幕坐标。"""
    user32_si.SetCursorPos(int(x), int(y))




def _foreground_satisfies(target):
    """前台**已经**是本窗（或它的子窗／同族窗／**它自己拥有的弹窗**）⇒ True。

    为什么这层判据必须单独存在（并且**要在动手之前**问）：
      `_ensure_foreground` 以前是「先无条件 `SetForegroundWindow`、再核验」，于是遇到
      「目标窗本来就是活动面、只是前台被它自己的模态框占着」时，那一下会把前台硬拉回主窗
      ⇒ **弹窗内部的当前焦点被重置**。实测：IDA 文件对话框里刚点亮的地址栏，一调
      `send_shortcut` 编辑态就没了、字打不进去（每次调用都抢一次前台，就是这个）。
    owned 弹窗在前台 ⇒ 主窗**就是**活动面，不必也不该再激活。
    """
    try:
        if not target:
            return False
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        fg, t = int(fg), int(target)
        if fg == t or user32.IsChild(wintypes.HWND(t), wintypes.HWND(fg)):
            return True
        try:                                   # UWP 一族：一标题两扇窗，族根相同即视为就位
            if _window_family_root(fg) == _window_family_root(t):
                return True
        except Exception:
            pass
        cur = fg                               # ★前台是目标**拥有的弹窗**（模态框/提示框）⇒ 也算就位
        for _ in range(8):
            owner = int(user32.GetWindow(wintypes.HWND(cur), _GW_OWNER) or 0)
            if not owner:
                return False
            if owner == t:
                return True
            cur = owner
        return False
    except Exception:
        return False


def _ensure_foreground(target):
    """获取前台焦点。主窗口 SetForegroundWindow 后焦点即就绪，无需额外点击。

    设计要点：**弱调用必须被核验**——
      只有 SetForegroundWindow + 睡 50ms、**不看返回值、不核对**的写法不够：Windows 有一条前台锁，
      **调用进程自己不是前台时，SetForegroundWindow 会被静默拒绝**（只返回 FALSE，不报错）。
      一旦静默失败，后续输入就会落到错误窗口上。因此按"决定走前台 ⇒ 前台一定切到位"补上
      **核验 + 强路由兜底**：强路由就是本文件既有的 `_activate()`（AttachThreadInput + TOPMOST +
      BringWindowToTop + FlashWindow）——`focus_window` 一直用它。
    ★：**先判后动**——前台已经满足（含"前台是它自己拥有的弹窗"）就**一次系统调用都不发**、直接返回。
      这不是省时间，是**保住目标窗内部的当前焦点**（见 `_foreground_satisfies`）。
    返回：True＝前台确已就位；False＝没切到位（调用方据此如实报告，别再盲打）。"""
    if _foreground_satisfies(target):
        return True                            # ★已经就位 ⇒ 不激活（别把弹窗里的焦点冲掉）
    user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    user32.SetForegroundWindow(target)
    time.sleep(0.05)
    if _foreground_satisfies(target):
        return True
    focus_window(target, None)                 # 强路由兜底＝**本文件既有的那条激活逻辑**
                                               # （focus_window：AttachThreadInput + SetForegroundWindow
                                               #   + BringWindowToTop + FlashWindow；focus_window 工具与
                                               #   _focus_and_remember 走的就是它）
    time.sleep(0.08)
    return _foreground_satisfies(target)



# ── window= 参数：调用时直接指定"用哪扇窗" ──
#   语义：传 window= 标题子串 或 hwnd ⇒ 声明"本次作用在这扇窗上"——
#     · 它**不是**当前记住的窗口（或还没记住）→ **先聚焦并记住**，再执行；
#     · 它**就是**当前记住的窗口 → **直接跳过聚焦**（即使传了参数也不真调聚焦、不重复抢前台）。
#   不传 window ⇒ 完全照旧（现有行为一字不变）。
_WINDOW_ARG_TOOLS = {
    "capture_screenshot", "send_keystrokes", "send_shortcut",
    "scroll", "set_window", "mouse_click", "mouse_drag",
    "mouse_move",                    # 它的描述一直写着"可用本工具的 window="，但集合里没有它
                                     #   ⇒ 传了会被当未知参数丢掉、**不聚焦也不报错**（描述与实现不一致，已对齐）
    "sequence",                      # 支持 window= 一步聚焦
    "mouse_transfer", "hover",       # 搬移对象／悬停（同样支持 window= 一步聚焦）
    "find_click", "wait",            # 模板匹配找图／条件监听（同样支持 window= 一步聚焦）
}


def _window_arg_target(spec):
    """把 window= 解析成 hwnd：可传 hwnd（int / "0x…"）或标题子串。解析不出返回 None。"""
    try:
        if isinstance(spec, int):
            return spec
        t = str(spec).strip()
        if not t:
            return None
        if re.fullmatch(r"0[xX][0-9a-fA-F]+", t):
            return int(t, 16)
        if t.isdigit():
            return int(t)
        r = find_window_by_title(t)
        return ((_normalize_window(r) or r)[0]) if r else None
    except Exception:
        return None


def _focus_and_remember(hwnd, title=""):
    """"先聚焦再执行"的第一步：与 focus_window 工具的收尾同一规则（真聚焦 + 记住句柄）。"""
    global _focus_hwnd, _focus_child_hwnd, _focus_pid
    child = _find_terminal_child(hwnd)
    focus_window(hwnd, None)
    _show_indicator(window=title or "", action="聚焦")
    _focus_hwnd = hwnd
    _focus_child_hwnd = child if child and child != hwnd else None
    _focus_pid = _pid_of(hwnd)
    return hwnd


def _pid_of(hwnd):
    """窗口所属进程号（取不到 ⇒ 0）。"""
    try:
        _pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(_pid))
        return int(_pid.value) or 0
    except Exception:
        return 0


def _recover_focus_target():
    """聚焦窗**已关闭**时：只做**同进程**自动接管。返回 `(hwnd 或 None, 说明)`。

    为什么只认同进程：跨应用回退会打错窗口（本线铁律：**绝不回退到前台/别的应用**）；
      同一个进程＝同一个应用，且**回执如实写明接管了谁**，不会让你以为还在原来那扇窗上。
    判据优先级：
      ① 当前前台窗与旧窗**同 pid**（含"旧窗的弹窗"这类同进程顶层窗）⇒ 接管；
      ② 该 pid 的**可见顶层窗**里 z 序最上那扇（EnumWindows 顺序＝上→下）⇒ 接管。
    取不到 ⇒ `(None, "")`，由调用方报原来那条错（不静默换目标）。
    """
    global _focus_hwnd, _focus_child_hwnd, _focus_pid, _popup_hwnd
    old = _focus_hwnd
    pid = int(_focus_pid or 0)
    if not old or not pid:
        return None, ""
    cand = 0
    try:
        fg = user32.GetForegroundWindow()
        if fg and _pid_of(fg) == pid:
            cand = int(fg)
    except Exception:
        cand = 0
    if not cand:
        hits = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(h, _l):
            hh = int(h)
            if _pid_of(hh) != pid:
                return True
            if not user32.IsWindowVisible(wintypes.HWND(hh)):
                return True
            if _is_dwm_cloaked(hh):
                return True
            hits.append(hh)
            return len(hits) < 400

        try:
            user32.EnumWindows(_cb, 0)
        except Exception:
            pass
        if hits:
            cand = hits[0]
    if not cand:
        return None, ""
    _focus_hwnd = cand
    try:
        _focus_child_hwnd = _find_terminal_child(cand)
    except Exception:
        _focus_child_hwnd = None
    _popup_hwnd = None
    buf = ctypes.create_unicode_buffer(256)
    try:
        user32.GetWindowTextW(wintypes.HWND(cand), buf, 256)
    except Exception:
        pass
    note = ("原聚焦窗已关闭（hwnd=%d）⇒ **自动接管同进程窗口**：'%s'（hwnd=%d）。"
            "要换别的窗口请显式 focus_window（按设计不跨应用回退）"
            % (int(old), buf.value or "(无标题)", cand))
    return cand, note


# ── 聚焦不回退：切换了新的聚焦，之后的发送只发给它；聚焦空了就不发，**绝不切到其他窗口**——
#    回退会打错窗口，得到错误结果。
#   ⇒ 目标唯一来源 = `_focus_hwnd`（只有 focus_window / window= 前置能改）。
#     它**没设**或**已失效**：一律报错、**不发也不抓**；绝不允许悄悄改用前台窗口。
#     ★唯一例外：窗口**自己关掉了**，而**同一进程**还有显示着的窗 ⇒ 自动接管那扇并**在回执写明**
#       （跨应用仍然一步都不退）。
def _require_focus_target(what="操作"):
    """严格取目标窗口：无聚焦 ⇒ 抛错；聚焦窗**已关闭** ⇒ 只尝试**同进程**自动接管（回执写明）。"""
    global _LAST_FOCUS_NOTE
    if not _focus_hwnd:
        raise RuntimeError(f"未聚焦窗口，请先调用 focus_window（{what}不回退到其他窗口）")
    if not _is_window_alive(_focus_hwnd):
        _new, _note = _recover_focus_target()
        if _new:
            _LAST_FOCUS_NOTE = _note
        else:
            raise RuntimeError(
                f"聚焦窗口已失效（hwnd={_focus_hwnd}），请重新调用 focus_window"
                f"（按设计不回退、{what}已中止）"
            )
    # **DWM 隐藏的幽灵窗**（UWP 关窗后仍挂着的帧，典型：计算器）——它屏幕上**根本不在那儿**，
    #   按坐标点/拖/滚会打在**别人**身上 ⇒ **按坐标的动作一律中止**；
    #   只读的路不受影响（list_windows(include_hidden=true) 照常）。
    #   要动它：① 先把它唤到前台再操作；② 关它走执行链（运行框跑 taskkill /IM <进程>.exe /F）。
    if _is_dwm_cloaked(_focus_hwnd):
        raise RuntimeError(
            f"目标窗口是 **DWM 隐藏的幽灵窗**（屏幕上你看不到它，hwnd={_focus_hwnd}）——"
            f"{what}会按它的幽灵坐标打到别的窗口身上，已中止。"
            f"要动它：**让它自己显示出来**（重开那个应用）或走执行链；关它：运行框跑 taskkill /IM <进程>.exe /F。"
            f"只想看它：走只读路（list_windows）。\n"
            f"   ★实测：**聚焦的'强制拉前台'对 DWM 隐藏窗无效**——"
            f"它能被找到、能被设成操作面，但标记仍是 [DWM隐藏]，PrintWindow 抓出来是全黑；"
            f"DWM cloaking 由应用/DWM 决定，SetForegroundWindow 拉不动它。"
        )
    return _focus_hwnd


def _is_terminal_or_child(target=None):
    """有终端子控件就后台 PostMessage，没有就前台。"""
    return bool(_focus_child_hwnd)


def _resolve_input_strategy(target, mode="auto", interrupt=False):
    """统一输入策略，返回 "foreground"|"background"。"""
    if mode == "foreground" or interrupt:
        _ensure_foreground(target)
        return "foreground"
    if mode == "background":
        return "background"
    if _is_terminal_or_child(target):
        return "background"
    _ensure_foreground(target)
    return "foreground"


# ══════════════════════════════════════════════════════════════════════════════
# 【全局速度设置】
#   这一块是**唯一**调速度的地方，函数体里不许再有散落魔数；且**键盘／鼠标两块各自独立**
#     （KBD_* 与 MOUSE_*/DRAG_* 互不影响，**不要合成为一个"总速度"**）。
#   同类改动要一起改：键盘的多条输入路径（`_parse_sendinput_text`／`_foreground_send_keys`
#   + `_send_combo` 内部）必须同时改到，否则打字速度不一致。
# ══════════════════════════════════════════════════════════════════════════════
# ── 键盘（单位：秒）──
KBD_CHAR_GAP  = (0.012, 0.06, 0.025)   # SendInput 文本路：逐字间隔（lo, hi, 中位）
KBD_SCAN_GAP  = (0.012, 0.028)         # 扫描码路（`_type_scancode`）：逐字间隔（shell/对话框家族走这条）
KBD_HUMAN_GAP = (0.05, 0.13, 0.09)     # 「人影档」（human=true 时用）：逐字间隔中位 ≈90ms ⇒ ≈10 字/秒
KBD_HUMAN_CPS = 10.0                   # 人影档**打字速度上限**（字/秒）＝上一条那个 ≈90ms/字
KBD_CPS_MIN   = 0.5                    # 打字自定义速度下限（字/秒）＝最慢 2 秒/字（与鼠标 HUMAN_SPEED_MIN 同义），像真人敲
KBD_KEY_HOLD  = (0.003, 0.006)         # 单个普通字符的按住时长（keydown→keyup）
KBD_KEY_GAP   = (0.02, 0.035)          # 组合键键间间隔（调定后勿轻改）
KBD_WIN_HOLD  = (0.06, 0.09)           # Win 家族按住时长（实测 ≥60ms 才稳触发 shell 热键）
KBD_TEXT_GAP  = (0.002, 0.006)         # 逐字发送（`_foreground_send_keys`）字间间隔
KBD_BLOCK_GAP = (0.08, 0.45, 0.18)     # {KEY} 块间隔＝send_shortcut 的 interval 默认
# ── 鼠标 ──
# 快档定档：**太快会被 Windows 拒绝点击** ⇒ 快档 = 人影档 ×1.6（移动 4000 / 拖拽 2500），
#   **不再往上加**：再快会丢点击。
MOUSE_PX_PER_SEC = 4000.0              # 移动速度（像素/秒）：1000px≈0.25s
MOUSE_MIN_SEC    = 0.08                # 短距离下限（几乎瞬到，但不瞬移）
MOUSE_MAX_SEC    = 0.40                # 长距离上限（横穿全屏也就 0.4s）
DRAG_PX_PER_SEC  = 2500.0              # 拖拽速度（比移动慢一档，要落准点）
DRAG_MIN_SEC     = 0.12
DRAG_MAX_SEC     = 0.60
# 「人影档」（human=true）—— **只改速度**；轨迹算法（缓入缓出/对数正态抖动/弧线，`_human_trace`）一个字节不动
MOUSE_PX_PER_SEC_HUMAN = 2500.0        # 移动：人的手速本来就快（FPS 拉枪更快），慢的是"落点不飘"，不是速度
MOUSE_MIN_SEC_HUMAN    = 0.14
MOUSE_MAX_SEC_HUMAN    = 0.60
DRAG_PX_PER_SEC_HUMAN  = 1600.0        # 拖拽：比移动再稳一点（滑块验证码常看这一段）
DRAG_MIN_SEC_HUMAN     = 0.20
DRAG_MAX_SEC_HUMAN     = 0.80
# 人影档允许**自带速度**：调用方传 `speed`（像素/秒）⇒ 夹在 `[HUMAN_SPEED_MIN, 人影档上限]` 内取用，
#   上限就是上面那对人影档值（移动 2500／拖拽 1600）——想传 300（慢）、600（正常）、1000（快）都行。
#   只有人影档能调速；快档**没有调速度的选项**（它就是那么快）。
#   ⚠ **同时必须把"时长界"换掉**：人影档原本夹 0.14~0.60 秒，若沿用，`speed=300` 会被悄悄夹回
#     ≈1667 px/s（等于白调）⇒ 自定义速度时改用下面这对**防呆界**，且**夹取一律写进回执**。
HUMAN_SPEED_MIN        = 20.0          # 自定义速度下界（再慢就等于把工具挂住了）
HUMAN_SEG_MIN_SEC      = 0.02          # 自定义速度下：单段最短秒数
HUMAN_SEG_MAX_SEC      = 5.0           # 自定义速度下：单段最长秒数（防呆上限）
# **相对位移也能调速** —— 它不是"轨迹时长"，而是把增量**切成小块、按时间铺开**送出。
#   切块间隔＝一帧（60fps ≈ 16.7ms）；步长＝速度×间隔（不足 1px 按 1px，整数累加防漂移）。
REL_PACING_INTERVAL_MS = 16.0          # 相对位移铺时间的默认帧间隔（毫秒）


def _mouse_speed(human=False, drag=False, speed=None):
    """取鼠标速度档 (像素/秒, 下限秒, 上限秒)：human=True → 人影档；默认 → 快档。**只给速度，不动轨迹。**

    `speed`（**只有人影档可调速**）：给了 `speed` ⇒ 当像素/秒用，**夹在
      `[HUMAN_SPEED_MIN, 人影档上限]`**（移动 2500／拖拽 1600），夹了由调用方写进回执；
      并把**时长界换成防呆界**（`HUMAN_SEG_MIN_SEC ~ HUMAN_SEG_MAX_SEC`）——否则原来那对 0.14~0.60s
      会把"300 px/s"悄悄拉回 ≈1667 px/s（铁律：夹了必须报，不许静默）。
      快档（human=False）传了 `speed` ⇒ **抛错**（它就是那么快，没有调速度这个选项）。"""
    if speed is not None:
        if not human:
            raise ValueError(
                "自定义速度只在**人影档**可用（传 human=true）："
                "快档速度是固定的（移动 %g／拖拽 %g 像素/秒），它没有调速度的选项"
                % (MOUSE_PX_PER_SEC, DRAG_PX_PER_SEC))
        cap = DRAG_PX_PER_SEC_HUMAN if drag else MOUSE_PX_PER_SEC_HUMAN
        px = max(HUMAN_SPEED_MIN, min(float(speed), cap))
        return (px, HUMAN_SEG_MIN_SEC, HUMAN_SEG_MAX_SEC)
    if drag:
        return ((DRAG_PX_PER_SEC_HUMAN, DRAG_MIN_SEC_HUMAN, DRAG_MAX_SEC_HUMAN) if human
                else (DRAG_PX_PER_SEC, DRAG_MIN_SEC, DRAG_MAX_SEC))
    return ((MOUSE_PX_PER_SEC_HUMAN, MOUSE_MIN_SEC_HUMAN, MOUSE_MAX_SEC_HUMAN) if human
            else (MOUSE_PX_PER_SEC, MOUSE_MIN_SEC, MOUSE_MAX_SEC))


def _kbd_gap_speed(human=False, cps=None):
    """打字间隔（**与 `_mouse_speed` 逐字同形**）＋可选的 `cps` 自定义速度。

    `cps`（**字/秒**）——与鼠标的 `speed` 同形：
    ⇒ 规则与鼠标那条一致：
      · **只有人影档可调速**（human=true）；**快档传 cps ⇒ 抛错**（它就是那么快，没有调速这个选项）；
      · 夹在 `[KBD_CPS_MIN, KBD_HUMAN_CPS]`；**夹了必须报**（返回的 note 由调用方写进回执）；
      · 抖动带按**同一比例**缩放（保住人影档的手感：0.556×中位 ~ 1.444×中位）。

    返回 `(间隔元组, 说明)`；未传 cps ⇒ 说明为空串（老行为逐字不变）。
    """
    if cps is None:
        return (KBD_HUMAN_GAP if human else KBD_TEXT_GAP), ""
    if not human:
        raise ValueError("自定义打字速度只在**人影档**可用（传 human=true）："
                         "快档打字速度是固定的（逐字 %g~%gms），它没有调速度这个选项"
                         % (KBD_TEXT_GAP[0] * 1000, KBD_TEXT_GAP[1] * 1000))
    _want = float(cps)
    _c = max(KBD_CPS_MIN, min(_want, KBD_HUMAN_CPS))
    _g = 1.0 / _c
    if _want > KBD_HUMAN_CPS:
        _note = "想 %g 字/秒 ⇒ **夹到人影档上限 %g 字/秒**" % (_want, KBD_HUMAN_CPS)
    elif _want < KBD_CPS_MIN:
        _note = "想 %g 字/秒 ⇒ **夹到下限 %g 字/秒**" % (_want, KBD_CPS_MIN)
    else:
        _note = "%g 字/秒（逐字中位 %.0fms）" % (_c, _g * 1000)
    return ((_g * 0.556, _g * 1.444, _g), _note)


def _foreground_send_keys(target, *key_strings, gap=None, literal=False):
    """前台键盘注入：逐字 keybd_event，兼容 Shift 组合字符。

    提速：原来每字后面固定 `_human_pause(0.01, 0.03)`
      （10~30ms）+ `_send_combo` 内部还有两段 20~35ms ⇒ **每字约 65ms**（45 字要 3s）。
      现在这里降到 2~6ms；`_send_combo` 里对「单个普通字符」也走快路（见那里）。
    """
    global _last_key_method, _last_key_verified
    for key in key_strings:
        _send_combo(key, literal=literal)
        _human_pause(*(gap or KBD_TEXT_GAP))
    _last_key_method = "keybd_event"
    _last_key_verified = False
    return len(key_strings)

_last_key_method = "?"
_last_key_verified = False




# 扩展 VK 映射（{KEY} 语法）
_SYMBOLIC_MAP = {**_SI_VK_MAP,
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "bs": 0x08, "backspace": 0x08,
    "space": 0x20, "delete": 0x2E, "pgup": 0x21, "pgdn": 0x22,
    "home": 0x24, "end": 0x23, "insert": 0x2D,
}
for _k in list(_SYMBOLIC_MAP.keys()):
    _SYMBOLIC_MAP[_k.upper()] = _SYMBOLIC_MAP[_k]


# ── E0 扩展键处理（这是"WIN 键发不出去"的真因）──
# 实测：{WIN}/{WIN+R}/press:["win"] 三种调法全无反应，而 R 键照旧进应用（变成文字落进输入框）——
# 因为**普通应用只看 VK，系统 shell 还要看 E0 前缀**：WIN 家族是扩展键（LWin=E0 5B、RWin=E0 5C、
# Apps=E0 5D），注入时不带 KEYEVENTF_EXTENDEDKEY，shell 就不认它的"身份"，于是开始菜单/运行框都不触发。
# 旧版 mcp_server_backup.py 同样缺这一条 ⇒ 不是回归，是从没实现过。
_EXTENDED_VKS = {
    0x5B, 0x5C, 0x5D,                                  # LWin / RWin / Apps
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,    # PgUp/PgDn/End/Home/←/↑/→/↓
    0x2D, 0x2E,                                        # Insert / Delete
    0x6F, 0x90,                                        # 小键盘 / 与 NumLock
    0xA3, 0xA5,                                        # 右 Ctrl / 右 Alt
}
_VK_SCAN_OVERRIDE = {0x5B: 0x5B, 0x5C: 0x5C, 0x5D: 0x5D}   # WIN 家族扫描码（MapVirtualKey 在部分布局下给 0）


def _vk_scan_code(vk):
    """VK → 扫描码：WIN 家族用固定值，其余问系统，取不到就退回 VK 本身。"""
    sc = _VK_SCAN_OVERRIDE.get(vk)
    if sc:
        return sc
    try:
        sc = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    except Exception:
        sc = 0
    return sc or vk



# 实测定案（系统级读回 + 肉眼双证）：
#   SendInput(SCANCODE|EXT, wVk=0, wScan=0x5B) 按下 LWIN ⇒ GetAsyncKeyState(LWIN)=0x8001（系统认了）
#   keybd_event(VK_LWIN, 0x5B, EXT)            按下 LWIN ⇒ 一直 0x0（**根本没进系统**）
#   原因：keybd_event 的 dwFlags 只有 EXTENDEDKEY/KEYUP，**没有 SCANCODE 开关**，
#        只能发"虚拟键形态"；而 Win 键在这台机器上**只认扫描码形态**。
#        别的键两种形态都认（对照：SHIFT/A 走 SendInput 都能登记），所以"别处 keybd_event 最好用、
#        偏偏 Win 键不行"。
#   ⇒ 修法：**只把 Win 家族改道** 走 SendInput 纯扫描码；其余键一个字节不动
#      （有线终端输入依赖 keybd_event 那条路——注入方式不许整体替换）。
_SCANCODE_ONLY_VKS = {0x5B, 0x5C, 0x5D}   # LWin / RWin / Apps


def _si_move_abs(x, y):
    """SendInput 绝对移动光标（0~65535 归一化，物理像素空间）。
    复用本文件现成的 INPUT / SendInput 设施（与 `_si_scancode_key` 同一套）。
    绝对移动**不受指针加速影响**，这是它比相对移动可靠的地方。"""
    try:
        sw = user32.GetSystemMetrics(0) or 1920
        sh = user32.GetSystemMetrics(1) or 1080
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.mi = MOUSEINPUT(int(x * 65535 / max(1, sw - 1)),
                            int(y * 65535 / max(1, sh - 1)),
                            0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, None)
        return user32_si.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    except Exception:
        return 0


def _si_scancode_key(vk, keyup=False):
    """用 SendInput 发「纯扫描码」按键（wVk=0）。仅 Win 家族使用。"""
    sc = _vk_scan_code(vk)
    flags = KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY
    if keyup:
        flags |= KEYEVENTF_KEYUP
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(0, sc, flags, 0, None)
    return user32_si.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _key_event(vk, keyup=False):
    """统一的按键注入。**除 Win 家族外仍走 keybd_event**——有线终端输入依赖它，不许换。
    两处缺失已补：① 扩展键带 KEYEVENTF_EXTENDEDKEY；② 扫描码用真实值。
    ★Win 家族（0x5B/0x5C/0x5D）额外改道 _si_scancode_key()：keybd_event 根本发不进系统
    （系统级实测定案），只有 SendInput 纯扫描码形态能被 shell 认。"""
    if vk in _SCANCODE_ONLY_VKS:
        _si_scancode_key(vk, keyup)      # Win 家族：keybd_event 发不进去，只能走纯扫描码
        return
    flags = KEYEVENTF_KEYUP if keyup else KEYEVENTF_KEYDOWN
    if vk in _EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    user32.keybd_event(vk, _vk_scan_code(vk), flags, 0)


# ══════════════════ 输入策略：按「窗口族」分流 ══════════════════
#   **不要"改一条路"，要"按窗口族分流"**。三条铁规矩：
#     ① **默认通道一个字节不动**——有线终端依赖 keybd_event/PostMessage；
#     ② 只有**实测证明默认通道进不去**的窗口族，才在下面这张表里定向改道；
#     ③ 每条改道都带**读回验证**，回执要说清走的是哪条通道、验没验过。
#   表是「规则列表、先匹配先赢」，不是给某个窗口写死——新窗口族往表里加一条即可（这才是通用）。
_INPUT_RULES = [
    # shell 对话框（explorer 的「运行」框 class=#32770 等）：
    #   实测（判据 IsWindowEnabled(确定) + 标记文件）——
    #   keybd_event 字母 ✗、SendInput 虚拟键 ✗、消息级（WM_CHAR/WM_SETTEXT）✗
    #   **只有 SendInput 纯扫描码形态 ✓**：字真进去，而且把「确定」从禁用点成可用；
    #   UIA SetValue 能写字但**点不亮按钮**（对话框认为"没有输入过"）⇒ 这类窗口连 UIA 优先也让位。
    #   ★★顺序红线：本族**必须 scancode 真打字在先**、
    #      UIA SetValue 只做「真打字没进去/被输入法转掉」时的纠正兜底。把 SetValue 提到前面 ⇒
    #      字写对了、按钮还是灰的 ⇒ 整条「写入→提交」链断在提交上。改下面几行之前先读完这三行。
    {"name": "scancode", "class": "#32770", "proc": ("explorer",), "skip_uia": True,
     "why": "shell 对话框：keybd_event/虚拟键/消息级都进不去，只有纯扫描码写得进且能点亮按钮"},
]


def _input_profile(hwnd):
    """按目标窗口判定输入策略：命中表的规则 dict；没命中返回 {} ⇒ 走**默认通道**（原样）。"""
    try:
        cls = _hwnd_cls(hwnd) or ""
        proc = (_fg_proc_name(hwnd) or "").lower()
        title = _hwnd_text(hwnd) or ""
    except Exception:
        return {}
    for r in _INPUT_RULES:
        if r.get("class") and r["class"] != cls:
            continue
        if r.get("proc") and not any(proc.startswith(p) for p in r["proc"]):
            continue
        if r.get("title_has") and r["title_has"] not in title:
            continue
        return r
    return {}


def _type_scancode(s, gap=None):
    """逐字符「真打字」：SendInput **纯扫描码**形态（含需要 Shift 的符号 `>` `:` 等）。
    这是**定向通道**，只给 `_input_profile()` 判为 scancode 的窗口族用；默认通道不受影响。
    ⚠⚠ **本函数必须排在 UIA SetValue 之前**（顺序红线）：只有"真输入"能把「确定」
      从【禁用】翻成【可用】；把 SetValue 提到前面 = 字对了、按钮还是灰的 ⇒ 提交不上去。"""
    for ch in s:
        try:
            vk = user32.VkKeyScanW(ord(ch))
            if vk == -1:
                continue
            low, need_shift = vk & 0xFF, bool((vk >> 8) & 1)
            sc, ssc = _vk_scan_code(low), _vk_scan_code(0x10)      # 0x10 = VK_SHIFT
            if need_shift:
                _si_kbd_raw(0, ssc, KEYEVENTF_SCANCODE)
            _si_kbd_raw(0, sc, KEYEVENTF_SCANCODE)
            _si_kbd_raw(0, sc, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)
            if need_shift:
                _si_kbd_raw(0, ssc, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)
            _human_pause(*(gap or KBD_SCAN_GAP))
        except Exception:
            continue


def _scancode_enter():
    """回车也走扫描码形态（同一族窗口里，注入/消息级回车实测都不可靠，真按键才作数）。"""
    _si_kbd_raw(0, _vk_scan_code(0x0D), KEYEVENTF_SCANCODE)
    _si_kbd_raw(0, _vk_scan_code(0x0D), KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)


# ── `_uia_setvalue()` 已**删除**：**禁止整篇写** ──
#   它做的事＝`ValuePattern.SetValue`＝**整篇覆盖**（**不是**"在光标处打字"；记事本实录：文档
#   `kjaa bbcc dd` 一发之后**只剩新写的字**）。UIA **没有**"在光标处插入"的 API ⇒ 这条机制
#   **从机制上就只能整篇写** ⇒ 整条撤掉：产品里**任何地方都不再调用 `SetValue`**
#   （判据＝`grep -n "SetValue("` **0 命中** ＋ 静态闸门里那条同义断言）。
#   两个调用点都改走键盘路：① `_do_send_keystrokes` 的 UIA 快路**整段删**；② shell 对话框那族的
#   "真打字没进去时用它纠正文"改为**如实报 `verified=False`**（宁可写不进去，也绝不整篇覆盖）。
#   ⚠ 「UIA 能写字却点不亮「确定」」的判断保留在 `send_keystrokes` 的工具面描述里，**不随代码删除**。


# UIA 读文族整段删除：
#   _read_edit_value / _READ_LAYER_MEMO / _read_layer_memo_key / _read_layer_memo_set /
#   _read_via_wmgettext / _uia_doc_text / _read_target_text


def _send_combo(keys_str, literal=False):
    """发送单个组合键（如 'ctrl+c'），用 keybd_event 保持兼容性。

    形参 `literal`（**带默认值 ⇒ 组合键那条老路逐字不变**）：
      `literal=True` ＝ 这是**要打出去的正文**（逐字）⇒ **大小写有意义**（大写字母必须靠 Shift 才出得来）；
      `literal=False`（默认）＝ **组合键 token**（`{WIN+R}` 里的 `R` 是**键名**，给它补 Shift 会把热键
      发成 Win+Shift+R ⇒ 那条大小写修正必须保留）。
    ⚠ **真机缺陷**：原先**不区分**这两种情形——那句 `k.lower()` 对**所有**单个字母生效
      ⇒ 正文里的 `V` 也按小写查表 ⇒ **Shift 位恒为 0**
      ⇒ `send_keystrokes(text=VT)` 打出来是 `vt`。扫描码那条路（`_type_scancode`）用原字符、**没这毛病**。"""
    # 正文里的**换行/制表符**要原样按键——原来它们落进下面"空 token"分支
    #   （`strip()==""`）会被当成**空格**打出去（`text` 里带 `\n` 就变成空格，静默改语义）。
    if keys_str in ("\n", "\r", "\r\n"):
        _key_event(0x0D, False)
        _key_event(0x0D, True)
        return
    if keys_str == "\t":
        _key_event(0x09, False)
        _key_event(0x09, True)
        return
    keys = [k.strip() for k in keys_str.split("+")]
    vks = []
    for k in keys:
        vk = None
        if k:
            vk = _SYMBOLIC_MAP.get(k.lower())
            if vk is None and len(k) == 1:
                # 大小写处理：原来传 ord(k) 原样大小写 ⇒ **大写字母**
                #   被 VkKeyScanW 判成"需要按住 Shift 才打得出" ⇒ 代码补发 SHIFT ⇒
                #   {WIN+R} 实际发出去的是 Win+Shift+R（单字母组合键中此枪），
                #   所以运行框永远打不开。字母一律按小写查，真需要 Shift 的符号不受影响。
                # `literal=True`（正文逐字）时**保留原大小写**——否则大写字母的 Shift 位
                #   查不出来、打出来变小写（真机：`VT` → `vt`）。小写化只对**组合键 token** 成立。
                _ch = k.lower() if (k.isalpha() and not literal) else k
                key_info = ctypes.windll.user32.VkKeyScanW(ord(_ch))
                if key_info == -1:
                    # 本布局打不出的字符（非 ASCII：中文/emoji 等）⇒ **跳过、一个键都不发**。
                    #   原来会落成 vk=0xFF ＋ 补发一个 Shift（-1 的 bit8 为 1）——
                    #   实测后果：字没进去，还把目标窗输入法中/英态**翻转**（4 个中文字＝4 次翻转）。
                    #   直发非 ASCII 只在 `unicode_text=true` 时走 Unicode 注入（_type_mixed_text），
                    #   默认由 `_do_send_keystrokes` 在动手前**明确报错**。
                    continue
                vk = key_info & 0xFF
                if (key_info >> 8) & 1:  # Shift needed
                    if 0x10 not in vks:
                        vks.append(0x10)  # VK_SHIFT
        elif keys_str.strip() == "":
            vk = 0x20  # 空格
        if vk is not None and vk not in vks:
            vks.append(vk)
    if not vks:
        return
    mods = {0x11, 0x10, 0x12, 0x5B, 0x5C}     # 修饰键：Ctrl/Shift/Alt/LWin/RWin
    # 提速：下面四段原本统一 20~35ms。
    #   那是**给组合键**调的（修饰键必须先按住才配得上，热键也不能被抖丢）。
    #   可逐字打字时 vks 只有一个普通字符，**keyup 之后还睡 20~35ms 纯属浪费**
    #   ——实测每字 40~70ms 就耗在这里；叠加 `_foreground_send_keys` 的 10~30ms ⇒ 每字 ~65ms。
    #   规则：**单个普通字符走快路**（按住 3~6ms、keyup 后不睡）；**真组合键一个字不动**。
    _plain_single = (len(vks) == 1 and vks[0] not in mods)
    for vk in vks:
        if vk in mods:
            _key_event(vk, keyup=False)
            # Win 家族（扫描码形态）留足按住时间，实测 60ms 稳定触发 shell 热键
            # 抖动只往上加、绝不低于原来的值（免得把热键抖丢）
            if vk in _SCANCODE_ONLY_VKS:
                _human_pause(*KBD_WIN_HOLD)
            else:
                _human_pause(*KBD_KEY_GAP)
    for vk in vks:
        if vk not in mods:
            _key_event(vk, keyup=False)
            if _plain_single:
                _human_pause(*KBD_KEY_HOLD)    # 单字：只留"按得住"的极小时间
            else:
                _human_pause(*KBD_KEY_GAP)
    for vk in reversed(vks):
        if vk not in mods:
            _key_event(vk, keyup=True)
            if not _plain_single:
                _human_pause(*KBD_KEY_GAP)      # 单字：松开后不睡（原来还睡 20~35ms）
    for vk in reversed(vks):
        if vk in mods:
            _key_event(vk, keyup=True)
            _human_pause(*KBD_KEY_GAP)


def _unescape_braces(s):
    """`{{`/`}}` ⇒ 字面花括号（先转占位符，避免与 {} 键语言冲突）。"""
    return (s or "").replace("{{", "\x00LB\x00").replace("}}", "\x00RB\x00")


def _restore_braces(s):
    return (s or "").replace("\x00LB\x00", "{").replace("\x00RB\x00", "}")


def _key_token_ok(tok):
    """`{...}` 里的东西是不是**认得的按键**（普通键/修饰键组合）？"""
    keys = [k.strip() for k in str(tok or "").split("+") if k.strip()]
    if not keys:
        return False
    for k in keys:
        if _SYMBOLIC_MAP.get(k.lower()) is None and _si_parse_key(k) is None:
            return False
    return True


def _split_key_parts(text):
    """把字符串拆成 [(是不是按键 token, 片段)]——**统一流**的解析入口。

    规则（不报错、直接输入）：认得的 `{KEY}`（{ENTER}/{TAB}/{CTRL+C}…）⇒ token；
    **认不出的花括号** ⇒ 退回**字面**（连花括号一起按普通字符打出去）；`{{`/`}}` ⇒ 字面花括号。
    """
    import re as _re
    out = []
    for part in _re.split(r'(\{[^}]+\})', _unescape_braces(text)):
        if not part:
            continue
        if (part.startswith("{") and part.endswith("}") and len(part) > 2
                and _key_token_ok(part[1:-1])):
            out.append((True, part[1:-1]))
        else:
            out.append((False, _restore_braces(part)))
    return out


def _post_key_combo(hwnd, tok):
    """后台通道发一个按键/组合键（PostMessage：修饰键先按下、普通键按下抬起、最后逆序抬修饰键）。

    实测定则（本机 Win11 记事本 Edit 子窗 · **无间隔连发**）：
      · **换行/制表符必须走 `WM_CHAR`**（0x0D／0x09）——它们属于"字符插入"路，实测顺序正确
        （`A1⏎A2` ✓）；原来发 `WM_KEYDOWN(VK_RETURN)` 时换行会**延后到后续文字之后**
        （`B1B2⏎` ✗＝顺序错）。⇒ 这是"后台回车不换行/换行跑位"的真因。
      · **退格必须走 `WM_KEYDOWN(VK_BACK)`**：`WM_CHAR(0x08)` 实测被无视（`C1C2` ✗）；
        KEYDOWN VK_BACK 按顺序生效（`D1`+BS+`D2` → `DD2` ✓）。
      · **VK 必须从 `_SYMBOLIC_MAP` 解析**（原来只查 `_SI_VK_MAP`）：`{BS}`/`{SPACE}`/`{PGUP}` 这些
        在 `_SYMBOLIC_MAP` 里有、在 `_SI_VK_MAP` 里没有 ⇒ 原来会被**静默跳过**
        （实测 `Z1{BS}Z2` 打成 `Z1Z2`，一个键都没发）。
      · 其余键（方向键/ESC/组合键）仍走 KEYDOWN/UP（后台对这类控制键本来就不保证，需前台）。
    """
    keys = [k.strip() for k in str(tok or "").split("+") if k.strip()]
    _MODS = ("ctrl", "shift", "alt", "win", "lwin", "rwin")
    mods = [k for k in keys if k.lower() in _MODS]
    norm = [k for k in keys if k.lower() not in _MODS]
    if not mods and len(norm) == 1:
        _ch = {"enter": 0x0D, "return": 0x0D, "tab": 0x09}.get(norm[0].lower())
        # 只有 **Edit/RichEdit 族** 才转 WM_CHAR（顺序正确，见函数头实测）；
        # 终端/其它族保持老路 KEYDOWN/UP（不许为了记事本改终端）。
        if _ch is not None and _is_text_edit_hwnd(hwnd):
            user32.PostMessageW(hwnd, WM_CHAR, _ch, 0)
            return
    for m in mods:
        vk = _SYMBOLIC_MAP.get(m.lower(), 0)
        if vk:
            user32.PostMessageW(hwnd, WM_KEYDOWN, vk, _make_lparam(vk))
    for k in norm:
        vk = _SYMBOLIC_MAP.get(k.lower()) or _si_parse_key(k)
        if vk is None:
            continue
        user32.PostMessageW(hwnd, WM_KEYDOWN, vk, _make_lparam(vk))
        user32.PostMessageW(hwnd, WM_KEYUP, vk, _make_lparam(vk, True))
    for m in reversed(mods):
        vk = _SYMBOLIC_MAP.get(m.lower(), 0)
        if vk:
            user32.PostMessageW(hwnd, WM_KEYUP, vk, _make_lparam(vk, True))


def _post_text_tokens(hwnd, text):
    """后台通道的**统一流**：正文走 WM_CHAR（发给文本输入子控件），`{KEY}` 按位置走 WM_KEYDOWN/UP。

    与前台同一套规则：认不出的花括号＝字面字符（不报错）；裸换行由 `_post_text` 转成回车键消息
    （同一 PostMessage 队列 ⇒ 顺序天然有序）。
    """
    for is_tok, piece in _split_key_parts(text):
        if is_tok:
            _post_key_combo(hwnd, piece)
        else:
            _post_text(hwnd, piece)


def _parse_sendinput_text(text, interval=None, typing_interval=None, unicode_text=False):
    """前台打字**统一流**：正文逐字 ＋ `{KEY}` 按键 ＋（`unicode_text=true` 时）非 ASCII 的 Unicode 注入，
    全部按字符串顺序发出（同一条系统输入队列 ⇒ 顺序有保证，回车不会抢跑）。

    · 认不出的花括号 ⇒ 按字面输入（不报错）；`{{` / `}}` ⇒ 字面花括号；
    · `unicode_text=true` ⇒ 非 ASCII 走 `KEYEVENTF_UNICODE`（不经过输入法）；
      默认 False ⇒ 非 ASCII 由 `_send_combo` 跳过（调用方负责提前明确报错）。
    · interval：`{KEY}` 之间间隔；typing_interval：普通字符之间间隔（可传 (lo,hi,中位) 抖动带）。
    """
    global _last_key_method, _last_key_verified
    for is_tok, piece in _split_key_parts(text):
        if is_tok:
            _send_combo(piece)
            if interval is None:
                _human_pause(*KBD_BLOCK_GAP)
            elif isinstance(interval, (tuple, list)):
                _human_pause(*interval)          # 抖动带（lo, hi, 中位）⇒ 有波动，不是打字机
            else:
                time.sleep(interval)
        else:
            for ch in piece:
                if unicode_text and not ch.isascii():
                    _si_unicode_char(ch)          # 显式允许 ⇒ Unicode 注入
                else:
                    _send_combo(ch, literal=True)  # 正文逐字 ⇒ 保留大小写（大写要靠 Shift）
                if typing_interval is None:
                    _human_pause(*KBD_CHAR_GAP)
                elif isinstance(typing_interval, (tuple, list)):
                    _human_pause(*typing_interval)   # 抖动带 ⇒ 人影档打字有波动
                else:
                    time.sleep(typing_interval)
    _last_key_method = "keybd_event"
    _last_key_verified = False


# ══════════════════════════════════════════════════════════════════════════════
# 【直发非 ASCII（中文/emoji…）】
#   背景：原设计**不允许直发中文**（中文走输入法拼音上屏）。但"不允许"的实现是坏的：
#     非 ASCII 字符落进 `_send_combo` 的 VkKeyScanW=-1 分支 ⇒ 补发 Shift＋VK255
#     ⇒ **字没进去，还把目标窗输入法中/英态翻转**（实测 4 个中文字＝4 次翻转），且静默。
#   做法：工具面新增 `unicode_text`（默认 False）：
#     · 默认 ⇒ 非 ASCII **动手前明确报错**（不发任何键、不碰输入法）；
#     · true ⇒ 非 ASCII 段走 SendInput 的 `KEYEVENTF_UNICODE`（**不经过输入法**），
#       ASCII 段仍走原键盘路（老手感/老兼容性零改动）。
#   边界（如实）：Unicode 注入对绝大多数 Win32/WinUI 文本控件有效；DirectInput/游戏窗口通常收不到
#     （那种场景继续用 ASCII＋键位）；识别层已删 ⇒ 只如实报"发了哪条通道"，不做读回验证。
# ══════════════════════════════════════════════════════════════════════════════

def _si_unicode_char(ch):
    """SendInput 注入**一个字符**（KEYEVENTF_UNICODE，wVk=0 / wScan=码元）——不经过输入法。

    超出 BMP 的字符（emoji）按 UTF-16 拆代理对，逐码元各发一对 down/up
    （SendInput 的 UNICODE 形态以 16-bit 码元为单位，这是标准做法）。
    """
    code = ord(ch)
    if code > 0xFFFF:
        code -= 0x10000
        units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
    else:
        units = [code]
    for cu in units:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = 1                    # INPUT_KEYBOARD
            inp.ki = KEYBDINPUT(0, cu, flags, 0, None)
            user32_si.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _type_mixed_text(text, gap=None):
    """前台逐字注入：**ASCII 走原键盘路，非 ASCII 走 Unicode 注入**。

    · 为什么混着走：ASCII 那条路（`_send_combo`，含 Shift/OEM 符号、大小写）是调好的；
      Unicode 注入只接管"键盘布局打不出来的字符"，不改变 ASCII 的手感与兼容性。
    · 只给 `unicode_text=true` 调用（默认不允许直发非 ASCII，见 `_do_send_keystrokes` 的前置闸）。
    """
    for ch in text:
        if ch.isascii():
            _send_combo(ch, literal=True)
        else:
            _si_unicode_char(ch)
        _human_pause(*(gap or KBD_TEXT_GAP))
    return "keybd_event+unicode"


def _text_input_child(parent_hwnd):
    """找『能收 WM_CHAR 的文本输入子控件』（Edit/RichEdit/Scintilla 族），找不到 ⇒ None。

    实测的洞：`mode=background` 的 `_post_text` 把 WM_CHAR 发给**顶层窗**——
      记事本（Win11，顶层是框架窗、真正吃字的是子 Edit）**一个字都进不去**，而且不报错。
      ⇒ 后台通道先解析文本子控件；找不到（终端/浏览器/自绘控件）⇒ 原样用 target（老行为不变）。
    """
    result = None

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(child, lparam):
        nonlocal result
        if result:
            return False
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, buf, 256)
        if buf.value.lower() in _TEXT_INPUT_CLASSES and user32.IsWindowVisible(child):
            rect = wintypes.RECT()
            user32.GetWindowRect(child, ctypes.byref(rect))
            w, h = _rect_size(rect)
            if w >= 50 and h >= 30:
                result = child
                return False
        user32.EnumChildWindows(child, enum_cb, 0)
        return True

    try:
        user32.EnumChildWindows(parent_hwnd, enum_cb, 0)
    except Exception:
        return None
    return result


_TEXT_INPUT_CLASSES = {"edit", "richedit", "richedit20a", "richedit20w",
                       "richedit50w", "richeditd2dpt", "scintilla", "textedit"}


def _is_text_edit_hwnd(hwnd):
    """该窗口是不是「文本编辑控件」（Edit/RichEdit/Scintilla 族）？

    后台换行/制表符**按窗口族分流**，终端老路一个字不动——
      · Edit/RichEdit 族：换行/制表必须走 `WM_CHAR`（0x0D/0x09）——实测 `WM_KEYDOWN(VK_RETURN)`
        的换行会**延后到后续文字之后**（换行跑位）；`WM_CHAR` 顺序正确。
      · **终端族/其它**：保持原路 `WM_KEYDOWN/UP`（既有实现一直这么发；不许为了记事本去改终端）。
    判据只认类名：认不出 ⇒ 当"非编辑控件"处理 ⇒ 走老路（保守）。
    """
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value.lower() in _TEXT_INPUT_CLASSES
    except Exception:
        return False


def _check_ime():
    """检测键盘布局是否为中文。返回 True 表示中文布局，keybd_event 可能被干扰。"""
    try:
        hkl = ctypes.windll.user32.GetKeyboardLayout(0)
        return (hkl & 0xFFFF) == 0x0804
    except:
        pass
    return False


# ========== Imm32 IME 控制 API ==========

# Imm32 API 必须显式声明 argtypes/restype，否则句柄可能截断导致 NULL。HIMC 需手动定义
_HIMC = ctypes.c_void_p
_ctypes_wnd = ctypes.windll.imm32
_ctypes_wnd.ImmGetContext.argtypes = [wintypes.HWND]
_ctypes_wnd.ImmGetContext.restype = _HIMC
_ctypes_wnd.ImmReleaseContext.argtypes = [wintypes.HWND, _HIMC]
_ctypes_wnd.ImmReleaseContext.restype = ctypes.c_bool
_ctypes_wnd.ImmGetOpenStatus.argtypes = [_HIMC]
_ctypes_wnd.ImmGetOpenStatus.restype = ctypes.c_bool
_ctypes_wnd.ImmSetOpenStatus.argtypes = [_HIMC, ctypes.c_bool]
_ctypes_wnd.ImmSetOpenStatus.restype = ctypes.c_bool
_ctypes_wnd.ImmGetDefaultIMEWnd.argtypes = [wintypes.HWND]
_ctypes_wnd.ImmGetDefaultIMEWnd.restype = wintypes.HWND
# ImmSimulateHotKey: 模拟 IME 热键 (IME_THOTKEY_IME_NONIME_TOGGLE=0x10，相当于按 Shift 切换中/英)
_ctypes_wnd.ImmSimulateHotKey.argtypes = [wintypes.HWND, ctypes.c_ulong]
_ctypes_wnd.ImmSimulateHotKey.restype = ctypes.c_bool
# ImmGetConversionStatus / ImmSetConversionStatus: 检测/设置 IME 中英模式（所有输入法通用）
_ctypes_wnd.ImmGetConversionStatus.argtypes = [_HIMC, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong)]
_ctypes_wnd.ImmGetConversionStatus.restype = ctypes.c_bool
_ctypes_wnd.ImmSetConversionStatus.argtypes = [_HIMC, ctypes.c_ulong, ctypes.c_ulong]
_ctypes_wnd.ImmSetConversionStatus.restype = ctypes.c_bool

_IME_CMODE_ALPHANUMERIC = 0x0000  # 英文/字母数字模式
_IME_CMODE_NATIVE       = 0x0001  # 中文/本地输入模式
# **默认 IME 窗口**那条路的两个常量——TSF 输入法（Win11 记事本那类）
#   只有经 `ImmGetDefaultIMEWnd(hwnd)` 发 `WM_IME_CONTROL` 才读得到/设得动中英模式；
#   IMM 那对 API（ImmGetContext + ImmGetConversionStatus）对它是「读回 0 ⇒ 看起来是英文态」的假信号。
WM_IME_CONTROL          = 0x0283
IMC_GETCONVERSIONMODE   = 0x0001
IMC_SETCONVERSIONMODE   = 0x0002
# **输入法候选窗**的进程名（唯一出处）：拿"窗口变化"当**后检测**——候选框一可见就进快照、被记成"新增"。
#   消费方：「**新冒出来的输入法窗**」当信号（`_ime_new_windows`：`_win_delta_text` 的后检测 ＋ `auto` 的〈a〉）。
_IME_CAND_PROCS = {"textinputhost.exe", "ctfmon.exe"}


def _ime_new_windows(pre):
    """对比两份窗口快照，回**新冒出来的输入法窗**（`标题[进程]` 列表）。

    一个出口、两处共用：
      · `_win_delta_text()` ⇒ 回执里那句「⚠ 出现了**输入法候选窗**」（后检测）；
      · `_ime_auto_type()` ⇒ 判据〈a〉（打完 head 看有没有新窗冒出来）。
    判据＝`h not in pre` ＋（进程在 `_IME_CAND_PROCS` ／ 标题含 text input・输入法・候选）。
    `pre` 取不到 ⇒ **空表**（不猜）。**只读**，不改任何状态。
    """
    if not pre:
        return []
    try:
        cur = _win_snapshot_light()
    except Exception:
        return []
    return [f"{t}[{p}]" for h, (t, p) in cur.items()
            if h not in pre and ((p or "").strip().lower() in _IME_CAND_PROCS
                                 or "text input" in (t or "").lower()
                                 or "输入法" in (t or "")
                                 or "候选" in (t or ""))]

_LANG_NAMES = {0x0804: "中文简体", 0x0409: "英文(美国)", 0x0404: "中文繁体"}


_HKL_EN = 0x04090409   # 英文(美国) - 美式键盘
_HKL_ZH = 0x08040804   # 中文(简体) - 美式键盘
_WM_INPUTLANGCHANGEREQUEST = 0x0050


def _get_ime_status_detail(hwnd=None):
    """检测输入法状态。直接调用 _check_ime()，与 send_keystrokes 警告完全一致。"""
    try:
        if hwnd is None:
            # 严格规则：只认"记住的聚焦窗口"，**不拿前台窗口冒充目标**
            if not (_focus_hwnd and _is_window_alive(_focus_hwnd)):
                return {"hwnd": None, "win_title": "(未聚焦)", "is_cn": _check_ime()}
            hwnd = _focus_hwnd
        title_buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetWindowTextW(hwnd, title_buf, 256)
        win_title = title_buf.value or "(前台窗口)"
        is_cn = _check_ime()
        return {
            "hwnd": hex(hwnd), "win_title": win_title,
            "is_cn": is_cn,
        }
    except Exception as e:
        return {"error": str(e), "is_cn": None}


def _resolve_ime_target(hwnd=None):
    """严格规则：只认显式 hwnd 或"记住的聚焦窗口"；
    **不回退到前台窗口**——聚焦空了就报错（回退会让中/英切换落到别的窗口上，结果全错）。"""
    if hwnd: return hwnd, "指定窗口"
    if _focus_hwnd and _is_window_alive(_focus_hwnd): return _focus_hwnd, "已聚焦窗口"
    raise RuntimeError("未聚焦窗口或聚焦已失效，请先调用 focus_window（IME 目标按设计不回退到前台窗口）")


def _shift_toggle(hwnd):
    """SendInput SCANCODE 发送 Shift（绕过 IME 拦截）。"""
    _ensure_foreground(hwnd)
    time.sleep(0.05)
    shift_scan = 0x2A
    inputs = (INPUT * 2)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].ki.wVk = 0
    inputs[0].ki.wScan = shift_scan
    inputs[0].ki.dwFlags = KEYEVENTF_SCANCODE
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].ki.wVk = 0
    inputs[1].ki.wScan = shift_scan
    inputs[1].ki.dwFlags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP
    user32_si.SendInput(2, inputs, ctypes.sizeof(INPUT))


_IME_THOTKEY_IME_NONIME_TOGGLE = 0x10  # Shift 翻转中/英


def _set_ime_open(enable, hwnd=None):
    """切换中/英文状态（不切换输入法）。四大策略逐层尝试。"""
    try:
        hwnd, source = _resolve_ime_target(hwnd)
        title_buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetWindowTextW(hwnd, title_buf, 256)
        win_title = title_buf.value or "(无标题)"
        tid = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
        before_hkl = ctypes.windll.user32.GetKeyboardLayout(tid)
        label = f"[{win_title}]"

        def _mk(success, strategy, msg):
            after_win = ctypes.windll.user32.GetKeyboardLayout(tid)
            return {"success": success, "strategy": strategy, "win_title": win_title,
                    "before_hkl": hex(before_hkl), "after_hkl": hex(after_win),
                    "source": source, "message": f"{label} {source} · {msg}"}

        # ── 四大策略切换 IME 状态，_check_ime() 验证 ──
        # 0. 已经是目标状态 → 直接成功
        if _check_ime() == enable:
            return _mk(True, "0.unchanged",
                       f"已是{'中' if enable else '英'}文状态，无需切换")

        # 策略1: ImmSimulateHotKey（热键翻转，已验证可用）
        _ctypes_wnd.ImmSimulateHotKey(hwnd, _IME_THOTKEY_IME_NONIME_TOGGLE)
        time.sleep(0.15)
        if _check_ime() == enable:
            return _mk(True, "1.ImmSimulateHotKey",
                       f"ImmSimulateHotKey → {'中' if enable else '英'}")

        # 策略2: ImmSetConversionStatus（直接设中英模式）
        himc = _ctypes_wnd.ImmGetContext(hwnd) or _ctypes_wnd.ImmGetContext(0)
        if himc:
            target_conv = _IME_CMODE_NATIVE if enable else _IME_CMODE_ALPHANUMERIC
            if _ctypes_wnd.ImmSetConversionStatus(himc, target_conv, 0):
                _ctypes_wnd.ImmReleaseContext(hwnd, himc)
                if _check_ime() == enable:
                    return _mk(True, "2.ImmSetConversionStatus",
                               f"ImmSetConversionStatus → {'中' if enable else '英'}")
            _ctypes_wnd.ImmReleaseContext(hwnd, himc)

        # 策略3: SendInput SCANCODE Shift
        _ensure_foreground(hwnd); time.sleep(0.05)
        shift_scan = 0x2A
        inputs = (INPUT * 2)()
        inputs[0].type = INPUT_KEYBOARD; inputs[0].ki.wVk = 0; inputs[0].ki.wScan = shift_scan
        inputs[0].ki.dwFlags = KEYEVENTF_SCANCODE
        inputs[1].type = INPUT_KEYBOARD; inputs[1].ki.wVk = 0; inputs[1].ki.wScan = shift_scan
        inputs[1].ki.dwFlags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP
        user32_si.SendInput(2, inputs, ctypes.sizeof(INPUT))
        time.sleep(0.15)
        if _check_ime() == enable:
            return _mk(True, "3.SendInput·SCANCODE",
                       f"SendInput SCANCODE → {'中' if enable else '英'}")

        # 策略4: keybd_event Shift（不受 UIPI，最后兜底）
        user32.keybd_event(0x10, shift_scan, 0, 0)
        user32.keybd_event(0x10, shift_scan, KEYEVENTF_KEYUP, 0)
        time.sleep(0.15)
        if _check_ime() == enable:
            return _mk(True, "4.keybd_event",
                       f"keybd_event → {'中' if enable else '英'}")

        # 所有策略 API 未检测到变化（_check_ime 无法区分中英模式时正常），信任已生效
        return _mk(True, "5.trust",
                   f"已发送切换，信任生效 → {'中' if enable else '英'}")
    except Exception as e:
        return {"success": False, "message": str(e)}


def _ime_ctx_hwnds(hwnd):
    """IME 上下文的候选窗口：顶层 → 它的 Edit 子控件。
    运行框实测：ImmGetContext(顶层 #32770) 返回 NULL，上下文挂在带光标的 Edit 上。"""
    out = [hwnd]
    try:
        _fwe = ctypes.windll.user32.FindWindowExW
        _fwe.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
        _fwe.restype = wintypes.HWND
        child = _fwe(hwnd, None, "Edit", None)
        if child:
            out.append(child)
    except Exception:
        pass
    return out


def _ime_mode_imewin(hwnd, set_mode=None):
    """**经「默认 IME 窗口」读/写中英模式**——TSF 输入法（Win11 记事本那类）
    只有这条读得到：`ImmGetDefaultIMEWnd(hwnd)` ＋ `WM_IME_CONTROL/IMC_GETCONVERSIONMODE`。

    为什么必需（真机实测）：老路 `_ime_cn_for` 对 TSF 窗口**读不到**（`ImmGetContext`
    拿到的转换状态恒为 0）⇒ 老守卫按「**读不到就绝不盲翻**」静默跳过 ⇒ ASCII 照样被
    输入法吃掉（记事本实录：`zz88` → 「这种8」、`aa11 bb22` → 「啊啊1 不变2」）。

    返回：**当前转换模式**（int）｜`None` ＝ 这个 IME 不吃这条消息（那就老实承认读不到，不猜）。
    `set_mode` 给定则先设、再把设完的模式读回来（读回不回 ⇒ None）。"""
    try:
        dw = _ctypes_wnd.ImmGetDefaultIMEWnd(hwnd)
        if not dw:
            return None
        if set_mode is not None:
            user32.SendMessageW(dw, WM_IME_CONTROL, IMC_SETCONVERSIONMODE, int(set_mode))
        r = user32.SendMessageW(dw, WM_IME_CONTROL, IMC_GETCONVERSIONMODE, 0)
        if r is None or r == -1 or r == 0xFFFFFFFF:
            return None
        return int(r)
    except Exception:
        return None


def _ime_read_mode_deep(hwnd):
    """读**该窗族真实的**中英模式：扫全部可见子窗（**任一报中文就算中文**），再退回顶层；读不到 ⇒ None。

    实测（WPS Office，真机）：
      · 只看顶层会骗人——`ImmGetContext(顶层)` 有上下文、`_ime_force_ascii` 也"设成功"，但真正吃键的
        正文子窗（`_WwG`）仍报 `1025`（中文）；
      · 只取**第一个**能答的子窗也会被骗（有的子窗始终报 0，而正文字窗报 1025）⇒ 实测切回中文后
        首个子窗仍返回 0 ⇒ 漏判 ⇒ 守卫不切 ⇒ 空格被输入法吃掉。
      ⇒ 改成**"任一子窗报中文就算中文"**（宁可多切一次，也别漏判）。
      · 全 None ⇒ 返回 None，调用方**不许盲切**（盲按 Shift 会把原本英文的窗翻成中文）。
    """
    vals = []
    try:
        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(child, lparam):
            try:
                if not user32.IsWindowVisible(child):
                    return True
                r = wintypes.RECT()
                user32.GetWindowRect(child, ctypes.byref(r))
                if (r.right - r.left) < 80 or (r.bottom - r.top) < 40:
                    return True
                m = _ime_mode_imewin(child)
                if m is not None:
                    vals.append(m)
            except Exception:
                pass
            return True
        user32.EnumChildWindows(hwnd, _cb, 0)
    except Exception:
        pass
    if vals:
        return _IME_CMODE_NATIVE if any(v & _IME_CMODE_NATIVE for v in vals) else 0
    try:
        return _ime_mode_imewin(hwnd)
    except Exception:
        return None


def _ime_cn_for(hwnd):
    """**该窗口**当前是否中文输入态（不同的程序输入法状态可能不同）。
    依次问候选窗口的 IME 上下文（ImmGetConversionStatus）；都读不到返回 None ⇒ 不猜、不动。"""
    for h in _ime_ctx_hwnds(hwnd):
        try:
            himc = _ctypes_wnd.ImmGetContext(h)
            if not himc:
                continue
            try:
                conv, sent = ctypes.c_ulong(0), ctypes.c_ulong(0)
                if not _ctypes_wnd.ImmGetConversionStatus(himc, ctypes.byref(conv), ctypes.byref(sent)):
                    continue
                return bool(conv.value & _IME_CMODE_NATIVE)
            finally:
                _ctypes_wnd.ImmReleaseContext(h, himc)
        except Exception:
            continue
    # ② IMM 读不到（TSF 输入法就是这种）⇒ 走**默认 IME 窗口**那条
    _m = _ime_mode_imewin(hwnd)
    if _m is not None:
        return bool(_m & _IME_CMODE_NATIVE)
    return None


def _ime_thread_is_cn(hwnd):
    """兜底判据：该窗口所属线程的键盘布局是不是中文（0x0804）。
    用来处理"IME 上下文读不到、但布局明明是中文"的窗口（运行框就是）。"""
    try:
        tid = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
        hkl = ctypes.windll.user32.GetKeyboardLayout(tid)
        return (hkl & 0xFFFF) == 0x0804
    except Exception:
        return False


def _text_is_ascii(s):
    """纯 ASCII 文本才需要输入法守卫；中文文本一个字都不许动（只许砍代价，不许连功能一起砍）。"""
    try:
        (s or "").encode("ascii")
        return bool(s)
    except Exception:
        return False


def _ime_ascii_guard(target):
    """注入纯 ASCII 前把**目标窗口**切英文；返回 True=已切过（用完要恢复）。
    不切的后果（实测）：目标窗中文态下 py→朋友、cmd→聪明的，命令根本进不去。
    判据两级：① 该窗口 IME 上下文的中/英模式；② 读不到时退一步看该线程键盘布局是否中文。"""
    try:
        cn = _ime_cn_for(target)
        if cn is None:
            # 接上**二级判据**（`_ime_thread_is_cn`）。它答的是「该线程键盘布局是不是中文」：
            #   读不到 IME 上下文时这是唯一还能问到的信号；它也说不是中文 ⇒ 才按「不盲翻」退回。
            cn = _ime_thread_is_cn(target)
        if not cn:
            return False                       # 已经是英文态（或布局就不是中文）⇒ 不用切
        if _ime_force_ascii(target):
            return _ime_cn_for(target) is False
        # 直设不动（TSF 输入法常见）⇒ 走**实测有效**的那条：SendInput 形态点一下 Shift
        _ime_toggle_sendi()
        back = _ime_cn_for(target)
        if back is False:
            return True                        # 切完**读回核对**：读回是英文 ⇒ 成功
        if back is None:
            # 切完还是读不到（TSF 且默认 IME 窗口也不答这条消息）⇒ **按"已切过"上报**，
            #   让调用方照常恢复——否则可能把输入法**留在英文态**（那比多切一下更糟）。
            #   这不算假报：待切状态是**布局判据**给的阳性信号，恢复路径与它对称（`_ime_guard_restore`）。
            return True
        return False                           # 读回仍是中文 ⇒ 没切成，不假报
    except Exception:
        return False


# 输入法宿主进程名单的唯一出处＝上面的 `_IME_CAND_PROCS`
#   （不在这里另留第二份名单——「一个值只有一个出处」）。


# _ime_host_hwnd / _ime_host_panel_names 已整段删除（UIA 读宿主窗）。


# IME 的 UIA 检测族整段删除：
#   _ime_composing / _IME_AUTO_WAIT_S / _IME_AUTO_POLL_S / _ime_auto_type / _ime_auto_tail_check
#   （`ime_mode=auto` 的**读态判断**保留：`_ime_ascii_guard`，走 IMM/线程布局，不依赖 UIA。）


def _ime_guard_typed(text, target=None, mode="temp"):
    """**打字党的输入法闸**——纯 ASCII 文字注入前把**目标窗口**切英文。

    ★★ 关键点：**切不切由调用方声明，不再靠"读该窗中英态"去猜**。真机教训：
      该窗的中英态**读不准**——IMM 那对 API 对 TSF 输入法读不到（恒 0），补的"默认 IME 窗口"
      这条路读到的又不一定是它那一扇 ⇒ **靠读去决定** ⇒ 时切时不切（实录：守着的 `aa44` 照样被
      吃成「😄4」，同一批里守着的 `zz88` 又是原样的）。⇒ 判据改成最稳的那层：
      **"要什么"由调用方说**，读只用来记"切之前是什么"以便还原。

    `mode`（**四个取值**）：
      · `temp`（默认）＝**临时切换**：切英文 → 打完 → 由调用方 `_ime_guard_restore` **还原**；
      · `perm`＝**永久切换**：切英文 → 打完 → **不还原**（调用方自己承担"这扇窗从此是英文"）；
      · `auto`＝**读着判断**：走本文件既有的 `_ime_ascii_guard()`——**读得到中文态才切、读不到就一个字不碰**；
      · `off`＝**一个字都不碰输入法**（＝老参数 `ime_ascii: false` 那条，等价写法）。
      ⚠ **只保留「防被吃」这一个方向**：切中文会混乱，不做。

    返回 True ＝ 本站做过切换且**还需要还原**（`temp` ⇒ 调用方必须 `_ime_guard_restore`；
    `perm`／`off` ⇒ 不还原）。"""
    try:
        try:
            _IME_GUARD_LAST.clear()             # 每次守卫先清账，免得读到上一条的旧结论
        except Exception:
            pass
        if mode == "off" or not _text_is_ascii(text):
            return False
        _t = target if target is not None else _resolve_target()
        if mode == "auto":
            # auto＝**读着判断**：`_ime_ascii_guard` 读得到中文态才切、读不到就一个字不碰。
            #   不能降级成 temp（那会无条件切英文，违背 auto 的语义）。
            _preA = _ime_mode_imewin(_t)          # 只用来记「切之前是什么」⇒ 可精确还原
            _sw = _ime_ascii_guard(_t)            # 读不准就不切（不盲翻）
            if _sw and _t:
                try:
                    _IME_GUARD_PREV[int(_t)] = _preA
                except Exception:
                    pass
            return bool(_sw)                      # True ＝ 切过、打完须 `_ime_guard_restore`
        _restore = (mode == "temp")                     # 只有临时切换打完要还原
        _pre = _ime_mode_imewin(_t) if _restore else None    # 只用来记「切之前是什么」
        _done = _ime_force_ascii(_t)                    # 绝对设值（不是翻转）
        # `perm` 也要试一次 Shift 兜底（原来只有 temp 才兜底；直设不动时 perm 会静默失败）
        if not _done and mode in ("temp", "perm"):
            _ime_toggle_sendi()                         # 直设不动 ⇒ 点一下 Shift（老路最后手段）
        if _restore and _t:
            try:
                _IME_GUARD_PREV[int(_t)] = _pre              # 读不到就是 None ⇒ 恢复**不动**（不再盲设中文）
            except Exception:
                pass
        return _restore          # True ＝ 还需要还原（perm／off ⇒ 调用方别调 restore）
    except Exception:
        return False


_IME_GUARD_PREV = {}   # hwnd → 「守卫切换前」读到的模式（读不到＝None）——恢复用
_IME_GUARD_LAST = {}   # 最近一次守卫的结论 {mode,before,after,toggled,ok}（回执用）


def _ime_guard_restore(target=None):
    """与 `_ime_guard_typed` 成对：把守卫动过的窗口**还原成切换前那个模式**。

    修正要点：原来这里只调 `_set_ime_open(True)`，而它**第一步就短路**——
      `if _check_ime() == enable: return`，而 `_check_ime()` 查的是**服务端自己线程**的输入法状态
      （文档自己写着「那是查服务端自己线程的，等于没验」）⇒ 常常直接返回、**一处都不设**
      ⇒ 目标窗被留在英文态。实测指纹（两条命令挨着发、同一扇窗）：
        ① 守着的 `zz88` 原样落下 ✓（守卫真把它切成了英文）
        ② 紧接着**不守**的 `yy77` **也原样落下**（本该被输入法吃成「原因7」）⇒ 那扇窗当时还是英文。
      修法：① 恢复的**目标值＝切换前读到的那个模式**（不是无脑设中文——
      「状态要验证正确才能切，别把英文切成中文」同样管恢复）；② 走真路（默认 IME 窗口
      `IMC_SETCONVERSIONMODE` ＋ 读回核对）；③ 老路 `_set_ime_open(True)` 留作读不到时的兜底。"""
    try:
        _t = target if target is not None else _resolve_target()
    except Exception:
        _t = None
    _want = None
    try:
        if _t:
            _want = _IME_GUARD_PREV.pop(int(_t))
    except Exception:
        _want = None
    if _t and _want is not None:
        try:
            if _ime_mode_imewin(_t, set_mode=_want) is not None:
                _b = _ime_mode_imewin(_t)
                if _b is not None and (_b & _IME_CMODE_NATIVE) == (_want & _IME_CMODE_NATIVE):
                    return True             # 读回确认：已还原成切换前那个模式 ✓
        except Exception:
            pass
        # 实测（WPS）：默认 IME 窗口**设不动它的正文输入** ⇒ 状态对不上时用**真 Shift 翻回来**
        #   （要还原的目标值已经记录，不是盲翻）；翻完再读回一次确认。
        try:
            _cur = _ime_read_mode_deep(_t)
        except Exception:
            _cur = None
        if _cur is not None and (_cur & _IME_CMODE_NATIVE) != (_want & _IME_CMODE_NATIVE):
            try:
                _ime_toggle_sendi()
            except Exception:
                pass
            try:
                _cur2 = _ime_read_mode_deep(_t)
            except Exception:
                _cur2 = None
            if _cur2 is not None and (_cur2 & _IME_CMODE_NATIVE) == (_want & _IME_CMODE_NATIVE):
                return True
        # 默认 IME 窗口设不动 ⇒ 只在"确实记录到原模式"时按**记录值**恢复（中文→中文／英文→英文）
        try:
            _set_ime_open(bool(_want & _IME_CMODE_NATIVE), hwnd=_t)
        except Exception:
            pass
        return False
    # **读不到"切换前是什么" ⇒ 一个字都不动**。
    #   原来这里兜底 `_set_ime_open(True)`＝**无脑设回中文** ⇒ 本来就是英文的窗会被它翻成中文。
    #   宁可报"没切回来"（回执会写），也绝不盲设中文。
    return False


def _ime_mixed_guard(target=None, mode="temp"):
    """**混排打字闸**：文本里既有非 ASCII（走 Unicode 注入）又有 ASCII（走 keybd_event）时，
    先把目标窗切成英文——ASCII 段要经过输入法，中文态下字母会进拼音组合、英文"放不下来"。
    语义与 `_ime_guard_typed` 对齐：
    `off` 不碰｜`perm` 切了不还原｜`temp` 切了要还原。
    返回 True ＝ 调用方打完必须 `_ime_guard_restore(target)`。
    """
    try:
        if str(mode).lower() == "off":
            return False
        _t = target if target is not None else _resolve_target()
        _restore = (str(mode).lower() == "temp")
        _pre = _ime_read_mode_deep(_t) if _restore else None
        _done = _ime_force_ascii(_t)                    # 绝对设值（不是翻转）
        # 与 `_ime_guard_typed` 同一套"读回核对 + Shift 兜底"（自管 IME 的窗 force 会假成功）
        _toggled = False
        _after = _ime_read_mode_deep(_t)
        if _after is not None and (_after & _IME_CMODE_NATIVE):
            _ime_toggle_sendi()                         # 直设不动 ⇒ 点一下 Shift（老闸同款最后手段）
            _toggled = True
            _after = _ime_read_mode_deep(_t)
            if _after is not None and not (_after & _IME_CMODE_NATIVE):
                time.sleep(0.12)
        _ok = None if _after is None else (not (_after & _IME_CMODE_NATIVE))
        try:
            _IME_GUARD_LAST.clear()
            _IME_GUARD_LAST.update({"mode": str(mode).lower(), "before": _pre, "after": _after,
                                    "toggled": _toggled, "ok": _ok})
        except Exception:
            pass
        if _restore and _t:
            try:
                _IME_GUARD_PREV[int(_t)] = _pre         # 读不到就是 None ⇒ 恢复走兜底
            except Exception:
                pass
        return _restore
    except Exception:
        return False


def _ime_force_ascii(hwnd):
    """把该窗口（及其 Edit 子控件）的 IME 上下文**直接设成英文/字母数字态**。
    不赌「按一下 Shift 翻转」（实测：`_set_ime_open` 那条老路对运行框无效，
      它内部还用 `_check_ime()` 自证——那是查服务端自己线程的，等于没验）。
    返回 True=至少设成功一个上下文。"""
    done = False
    for h in _ime_ctx_hwnds(hwnd):
        try:
            himc = _ctypes_wnd.ImmGetContext(h)
            if not himc:
                continue
            try:
                conv, sent = ctypes.c_ulong(0), ctypes.c_ulong(0)
                _ctypes_wnd.ImmGetConversionStatus(himc, ctypes.byref(conv), ctypes.byref(sent))
                if _ctypes_wnd.ImmSetConversionStatus(himc, _IME_CMODE_ALPHANUMERIC, sent.value):
                    done = True
            finally:
                _ctypes_wnd.ImmReleaseContext(h, himc)
        except Exception:
            continue
    if done:
        return True
    # IMM 一处都设不动（TSF 输入法常见）⇒ 走默认 IME 窗口设一次，并**读回核对**
    if _ime_mode_imewin(hwnd, set_mode=_IME_CMODE_ALPHANUMERIC) is None:
        return False
    _back = _ime_mode_imewin(hwnd)
    return (_back is not None) and not (_back & _IME_CMODE_NATIVE)


def _check_uipi(target_hwnd):
    """诊断 UIPI 阻止原因，返回简短描述。"""
    try:
        self_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        pid_buf = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(target_hwnd, ctypes.byref(pid_buf))
        target_pid = pid_buf.value
        # 检查目标进程是否 admin：看窗口标题前缀
        title_buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(target_hwnd, title_buf, 256)
        target_title = title_buf.value or ""
        target_admin = u"\u7ba1\u7406\u5458" in target_title  # "管理员"
        return f"本进程={'admin' if self_admin else 'user'} 目标={'admin' if target_admin else 'user'}(PID={target_pid})"
    except:
        return "本进程=? 目标=?"


def _si_key_down(*key_strings):
    """按下不放（KEYDOWN）。走 `_key_event` —— 原来这里扫描码写死 0
    且不带 E0，WIN 键按下等于石沉大海（{WIN} 不开开始菜单的第二个原因）。
    同时记进**按住状态账本**（`_held_desc()` 会给回执提醒"当前按住中"）。"""
    for ks in key_strings:
        vk = _si_parse_key(ks)
        if vk is not None:
            _key_event(vk, keyup=False)
            try:
                _held_mark_key(ks, True)
            except Exception:
                pass


def _si_key_up(*key_strings):
    """松开（KEYUP），与 _si_key_down 同一套扫描码/E0 处理。同步出账本。"""
    for ks in key_strings:
        vk = _si_parse_key(ks)
        if vk is not None:
            _key_event(vk, keyup=True)
            try:
                _held_mark_key(ks, False)
            except Exception:
                pass


def _si_hold_key(duration_ms, *key_strings):
    _si_key_down(*key_strings)
    time.sleep(duration_ms / 1000.0)
    _si_key_up(*key_strings)


# ========== 操作指示器（主进程 tkinter 线程预创建，不抢焦点） ==========

_indicator_root = None
_indicator_label = None
_indicator_esc_label = None
_indicator_timer = None
_indicator_ready = None
# **本进程指示条窗口的 hwnd**——枚举时**只按这个身份**精确跳过；不按标题匹配、不做任何泛化 Tk 规则。
#   不做跨进程排除（会误伤别人的 Tk 窗）。
_indicator_hwnd = 0

INDICATOR_MIN_W = 280
INDICATOR_MAX_W_RATIO = 0.88
INDICATOR_PAD_X = 12
INDICATOR_PAD_ESC = 110
INDICATOR_H = 34


def _est_text_width(text):
    """估算文本像素宽度：中文≈13px，英文/数字≈7px"""
    w = 0
    for ch in text:
        if ('\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef'
                or '\u2e80' <= ch <= '\u2fff' or '\u3400' <= ch <= '\u4dbf' or '\uf900' <= ch <= '\ufaff'):
            w += 15  # 中文字符宽度（原14偏小）
        else:
            w += 8   # ASCII 字符宽度（原7偏小）
    return w


def _do_show(full_text, esc_text, w, h, x, y):
    """在 tkinter 线程中更新并显示窗口"""
    root = _indicator_root
    if not root:
        return
    r = 8
    for child in root.winfo_children():
        if child.winfo_class() == 'Canvas':
            child.delete('all')
            child.create_rectangle(r, 0, w - r, h, fill='#16162a', outline='')
            child.create_rectangle(0, r, w, h - r, fill='#16162a', outline='')
            child.create_arc(0, 0, r * 2, r * 2, start=90, extent=90, fill='#16162a', outline='')
            child.create_arc(w - r * 2, 0, w, r * 2, start=0, extent=90, fill='#16162a', outline='')
            child.create_arc(0, h - r * 2, r * 2, h, start=180, extent=90, fill='#16162a', outline='')
            child.create_arc(w - r * 2, h - r * 2, w, h, start=270, extent=90, fill='#16162a', outline='')
            break
    if _indicator_label:
        _indicator_label.config(text=full_text)
    if _indicator_esc_label:
        _indicator_esc_label.config(text=esc_text)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.deiconify()
    try:
        hwnd_id = root.winfo_id()
        # ★Tk 的 winfo_id() 是**子窗口**句柄，EnumWindows 枚举到的是它的父窗（TkTopLevel）⇒
        #   排除必须记顶层，否则身份永远对不上（动作回执会冒出「新增 tk[python.exe]」）。
        try:
            _top = ctypes.windll.user32.GetParent(hwnd_id)
            if _top:
                hwnd_id = _top
        except Exception:
            pass
        # 记下**本进程**指示条的真实顶层 hwnd ⇒ `enum_all_windows()` 按身份精确跳过（只排自己的）。
        global _indicator_hwnd
        _indicator_hwnd = hwnd_id
        ctypes.windll.user32.SetWindowPos(hwnd_id, 0, x, y, w, h, 0x0053)  # SWP_SHOWWINDOW|NOACTIVATE
    except:
        pass


def _indicator_thread():
    """tkinter 主循环线程（只创建一次）"""
    global _indicator_root, _indicator_label, _indicator_esc_label
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        # 透明度：原来 0.94 几乎不透明，指示条压在浏览器标题栏上会挡住下面的字。
        #   0.60＝能看清条上写了什么、也能透出底下的文字。嫌淡/嫌挡，改这一个数字即可。
        root.attributes('-alpha', 0.60)
        root.configure(bg='#0f0f18')
        root.update_idletasks()
        try:
            hwnd = root.winfo_id()
            ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex | 0x08000000 | 0x20)  # WS_EX_NOACTIVATE + WS_EX_TRANSPARENT
        except:
            pass
        canvas = tk.Canvas(root, bg='#0f0f18', highlightthickness=0, bd=0)
        canvas.pack(fill=tk.BOTH, expand=True)
        _indicator_label = tk.Label(root, fg='#e0e0e0', bg='#16162a', font=('Segoe UI', 9), anchor='w')
        _indicator_esc_label = tk.Label(root, fg='#888899', bg='#16162a', font=('Segoe UI', 9), anchor='e')
        _indicator_label.place(relx=0, rely=0.5, anchor='w', x=16)
        _indicator_esc_label.place(relx=1.0, rely=0.5, anchor='e', x=-24)
        _indicator_root = root
        if _indicator_ready:
            _indicator_ready.set()

        # 线程间消息通过 root.after 处理
        msgs = []
        def poll():
            nonlocal msgs
            if _indicator_deque:
                try:
                    while True:
                        msgs.append(_indicator_deque.get_nowait())
                except:
                    pass
            for cmd, args in msgs:
                if cmd == 'show':
                    _do_show(*args)
                elif cmd == 'hide':
                    root.withdraw()
                elif cmd == 'stop':
                    root.quit()
                    return
            msgs.clear()
            root.after(150, poll)
        root.after(100, poll)
        root.mainloop()
    except Exception:
        pass


_indicator_deque = None


def _show_indicator(window="", action="", command="", hold_s=3.0):
    """显示操作指示器 — 顶部居中条带；取消键＝ESC+`。

    `hold_s`：自动消失的秒数（默认 3.0）；传 **None / 0 ⇒ 常驻**，
    一直显示到显式调 `_hide_indicator()`。队列 worker 用 `hold_s=None`、
    并在**每一步**刷新文字（见 `_seq_indicator_show`）。
    """
    global _indicator_timer, _indicator_deque, _indicator_ready

    if _indicator_root is None:
        from queue import Queue
        _indicator_deque = Queue()
        _indicator_ready = threading.Event()
        threading.Thread(target=_indicator_thread, daemon=True).start()
        # 等待时间：0.5s → **0.05s**。
        #   原来首发要等指示条 Tk 线程就绪（最多 0.5s）才继续，等于**新进程第一发动作白等半秒**。
        #   现在只等 50ms；没就绪也照样往下走——`_indicator_deque` 在起线程前就建好了，
        #   `show` 消息先入队，线程起来后自己消费（指示条晚几十~几百毫秒出现，动作不再等它）。
        #   工具调用在主线程串行、HTTP 侧还有 `_HTTP_TOOL_LOCK` ⇒ 不存在"第二个调用趁未就绪再起一条线程"。
        _indicator_ready.wait(timeout=0.05)

    if command:
        action_text = f"执行 {command}"
    elif action:
        action_text = action
    else:
        action_text = ""

    full_text = f"操作 {window} {action_text}" if window and action_text else (window or action_text)
    esc_text = "  |  按 ESC+` 取消"

    left_w = _est_text_width(full_text)
    right_w = _est_text_width(esc_text)
    w = left_w + right_w + 16 + 32 + 30  # 左边距16 + 右边距32 + 中间间隙30
    w = max(w, INDICATOR_MIN_W)
    try:
        screen_w = _indicator_root.winfo_screenwidth() if _indicator_root else 1920
    except:
        screen_w = 1920
    w = min(w, int(screen_w * INDICATOR_MAX_W_RATIO))
    x, y = (screen_w - w) // 2, 8

    if _indicator_deque:
        _indicator_deque.put(('show', (full_text, esc_text, w, INDICATOR_H, x, y)))

    if _indicator_timer:
        _indicator_timer.cancel()
        _indicator_timer = None
    if hold_s:                      # ★None/0 ⇒ 常驻（序列在跑时用；由调用方收尾时 _hide_indicator）
        _indicator_timer = threading.Timer(float(hold_s), _hide_indicator)
        _indicator_timer.daemon = True
        _indicator_timer.start()


def _hide_indicator():
    """隐藏操作指示器"""
    global _indicator_timer
    if _indicator_timer:
        _indicator_timer.cancel()
        _indicator_timer = None
    if _indicator_deque:
        _indicator_deque.put(('hide', ()))


def _get_focus_title():
    """获取当前聚焦窗口标题（用于指示器）"""
    try:
        hwnd = _focus_hwnd
        if hwnd and user32.IsWindow(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            return buf.value or "未命名"
    except:
        pass
    return ""

# OCR / 识别层已整层删除（视觉只靠截图）——原 Tesseract 初始化与 RapidOCR 引擎层不再存在。

# GPU 串行护栏与 RapidOCR 预热带删（识别层不存在）。


sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SCREENSHOT_DIR = os.environ.get("MCP_SCREENSHOT_DIR", os.path.join(os.path.expanduser("~"), "Pictures", "screenshots"))


# ========== 窗口管理 ==========

user32 = ctypes.windll.user32

def _get_process_name(hwnd):
    """获取窗口所属进程名，带缓存（hwnd→pid→name）。"""
    cache_key = f"hwnd_{hwnd}"
    if cache_key in _process_name_cache:
        return _process_name_cache[cache_key]
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_val = pid.value
        pid_key = f"pid_{pid_val}"
        if pid_key in _process_name_cache:
            name = _process_name_cache[pid_key]
            _process_name_cache[cache_key] = name
            return name
        try:
            import psutil
            name = psutil.Process(pid_val).name()
        except:
            name = str(pid_val)
        _process_name_cache[pid_key] = name
        _process_name_cache[cache_key] = name
        return name
    except:
        return "?"


_process_name_cache = {}


# ── DWM 幽灵窗（UWP 关窗后仍挂着的帧）
#   实况：UWP/商店应用（计算器）**关窗 ≠ 退进程**——窗口资源还在、`IsWindowVisible` 仍为真，
#   但 DWM 已经把它 cloaked 掉了（屏幕上根本看不到）。`list_windows` 照列 ⇒ 拿它当靶子，
#   人却看不见 ⇒ 人和工具看到的不是同一套。
#   规则：**默认只列"屏幕上真的显示着"的窗口**（Win32 可见 且 未被 DWM cloaked）；
#        要看全部（含幽灵窗）才显式传 include_hidden=true。
_DWMWA_CLOAKED = 14


def _is_dwm_cloaked(hwnd):
    """DWM cloaked（UWP 关窗后仍挂着的典型签名）。取不到属性时按"未隐藏"处理。"""
    try:
        val = ctypes.c_int(0)
        hres = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(int(hwnd)), _DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
        return hres == 0 and val.value != 0
    except Exception:
        return False


def _is_shown_window(hwnd):
    """屏幕上真的显示着：存在 + Win32 可见 + 未被 DWM cloaked。"""
    try:
        return bool(user32.IsWindow(wintypes.HWND(int(hwnd)))
                    and user32.IsWindowVisible(wintypes.HWND(int(hwnd)))
                    and not _is_dwm_cloaked(hwnd))
    except Exception:
        return False


def _window_family_root(hwnd):
    """同一扇窗的"族根"：取顶层所有者（GetAncestor GA_ROOTOWNER）。
    用途：UWP 的 ApplicationFrameWindow 与它的
    CoreWindow 是**同一扇窗的两个句柄**、标题还一模一样 ⇒ 不归一会把"同一扇"误判成
    "换了一扇"，于是重复聚焦（实况：计算器一个标题两扇顶层窗）。"""
    try:
        r = user32.GetAncestor(wintypes.HWND(int(hwnd)), 3)   # GA_ROOTOWNER
        return int(r) if r else int(hwnd)
    except Exception:
        return int(hwnd)


def enum_all_windows(include_hidden=False):
    """列举窗口（标题非空），含进程信息。
    默认**只列屏幕上显示着的**（见 _is_shown_window）；include_hidden=True 才连 DWM 隐藏的
    幽灵窗一起列（列出来时会被标注 [DWM隐藏]）。"""
    global _pid_name_cache
    if '_pid_name_cache' not in globals():
        _pid_name_cache = {}
    windows = []
    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def enum_callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if not include_hidden and _is_dwm_cloaked(hwnd):
            return True          # DWM 隐藏的幽灵窗不算"显示着"（默认不列）
        length = user32.GetWindowTextLengthW(hwnd) + 1
        if length <= 1:
            return True
        buffer = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buffer, length)
        title = buffer.value
        # **只按 hwnd 身份排掉本进程那一条指示条**：
        #   不按标题匹配、不做任何泛化 Tk 规则；真正的 Tk 程序窗口（标题 `tk` 或它自己的）照常枚举。
        if hwnd and hwnd == _indicator_hwnd:
            return True
        if title.strip():
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = _rect_size(rect)
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc_name = ""
            pid_val = pid.value
            if pid_val in _pid_name_cache:
                proc_name = _pid_name_cache[pid_val]
            else:
                try:
                    import psutil
                    proc_name = psutil.Process(pid_val).name()
                except Exception:
                    try:
                        import subprocess
                        r = subprocess.run(['tasklist', '/FI', f'PID eq {pid_val}', '/FO', 'CSV', '/NH'], capture_output=True, text=True, timeout=2)
                        if r.stdout:
                            proc_name = r.stdout.strip().split(',')[0].strip('"')
                    except:
                        pass
                _pid_name_cache[pid_val] = proc_name
            windows.append({
                "hwnd": hwnd,
                "title": title,
                "class": class_buf.value,
                "pid": pid.value,
                "process": proc_name,
                "left": rect.left,
                "top": rect.top,
                "width": w,
                "height": h,
            })
        return True
    t0 = time.time()
    user32.EnumWindows(enum_callback, 0)
    _timing_add("enum", time.time()-t0)
    return windows


# **键注入类动作也要报「窗口变了什么」**。
#   否则工具只说「发了」，调用方只能猜「到底弹没弹」——{WIN+R} 明明发出了、
#   却只能靠外部探针和肉眼判断。
#   实现上**复用** enum_all_windows()（只列可见且标题非空的窗口），不另写枚举逻辑。
def _fg_proc_name(hwnd, hint=""):
    """前台窗口的进程名。hint=快照里 enum_all_windows() 已解析好的值（优先）；
    否则走 _get_process_name()，但它在 psutil 不可用时只回落成 pid 字符串
    （实测拿到 `[3636]` 这种），这时再补一次 tasklist 解析——与 enum_all_windows()
    内联的兜底同一套做法（这里不直接改那个共享函数，只复用它的手法）。"""
    name = (hint or "").strip()
    if not name:
        try:
            name = (_get_process_name(hwnd) or "").strip()
        except Exception:
            name = ""
    if name.isdigit():
        try:
            import subprocess
            r = subprocess.run(['tasklist', '/FI', f'PID eq {name}', '/FO', 'CSV', '/NH'],
                               capture_output=True, text=True, timeout=2)
            if r.stdout:
                name = r.stdout.strip().split(',')[0].strip('"') or name
        except Exception:
            pass
    return name


def _win_snapshot_light():
    """轻量窗口快照：{hwnd: (title, process)}（复用 enum_all_windows）"""
    try:
        snap = {w["hwnd"]: ((w.get("title") or "").strip(), w.get("process") or "")
                for w in enum_all_windows()}
    except Exception:
        snap = {}
    # 补前台窗口：enum_all_windows() 只列「标题非空」的窗口，而开始菜单 / 搜索 / 任务栏弹层
    #   这类 shell 窗口经常没有标题 ⇒ 它们根本进不了快照（{WIN+R} 出来的是开始菜单，
    #   工具这边一点都看不见）。前台窗口永远算数，补进去，「新增/消失」才看得见这类弹层。
    try:
        h = user32.GetForegroundWindow()
        if h and h not in snap:
            _b = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(h, _b, 512)
            snap[h] = ((_b.value or "").strip() or "(无标题)", _fg_proc_name(h))
    except Exception:
        pass
    return snap


def _fg_window_line(snap=None):
    """当前前台窗口一行描述（复用 _get_process_name）。**必报**——理由同上：无标题的 shell 弹层
    （开始菜单/搜索）进不了 enum_all_windows() 的清单，只有「谁在前台」这条能暴露它。"""
    try:
        h = user32.GetForegroundWindow()
        if not h:
            return ""
        _b = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(h, _b, 512)
        _c = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, _c, 256)
        title = (_b.value or "").strip() or "(无标题)"
        _hint = (snap or {}).get(h, ("", ""))[1]
        proc = _fg_proc_name(h, _hint)
        bits = [title]
        if proc:
            bits.append(f"[{proc}]")
        if _c.value:
            bits.append(f"class={_c.value}")
        return f"前台窗口: {' '.join(bits)} [hwnd={h}]"
    except Exception:
        return ""


def _win_delta_text(pre):
    """动作前后的窗口差异 + 当前前台窗口 → 一行人读文本（pre 取不到时返回 ""）。
    采样策略：先立刻看一次（弹窗常在这一刻已经出现）；没看到再等 180ms 看第二次
    （弹窗稍有延迟的情形）。两次都没变化才放弃——把开销限制在一次 180ms 以内。"""
    if not pre:
        return ""
    cur = _win_snapshot_light()
    if set(cur) == set(pre):
        time.sleep(0.18)
        cur = _win_snapshot_light()
    new = [f'{t}[{p}]' if p else t for h, (t, p) in cur.items() if h not in pre]
    gone = [t for h, (t, _p) in pre.items() if h not in cur]
    # **输入法候选框的后检测**：候选框＝`TextInputHost.exe` 的 `Microsoft Text Input Application`
    #   那个窗（TSF 输入法；有标题、可见时才进 `enum_all_windows()` 的清单）⇒ 它一冒出来就会被这里
    #   记成"新增"。**为什么需要它**：该窗的中英态**读不准**（IMM 对 TSF 恒读 0、默认 IME 窗口读到
    #   的又不一定是它那一扇）⇒ "输入法是不是真在吃字"只能靠**它有没有冒出来**这个外部信号判。
    _ime_new = _ime_new_windows(pre)     # 共用件（与 `auto` 的〈a〉同一个出口）
    bits = []
    if new:
        bits.append('新增 ' + '、'.join(new[:6]))
    if gone:
        bits.append('消失 ' + '、'.join(gone[:6]))
    lines = ['窗口变化: ' + ('；'.join(bits) if bits else '无')]
    if _ime_new:
        lines.append("⚠ 出现了**输入法候选窗**（%s）⇒ 刚注入的 ASCII **很可能被输入法吃掉了**"
                     "（本线实测：TSF 输入法的中英态读不准，这个「新窗口」是最可靠的后检测；"
                     "要原样落字请改用 `ime_mode`／`mode=background`，或先手工把该窗切英文）"
                     % '、'.join(_ime_new[:3]))
    _fg = _fg_window_line(cur)
    if _fg:
        lines.append(_fg)
    return '\n' + '\n'.join(lines)


def list_child_windows(hwnd):
    """列举指定窗口的所有子窗口（含类名、大小）"""
    children = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(child, lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, buf, 256)
        cn = buf.value
        user32.GetWindowTextW(child, buf, 256)
        ct = buf.value
        rect = wintypes.RECT()
        user32.GetWindowRect(child, ctypes.byref(rect))
        w, h = _rect_size(rect)
        children.append({
            "hwnd": child,
            "class": cn,
            "title": ct,
            "left": rect.left,
            "top": rect.top,
            "width": w,
            "height": h,
        })
        return True

    user32.EnumChildWindows(hwnd, enum_cb, 0)
    return children


_INVISIBLE_CHARS = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff\u00ad")


def _strip_invisible(s):
    """剥离零宽/不可见字符后再比对。
    实测：Edge 窗口标题是 'Microsoft\u200b Edge'——U+200B 零宽空格，
    屏幕上看不见，人工照屏幕敲 'Microsoft Edge' 永远打不中（'Edge'/'Microsoft' 却能中）。
    匹配前两边一起归一化。"""
    return (s or "").translate(_INVISIBLE_CHARS)


def find_window_by_title(keyword, class_name=None, process=None, pid=None, first_only=True,
                         exact=False, case_sensitive=False, regex=False, include_hidden=False):
    """按标题/类名/进程名/PID 查找窗口。支持精确/大小写/正则匹配。
    include_hidden=True ⇒ **连 DWM 隐藏的幽灵窗一起找**（聚焦可尝试把它拉到前台）。"""
    results = []
    for w in enum_all_windows(include_hidden=include_hidden):
        if pid is not None:
            if w.get("pid") == pid:
                results.append(w)
        else:
            for field_val, search_val in [(w["title"], keyword), (w.get("class",""), class_name), (w.get("process",""), process)]:
                if not search_val:
                    continue
                field_val, search_val = _strip_invisible(field_val), _strip_invisible(search_val)
                if regex:
                    import re
                    flags = 0 if case_sensitive else re.IGNORECASE
                    try:
                        if re.search(search_val, field_val, flags):
                            results.append(w)
                            break
                    except:
                        pass
                elif exact:
                    a, b = (field_val, search_val) if case_sensitive else (field_val.lower(), search_val.lower())
                    if a == b:
                        results.append(w)
                        break
                else:
                    a, b = (field_val, search_val) if case_sensitive else (field_val.lower(), search_val.lower())
                    if search_val and b in a:
                        results.append(w)
                        break
    if not (keyword or class_name or process or pid is not None):
        return None if first_only else []
    if first_only:
        return (results[0]["hwnd"], results[0]["title"]) if results else None
    return results


def _resolve_window(keyword, target="auto"):
    """
    查找窗口并解析最佳交互句柄。合并：标题查找 → 弹窗重定向 → 父窗口修正 → 终端子控件查找。
    返回 (main_hwnd, title, child_hwnd, was_popup_title)，child_hwnd 可能等于 main_hwnd。
    was_popup_title: 原始匹配到弹窗时的标题（用于 UI 提示），否则为 None。
    target: "auto"=默认(弹窗自动重定向到主窗口), "popup"=只匹配弹窗不重定向, "main"=只匹配主窗口
    """
    result = find_window_by_title(keyword)
    if not result:
        return None

    hwnd, title = result
    original_hwnd = hwnd
    was_popup_title = None  # 原始弹窗标题（用于 UI 提示）

    # 弹窗→主窗口重定向（target != "popup" 时自动处理）
    if target == "popup":
        pass  # 保持弹窗句柄，不重定向
    elif _is_popup_window(hwnd):
        # 匹配到弹窗 → 记录标题并尝试重定向到同进程主窗口
        was_popup_title = title
        if target != "popup":
            fixed = None
            try:
                pid_buf = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
                pid = pid_buf.value
                for w in enum_all_windows():
                    if _is_main_window(w["hwnd"]) and user32.IsWindowVisible(w["hwnd"]):
                        pid2 = ctypes.c_ulong()
                        user32.GetWindowThreadProcessId(w["hwnd"], ctypes.byref(pid2))
                        if pid2.value == pid:
                            fixed = (w["hwnd"], w["title"])
                            break
            except:
                pass
            if fixed:
                hwnd, title = fixed
    else:
        fixed = _normalize_window(result)
        if fixed:
            hwnd, title = fixed

    # 在原始句柄和主窗口句柄中查找终端子控件
    child_hwnd = _find_terminal_child(original_hwnd)
    if not child_hwnd:
        child_hwnd = _find_terminal_child(hwnd)

    return (hwnd, title, child_hwnd, was_popup_title)


def _parent_is_visible(parent_hwnd):
    """检查父窗口是否合理大小（≠1x1 隐形窗口），避免归一化到隐形主窗口。"""
    prect = wintypes.RECT()
    if user32.GetWindowRect(parent_hwnd, ctypes.byref(prect)):
        pw = prect.right - prect.left
        ph = prect.bottom - prect.top
        return pw >= 100 and ph >= 50
    return False


def _normalize_window(result):
    """
    如果找到的窗口是屏幕外/极小窗口（如标签头子窗口），
    则尝试查找其父窗口或所有者窗口。
    关键：只归一化到合理大小的父窗口，避免某些终端窗口的 1x1 隐形主窗口。
    """
    if not result:
        return None
    hwnd, title = result

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top

    # 判断是否标签头：屏幕外或极小
    if rect.left <= -32000 or rect.top <= -32000 or w < 100 or h < 100:
        # 1) 尝试 GetParent（子窗口），但必须父窗口合理可见
        parent = user32.GetParent(hwnd)
        if parent and _parent_is_visible(parent):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(parent, buf, 256)
            if buf.value:
                return (parent, buf.value)

        # 2) 尝试 GetWindow(GW_OWNER)（所有者窗口）
        GW_OWNER = 4
        owner = user32.GetWindow(hwnd, GW_OWNER)
        if owner and _parent_is_visible(owner):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(owner, buf, 256)
            if buf.value:
                return (owner, buf.value)

    return result


def focus_window(hwnd, app_type=None):
    """激活指定窗口，根据前景遮挡情况选路：全屏遮挡走 SetForegroundWindow，否则走无闪烁置顶。"""
    # 恢复 offscreen/minimized
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    offscreen = _is_offscreen(rect.left, rect.top)
    minimized = user32.IsIconic(hwnd)

    if offscreen or minimized:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)

    foreground = user32.GetForegroundWindow()
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2

    # 窗口被挡检测：前景不是目标 或 目标被其他窗口覆盖
    covered = False
    if foreground == hwnd:
        # 前景是自己，但仍可能被无焦点窗口遮挡 → WindowFromPoint 检查中心点
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        pt_hwnd = user32.WindowFromPoint(wintypes.POINT(cx, cy))
        covered = pt_hwnd != hwnd and not (pt_hwnd and user32.IsChild(hwnd, pt_hwnd))

    # 窗口被挡住（含全屏和普通窗口）→ 旧方案；窗口已在前台 → 新方案
    if (foreground and foreground != hwnd) or covered:
        # 旧方案：SetForegroundWindow 抢焦点（被挡时必须，任务栏会闪一下）
        user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        attached = False
        if foreground_thread and foreground_thread != current_thread:
            attached = user32.AttachThreadInput(current_thread, foreground_thread, True)

        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)

        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)

        time.sleep(0.1)
        user32.FlashWindow(hwnd, False)
    else:
        # 新方案：窗口已在前台，置顶即可，零闪烁
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    # ★恢复核验：最小化的全屏窗 SW_RESTORE 之后常要重切显示模式，0.3s 内没起来就抢不到前台
    #   ⇒ 没起来**固定重试一次**（不做长轮询，最多多花约 0.5s），结果如实回给调用方。
    def _raised():
        try:
            return (not user32.IsIconic(hwnd)) and (user32.GetForegroundWindow() == hwnd)
        except Exception:
            return False

    if not _raised():
        try:
            user32.ShowWindow(hwnd, 9)
            time.sleep(0.3)
            user32.AllowSetForegroundWindow(-1)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            time.sleep(0.15)
        except Exception:
            pass
    try:
        if _raised():
            return "raised"
        if user32.IsIconic(hwnd):
            return "still_minimized"
    except Exception:
        pass
    return "not_foreground"


def get_window_rect(hwnd):
    """获取窗口位置和大小（通过 DWM 物理坐标）"""
    l, t, r, b = _get_physical_window_rect(hwnd)
    return l, t, r - l, b - t


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),   # 显示器全区域
        ("rcWork", wintypes.RECT),       # 工作区（排除任务栏）
        ("dwFlags", wintypes.DWORD),
    ]


def _is_fullscreen_block(hwnd):
    """检测窗口是否覆盖显示器 ≥90%（最大化或拖拽伪全屏）。"""
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False

    monitor = user32.MonitorFromWindow(hwnd, 1)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
        return False

    mw = mi.rcWork.right - mi.rcWork.left
    mh = mi.rcWork.bottom - mi.rcWork.top
    if mw <= 0 or mh <= 0:
        return False

    return (w * h) >= (mw * mh * 0.9)


def _pw_scale_for_window(hwnd):
    """PrintWindow 的**绘制缩放补偿系数**（默认 1.0＝不补偿）。

    ★0.1.65（实测坑）：**DPI 未感知**（PROCESS_DPI_UNAWARE）的窗口在缩放显示器上由 DWM 拉伸显示，
      但 `PrintWindow` 只按窗口自己的 96-DPI 坐标绘制 ⇒ 物理尺寸的位图里**内容只占一角**
      （实测 1145×799 的位图，内容只有 904 宽，其余是白底）⇒ 图/几何/点击三套对不上。
      修法：按 `物理/缩放系数` 建位图做 PW，再把结果放大回物理尺寸（与 DWM 的拉伸一致）。
      其它情况返回 1.0：per-monitor aware(2) 本就是物理绘制；system aware(1) 由系统按系统 DPI
      渲染，不做二次缩放（再乘会放大两遍）。
    """
    try:
        _pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(_pid))
        if not _pid.value:
            return 1.0
        _k32 = ctypes.windll.kernel32
        _ph = _k32.OpenProcess(0x1000, False, _pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not _ph:
            return 1.0
        try:
            _aw = ctypes.c_int()
            _sh = ctypes.windll.shcore
            if _sh.GetProcessDpiAwareness(_ph, ctypes.byref(_aw)) != 0:
                return 1.0
            if _aw.value != 0:                 # 只有**未感知**才补偿
                return 1.0
        finally:
            _k32.CloseHandle(_ph)
        _mon = user32.MonitorFromWindow(hwnd, 2)            # MONITOR_DEFAULTTONEAREST
        _dx = ctypes.c_uint()
        _dy = ctypes.c_uint()
        if ctypes.windll.shcore.GetDpiForMonitor(_mon, 0, ctypes.byref(_dx), ctypes.byref(_dy)) == 0:
            _s = _dx.value / 96.0
            if _s > 1.001:
                return _s
        return 1.0
    except Exception:
        return 1.0


# ══════════════════════════════════════════════════════════════════════════════
# 【窗口截图必须带上「这扇窗自己的弹窗」】
#   病根：PrintWindow 只画**这一扇窗自己的像素**；而模态框/提示框是该进程**另一个顶层 HWND**
#     （owner = 主窗）⇒ 窗口截图里根本看不见它。实测：IDA 的 "Load PDB file"／"Information"
#     弹窗明明在 `list_windows`／窗口树里，`region="window"` 的图里一个都没有 ⇒ 只能反复截图、
#     反复重新聚焦才发现新窗口（效率极低，而且会漏掉正在等人确认的模态框）。
#   修法：取该窗**拥有的可见顶层弹窗**（递归：弹窗自己也可能挂弹窗），各自 PrintWindow 后按
#     `GetWindowRect` 的相对偏移**贴回主图画布**。画布几何不变 ⇒ 图内坐标照样能点，而且
#     **直接点弹窗**也点得中（贴的位置就是它的真实位置）。
#   回执必须写明叠了哪几扇（不静默：让人知道图上那块是"另一个窗口"，不是主窗自己画的）。
_GW_OWNER = 4
_GW_HWNDFIRST = 0
_GW_HWNDNEXT = 2
_LAST_POPUP_ATTACHED = []      # 最近一次窗口截图实际叠上的弹窗标题（回执用；每次截图重写）


def _owned_popup_windows(hwnd, depth=0, seen=None):
    """`hwnd` **拥有的可见顶层弹窗**（递归）。返回 `[(hwnd, title, (l,t,r,b))]`。

    判据只用 Win32 关系（owner 链 + 可见性 + 未 DWM cloaked），**不靠标题/尺寸猜测**——
      尺寸启发式（"<50x50 算隐藏"、"全屏算主窗"）会把真正的输入框/小提示框误杀。
    """
    out = []
    if depth > 4:
        return out
    base = int(hwnd)
    seen = set() if seen is None else seen

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _l):
        hh = int(h)
        if hh == base or hh in seen:
            return True
        try:
            if int(user32.GetWindow(wintypes.HWND(hh), _GW_OWNER) or 0) != base:
                return True
            if not user32.IsWindowVisible(wintypes.HWND(hh)):
                return True
            if _is_dwm_cloaked(hh):
                return True
            r = wintypes.RECT()
            if not user32.GetWindowRect(wintypes.HWND(hh), ctypes.byref(r)):
                return True
        except Exception:
            return True
        buf = ctypes.create_unicode_buffer(256)
        try:
            user32.GetWindowTextW(wintypes.HWND(hh), buf, 256)
        except Exception:
            pass
        seen.add(hh)
        out.append((hh, buf.value or "(无标题)",
                    (int(r.left), int(r.top), int(r.right), int(r.bottom))))
        return True

    try:
        user32.EnumWindows(_cb, 0)          # 枚举顺序＝z 序（最上→最下）
    except Exception:
        pass
    for (ph, _t, _r) in list(out):           # 弹窗自己也可能拿着弹窗（模态框再弹消息框）
        try:
            out.extend(_owned_popup_windows(ph, depth + 1, seen))
        except Exception:
            pass
    return out


def _popup_sig(hwnd):
    """一扇窗「自己弹窗」的**指纹**（hwnd 排序元组）——判缓存帧里的弹窗集合还新不新鲜。

    用途：`_frame_take()` 第三问答"是不是同一扇窗"，但**弹窗出现/消失不改这扇窗的身份**
      ⇒ 光比 hwnd 会把"弹窗冒出来之前的旧帧"当新的复用（实测正是它导致反复截图也看不到新窗口）。
    """
    try:
        if not hwnd or not user32.IsWindow(wintypes.HWND(int(hwnd))):
            return ()
        return tuple(sorted(int(h) for (h, _t, _r) in _owned_popup_windows(int(hwnd))))
    except Exception:
        return ()


def _composite_owned_popups(img, hwnd, depth=0):
    """把 `hwnd` 拥有的可见顶层弹窗**按真实偏移贴回** `img`（画布几何不变）。

    返回实际叠上的标题列表（按绘制顺序）；没有弹窗／取不到 ⇒ 空列表。
    递归闸：每个弹窗走 `_capture_window(_depth=…​)`（那一层不再叠，弹窗清单已由
      `_owned_popup_windows` 递归收全 ⇒ 每扇只贴一次，不会重复叠加）。
    """
    if depth > 3 or img is None:
        return []
    try:
        pops = _owned_popup_windows(hwnd)
        if not pops:
            return []
        base = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(base)):
            return []
        bx, by = int(base.left), int(base.top)
        if img.mode != "RGB":
            img = img.convert("RGB")
        titles = []
        for (ph, title, (pl, pt, _pr, _pb)) in reversed(pops):   # 反转＝z 序由下到上
            try:
                sub = _capture_window(ph, None, _depth=depth + 1)
            except Exception:
                continue
            if sub is None:
                continue
            try:
                img.paste(sub, (pl - bx, pt - by))   # PIL 自己裁越界部分（负偏移也安全）
            except Exception:
                continue
            titles.append(title)
        return titles
    except Exception:
        return []


def _popup_attach_note():
    """「已叠加弹窗」那一行回执文本（没叠 ⇒ 空串）。不静默：必须让人知道图上那块是另一个窗口。"""
    try:
        _ts = [_t for _t in (_LAST_POPUP_ATTACHED or []) if _t]
    except Exception:
        return ""
    if not _ts:
        return ""
    _head = "、".join("'%s'" % _t for _t in _ts[:3])
    _more = ("（共 %d 扇）" % len(_ts)) if len(_ts) > 3 else ""
    return ("已叠加这扇窗自己的弹窗：" + _head + _more
            + "——图上就是它的真实位置，可以直接点")


_POPUP_NOTE_PREFIX = "已叠加这扇窗自己的弹窗"


def _note_append_popup():
    """把「已叠加弹窗」这条留痕并进**本次附图**的说明里。

    只认自己这一条：`_LAST_SHOT_NOTE` 里可能还留着上一枪的旧留痕（各条截图路径都会写它），
      带过来会让人误以为这一帧也那样。**别的一般不留，这条必须留**（不静默）。
    """
    try:
        _pan = _popup_attach_note()
        if not _pan:
            return
        _prev = _LAST_SHOT_NOTE or ""
        if not _prev.startswith(_POPUP_NOTE_PREFIX):
            _prev = ""
        _set_shot_note(((_prev + "；") if _prev else "") + _pan)
    except Exception:
        pass


def _capture_window(hwnd, save_path=None, _depth=0):
    """通过 PrintWindow API 直接截取完整窗口画面（即使被其他窗口遮挡）。

    **默认不落盘**：
      · `save_path=None`（默认）⇒ **一个文件都不写**，返回内存里的 `PIL.Image`；
      · 传了路径 ⇒ **老行为逐字不变**（照样 `img.save(path,'PNG')`）。
      ⇒ 两个老调用方（`_capture_for_ocr` 与 `_grab_window_png_padded` 那条路）都传路径，行为不动；
        「只要内存」是新用法，故返回 PIL 属**纯新增**（老调用方忽略返回值即可）。
    """
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("无法获取窗口矩形")

    w, h = _rect_size(rect)

    if w < 10 or h < 10:
        raise RuntimeError(f"窗口太小: {w}x{h}")

    # ★0.1.65：DPI 未感知窗的 PW 缩放补偿（见 `_pw_scale_for_window`）——
    #   按「物理/缩放」建位图做 PW，最后放大回物理尺寸；正常窗 `_ps=1.0`，下面全等于原实现。
    _ps = _pw_scale_for_window(hwnd)
    _bw, _bh = ((max(1, int(round(w / _ps))), max(1, int(round(h / _ps))))
                if _ps > 1.001 else (w, h))

    gdi32 = ctypes.windll.gdi32
    # 64位兼容：设置 GDI32 函数类型声明
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HGDIOBJ, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), ctypes.c_uint]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.CreateSolidBrush.argtypes = [ctypes.c_uint32]
    gdi32.CreateSolidBrush.restype = wintypes.HGDIOBJ
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HGDIOBJ]
    user32.FillRect.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_uint32]
    user32.PrintWindow.restype = ctypes.c_bool
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int

    hwnd_dc = user32.GetDC(0)  # 屏幕 DC
    if not hwnd_dc:
        raise RuntimeError("获取屏幕 DC 失败")

    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, _bw, _bh)
    old_bitmap = gdi32.SelectObject(mem_dc, bitmap)

    try:
        # 白色填充背景，避免 PrintWindow 残留屏幕内容
        white_brush = gdi32.CreateSolidBrush(0xFFFFFF)
        fill_rect = wintypes.RECT(0, 0, _bw, _bh)
        user32.FillRect(mem_dc, ctypes.byref(fill_rect), white_brush)
        gdi32.DeleteObject(white_brush)

        # PW_RENDERFULLCONTENT = 0x00000002 (Win 8.1+)
        # 回退: 0 = 仅可见区域
        for flags in (0x00000002, 0):
            result = user32.PrintWindow(hwnd, mem_dc, flags)
            if result:
                break

        bmpinfo = BITMAPINFOHEADER()
        bmpinfo.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmpinfo.biWidth = _bw
        bmpinfo.biHeight = -_bh  # 负值 = top-down (正序)
        bmpinfo.biPlanes = 1
        bmpinfo.biBitCount = 32
        bmpinfo.biCompression = 0  # BI_RGB

        buf_size = _bw * _bh * 4
        buf = ctypes.create_string_buffer(buf_size)
        lines = gdi32.GetDIBits(mem_dc, bitmap, 0, _bh, buf, ctypes.byref(bmpinfo), 0)
        if lines == 0:
            raise RuntimeError("GetDIBits 失败")

        from PIL import Image
        img = Image.frombuffer('RGBA', (_bw, _bh), buf, 'raw', 'BGRA', 0, 1)
        # 转 RGB 去 alpha（PrintWindow 的 alpha 通道通常全 0）
        img = img.convert('RGB')
        if (_bw, _bh) != (w, h):
            # ★0.1.65：缩放补偿的结果放大回物理尺寸（与 DWM 拉伸一致，几何才能按 k=1 算）
            img = img.resize((w, h), Image.LANCZOS)
        if save_path:                      # ★默认不落盘：只有给了路径才写（见 docstring）
            img.save(save_path, 'PNG')
        _cap_img = img
    finally:
        gdi32.SelectObject(mem_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, hwnd_dc)
    # ★叠上这扇窗**自己拥有的可见弹窗**（模态框/提示框是另一个顶层 HWND，PrintWindow 不含它）
    #   只在最外层做：递归进来的那一层不再套（弹窗清单已由 `_owned_popup_windows` 递归收全）。
    if _depth == 0:
        global _LAST_POPUP_ATTACHED
        try:
            _LAST_POPUP_ATTACHED = _composite_owned_popups(_cap_img, hwnd, depth=0) or []
        except Exception:
            _LAST_POPUP_ATTACHED = []
    return _cap_img                       # ★纯新增返回值：调用方要内存图就拿它（老调用方忽略即可）


# ========== ESC 取消 与 操作取消 ==========

VK_ESCAPE = 0x1B
_cancel_flag = False

# ── 取消键：ESC ＋ `（反引号）**两个键一起按**
#   为什么最终是组合键：
#     ① 序列里自己发一个 ESC 会把序列掐死（真机实测：1/3 步 ｜ 0.001s ｜ ⛔ 被取消（ESC））；
#     ② 试过「自注入 ESC 免疫（窄窗）」，但 hold 那种"一直按着 ESC"期间，
#        那次 ESC 会被整个吞掉，等于取消了取消权（要是一直按着 ESC，就没法再取消了）；
#     ③ ⇒ 定案：**取消键＝ESC ＋ ` 同时按下**。两条收益一次拿到：
#        · 序列**永远不发 `** ⇒ 自己发的 ESC（游戏里开菜单之类）永不触发取消 ⇒ 不会自掐；
#        · ` 与正在发什么键**无关** ⇒ **任何时刻**都能取消，连正按着 ESC 时也一样。
#   ⇒ 自注入免疫那套已**整套撤除**（它反而会在这时候吞掉组合键）——见 is_esc_pressed()。
#   为什么不「精确区分谁按的」：GetAsyncKeyState 分不出按键来源；唯一能分的是底层钩子的
#   LLKHF_INJECTED 标志，但那要在宿主机挂**全局**键盘钩子（安全软件会当键盘记录器）⇒ 否掉。
VK_OEM_3 = 0xC0              # 「`」那个键（美式键盘 OEM_3；反引号/波浪号那个位置）

# 第二版：原来只认「**采样那一瞬间两键都按着**」；而采样间隔 0.15s、人手打组合键的重叠常常只有
#   30~60ms ⇒ 成片漏掉（于是显得"要按住半天"）。⇒ 两处一起改：
#     ① 采样加密：见 `_CANCEL_POLL_S`（wait_with_cancel 的分片间隔）；
#     ② 给两键一个**很短的相识窗**：各自"被看见按下"的时刻相隔 ≤ `_CANCEL_WIN` 就算一起按。
#   ⇒ 仍然是「必须两键」才取消——相识窗只补"先后几十毫秒"，不改变"单键不算"。
_CANCEL_SEEN = {"esc": 0.0, "tick": 0.0}   # 两键各自最近一次"被看见按下"的时刻（epoch 秒）
_CANCEL_WIN = 0.10                         # 两键相隔 ≤ 它就算"一起按"（秒）
_CANCEL_POLL_S = 0.03                      # 取消轮询间隔（秒）——原 0.15s 太粗，点按会漏


def is_esc_pressed():
    """检测【取消组合键】是否【此刻】同时按着：ESC ＋ `（反引号）。
    原来判 (state & 0x8001)：0x0001 是 GetAsyncKeyState 的
    latch 位（"自上次查询以来按过"）——宿主上任何时刻按过一次 ESC（关对话框/退全屏/Ctrl+ESC）
    都会被记住；而 reset_cancel() 只清内部 _cancel_flag、不排空 latch ⇒ 第一个做取消轮询的
    工具（focus_window / wait / 部分 handler）会被当场误判成"取消"（实测 0.0s 返回）。
    改为只看 0x8000（此刻是否按下）：按住 ESC 仍可取消（轮询间隔 0.15s），历史按键不再误伤。
    现行：**取消键＝ESC ＋ `（反引号）两个键一起按**（见上面那段）。两条收益：
      · 序列**从不发 `** ⇒ 自己发的 ESC（游戏里开菜单）**不会把自己掐死**（真机实测过该症状）；
      · ` 与手上正在发什么键无关 ⇒ **任何时刻**都能取消，连正按着 ESC 的时候也一样。"""
    now = time.time()
    state = user32.GetAsyncKeyState(VK_ESCAPE)
    if int(state) & 0x8000:
        _CANCEL_SEEN["esc"] = now
    if user32.GetAsyncKeyState(VK_OEM_3) & 0x8000:
        _CANCEL_SEEN["tick"] = now
    if not (now - _CANCEL_SEEN["esc"] <= _CANCEL_WIN
            and now - _CANCEL_SEEN["tick"] <= _CANCEL_WIN):
        return False                     # 两键没在相识窗内先后按到 ⇒ 不算取消（自己发的 ESC 就落在这里）
    # 【诊断保留·只在"真要取消"时出声】实测：VMware 抢键会让宿主的 ESC 卡在
    # "按住"状态（keyup 丢失），此时所有做取消轮询的工具都会 0.0s 返回"操作被取消"。
    # 有这一行，下次再出现就能一眼判定，不必再猜。
    try:
        print(f"[esc-cancel] 取消组合键按下 raw=0x{(int(state) & 0xFFFF):04X} fg={user32.GetForegroundWindow()}"
              f" —— 若没人按着，多半是宿主卡键：把焦点切到宿主按一下再松开即可解开",
              file=sys.stderr, flush=True)
    except Exception:
        pass
    return True                          # ESC ＋ ` 同时按着 ⇒ 要取消

def reset_cancel():
    global _cancel_flag
    _cancel_flag = False

def poll_cancel():
    """检测 ESC，被按则置位取消标志并隐藏指示器。返回是否已被取消"""
    global _cancel_flag
    if not _cancel_flag and is_esc_pressed():
        _cancel_flag = True
        _hide_indicator()
    return _cancel_flag


def _check_cancel():
    """handler 专用：若已取消则返回标准 cancel 响应，否则返回 None。
    用法： if (cancel := _check_cancel()): return cancel
    """
    if poll_cancel():
        return {"content": [{"type": "text", "text": "操作已被取消"}], "isError": True}
    return None

def wait_with_cancel(seconds, interval=_CANCEL_POLL_S):
    """等待指定秒数，期间检测【ESC＋`】取消。返回 True=完成, False=被取消。

    间隔默认 `_CANCEL_POLL_S`（0.03s）：由 0.15s 加密——组合键"两键同时按着"
      的重叠常常只有几十毫秒，0.15s 的采样会成片漏掉。代价＝每 33ms 一次 `GetAsyncKeyState`，可忽略。
    """
    elapsed = 0
    while elapsed < seconds:
        if poll_cancel():
            return False
        chunk = min(interval, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
    return True


# ========== 核心操作 ==========



def _shot_monitor(region="fullscreen", window_title=None):
    """解析截图区域（落盘截图与内存抓图共用同一套语义）。
    region: 'fullscreen'全屏 / 'window'窗口(需传window_title或截记住的操作面) / 'monitor_N'第N个显示器
    ★0.1.67：`region="window"` 的目标解析与 PW 那条**同一套**（`_shot_window_hwnd`）：
      标题 → **记住的操作面**（弹窗优先）→ 最后才退前台。
    返回 mss 的 monitor dict；None＝主屏全屏。"""
    monitor = None
    if region == "window":
        # ★0.1.67：与 `_shot_window_hwnd` 共用同一解析，不各自为政（前台被抢时也抓操作面）
        _h = _shot_window_hwnd(window_title)
        if not _h:
            return None
        left, top, w, h = get_window_rect(_h)
        if w >= 50 and h >= 50:
            return {"left": left, "top": top, "width": w, "height": h}
        return None
    elif isinstance(region, dict):
        # 直接使用传入的 monitor rect，如 {"top": 0, "left": -2, "width": 1021, "height": 1088}
        monitor = region
    elif region and isinstance(region, str) and region.startswith("monitor_"):
        try:
            idx = int(region.split("_")[1])
            with mss.mss() as sct:
                if 1 <= idx < len(sct.monitors):
                    m = sct.monitors[idx]
                    monitor = {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
        except:
            monitor = None
    return monitor


def _shot_rect_monitor(region="fullscreen", window_title=None, rect=None):
    """把一块 **屏幕物理矩形** 变成 mss 的 monitor dict，交给 mss **一次只抓那一块**
    （不是先抓整屏再裁——省时间也省内存）。

    ★0.1.63：`rect` 语义改成**屏幕物理坐标**（绝对）——与刻度尺读数、`mouse_click` 坐标、
      `_rect_abs_to_local`／`_view_prep(crop=…)` **完全同一套**。
      旧版是"相对 region 图左上角"：全屏在主屏 (0,0) 时看不出差别，
      副屏（负坐标）或窗口图就会系统性偏掉，而模型照尺子读数传 rect 时根本发现不了。
      `region`/`window_title` 现在**只进 note 说明**，不再参与换算。

    ★ 为什么要有它：图本身是全尺寸，但图一大送达端就会缩（送达档≈1708×961，像素≈1.64MP；
      实测 1920×1080→1680×962）⇒ 细节会损失。**用 rect 裁一块局部 ⇒ 1:1 不缩**，每格保真。

    返回 `(monitor_dict, note_str)`：
      · `monitor_dict` 只含 left/top/width/height 四键（**不塞额外键**，直接喂 mss）；
      · `note_str` 是给人看的实取矩形（超出屏幕被夹时写明「请求什么、夹成什么」）。
    异常：rect 解析不了/宽高非正 ⇒ **抛 ValueError**（不静默退回全屏——「要局部却拿到全屏」
      比报错更坏，这是本线「静默失效是头号敌人」那条的老规矩）。
    """
    import re as _re
    if rect is None or rect == "":
        return None, ""
    x = y = w = h = None
    if isinstance(rect, dict):
        x = int(rect.get("left", rect.get("x", 0)))
        y = int(rect.get("top", rect.get("y", 0)))
        w = int(rect.get("width", rect.get("w", 0)))
        h = int(rect.get("height", rect.get("h", 0)))
    elif isinstance(rect, (list, tuple)):
        if len(rect) != 4:
            raise ValueError("rect 序列必须是 [x, y, w, h] 四项，收到 %d 项" % len(rect))
        x, y, w, h = [int(v) for v in rect]
    else:
        _parts = [p for p in _re.split(r"[,\s]+", str(rect).strip()) if p != ""]
        if len(_parts) != 4:
            raise ValueError("rect 字符串必须是 'x,y,w,h' 四项，收到「%s」" % str(rect))
        try:
            x, y, w, h = [int(float(p)) for p in _parts]
        except Exception:
            raise ValueError("rect 四项必须都是数字，收到「%s」" % str(rect))
    if w <= 0 or h <= 0:
        raise ValueError("rect 的宽高必须为正，收到 %dx%d" % (w, h))

    # ★0.1.63：rect 就是屏幕物理坐标 ⇒ 只需夹到虚拟屏幕，**不再叠加任何基准矩形偏移**
    _sl, _st, _sr, _sb = _screen_virtual_rect()
    _cx = max(_sl, min(x, _sr - 1))
    _cy = max(_st, min(y, _sb - 1))
    _cw = max(1, min(w, _sr - _cx))
    _ch = max(1, min(h, _sb - _cy))
    _mon = {"left": _cx, "top": _cy, "width": _cw, "height": _ch}
    if (_cx, _cy, _cw, _ch) == (x, y, w, h):
        _note = "rect 局部图（图内坐标已换算成执行矩形）"
    else:
        _note = (f"⚠ rect {x},{y},{w},{h} 超出屏幕 {_sl},{_st}–{_sr},{_sb}"
                 f" ⇒ 已夹到 {_cx},{_cy},{_cw},{_ch}")
    return _mon, _note


def _parse_rect4(rect):
    """把 rect 解析成 `(x, y, w, h)`；**失败抛 ValueError**（调用方决定是当场报错还是回退）。

    接受 `"x,y,w,h"` 字符串 / 4 元序列 / 带 left/top/width/height（或 x/y/w/h）的 dict。
    `_shot_rect_monitor`（走 mss monitor 的那条）内部另有一份同样的解析——
      那份**已验收、刻意不动**；这份是给「PrintWindow 抓整窗后再裁」那条路用的。
    """
    import re as _re
    if isinstance(rect, dict):
        x = int(rect.get("left", rect.get("x", 0)))
        y = int(rect.get("top", rect.get("y", 0)))
        w = int(rect.get("width", rect.get("w", 0)))
        h = int(rect.get("height", rect.get("h", 0)))
    elif isinstance(rect, (list, tuple)):
        if len(rect) != 4:
            raise ValueError("rect 序列必须是 [x, y, w, h] 四项，收到 %d 项" % len(rect))
        x, y, w, h = [int(v) for v in rect]
    else:
        _parts = [p for p in _re.split(r"[,\s]+", str(rect).strip()) if p != ""]
        if len(_parts) != 4:
            raise ValueError("rect 字符串必须是 'x,y,w,h' 四项，收到「%s」" % str(rect))
        try:
            x, y, w, h = [int(float(p)) for p in _parts]
        except Exception:
            raise ValueError("rect 四项必须都是数字，收到「%s」" % str(rect))
    if w <= 0 or h <= 0:
        raise ValueError("rect 的宽高必须为正，收到 %dx%d" % (w, h))
    return x, y, w, h


def _rect_abs_to_local(rect, origin_xy=(0, 0), size_wh=None):
    """**屏幕物理矩形 → 图内裁剪矩形**（`rect_for_shot` 的唯一换算处，0.1.63）。

    ★语义（模型面唯一一套）：`rect="x,y,w,h"` 一律是**屏幕物理坐标**（绝对），
      也就是刻度尺上印的那些数字、`mouse_click` 用的那套坐标。
      **不是**"相对图内左上角"——那套会让"照尺子读数传 rect"系统性偏掉窗口外黑边那么多像素
      （最大化窗口实测 9px；因为 PW 整窗图原点在 GetWindowRect = 屏幕 (-9,-9)）。
    ★为什么收在这里：`_view_prep()` 的 crop 早就被当作**屏幕绝对矩形**（`geom["img_rect"]=crop`），
      只有上游"真正裁像素"的几个函数还在按图内坐标解释 ⇒ 同一参数两套含义。现在全部走本函数。
    `origin_xy`：这张图左上角对应的**屏幕物理坐标**（PW 整窗图 = GetWindowRect 左上；
      裁边后的视图图 = geom["img_rect"][:2]）；`size_wh`：图尺寸（给了才夹）。
    返回 `(x, y, w, h, abs4)`：前四项是图内裁剪框，`abs4` 是**实取**的屏幕物理矩形
      （越界被夹后的真情，回执/标尺用它，不能拿请求值冒充）。
    """
    x, y, w, h = _parse_rect4(rect)
    ox, oy = int(origin_xy[0]), int(origin_xy[1])
    if size_wh:
        W, H = int(size_wh[0]), int(size_wh[1])
        cx = max(0, min(int(x) - ox, W - 1))
        cy = max(0, min(int(y) - oy, H - 1))
        cw = max(1, min(int(w), W - cx))
        ch = max(1, min(int(h), H - cy))
    else:
        cx = max(0, int(x) - ox)
        cy = max(0, int(y) - oy)
        cw, ch = int(w), int(h)
    abs4 = [ox + cx, oy + cy, ox + cx + cw, oy + cy + ch]
    return cx, cy, cw, ch, abs4


def _shot_window_pw_enabled():
    """window 截图走 PrintWindow 的闸（**默认开**）。
    `MCP_SHOT_WINDOW_PW=0` ⇒ **一键回老路**（mss 屏幕裁剪，代码逐字保留）。"""
    return os.environ.get("MCP_SHOT_WINDOW_PW", "1").strip() != "0"


def _shot_window_hwnd(window_title=None):
    """解析 window 截图的**窗口句柄**（PrintWindow 那条路要它）。

    ★目标解析顺序（0.1.67 定，**记住焦点优先**）：
      ① 给了标题 ⇒ 按标题找；
      ② 没给标题 ⇒ **记住的操作面**（`_popup_hwnd` 优先，其次 `_focus_hwnd`）——
         所以前台被指示条/别的应用抢走时，窗口截图**照样抓操作面**，不受影响；
      ③ 连操作面都没有（从未聚焦）⇒ 才退到**当前前台窗口**（`take_screenshot` 的老语义）。
    ★为什么以前是"没标题就是用前台"：早先 `capture_screenshot` 调之前会先聚焦目标窗，
      前台≈操作面；后来指示条（Tk 顶层窗）在调用开头弹出会短暂抢前台 ⇒ 前台语义会抓错
      （实测抓到指示条 220×1 的图）。现在**统一走"记住焦点"**，不依赖前台。
    找不到 / 句柄已失效 ⇒ None（调用方回退老路，不静默给错图）。
    """
    hwnd = None
    if window_title:
        _r = find_window_by_title(window_title)
        if _r:
            _fx = _normalize_window(_r)
            hwnd = (_fx or _r)[0]
    else:
        # ★0.1.67：记住的操作面优先（弹窗优先），不依赖当前前台
        for _cand in (_popup_hwnd, _focus_hwnd):
            if _cand and user32.IsWindow(_cand):
                hwnd = _cand
                break
        if not hwnd:
            hwnd = user32.GetForegroundWindow()
    if hwnd and user32.IsWindow(hwnd):
        return hwnd
    return None


def _axis_shot_hwnd():
    """最近一次**窗口整图**附图的坐标轴属于哪扇窗（全屏轴 ⇒ None）。

    判据两层：轴里记了 hwnd **且** 轴自身的 region 是 "window"——全屏截图也会把"当前操作面"
    记进轴（为了算窗口矩形），只看 hwnd 会把全屏轴误判成窗口轴。
    用途：`rect` 局部截图的缺省路径判定——如果轴来自窗口截图，rect 就是"那扇窗里的一块"，
    必须用同一扇窗的 PrintWindow 图去裁；若改走 mss 屏幕裁剪，被遮挡时抓到的是遮挡者
    （实测：前台别的窗口冒充目标窗，画面看起来还像正常结果）。
    """
    try:
        if (_AXIS and isinstance(_AXIS, dict) and _AXIS.get("hwnd")
                and str(_AXIS.get("region") or "") == "window"):
            return int(_AXIS["hwnd"])
    except Exception:
        pass
    return None


def _shot_region_default(arguments):
    """缺省 `region` 时的取值（修「不传 region ⇒ 一律掉进全屏 mss 路径」）。

    返回 `(region, why)`；`why` 非空＝自动选了某条路，回执必须写明（不静默）。
    判定顺序：
      ① 显式 `region` ⇒ 按调用方；
      ② 显式 `window=` / `window_title=` ⇒ `"window"`（PrintWindow 优先）；
      ③ 没给 region/window，但给了 `rect` 且**最近截图的轴属于某扇窗**
         ⇒ `"window"`：rect 是那扇窗里的一块，用同窗 PW 图裁；
      ④ 其余 ⇒ `"fullscreen"`（无窗口上下文的 rect 仍是屏幕裁剪）。
    """
    _ra = arguments.get("region")
    if _ra not in (None, ""):
        return _ra, ""
    if arguments.get("window") or arguments.get("window_title"):
        return "window", ""
    if arguments.get("rect") not in (None, "") and _axis_shot_hwnd():
        return "window", ("region 未传 ⇒ 自动按「最近截图那扇窗」走 window"
                          "（rect 归窗裁、PrintWindow 优先；要屏幕裁剪请显式 region=\"fullscreen\"）")
    return "fullscreen", ""


def _shot_window_pw_png(window_title=None, rect=None):
    """用 **PrintWindow** 取窗口图。

    目标解析统一走 `_shot_window_hwnd`：标题 → **记住的操作面（弹窗优先）** → 前台兜底。
    **为什么改**：`take_screenshot(region="window")` 此前走的是 **mss 屏幕裁剪**——靶窗被别的窗
      挡住时**抓到的是遮挡者、而且不报错**。实测复现：想截
      被 Edge 盖住的 cmd，拿回来的是 Edge 的画面。`_grab_png_bytes` 那条路早已
      统一走 PW（「统一使用 PW；如果 PW 不行再回退到可见截图」）——**只有
      `take_screenshot` 这条没跟上** ⇒ 现在对齐。

    `rect` 是**屏幕物理坐标**的矩形（★0.1.63 起；= 刻度尺读数、= 点击坐标）：
      先按 `_capture_window` 拿整窗图，再用 `_rect_abs_to_local` 换成图内框并 PIL 裁——
      图原点由 GetWindowRect 推出，**调用方不许自己减偏移**。
      超界 ⇒ 夹到边界，**实取矩形**随返回值回传（说明由调用方写进回执）。

    返回 `(PNG 字节, 实拍窗口 hwnd, 实取屏幕矩形 abs4 或 None)`（不落盘）；
      **拿不到（无 hwnd／PW 抛错／开关关掉）⇒ None** ⇒ 调用方回退老路。
    返回值从"只给字节"改成"字节＋实拍窗口"（**唯一调用点是 `take_screenshot`**，
      它要用这个 hwnd 登记"最新帧是哪扇窗的"，见 `_frame_set(hwnd=…)`）。
    rect 解析失败 ⇒ **抛 ValueError**（不吞掉——"要局部却拿到整窗"比报错更坏）。
    """
    if not _shot_window_pw_enabled():
        return None
    hwnd = _shot_window_hwnd(window_title)   # ★0.1.67：统一解析（记住焦点优先）
    if not hwnd:
        return None
    try:
        _ci = _capture_window(hwnd, None)      # PrintWindow 实现，只回内存 PIL
    except Exception:
        return None
    if _ci is None:
        return None
    try:                                   # ★：图里已叠上这扇窗自己的弹窗（几何不变、可直接点）
        _note_append_popup()
    except Exception:
        pass
    _abs4 = None
    if rect:
        # ★0.1.63：rect = **屏幕物理坐标**；换算只走 `_rect_abs_to_local`。
        #   ⚠ 图原点必须是**真 GetWindowRect 左上**（PW 整窗图 = GetWindowRect 尺寸，含屏幕外黑边），
        #     **不能**用 `_get_physical_window_rect()`——那个是 DWMWA 可见框（最大化时 (0,0,1920,1080)，
        #     与 PW 图原点 (-9,-9) 差一条黑边；用错就系统性偏 9px，正是这次要修的病）。
        _wr = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(_wr)):
            return None
        _cx, _cy, _cw, _ch, _abs4 = _rect_abs_to_local(rect, (int(_wr.left), int(_wr.top)), _ci.size)
        _ci = _ci.crop((_cx, _cy, _cx + _cw, _cy + _ch))
    # 连**实拍窗口** + **实取屏幕矩形**一起回（调用方登记"最新帧"/标尺要用）。
    return _pil_png_bytes(_ci), hwnd, _abs4


class _BytesSink(__import__("io").BytesIO):
    """to_png 可能会 close 输出流；屏蔽 close，保证之后还能 getvalue()。"""
    def close(self):
        pass


# PW（PrintWindow）的**后台截图**能力是主力保留——被挡住/在后台也能拿到窗口自己的画面；
#   只把**浏览器类**排开：Chromium 的 GPU 合成窗口对 PW_RENDERFULLCONTENT 敏感
#   （可能黑图/卡住）。被保护时改抓全屏（BitBlt），
#   与其它截图软件同一条路——安全、零副作用——并在回执里**如实报出来**。
_LAST_SHOT_NOTE = ""
# ★0.1.63：最近一次截图的**实取屏幕矩形**（只有传了 rect 的局部图才有；标尺/回执拿它，
#   不许再拿"请求值"冒充——被夹过的时候两者不同）。
_LAST_SHOT_RECT = None


def _set_shot_note(msg):
    global _LAST_SHOT_NOTE
    _LAST_SHOT_NOTE = msg or ""


def _set_shot_rect(rect4):
    global _LAST_SHOT_RECT
    _LAST_SHOT_RECT = [int(v) for v in rect4] if rect4 else None


def _screen_virtual_rect():
    """虚拟屏幕（所有显示器）矩形：left, top, right, bottom。"""
    try:
        gsm = user32.GetSystemMetrics
        return (gsm(76), gsm(77), gsm(76) + gsm(78), gsm(77) + gsm(79))
    except Exception:
        return (0, 0, 1920, 1080)


def _is_fullscreen_window(hwnd):
    """窗口矩形≈整块屏幕（全屏/无边框全屏）⇒ True。

    用途：全屏窗口改走屏幕抓取。**PrintWindow 对 D3D/Unity 全屏会反复返回同一张旧帧**
    （实测：同一块连抓两次 sha 完全相同，而屏幕抓取两次 sha 不同），窗口路径拿到的是不刷新的画面。
    最大化窗口不算（DWM 外框 -9,-9 1938×1098，与屏差 18px）。
    """
    try:
        _wr = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(_wr)):
            return False
        sl, st, sr, sb = _screen_virtual_rect()
        return (abs(int(_wr.left) - sl) <= 2 and abs(int(_wr.top) - st) <= 2
                and abs((int(_wr.right) - int(_wr.left)) - (sr - sl)) <= 2
                and abs((int(_wr.bottom) - int(_wr.top)) - (sb - st)) <= 2)
    except Exception:
        return False


def _minimized_note(hwnd):
    """目标窗已最小化时给一句中性说明（图里只有标题条，要看内容需要切前台）。"""
    try:
        if hwnd and user32.IsIconic(hwnd):
            return "｜该窗口当前已最小化，图里只有标题条，要看内容需要先把窗口切到前台"
    except Exception:
        pass
    return ""


def _window_visible_rect(hwnd):
    """要保护的窗口**只截它自己在屏幕上的可见区域**，不截整屏
    （不是所有需要保护的窗口都是全屏的）。返回 dict(left, top, width, height)＝窗口矩形 ∩ 虚拟屏幕；
    窗口完全在屏外 ⇒ None。"""
    try:
        l, t, r, b = _get_physical_window_rect(hwnd)
    except Exception:
        return None
    sl, st, sr, sb = _screen_virtual_rect()
    x1, y1 = max(l, sl), max(t, st)
    x2, y2 = min(r, sr), min(b, sb)
    if x2 <= x1 or y2 <= y1:
        return None
    return {"left": x1, "top": y1, "width": x2 - x1, "height": y2 - y1}


def _grab_window_png_padded(hwnd, rect=None):
    """保护窗口的取图：抓「窗口可见区域」的屏幕像素，再**贴回整窗画布**（屏幕外部分留黑）。

    ★（0.1.62）：画布尺寸改成 **GetWindowRect 整窗尺寸**（原来用 DWM 可见框尺寸）——
       这样回退图与 PW 图**语义完全一致**（都是"整窗矩形"），上层只需要一套几何
       （GetWindowRect + 只往外裁黑边），不用再按尺寸猜来源。
    为什么必须贴回：坐标换算假定「图 = 窗口矩形」；贴回之后图与原语义一致、不会错位。
    ★0.1.63：`rect` 同样是**屏幕物理坐标**（画布原点 = GetWindowRect 左上），换算走
      `_rect_abs_to_local`；返回 `(PNG 字节, 实取屏幕矩形或 None)`。
    """
    try:
        _wr = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(_wr)):
            return None
        l, t, r, b = int(_wr.left), int(_wr.top), int(_wr.right), int(_wr.bottom)
    except Exception:
        return None
    sl, st, sr, sb = _screen_virtual_rect()
    x1, y1 = max(l, sl), max(t, st)
    x2, y2 = min(r, sr), min(b, sb)
    if x2 <= x1 or y2 <= y1:
        return None
    vis = {"left": x1, "top": y1, "width": x2 - x1, "height": y2 - y1}
    try:
        with mss.mss() as sct:
            shot = sct.grab(vis)
        from PIL import Image
        vis_img = Image.frombytes("RGB", shot.size, shot.rgb)
        canvas = Image.new("RGB", (max(r - l, 1), max(b - t, 1)), (0, 0, 0))
        canvas.paste(vis_img, (x1 - l, y1 - t))
        _abs4 = None
        if rect:
            _cx, _cy, _cw, _ch, _abs4 = _rect_abs_to_local(rect, (l, t), canvas.size)
            canvas = canvas.crop((_cx, _cy, _cx + _cw, _cy + _ch))
        # 这一段原先自己 Save 到 io.BytesIO 再取字节，与 `_pil_png_bytes()` **逐字同一件事**
        #   ⇒ 改成调那一个出口，**这里不再是第二份实现**。
        #   ⚠ 它跑在「PW 失败的回退路」上（调用点全是回退支），**不是热点**。
        return _pil_png_bytes(canvas), _abs4
    except Exception:
        return None


def _grab_png_bytes(region="fullscreen", window_title=None):
    """内存里取图：直接返回 PNG 字节，**不落盘**（「图片给模型」和「图片落盘」
    是两件事——附给调用方的画面不该在磁盘上留文件）。

    设计要点：**窗口图必须复用原有的 `_capture_window()`（PrintWindow）**——那才是原来那条
      久经检验的窗口截图实现，它抓的是"窗口自己的像素"，
      DWM 扩展边框/DWM 感知都在里面做好了。原先对 region="window" 用 mss 去抓"屏幕上那块
      矩形"，而**最大化窗口的物理矩形是 (-9,-9 1938x1098)——四边都超出屏幕**，抓出来必然串位
      （实测同一屏两枪能差出一条标题栏的高度）。现在：窗口区域走 PrintWindow，失败回退 mss
      （与原实现同样的兜底策略）；全屏/显示器区域仍走 mss。"""
    if region == "window":
        hwnd = None
        if window_title:
            _r = find_window_by_title(window_title)
            if _r:
                _fixed = _normalize_window(_r)
                hwnd = (_fixed or _r)[0]
        else:
            # 严格规则：只用"记住的聚焦窗口 / 跟踪弹窗"，**不回退到前台窗口**
            hwnd = _popup_hwnd or _focus_hwnd
        if region == "window" and not (hwnd and user32.IsWindow(hwnd)):
            # 没有可用目标窗 ⇒ 不猜、不抓别人的窗口（也**不静默退回全屏**——那比没图更坏）
            _set_shot_note("未聚焦窗口（或已失效）：region=window 按设计跳过，不抓前台/全屏")
            return None
        if hwnd and user32.IsWindow(hwnd):
            # 统一使用 PW；如果 PW 不行再回退到可见截图。
            #   实测依据：对 Edge（msedge.exe）连打 10 次窗口截取，全部 0.2–0.3s 成功、无一卡住
            #   ⇒ **不做任何特判，所有窗口一律 PrintWindow**；失败才回退「该窗可见区」。
            #   从前这里建一个临时文件（`tempfile.mkstemp`）写盘再读回；
            #   `_capture_window` 现在支持「只回内存图」⇒ **这个临时文件整个不要了**（删都不用删）。
            try:
                _ci = _capture_window(hwnd, None)       # 原有实现（PrintWindow），只是不落盘
                if _ci is not None:
                    _data = _pil_png_bytes(_ci)
                    if _data:
                        _note_append_popup()        # ★图里已叠上这扇窗自己的弹窗
                        return _data
            except Exception:
                pass                                    # 失败回退（同原策略）
            _vis = _window_visible_rect(hwnd)           # PW 失败 ⇒ 回退"该窗可见区"，不抓整屏
            _png2, _ = _grab_window_png_padded(hwnd)
            if _png2:
                if _vis:
                    _set_shot_note(f"PW 失败 → 已回退可见截图 {_vis['width']}x{_vis['height']}@{_vis['left']},{_vis['top']}")
                else:
                    _set_shot_note("PW 失败 → 已回退可见截图")
                return _png2
    monitor = _shot_monitor(region, window_title)
    with mss.mss() as sct:
        img = sct.grab(monitor) if monitor else sct.grab(sct.monitors[1])
        try:
            _png = mss.tools.to_png(img.rgb, img.size)   # 新版 mss：output=None 直接回字节
            if isinstance(_png, (bytes, bytearray)) and _png:
                return bytes(_png)
        except Exception:
            pass
    # 兜底：走临时文件（读出来立刻删，不往截图目录堆文件）
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="mcp-shot-")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            mss.tools.to_png(img.rgb, img.size, output=tmp)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def take_screenshot(save_path=None, region="fullscreen", window_title=None, frame_kind=None, rect=None):
    """截图（区域语义见 `_shot_monitor`）。

    **`region="window"` 的目标解析**（0.1.67 起走 `_shot_window_hwnd`）：
      标题 → **记住的操作面（弹窗优先）** → 前台兜底 ⇒ **前台被指示条/别的应用抢走也照样抓操作面**。
    **默认不落盘**：
      · 不传 `save_path` ⇒ **不写任何文件**，返回内存 PNG **字节**（并登记进【最新帧】供附图复用）；
      · 传了 `save_path` ⇒ **老行为逐字不变**（写文件、返回路径字符串）。
      ⚠ 返回值随模式而变：`str`（路径）｜`bytes`（内存 PNG）——老调用方全部传了路径。
    `frame_kind`（可选，**纯新增**）：登记进【最新帧】时用的 kind，
      不传＝老行为 `"screen"`。**只有"按 rect 取的一块局部"才传 `"screen_rect"`**——
      这样 `_frame_take` 的 `want` 判据（window / 其余都算 screen）天然不匹配，
      **那张局部图就不会被附图通道当成全屏帧复用**（`_frame_take` 一字未改）。
    `rect`（可选）：`"x,y,w,h"`，**屏幕物理坐标**（★0.1.63 起；= 刻度尺读数、= 点击坐标那套）：
      · `region="window"` ⇒ 走 **PrintWindow**（`_shot_window_pw_png`）抓整窗后按
        `_rect_abs_to_local` 换算再**用 PIL 裁**（图原点由 GetWindowRect 推出，不按图内坐标解释）；
      · 其余 region ⇒ 仍让 **mss 一次只抓那一块**（更快）；
      · 不传 ⇒ **逐字老行为**。
      实取矩形写进模块级 `_LAST_SHOT_RECT`（越界被夹后可能 ≠ 请求值，回执/标尺只能用实取值）。
    **`region="window"` 行为**：由「mss 屏幕裁剪」改为「**PrintWindow 优先**」——
      被遮挡时拿到的是**窗口自己的画面**，不再是遮挡者。
      **PW 拿不到 ⇒ 回退原来那条 mss 路（代码逐字保留）**，并用 `MCP_SHOT_WINDOW_PW=0` 可一键关掉。
    """
    _fk = frame_kind or "screen"   # rect 局部图传 screen_rect，防被附图当成全屏帧
    _set_shot_rect(None)           # ★每次重置：实取矩形只描述**本次**这一枪

    # ① window ⇒ PrintWindow
    _win_hwnd = None          # **这张图是哪扇窗的**（只有 window 那条路需要，登记帧时交给 _frame_set）
    if isinstance(region, str) and region == "window":
        # ★全屏窗口提前判：直接进现成的 mss 回退分支（贴回整窗画布，几何语义与 PW 图一致）。
        #   理由：PW 对 D3D/Unity 全屏会反复给同一张旧帧 ⇒ 窗口图不刷新、生效判定恒 0。
        try:
            _fs_hwnd = _shot_window_hwnd(window_title)
            _fs_on = bool(_fs_hwnd) and _is_fullscreen_window(_fs_hwnd)
        except Exception:
            _fs_hwnd, _fs_on = None, False
        if _fs_on:
            try:
                _gres = _grab_window_png_padded(_fs_hwnd, rect)
            except Exception:
                _gres = None
            if _gres:
                _pad, _abs4 = _gres
                _set_shot_rect(_abs4)
                _note_fs = ("全屏窗口 ⇒ 用屏幕抓取（PrintWindow 对全屏可能不刷新）"
                            + (f"｜rect {rect}（屏幕物理坐标，抓整窗后裁）" if rect else "")
                            + _minimized_note(_fs_hwnd))
                _set_shot_note(_note_fs)
                if save_path:
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, "wb") as _f:
                        _f.write(_pad)
                    _frame_set(save_path, _fk, _note_fs, hwnd=_fs_hwnd)
                    return save_path
                _frame_set(None, _fk, _note_fs, data=_pad, hwnd=_fs_hwnd)
                return _pad
        _shot = _shot_window_pw_png(window_title, rect)   # rect 解析失败会抛 ValueError，不吞
        if _shot:
            _pw, _win_hwnd, _abs4 = _shot   # ★实拍窗口 + 实取屏幕矩形（没给标题 ⇒ 系统前台）
            _set_shot_rect(_abs4)
            _note = "PrintWindow 窗口截图" + (
                f"｜rect {rect}（屏幕物理坐标，抓整窗后裁）" if rect else "") + _minimized_note(_win_hwnd)
            try:
                if _win_hwnd and _pw_scale_for_window(_win_hwnd) > 1.001:
                    _note += "｜DPI 未感知窗：已按显示器缩放补偿（与屏幕显示一致）"
            except Exception:
                pass
            _set_shot_note(_note)
            if save_path:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "wb") as _f:
                    _f.write(_pw)
                _frame_set(save_path, _fk, _note, hwnd=_win_hwnd)
                return save_path
            _frame_set(None, _fk, _note, data=_pw, hwnd=_win_hwnd)
            return _pw
        _win_hwnd = _shot_window_hwnd(window_title)   # PW 拿不到 ⇒ 回退路也要记对"是哪个窗"
        _set_shot_note("PrintWindow 不可用 ⇒ 已回退屏幕裁剪（整窗画布）")   # ★失败留痕，不静默
        # ★（0.1.62）：回退也**归一成"整窗矩形语义"**（贴回整窗画布、离屏留黑）——
        #   与 PW 图同一种语义 ⇒ 上层几何只有一套（GetWindowRect + 只往外裁黑边），不必按尺寸猜来源。
        if _win_hwnd:
            try:
                # ★0.1.63：rect 是**屏幕物理坐标**，换算+裁剪都在底层（画布原点 = GetWindowRect）
                _gres = _grab_window_png_padded(_win_hwnd, rect)
                if _gres:
                    _pad, _abs4 = _gres
                    _set_shot_rect(_abs4)
                    _note2 = "PrintWindow 不可用 ⇒ 已回退屏幕裁剪（整窗画布，离屏留黑）" + _minimized_note(_win_hwnd)
                    _set_shot_note(_note2)
                    if save_path:
                        os.makedirs(os.path.dirname(save_path), exist_ok=True)
                        with open(save_path, "wb") as _f:
                            _f.write(_pad)
                        _frame_set(save_path, _fk, _note2, hwnd=_win_hwnd)
                        return save_path
                    _frame_set(None, _fk, _note2, data=_pad, hwnd=_win_hwnd)
                    return _pad
            except Exception:
                pass

    # ★② 其余（fullscreen／monitor_N／dict 区域／window 回退）⇒ 原来的 mss 路，逐字保留
    monitor = _shot_monitor(region, window_title)
    if rect:
        _rm, _rect_note = _shot_rect_monitor(region, window_title, rect)   # ★rect=屏幕物理坐标
        monitor = _rm
        if _rm:
            _set_shot_rect([_rm["left"], _rm["top"],
                            _rm["left"] + _rm["width"], _rm["top"] + _rm["height"]])
        _set_shot_note(_rect_note)

    with mss.mss() as sct:
        if monitor:
            img = sct.grab(monitor)
        else:
            img = sct.grab(sct.monitors[1])
        if save_path:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            mss.tools.to_png(img.rgb, img.size, output=save_path)
        else:
            _png = mss.tools.to_png(img.rgb, img.size)      # ★内存 PNG（output=None ⇒ 返回 bytes）
    if save_path:
        _frame_set(save_path, _fk, "mss 区域截图", hwnd=_win_hwnd)   # ★登记（附图 region=fullscreen 时复用它）
        return save_path
    _frame_set(None, _fk, "mss 区域截图(内存)", data=_png, hwnd=_win_hwnd)
    return _png


# ══════════════════════════════════════════════════════════════════════════════
# 【抽帧观察 / 拼图】
#   把「连续截图」做成一次调用：持续采样 → 按画面变化量挑关键帧 → 拼成一张大图。
#   纯像素（截图 / 缩放 / 差分 / 拼图），不碰 OCR/UIA。
#   取帧判据（参数按本机屏幕标定）：
#     · 变化分＝64×36 网格上的 HSV 三通道加权差（0–255，约 0.2ms/帧）；
#     · 切点：分值够大且相对前后帧是尖峰（配不应期）⇒ 立刻留；
#     · 积分：分值不高但一直有，按「基准强度 × fps × 秒数」累够就留一帧；
#     · 兜底：长时间完全静止时留一张，说明「确实没变」；
#     · 去重：与上一保留帧差异太小就不留（切点/兜底不过这道闸）。
#   定位：**不新增工具**——截图工具（capture_screenshot）与序列工具（shot 步 / action=shot）
#   共用本块实现，避免同一能力两个出处。
# ══════════════════════════════════════════════════════════════════════════════
_SEE_SCAN_W, _SEE_SCAN_H = 64, 36
_SEE_CUT_ABS = 12.0          # 切点绝对地板（0–255 刻度：照片切换 16~46、滚网页 30~42、光标移动 0.7）
_SEE_CUT_SPIKE = 2.5         # 切点尖峰比
_SEE_CUT_MIN_GAP = 0.4       # 切点不应期（秒）
_SEE_MARK_MIN_GAP = 0.5      # 两个采样点之间最短间隔（秒）
_SEE_INTEGRAL_SEC = 3.0      # 积分取帧：中等强度画面攒够这么多秒算一个采样点
_SEE_REF_SCORE = 4.0         # 基准强度：中等运动每帧变化分
_SEE_DUP_MIN = 8.0           # 与上一保留帧差异小于它就不留
_SEE_FLOOR_SEC = 20.0        # 完全静止兜底：这么久没出帧就留一张


def _see_small_hsv(pil_img):
    """把一帧缩到 64×36 并转 HSV（各通道 0–255），用于逐帧变化分（不落盘、不识别）。"""
    import numpy as np
    from PIL import Image as _Image
    small = pil_img.convert("RGB").resize((_SEE_SCAN_W, _SEE_SCAN_H), _Image.BOX)
    arr = np.asarray(small, dtype=np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    h = np.zeros_like(mx)
    m = d > 0
    rm = (mx == r) & m
    gm = (mx == g) & m & ~rm
    bm = m & ~rm & ~gm
    dd = np.where(m, d, 1.0)
    h[rm] = ((g[rm] - b[rm]) / dd[rm]) % 6
    h[gm] = (b[gm] - r[gm]) / dd[gm] + 2
    h[bm] = (r[bm] - g[bm]) / dd[bm] + 4
    h = (h * 60.0) % 360.0
    hue = h / 360.0 * 255.0
    sat = np.where(mx == 0, 0.0, d / np.maximum(mx, 1e-6)) * 255.0
    val = mx * 255.0
    return np.stack([hue, sat, val], axis=-1)


def _see_score(prev_hsv, cur_hsv):
    """两帧 HSV 的加权绝对差均值（0–255）：色相按环形处理、低饱和像素的色相降权。"""
    import numpy as np
    dh = np.abs(cur_hsv[..., 0] - prev_hsv[..., 0])
    hue = np.minimum(dh, 255.0 - dh)
    hw = np.minimum(cur_hsv[..., 1], prev_hsv[..., 1]) / 255.0
    per = hue * hw + np.abs(cur_hsv[..., 1] - prev_hsv[..., 1]) + np.abs(cur_hsv[..., 2] - prev_hsv[..., 2])
    return float(per.mean())


def _pil_from_png_bytes(data):
    import io as _io
    from PIL import Image as _Image
    return _Image.open(_io.BytesIO(data)).convert("RGB")


def _sheet_tiers(frames_meta):
    """按「这一帧有多重要」给档位：切点/起手 1（最大格）、积分 2、兜底 3（最小格）。

    档位＝1/k 的格子边长（简化为三档；实时场景
    4~8 帧内足够，且比完整密度估计更可预测）。
    """
    out = []
    for fr in frames_meta or []:
        via = (fr.get("via") or "").lower()
        if via in ("cut", "start"):
            out.append(1)
        elif via == "mark":
            out.append(2)
        elif via == "floor":
            out.append(3)
        else:
            out.append(2)
    return out


def _tier_bounds(k, grow=(3, 4, 5, 6, 7, 8)):
    """档位 k 的边长区间（格宽单位）：

        k=1  [1/2, 1]     整幅只许缩到一半（两张 1 档正好并排）
        k=2  [1/3, 1/2]   只许缩小（半幅已是"能看清"的上限）
        k≥3  [1/(k+1), 1/(k-1)]  相邻一档之间连续取值（允许"升"到上一档）

    `grow` 只放 3..8 —— **1 和 1/2 一律不许升**（它们已经是满格）。
    """
    k = max(1, int(k or 1))
    lo = 1.0 / (k + 1)
    up = (1.0 / (k - 1)) if (k >= 3 and k in grow) else (1.0 / k)
    return lo, up


def _fill_row_sizes(tiers_row, grow=(3, 4, 5, 6, 7, 8)):
    """一行内让格子按档位上下界伸缩，**使总宽尽量等于 1**（二分法）。

    为什么要它：旧实现按 1/k 死尺寸排，行尾常剩 1/6~1/3 的**黑边**（如 [2,3]=5/6、
    [3,3]=2/3），既是浪费画布，也让人以为"图没画完"。解法是：同档可连续缩放到
    相邻一档（k≥3 可升到 1/(k-1)），二分出行宽让整行正好铺满。
    返回各格边长（格宽单位）；"拉满也不够" ⇒ 返回最大尺寸（上层补灰条）。
    """
    ks = [max(1, int(k or 1)) for k in tiers_row]
    cs = [1.0 / k for k in ks]
    los = [_tier_bounds(k, grow)[0] for k in ks]
    his = [_tier_bounds(k, grow)[1] for k in ks]
    if sum(los) > 1.0 + 1e-9:
        return cs                                  # 连最小都放不下（上层换行）
    if sum(his) <= 1.0 + 1e-9:
        return his                                 # 拉满也填不满 ⇒ 用最大尺寸
    a, b = 0.0, max(his)
    for _ in range(24):
        m = (a + b) / 2.0
        tot = sum(min(max(m, los[i]), his[i]) for i in range(len(ks)))
        if tot < 1.0:
            a = m
        else:
            b = m
    s = [min(max(b, los[i]), his[i]) for i in range(len(ks))]
    tot = sum(s)
    if tot > 1.0 + 1e-9:
        s = [v / tot for v in s]
    return s


def _layout_flow(tiers):
    """按档位把方格铺进整宽画布：**行分区 DP ＋ 行内拉伸铺满**。

    为什么不是单纯行流式：贪心会在 `[1,2,2]` 上排成 `[1,2]+[2]`（第二行只剩半格、留灰条），
    而更优解是 `[1]+[2,2]`（两行都铺满）。这里用一个小 DP：从后往前挑每一行装几帧，
    目标＝`Σ(本行占用面积) − Σ(本行行高)`（即"画布浪费最少"），一行最多 8 帧；
    行内尺寸交给 `_fill_row_sizes`（档位上下界内拉伸到整宽）。

    返回 `(sizes, rows, H)`：`sizes`＝各格标称边长（1/k）、`rows`＝每行的 `(序号, 边长)`、
    `H`＝总高（格宽单位）。
    """
    n = len(tiers)
    if n <= 0:
        return [], [], 0.0
    _MAX_ROW = 8
    NEG = float("-inf")
    best = [NEG] * (n + 1)
    best[n] = 0.0
    choice = [None] * n
    for i in range(n - 1, -1, -1):
        for ln in range(1, min(_MAX_ROW, n - i) + 1):
            ks = [max(1, int(tiers[i + j])) for j in range(ln)]
            if sum(_tier_bounds(k)[0] for k in ks) > 1.0 + 1e-9:
                break                       # 再长只会更宽
            if best[i + ln] == NEG:
                continue
            ss = _fill_row_sizes(ks)
            area = sum(s * s for s in ss)
            row_h = max(ss)
            val = (area - row_h) + best[i + ln]
            if val > best[i] + 1e-12:
                best[i] = val
                choice[i] = (ln, ss)
    rows = []
    i = 0
    guard = 0
    while i < n and choice[i] and guard < n + 1:
        guard += 1
        ln, ss = choice[i]
        rows.append(list(zip(range(i, i + ln), ss)))
        i += ln
    if not rows:                            # 兜底：DP 异常 ⇒ 一帧一行
        rows = [[(j, 1.0 / max(1, int(k)))] for j, k in enumerate(tiers)]
    H = sum(max(s for _, s in row) for row in rows)
    sizes = [1.0 / max(1, int(k)) for k in tiers]
    return sizes, rows, H


def _trim_for_layout(frames, min_tile=320, hard_max_w=1920):
    """在拼图前**减帧**以保证「最小格短边 ≥ min_tile」——宁少几帧，不把字糊掉。

    只在 `min_tile × 最大档位` 超过画布宽上限时才需要减；优先丢最老的、且**非切点/非起手**
    的帧（保留信息量最大的）。"""
    frames = list(frames or [])
    if not min_tile:
        return frames
    while len(frames) > 1:
        tiers = _sheet_tiers(frames)
        if max(tiers) * float(min_tile) <= float(hard_max_w):
            break
        drop = next((i for i, f in enumerate(frames)
                     if (f.get("via") or "").lower() not in ("cut", "start")), None)
        if drop is None:
            break
        frames.pop(drop)
    return frames


def _compose_sheet(tiles, cols=0, pad=6, label_prefix="f", tiers=None, min_tile=0, max_w=960):
    """把若干 `(PIL.Image, 角标文字)` 拼成一张图；返回 `(PNG bytes, meta)`。

    · `tiers=None` ⇒ **老等分网格**（与改前一致，fixed／effect_strip 等继续用）；
    · `tiers=[k…]` ⇒ **按档位大小＋行流式排布**：k=1 整幅宽、k=2 半幅、k=3 三分之一…
      重要的帧（切点/起手）给大格、次要帧（积分/兜底）给小格；**单帧时就是整幅**（细节最好）。
      `min_tile>0` ⇒ 最小格短边硬约束（配合 `_trim_for_layout` 减帧来满足）。
    """
    import io as _io
    import math as _math
    from PIL import Image as _Image, ImageDraw as _ImageDraw
    try:
        from PIL import ImageFont as _ImageFont
        _font = _ImageFont.load_default()
    except Exception:
        _font = None
    if not tiles:
        raise RuntimeError("拼图：没有可用帧")
    imgs = [t[0] for t in tiles]
    labels = [t[1] for t in tiles]
    n = len(imgs)
    tw, th = imgs[0].size
    _asp = tw / float(th or 1)

    def _label(draw, x, y, w, h, txt):
        if not txt or _font is None:
            return
        try:
            tb = draw.textbbox((0, 0), txt, font=_font)
            lw, lh = max(1, tb[2] - tb[0]), max(1, tb[3] - tb[1])
            bx, by = x + 3, y + h - lh - 6
            draw.rectangle([bx - 2, by - 1, bx + lw + 3, by + lh + 1], fill=(0, 0, 0))
            draw.text((bx, by), txt, fill=(235, 235, 235), font=_font)
        except Exception:
            pass

    if tiers and len(tiers) == n:
        sizes, rows, Hn = _layout_flow(tiers)
        _maxk = max(1, max(int(k) for k in tiers))
        W = int(max_w or 960)
        if min_tile:
            W = max(W, int(min_tile) * _maxk)
        W = max(160, min(1920, W))
        Hpx = max(90, int(round(W / _asp)))
        H = max(1, int(round(Hn * Hpx)))
        canvas = _Image.new("RGB", (W + 2 * pad, H + 2 * pad), (28, 28, 30))
        draw = _ImageDraw.Draw(canvas)
        # 两处细节：
        #   ① 缝 = 1px（画布底色透出来，相邻小格不连成一片）；
        #   ② **未覆盖区铺灰条**（(42,42,46)）——纯黑空洞看着像"图没画完"。
        _PAD_FILL = (42, 42, 46)
        def _label_tl(px, py, txt):
            """角标放**左上角、带描边、不画黑底**（黑底在小格上视觉噪音太大）。"""
            if not txt or _font is None:
                return
            try:
                for _dx, _dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
                    draw.text((px + 2 + _dx, py + 2 + _dy), txt, fill=(0, 0, 0), font=_font)
                draw.text((px + 2, py + 2), txt, fill=(255, 235, 59), font=_font)
            except Exception:
                pass
        y = 0.0
        cell_sizes = []
        max_cols = 0
        for row in rows:
            rh = max(s for _, s in row)
            row_h = max(1, int(round(rh * Hpx)))
            max_cols = max(max_cols, len(row))
            x = 0.0
            for i, s in row:
                cw = max(6, int(round(s * W)) - 1)          # 留 1px 缝
                ch = max(6, int(round(s * Hpx)) - 1)
                px = pad + int(round(x * W))
                py = pad + int(round(y * Hpx))
                im = imgs[i]
                if im.size != (cw, ch):
                    im = im.resize((cw, ch), _Image.BILINEAR)
                canvas.paste(im, (px, py))
                # 本行里比行高矮的格子 ⇒ 下面空出来的那块铺灰条
                _gap_h = row_h - ch
                if _gap_h > 2:
                    draw.rectangle([px, py + ch, px + cw, py + row_h], fill=_PAD_FILL)
                _label_tl(px, py, labels[i] if i < len(labels) else "")
                cell_sizes.append([cw, ch])
                x += s
            # 本行没铺满整宽（档位拉满也不够）⇒ 行尾铺灰条
            _right = pad + int(round(x * W))
            if W - int(round(x * W)) > 2:
                draw.rectangle([_right, pad + int(round(y * Hpx)),
                                pad + W, pad + int(round(y * Hpx)) + row_h], fill=_PAD_FILL)
            y += rh
        buf = _io.BytesIO()
        canvas.save(buf, "PNG")
        _png = buf.getvalue()
        _tiny = min(cell_sizes, key=lambda z: z[0] * z[1]) if cell_sizes else [0, 0]
        return _png, {"frames": n, "tile": _tiny, "grid": [max_cols, len(rows)],
                      "canvas": [W + 2 * pad, H + 2 * pad], "bytes": len(_png),
                      "layout": "tiers", "tiers": list(tiers),
                      "cell_sizes": cell_sizes}

    # —— 老等分网格（tiers=None；与改前一致）——
    c = int(cols or 0)
    if c <= 0:
        c = max(1, int(round(_math.sqrt(n * 1.35))))
    c = max(1, min(n, c))
    rows = int(_math.ceil(n / float(c)))
    W = c * tw + (c + 1) * pad
    H = rows * th + (rows + 1) * pad
    canvas = _Image.new("RGB", (W, H), (28, 28, 30))
    draw = _ImageDraw.Draw(canvas)
    for i, im in enumerate(imgs):
        col, row = i % c, i // c
        x = pad + col * (tw + pad)
        y = pad + row * (th + pad)
        if im.size != (tw, th):
            im = im.resize((tw, th), _Image.BILINEAR)
        canvas.paste(im, (x, y))
        _label(draw, x, y, tw, th, labels[i] if i < len(labels) else "")
    buf = _io.BytesIO()
    canvas.save(buf, "PNG")
    png = buf.getvalue()
    return png, {"frames": n, "tile": [tw, th], "grid": [c, rows],
                 "canvas": [W, H], "bytes": len(png), "layout": "grid"}


def _watch_frames(grab_fn, watch_ms=8000.0, fps=6.0, min_frames=3, max_frames=6,
                  tile_max=0, grid_cols=0, label_prefix="f", progress=None, cancel_ev=None,
                  sensitivity=1.0, cut_abs=None, integral_sec=None, dup_min=None):
    """**连续观察**：在 watch_ms 内按 fps 采样，按画面变化量挑关键帧，返回拼图 + meta。

    `grab_fn()` 必须返回 PIL.Image 或屏幕原始帧 dict（取图失败抛异常即可，本函数会计失败数并继续）。
    **变化量阈值可调**（不同内容/不同 AI 的手感不同）：
      · `sensitivity`（默认 1.0，范围 0.1~10）：**>1 更敏感**（更容易留帧、出图更密），**<1 更迟钝**（更省 token）；
      · `cut_abs`：切点绝对地板（默认按 12/sensitivity）；
      · `integral_sec`：积分取帧的"中等强度画面攒几秒算一个采样点"（默认 3.0 秒，再除以 sensitivity）；
      · `dup_min`：与上一保留帧差异小于它就不留（默认 8/sensitivity）。
    返回 `(PNG bytes, meta)`；一帧都没取到 ⇒ RuntimeError；等待期间 ESC ⇒ RuntimeError。
    """
    fps = max(0.5, min(20.0, float(fps or 6.0)))
    interval = max(0.06, 1.0 / fps)          # 采样间隔（≥60ms：再快也只是重复抓同一画面）
    dur = max(0.3, min(60.0, float(watch_ms or 0) / 1000.0))
    max_frames = max(2, min(24, int(max_frames or 6)))
    min_frames = max(1, min(max_frames, int(min_frames or 3)))
    tile_cap = int(tile_max or 0) or (640 if max_frames <= 6 else 480)
    tile_cap = max(64, min(1920, tile_cap))
    _sens = max(0.1, min(10.0, float(sensitivity or 1.0)))
    _cut = float(cut_abs) if cut_abs not in (None, "") else (_SEE_CUT_ABS / _sens)
    _integ = float(integral_sec) if integral_sec not in (None, "") else _SEE_INTEGRAL_SEC
    _dup_min = float(dup_min) if dup_min not in (None, "") else (_SEE_DUP_MIN / _sens)
    need = _SEE_REF_SCORE * (1.0 / interval) * _integ / _sens
    from PIL import Image as _PI

    t_start = time.perf_counter()
    pending = []
    kept = []
    prev_hsv = None
    prev_score_v = None
    last_kept_hsv = None
    last_bound = -1e9
    last_cut = -1e9
    acc = 0.0
    fails = 0
    total = 0
    tw = th = None

    def _keep(entry, via):
        nonlocal last_kept_hsv, last_bound, tw, th
        dup = None
        if last_kept_hsv is not None and entry.get("hsv") is not None:
            dup = _see_score(last_kept_hsv, entry["hsv"])
        if dup is not None and dup < _dup_min and via not in ("cut", "floor"):
            return False
        img = entry.get("img")
        if img is None and entry.get("raw"):
            try:
                img = _pil_of_raw_frame(entry["raw"])
            except Exception:
                img = None
        if img is None:
            return False
        if tw is None:
            w, h = img.size
            sc = min(1.0, tile_cap / float(max(w, h) or 1))
            tw, th = max(16, int(round(w * sc))), max(16, int(round(h * sc)))
        tile = img if img.size == (tw, th) else img.resize((tw, th), _PI.BILINEAR)
        kept.append({"t": entry["t"], "score": entry.get("score"), "via": via,
                     "img": tile, "hsv": entry.get("hsv")})
        last_kept_hsv = entry.get("hsv")
        last_bound = entry["t"]
        return True

    def _decide(entry, next_score):
        nonlocal acc, last_cut
        s = entry.get("score")
        if s is None:
            _keep(entry, "start")
            return
        px = entry.get("prev") or 0.0
        nx = next_score if next_score is not None else 0.0
        spike = (next_score is None) or (s > _SEE_CUT_SPIKE * max(1.0, px, nx))
        if s >= _cut and spike and (entry["t"] - last_cut) >= _SEE_CUT_MIN_GAP:
            last_cut = entry["t"]
            acc = 0.0
            _keep(entry, "cut")
            return
        acc += s
        if acc >= need and (entry["t"] - last_bound) >= _SEE_MARK_MIN_GAP:
            if _keep(entry, "mark"):
                acc = 0.0
            return
        if (entry["t"] - last_bound) >= _SEE_FLOOR_SEC:
            _keep(entry, "floor")

    i = 0
    while True:
        if cancel_ev is not None and cancel_ev.is_set():
            raise RuntimeError("抽帧观察已取消（已保留 %d 帧）" % len(kept))
        elapsed = time.perf_counter() - t_start
        if elapsed > dur:
            break
        total += 1
        frame = None
        try:
            frame = grab_fn()
        except Exception:
            frame = None
        t_now = time.perf_counter() - t_start
        if frame is not None:
            if isinstance(frame, dict):      # 屏幕路径：原始 BGRA，只算小图 HSV，不转整帧
                import numpy as _np
                _arr = _np.frombuffer(frame["bgra"], dtype=_np.uint8).reshape(
                    int(frame["h"]), int(frame["w"]), 4)
                cur_hsv = _px_bgra_scan_hsv(_arr)
                _img = None
            else:                            # 窗口路径：已是 PIL
                cur_hsv = _see_small_hsv(frame)
                _img = frame
            score = None if prev_hsv is None else _see_score(prev_hsv, cur_hsv)
            entry = {"t": t_now, "score": score, "prev": prev_score_v,
                     "img": _img, "raw": (frame if isinstance(frame, dict) else None),
                     "hsv": cur_hsv}
            prev_hsv = cur_hsv
            prev_score_v = score
            pending.append(entry)
            while len(pending) >= 2:
                _decide(pending[0], pending[1]["score"])
                pending.pop(0)
        else:
            fails += 1
        if progress is not None:
            try:
                progress.update({"samples": total, "kept": len(kept),
                                 "t": round(time.perf_counter() - t_start, 2)})
            except Exception:
                pass
        if len(kept) >= max_frames:
            break
        if elapsed >= dur and len(kept) >= min_frames:
            break
        i += 1
        nxt = t_start + i * interval
        left = nxt - time.perf_counter()
        if left > 0:
            if not wait_with_cancel(min(left, 0.25)):
                raise RuntimeError("抽帧观察被取消（已保留 %d 帧）" % len(kept))
        elif left < -0.5:
            i = int((time.perf_counter() - t_start) / interval) + 1
    while pending:
        _decide(pending[0], pending[1]["score"] if len(pending) > 1 else None)
        pending.pop(0)
    if not kept:
        raise RuntimeError("抽帧观察：%d 次采样全部失败（未取到画面）" % total)
    # 先减帧（保证最小格不糊），再按档位排布；显式给了 grid_cols 仍走老等分网格。
    _min_tile = 320
    if not grid_cols:
        kept = _trim_for_layout(kept, min_tile=_min_tile)
    tiles = []
    _fmeta = []
    for k, fr in enumerate(kept):
        sc = fr.get("score")
        lab = "%s%d %.0fms" % (label_prefix, k + 1, fr["t"] * 1000.0)
        if sc is not None:
            lab += " s%.0f" % float(sc)
        tiles.append((fr["img"], lab))
        _fmeta.append({"i": k + 1, "t_ms": int(round(fr["t"] * 1000)),
                       "score": (None if sc is None else round(float(sc), 1)),
                       "via": fr["via"]})
    _tiers = None if grid_cols else _sheet_tiers(_fmeta)
    png, gmeta = _compose_sheet(tiles, cols=grid_cols, label_prefix=label_prefix,
                                tiers=_tiers, min_tile=_min_tile)
    _elapsed = time.perf_counter() - t_start
    meta = dict(gmeta)
    meta.update({
        "mode": "watch", "watch_ms": int(round(dur * 1000)),
        "fps": round(1.0 / interval, 2), "samples": total, "fails": fails,
        "elapsed_s": round(_elapsed, 2),
        "eff_fps": (round(total / _elapsed, 1) if _elapsed > 0 else None),
        "sensitivity": _sens, "cut_abs": round(_cut, 3),
        "integral_sec": round(_integ, 3), "integral_need": round(need, 2),
        "dup_min": round(_dup_min, 2),
        "kept": len(kept), "min_frames": min_frames, "max_frames": max_frames,
        "frames_meta": _fmeta,
    })
    return png, meta


def _fixed_frames(grab_fn, frames=6, interval_ms=200, tile_max=0, grid_cols=0, label_prefix="f",
                  progress=None, cancel_ev=None):
    """**固定间隔抽 N 帧**并拼图（与 `_watch_frames` 共用 `_compose_sheet`）。
    固定模式适合「就是要 N 帧、不关心变化」的场合；观察动画/视频优先用 watch 模式。"""
    from PIL import Image as _PI
    n = max(2, min(60, int(frames or 2)))
    iv = max(0.0, min(60.0, float(interval_ms or 0) / 1000.0))
    tile_cap = int(tile_max or 0) or (960 if n <= 2 else (720 if n <= 4 else (640 if n <= 6 else 480)))
    tile_cap = max(64, min(1920, tile_cap))
    t_start = time.perf_counter()
    tiles = []
    times = []
    fails = 0
    tw = th = None
    for i in range(n):
        if cancel_ev is not None and cancel_ev.is_set():
            raise RuntimeError("抽帧已取消（已取 %d 帧）" % len(tiles))
        if i:
            due = t_start + i * iv
            left = due - time.perf_counter()
            if left > 0 and not wait_with_cancel(min(left, 1.0)):
                raise RuntimeError("抽帧被取消（已取 %d 帧）" % len(tiles))
        frame = None
        try:
            frame = grab_fn()
        except Exception:
            frame = None
        t_now = time.perf_counter() - t_start
        if progress is not None:
            try:
                progress.update({"samples": i + 1, "kept": len(tiles), "t": round(t_now, 2)})
            except Exception:
                pass
        if frame is None:
            fails += 1
            continue
        img = _pil_of_raw_frame(frame) if isinstance(frame, dict) else frame
        if tw is None:
            w, h = img.size
            sc = min(1.0, tile_cap / float(max(w, h) or 1))
            tw, th = max(16, int(round(w * sc))), max(16, int(round(h * sc)))
        tile = img if img.size == (tw, th) else img.resize((tw, th), _PI.BILINEAR)
        tiles.append((tile, "%s%d %.0fms" % (label_prefix, len(tiles) + 1, t_now * 1000.0)))
        times.append(int(round(t_now * 1000)))
    if not tiles:
        raise RuntimeError("抽帧：%d 帧全部取图失败" % n)
    png, gmeta = _compose_sheet(tiles, cols=grid_cols, label_prefix=label_prefix)
    meta = dict(gmeta)
    meta.update({"mode": "frames", "frames_requested": n, "frames_ok": len(tiles),
                 "fails": fails, "interval_ms": int(round(iv * 1000)),
                 "frames_meta": [{"i": k + 1, "t_ms": times[k]} for k in range(len(tiles))]})
    return png, meta


def _make_frame_grabber(region="fullscreen", window_title=None, rect_for_shot=None, prio=None):
    """构造**可重复调用的单帧取图闭包**（返回 `(grab_fn, close_fn)`）。

    与 `_grab_pil_for_frames` 的区别：mss 实例**只建一次**、所有帧复用（连续观察时
    每帧省一次 `mss.mss()` 的创建开销）；window 路径仍每帧走 PrintWindow。
    `close_fn()` 负责释放 mss（调用方用 finally 收尾）。
    `prio`：`"bg"`＝后台（工具调用中让路；抓屏忙就跳过本帧）｜`"mid"`＝单帧/peek（等工具让完后抓）｜None＝最高（工具附图，不等）。
    """
    from PIL import Image as _Image

    def _capgrab(_fn):
        if prio == "bg":
            # 后台采样：只试抢（工具截图/单帧正在抓时直接跳过本帧，不排队、不拖住前台）
            _got = _CAP_LOCK.acquire(timeout=0.05)
            if not _got:
                raise RuntimeError("后台让路：前台截图进行中（本帧跳过）")
            try:
                return _fn()
            finally:
                _CAP_LOCK.release()
        if prio == "mid":
            with _CAP_LOCK:
                return _fn()
        with _CAP_LOCK:
            return _fn()

    if isinstance(region, str) and region == "window":
        def _grab_win():
            hwnd = _shot_window_hwnd(window_title)
            if not hwnd:
                raise RuntimeError("窗口截图：解析不到窗口（未聚焦/标题不存在）")
            img, _tr, _g = _capgrab(lambda: _capture_window_for_view(hwnd))
            if rect_for_shot:
                # ★0.1.63：rect=屏幕物理坐标；图原点取 geom["img_rect"][:2]（与刻度尺同一真源）
                _ir = _g.get("img_rect") or [0, 0, 0, 0]
                _cx, _cy, _cw, _ch, _ = _rect_abs_to_local(rect_for_shot, (_ir[0], _ir[1]), img.size)
                img = img.crop((_cx, _cy, _cx + _cw, _cy + _ch))
            return img
        return _grab_win, (lambda: None)
    mon = _shot_monitor(region, window_title)
    _sct = mss.mss()
    _mon = mon if mon else _sct.monitors[1]

    def _grab_scr():
        """返回**原始 BGRA**（不先转 PIL）：连续观察时省掉每帧一次整帧转换，
        需要成图（被保留的帧）时再转。抓屏走全局单路优先级闸。"""
        raw = _capgrab(lambda: _sct.grab(_mon))
        return {"bgra": raw.bgra, "w": raw.width, "h": raw.height}

    def _close():
        try:
            _sct.close()
        except Exception:
            pass
    return _grab_scr, _close


def _pil_of_raw_frame(rf):
    """`{bgra,w,h}` → PIL.Image（只在「这一帧被保留」时才调用）。"""
    from PIL import Image as _Image
    return _Image.frombytes("RGB", (int(rf["w"]), int(rf["h"])), rf["bgra"], "raw", "BGRX")


# ── 后台观察任务层已删除：观察统一走「持续观察会话」`vision_action=start/pull/stop`；
#    一次性拼图保留为**同步返回**（`watch_ms`/`frames`，调用即等结果）。删掉的是旧的两步式
#    `watch_nowait=true`→`watch_result`/`watch_cancel`/`watch_overwrite` 任务层：它已被持续会话覆盖，
#    且与"后台只允许一路采集"的规则冲突。









# ── 持续观察会话（边看边玩；后台常驻，退出/空闲超时自动关）────────────────────────────
#   与「一次性观察任务」的区别：它**一直跑着**，每次取只回「自上次以来」的新帧（游标增量）；
#   没变化就只回一句回执、不出图。这是 pull 模型下最接近"边看边玩"的形态。
_VIS = {}
_VIS_LOCK = threading.Lock()
_VIS_SEQ = [0]
_VIS_TTL = 180.0        # 空闲多少秒自动关（防止忘了关而一直占用采集/CPU）
_VIS_LIFE_DEFAULT = 120  # 持续会话**默认存活秒数**（不能一直抓）；显式传 vision_seconds=0 才不限时
_VIS_MAX = 2            # 同时最多几个会话（新开会停掉旧的）
_CAP_LOCK = threading.Lock()   # 全局唯一抓屏锁：同一时刻只允许一路在抓（会话/任务/单帧共用）
# 后台单拍（peek）：独立任务，**不占观察会话槽、不与概览/游戏会话互覆**；只抓一张、不自动续
_PEEK = {}
_PEEK_LOCK = threading.Lock()
_PEEK_SEQ = [0]
# 工具调用期间后台让路：所有工具默认会附带截图（也可 include_image=false 不带），
# 不论带不带，工具调用在跑时后台采集都**让路**；单帧/peek 也要等工具调用结束。
# 要求：后台最多同时一路在抓；工具默认附图优先级最高。
_TOOL_ACTIVE = [0]
_TOOL_MON = threading.Condition()


def _tool_enter():
    with _TOOL_MON:
        _TOOL_ACTIVE[0] += 1


def _tool_exit():
    with _TOOL_MON:
        _TOOL_ACTIVE[0] = max(0, _TOOL_ACTIVE[0] - 1)
        _TOOL_MON.notify_all()


def _wait_tool_idle(timeout=2.0):
    with _TOOL_MON:
        _end = time.time() + max(0.0, float(timeout))
        while _TOOL_ACTIVE[0] > 0:
            _left = _end - time.time()
            if _left <= 0:
                return False
            _TOOL_MON.wait(min(_left, 0.05))
        return True



def _maybe_push_vis_frame(sess):
    """持续会话：**有新内容才拼一张 hero 版式、自动注入上下文**（与 pull 同一原理：
    最新帧大图＋变化区裁片＋历史小格），**不是每帧原始画面**。

    ★★**节流**——
    节奏设计：把唤醒压到 8/15/30 秒一档、并且一张图里
    攒好几帧才发；我们原来"每留一帧就注入"确实太烧。现在两条闸：
      ① **最短间隔** `push_interval_ms`（默认 10s，0＝不节流）；
      ② **至少新增 N 帧** `push_min_new`（默认 2；第一次推送不受此限，保证一开会先给一张现状）。
    节流期间帧照常进环形缓冲（历史小格因此更满），间隔到了下一个变化再推一张。
    静止（与上次同一画面）时不出图、不推送（和 pull 同判据，省 token）。"""
    try:
        if not _PUSH_SINK:
            return
        if sess.get("_pushing"):
            return
        _now = time.time()
        _iv = max(0.0, float(sess.get("push_interval_ms") or 0) / 1000.0)
        _last_t = float(sess.get("last_push_t") or 0.0)
        _kept_now = int(sess.get("kept") or 0)
        if _last_t and _iv and (_now - _last_t) < _iv:
            return                                   # ① 最短间隔未到
        if _last_t and (_kept_now - int(sess.get("last_push_kept") or 0)) < \
                max(1, int(sess.get("push_min_new") or 2)):
            return                                   # ② 新增帧不够，攒着
        _lr = sess.get("latest_raw")
        if _lr is None:
            return
        _fresh = _pil_of_frame_any(_lr)
        if _fresh is None:
            return
        sess["_pushing"] = True
        try:
            _prev = sess.get("last_push_img")
            _bbox = _diff_bbox_pil(_prev, _fresh)
            if _prev is not None and _bbox is None:
                return                               # 静止 ⇒ 不推送（与 pull 一致）
            with _VIS_LOCK:
                _frames = list(sess.get("frames") or [])
            _hn = max(1, min(6, int(sess.get("hero_history") or 2)))
            _hist = [fr["img"] for fr in _frames[-_hn:]]
            _hero = _hero_sheet(_fresh, _prev, _hist, max_w=960, split=False, max_history=_hn)
            sess["last_push_img"] = _fresh
            sess["last_push_t"] = _now
            sess["last_push_kept"] = _kept_now
            _m = _hero.get("meta") or {}
            _main = _m.get("main") or list(_fresh.size)
            _canvas = _m.get("canvas") or _main
            _gap = ("（节流 %.0fs）" % _iv) if _iv else ""
            _txt = ("【持续观察 · 自动注入】%s ｜ 主图 %dx%d ｜ 画布 %dx%d ｜ 历史小格 %d ｜ 缓冲 %d 帧%s"
                    % (sess.get("id"), _main[0], _main[1], _canvas[0], _canvas[1],
                       _m.get("history", 0), len(_frames), _gap))
            _push_event("frame", {"id": sess.get("id"), "text": _txt,
                                  "meta": {"tile": list(_main)},
                                  # ★必须取 `png`：hero 合成图在 png 字段；`main_png` 只在 split 模式才有值
                                  #   （这里曾误取 main_png ⇒ 注入消息只有文字、没有图）
                                  "png_b64": _b64_of_png(_hero.get("png"))})
        finally:
            sess["_pushing"] = False
    except Exception:
        sess["_pushing"] = False


def _push_vis_expired(sess, reason):
    """两条注入模式都有时间限制 ⇒ 到期要主动注入一条提示，
    别让模型干等（或直到 pull 才发现采集已经停了）。只推文本、不带图——到期没有新画面可看；
    每个会话只推一次（`_expired_pushed` 闸）。"""
    try:
        if not _PUSH_SINK:
            return
        if sess.get("_expired_pushed"):
            return
        sess["_expired_pushed"] = True
        _life = int(sess.get("life") or 0)
        _kept = int(sess.get("kept", 0) or 0)
        _total = int(sess.get("total", 0) or 0)
        _ring = len(sess.get("frames") or [])
        if reason == "expired":
            _txt = ("【持续观察 · 已到期】会话 %s 到限时 %ds（采样 %d 次、留帧 %d、缓冲 %d 帧）——采集已停。"
                    "要看新的就重新 vision_action=\"start\"（vision_seconds=0 可开不限时）。"
                    % (sess.get("id"), _life, _total, _kept, _ring))
        else:
            _txt = ("【持续观察 · 已超时关闭】会话 %s 空闲超过 %ds 无取图，已自动停（采样 %d 次、留帧 %d）——"
                    "要看新的就重新 vision_action=\"start\"。"
                    % (sess.get("id"), int(_VIS_TTL), _total, _kept))
        _push_event("expired", {"id": sess.get("id"), "text": _txt,
                                "meta": {"reason": reason, "life": _life,
                                         "kept": _kept, "total": _total}})
    except Exception:
        pass


def _vis_keep(sess, st, entry, via):
    """会话的一帧是否留：去重 → 缩放成小瓦片 → 入环形缓冲（旧的自动丢）。"""
    from PIL import Image as _PI
    hv = entry.get("hsv")
    if st.get("last_hsv") is not None and hv is not None:
        if _see_score(st["last_hsv"], hv) < st["dup_min"] and via not in ("cut", "floor"):
            return False
    try:
        img = entry.get("img")
        if isinstance(img, dict):
            img = _pil_of_raw_frame(img)
        if img is None:
            return False
        tile = img
        if max(tile.size) > 640:
            _sc = 640.0 / max(tile.size)
            tile = tile.resize((max(16, int(tile.size[0] * _sc)), max(16, int(tile.size[1] * _sc))),
                               _PI.BILINEAR)
        with _VIS_LOCK:
            sess["seq"] += 1
            sess["frames"].append({"seq": sess["seq"], "t": round(entry["t"], 3),
                                   "score": entry.get("score"), "via": via, "img": tile})
            _ring = max(2, int(sess.get("ring_max") or 12))
            if len(sess["frames"]) > _ring:
                sess["frames"] = sess["frames"][-_ring:]
            sess["kept"] = sess["seq"]
    except Exception:
        return False
    st["last_hsv"] = hv
    st["last_bound"] = entry["t"]
    # 持续会话**按拼图节奏自动注入**（有新内容才拼一张 hero 图；静止不推）。
    #   与概览拼图（tiers 版式）同一原理，**差别只是排版**。
    _maybe_push_vis_frame(sess)
    return True


def _vis_decide(sess, st, entry, next_score, cut, need, dup_min):
    s = entry.get("score")
    if s is None:
        _vis_keep(sess, st, entry, "start")
        return
    px = entry.get("prev") or 0.0
    nx = next_score if next_score is not None else 0.0
    spike = (next_score is None) or (s > _SEE_CUT_SPIKE * max(1.0, px, nx))
    if s >= cut and spike and (entry["t"] - st["last_cut"]) >= _SEE_CUT_MIN_GAP:
        st["last_cut"] = entry["t"]
        st["acc"] = 0.0
        _vis_keep(sess, st, entry, "cut")
        return
    st["acc"] += s
    if st["acc"] >= need and (entry["t"] - st["last_bound"]) >= _SEE_MARK_MIN_GAP:
        if _vis_keep(sess, st, entry, "mark"):
            st["acc"] = 0.0
        return
    if (entry["t"] - st["last_bound"]) >= _SEE_FLOOR_SEC:
        _vis_keep(sess, st, entry, "floor")


def _vis_run(sess):
    """会话主循环：持续采样→打分→挑帧→入环形缓冲；直到 stop/超时。"""
    try:
        _grab, _close = _make_frame_grabber(sess["region"], sess["window_title"], sess["rect_for_shot"],
                                    prio="bg")
    except Exception as _e:
        sess["state"] = "error"
        sess["error"] = "取图器创建失败：%s" % (str(_e)[:160],)
        return
    try:
        # ── 后台单拍（peek）：只抓一张、立即结束、**不自动续**；完成后 pull 取图 ──
        if sess.get("peek"):
            try:
                _fr = _grab()
                if _fr is None:
                    raise RuntimeError("取图失败")
                _pimg = _pil_of_frame_any(_fr)
                _ppng = _pil_png_bytes(_pimg)
                with _VIS_LOCK:
                    sess["batch_png"] = _ppng
                    sess["batch_meta"] = {"frames": 1, "tile": list(_pimg.size),
                                          "grid": [1, 1], "canvas": list(_pimg.size),
                                          "bytes": len(_ppng), "peek": True}
                    sess["total"] = 1
                    sess["state"] = "done"
            except Exception as _pe:
                sess["state"] = "error"
                sess["error"] = "单拍失败：%s" % (str(_pe)[:200],)
            finally:
                try:
                    _close()
                except Exception:
                    pass
                sess["finished"] = time.time()
            return
        # ── 后台概览模式：后台跑、不阻塞调用；完成后自动注入 ──
        if sess.get("batch_ms") or int(sess.get("batch_frames") or 0) > 1:
            try:
                _bp = sess.get("batch_progress")
                if _bp is None:
                    _bp = sess["batch_progress"] = {"samples": 0, "kept": 0}
                # 容量旋钮：`sheet_frames` 优先，不传沿用 `max_frames`（概览这边就是这张图的帧数）。
                _cap_frames = int(sess.get("sheet_frames") or 0)
                _maxf = _cap_frames or int(sess.get("batch_max_frames") or 6)
                _maxf = max(1, min(12, _maxf))
                _minf = max(1, min(_maxf, int(sess.get("batch_min_frames") or 3)))
                _cap_note = ""
                if sess.get("batch_ms"):
                    _want_ms = float(sess["batch_ms"])
                    _life = sess.get("life")
                    if _life:
                        # 概览任务同样受 `vision_seconds` 限时：**不越限**，到点就收工出图，
                        # 并在回执/注入里写明"到限时提前结束"（否则图出来得比说好的晚）。
                        _left_ms = max(400.0, (_life - (time.time() - sess["started"])) * 1000.0)
                        if _want_ms > _left_ms:
                            _want_ms = _left_ms
                            _cap_note = "（到限时 %ds 提前结束）" % int(_life)
                    _b_png, _b_meta = _watch_frames(
                        _grab, watch_ms=_want_ms, fps=float(sess["fps"] or 8.0),
                        min_frames=_minf, max_frames=_maxf,
                        tile_max=int(sess.get("tile_max") or 0),
                        grid_cols=int(sess.get("grid_cols") or 0),
                        label_prefix="f", progress=_bp, cancel_ev=sess["stop"],
                        sensitivity=float(sess.get("sensitivity") or 1.0),
                        cut_abs=sess.get("cut_abs"), integral_sec=sess.get("integral_sec"),
                        dup_min=sess.get("dup_min"))
                else:
                    _nf = int(sess.get("batch_frames") or 1)
                    if _cap_frames and _nf > _cap_frames:
                        _nf = _cap_frames
                        _cap_note = "（按 sheet_frames 只取 %d 帧）" % _nf
                    _b_png, _b_meta = _fixed_frames(
                        _grab, frames=_nf,
                        interval_ms=int(sess.get("batch_interval_ms") or 200),
                        tile_max=int(sess.get("tile_max") or 0),
                        grid_cols=int(sess.get("grid_cols") or 0),
                        label_prefix="f", progress=_bp, cancel_ev=sess["stop"])
                with _VIS_LOCK:
                    sess["batch_png"], sess["batch_meta"] = _b_png, _b_meta
                    sess["state"] = "done"
                # 概览拼图完成后自动注入上下文（不再要求调用方 pull）
                _wm = dict(_b_meta or {})
                _wtxt = ("【后台概览 · 自动注入】%s ｜ 保留 %d 帧 ｜ 单格 %d×%d ｜ 网格 %d×%d ｜ "
                         "画布 %d×%d ｜ %d 字节%s"
                         % (sess.get("id"), int(_wm.get("frames") or 0),
                            (_wm.get("tile") or [0, 0])[0], (_wm.get("tile") or [0, 0])[1],
                            (_wm.get("grid") or [0, 0])[0], (_wm.get("grid") or [0, 0])[1],
                            (_wm.get("canvas") or [0, 0])[0], (_wm.get("canvas") or [0, 0])[1],
                            int(_wm.get("bytes") or 0), _cap_note))
                _push_event("watch", {"id": sess.get("id"), "text": _wtxt, "meta": _wm,
                                      "png_b64": _b64_of_png(_b_png)})
            except Exception as _be:
                sess["state"] = "error"
                sess["error"] = "后台概览失败：%s" % (str(_be)[:200],)
            finally:
                try:
                    _close()
                except Exception:
                    pass
                sess["finished"] = time.time()
            return
        interval = max(0.06, 1.0 / max(0.5, min(20.0, float(sess["fps"] or 8.0))))
        _sens = max(0.1, min(10.0, float(sess["sensitivity"] or 1.0)))
        _cut = (float(sess["cut_abs"]) if sess["cut_abs"] not in (None, "")
                else (_SEE_CUT_ABS / _sens))
        _integ = (float(sess["integral_sec"]) if sess["integral_sec"] not in (None, "")
                  else _SEE_INTEGRAL_SEC)
        _dup = (float(sess["dup_min"]) if sess["dup_min"] not in (None, "")
                else (_SEE_DUP_MIN / _sens))
        need = _SEE_REF_SCORE * (1.0 / interval) * _integ / _sens
        st = {"last_hsv": None, "last_bound": -1e9, "last_cut": -1e9, "acc": 0.0,
              "dup_min": _dup}
        prev_hsv = None
        prev_score = None
        pending = []
        t0 = time.perf_counter()
        i = 0
        while not sess["stop"].is_set():
            _life = sess.get("life")
            if _life and (time.time() - sess["started"]) >= _life:
                sess["state"] = "expired"      # 默认限时到点自动停（不是一直抓）
                _push_vis_expired(sess, "expired")   # 到期主动注入提示
                break
            if time.time() - sess["last_activity"] > _VIS_TTL:
                sess["state"] = "timeout"
                _push_vis_expired(sess, "timeout")   # 空闲超时同样主动告知
                break
            frame = None
            try:
                frame = _grab()
            except Exception:
                frame = None
            t_now = time.perf_counter() - t0
            sess["total"] += 1
            if frame is not None:
                sess["latest_raw"] = frame        # pull 复用这一帧（不再二次抓屏）
                sess["latest_t"] = round(t_now, 3)
                if isinstance(frame, dict):
                    import numpy as _np
                    _arr = _np.frombuffer(frame["bgra"], dtype=_np.uint8).reshape(
                        int(frame["h"]), int(frame["w"]), 4)
                    cur_hsv = _px_bgra_scan_hsv(_arr)
                else:
                    cur_hsv = _see_small_hsv(frame)
                score = None if prev_hsv is None else _see_score(prev_hsv, cur_hsv)
                pending.append({"t": t_now, "score": score, "prev": prev_score,
                                "img": frame, "hsv": cur_hsv})
                prev_hsv = cur_hsv
                prev_score = score
                while len(pending) >= 2:
                    _vis_decide(sess, st, pending[0], pending[1]["score"], _cut, need, _dup)
                    pending.pop(0)
                # ★节流后的自动注入：间隔到了且攒够新帧才推一张（平时这里只是两次判断，很便宜）
                _maybe_push_vis_frame(sess)
            else:
                sess["fails"] += 1
            i += 1
            nxt = t0 + i * interval
            left = nxt - time.perf_counter()
            if left > 0:
                sess["stop"].wait(min(left, 0.25))
    except Exception as _e:
        sess["state"] = "error"
        sess["error"] = str(_e)[:200]
    finally:
        try:
            _close()
        except Exception:
            pass
        sess["finished"] = time.time()
        if sess["state"] == "running":
            sess["state"] = "stopped"


def _vis_start(region="fullscreen", window_title=None, rect_for_shot=None, fps=8.0,
               sensitivity=1.0, cut_abs=None, integral_sec=None, dup_min=None, ring_max=12,
               life_seconds=None, batch_ms=0, batch_frames=0, batch_interval_ms=200,
               batch_min_frames=3, batch_max_frames=6, tile_max=0, grid_cols=0, peek=False,
               hero_history=2, sheet_frames=0, push_interval_ms=10000, push_min_new=2):
    """开一个持续会话/后台概览任务：立刻返回会话号；后台按 fps 采样、按变化挑帧进环形缓冲。

    `life_seconds`＝**存活上限**（默认 120s；显式传 0 才不限时）——默认不能一直抓；
    **到期/空闲超时会主动注入一条上下文提示**（"已到期"，不用等调用方 pull）。
    **后台只允许一路采集**（会话之间也不并存，开会话会停掉旧会话）；`pull` 复用会话最近帧、不再二次抓屏。
    **批量模式（后台概览）**：`batch_ms>0` 或 `batch_frames>1` ⇒ 后台采一段时间/固定帧数，拼一张概览图，
    完成后**自动注入上下文**（`pull` 仍可主动取）；调用本身**立即返回、不阻塞**。
    `hero_history`＝hero 版式历史小格数（1–6，默认 2，自动注入与 pull 共用）；
    `sheet_frames`＝tiers/grid 拼图帧数上限（0＝沿用 `batch_max_frames`/`vision_max_frames`）。
    """
    _life = _VIS_LIFE_DEFAULT if life_seconds is None else int(life_seconds)
    if _life < 0:
        _life = _VIS_LIFE_DEFAULT
    _cancelled = 0          # 旧后台任务层已删：字段保留仅为回执兼容
    with _VIS_LOCK:
        _replaced = [s["id"] for s in _VIS.values()]
        for _s in list(_VIS.values()):
            if _s.get("state") == "running":
                try:
                    _s["stop"].set()
                except Exception:
                    pass
        _VIS.clear()
        _VIS_SEQ[0] += 1
        sid = "v%d" % _VIS_SEQ[0]
        sess = {"id": sid, "state": "running", "started": time.time(),
                "finished": None, "last_activity": time.time(),
                "life": _life, "cancelled_jobs": _cancelled, "replaced": _replaced,
                "frames": [], "seq": 0, "cursor": 0,
                "latest_raw": None, "latest_t": None,
                "region": region, "window_title": window_title, "rect_for_shot": rect_for_shot,
                "fps": float(fps or 8.0), "sensitivity": float(sensitivity or 1.0),
                "cut_abs": cut_abs, "integral_sec": integral_sec, "dup_min": dup_min,
                "ring_max": max(2, min(32, int(ring_max or 12))),
                "total": 0, "kept": 0, "fails": 0, "error": "",
                "batch_ms": int(batch_ms or 0), "batch_frames": int(batch_frames or 0),
                "batch_interval_ms": int(batch_interval_ms or 200),
                "batch_min_frames": int(batch_min_frames or 3),
                "batch_max_frames": int(batch_max_frames or 6),
                "tile_max": int(tile_max or 0), "grid_cols": int(grid_cols or 0),
                # 两个容量旋钮：hero 历史小格数 / tiers 拼图帧数上限。
                #   自动注入（_maybe_push_vis_frame）与 pull 都读这里 ⇒ 两条路规则一致。
                "hero_history": max(1, min(6, int(hero_history or 2))),
                "sheet_frames": max(0, min(12, int(sheet_frames or 0))),
                # ★注入节流（只对持续会话有效；概览拼图一次性出图、本来就只推一次）：
                #   `push_interval_ms` 最短间隔（默认 10s，0＝不节流）；`push_min_new` 至少新增几帧。
                "push_interval_ms": max(0, min(120000, int(push_interval_ms if push_interval_ms is not None else 10000))),
                "push_min_new": max(1, min(8, int(push_min_new or 2))),
                "last_push_t": 0.0, "last_push_kept": 0, "_pushing": False,
                "batch_progress": {"samples": 0, "kept": 0},
                "batch_png": None, "batch_meta": None,
                "peek": bool(peek),
                "stop": threading.Event()}
        _VIS[sid] = sess
    th = threading.Thread(target=_vis_run, args=(sess,), daemon=True, name="vis-" + sid)
    sess["thread"] = th
    th.start()
    return sess


def _parse_vision_life(v):
    """`vision_seconds` 参数：不传 ⇒ None（用默认值）；0 ⇒ 不限时；其余取非负整数。"""
    if v in (None, ""):
        return None
    try:
        _n = int(float(v))
    except Exception:
        return None
    return _n if _n >= 0 else None


def _vis_running():
    with _VIS_LOCK:
        return [s for s in _VIS.values() if s.get("state") == "running"]


def _vis_pick(spec=""):
    spec = str(spec or "").strip().lower()
    with _VIS_LOCK:
        if not spec or spec in ("latest", "last"):
            if not _VIS:
                return None, "还没有持续会话（先 `vision_action=\"start\"`）"
            return _VIS[list(_VIS.keys())[-1]], ""
        s = _VIS.get(spec)
        if s is None:
            return None, "没有这个持续会话：%s（`vision_action=\"status\"` 看现有会话）" % spec
        return s, ""


def _pil_of_frame_any(frame):
    """取图器返回 → PIL.Image（屏幕路径是 `{bgra,w,h}`，窗口路径已是 PIL）。"""
    if isinstance(frame, dict):
        return _pil_of_raw_frame(frame)
    return frame


def _diff_bbox_pil(prev_img, cur_img, pixel_delta=8):
    """两帧灰度差分 → 变化 bbox `(x, y, w, h)`（相对图像左上角）；无变化/不可比返回 None。"""
    if prev_img is None or cur_img is None:
        return None
    try:
        import numpy as _np
        a = _np.asarray(prev_img.convert("L"), dtype=_np.int16)
        b = _np.asarray(cur_img.convert("L"), dtype=_np.int16)
        if a.shape != b.shape:
            return (0, 0, int(b.shape[1]), int(b.shape[0]))
        mask = _np.abs(a - b) > int(pixel_delta)
        _n = int(mask.sum())
        if _n < max(8, int(mask.size * 0.0005)):
            return None
        ys, xs = _np.nonzero(mask)
        return (int(xs.min()), int(ys.min()),
                int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))
    except Exception:
        return None


def _hero_sheet(cur_img, prev_img, history, max_w=960, pad=6, split=False,
                max_change_frac=0.75, max_history=2):
    """游戏向版式：**最新帧当主角（整幅大图）＋变化区原生裁片（1:1）＋最多 N 张历史小格**。

    · 主图吃满宽度（细节优先）；变化区只按**原生像素**裁出来（面积小、不缩水）；
    · 变化区占屏过大（>max_change_frac，如整屏换页）⇒ 退化为"半幅小图"，不裁；
    · `split=True` ⇒ 返回**两张独立图**（主图＋变化裁片），用 token 换细节；
    · `max_history`（1–6，默认 2）＝**历史小格数量**。
      ★旧版把 2 写死、且排不下就 `break` **静默丢格**；
      现在按格数**动态定宽**（含变化区占宽），保证请求的格数都排得下，超高再按高缩一次；
    · 没有任何变化/历史 ⇒ 就是一张整幅大图（等于 detail 模式）。
    返回 `{"png", "main_png", "changed_png", "meta"}`。
    """
    from PIL import Image as _PI, ImageDraw as _PI_Draw
    try:
        from PIL import ImageFont as _PI_Font
        _font = _PI_Font.load_default()
    except Exception:
        _font = None
    if cur_img is None:
        raise RuntimeError("hero 版式：没有当前帧")
    cw, ch = cur_img.size
    asp = cw / float(ch or 1)
    W = max(320, min(1200, int(max_w or 960)))
    MW = W
    MH = max(90, int(round(MW / asp)))
    main_img = cur_img if cur_img.size == (MW, MH) else cur_img.resize((MW, MH), _PI.BILINEAR)
    bbox = _diff_bbox_pil(prev_img, cur_img) if prev_img is not None else None
    changed_native = None
    changed_show = None
    changed_label = ""
    changed_kind = ""
    if bbox:
        bx, by, bw, bh = bbox
        if (bw * bh) <= (cw * ch * float(max_change_frac)):
            bx = max(0, min(cw - 1, bx))
            by = max(0, min(ch - 1, by))
            bw = max(1, min(cw - bx, bw))
            bh = max(1, min(ch - by, bh))
            changed_native = cur_img.crop((bx, by, bx + bw, by + bh))
            _cw = min(changed_native.size[0], max(80, int(W * 0.45)))
            if _cw < changed_native.size[0]:
                _sc = _cw / float(changed_native.size[0] or 1)
                changed_show = changed_native.resize(
                    (max(16, _cw), max(16, int(round(changed_native.size[1] * _sc)))), _PI.BILINEAR)
            else:
                changed_show = changed_native
            changed_label = "变化 %d,%d %dx%d（原生）" % (bx, by, bw, bh)
            changed_kind = "roi"
        else:
            changed_show = cur_img.resize((max(16, W // 2), max(16, MH // 2)), _PI.BILINEAR)
            changed_label = "变化范围大(半幅)"
            changed_kind = "full"
    # ★历史小格：按 max_history（1–6，默认 2）取最后 N 张；宽度**按格数动态算**，
    #   保证"请求几格就画几格"（旧版写死 2、且排不下就 break 静默丢格 = 看不到历史）。
    _want_hist = max(0, min(6, int(max_history if max_history is not None else 2)))
    _hist_src = [h for h in list(history or []) if h is not None]
    _hist_src = list(reversed(_hist_src[-_want_hist:])) if _want_hist else []
    # ↑ 反转：**上1 = 最近的一帧**（旧版按时间正序，编号"上1"反而是最老的，看反了）

    thumbs = []
    if _hist_src:
        _n = len(_hist_src)
        _chg_w = changed_show.size[0] if changed_show is not None else 0
        _avail = max(64, W - ((_chg_w + pad) if _chg_w else 0))
        _tw = int((_avail - pad * max(0, _n - 1)) / _n)
        _tw = max(64, min(int(W * 0.22), _tw))
        for _h in _hist_src:
            _im = _h
            if _im.size[0] > _tw:
                _sc = _tw / float(_im.size[0])
                _im = _im.resize((_tw, max(16, int(round(_im.size[1] * _sc)))), _PI.BILINEAR)
            if _im.size[1] > MH:                       # 竖图/高图再按主图高缩一次，别把画布撑高
                _sh = MH / float(_im.size[1] or 1)
                _im = _im.resize((max(16, int(round(_im.size[0] * _sh))), MH), _PI.BILINEAR)
            thumbs.append(_im)

    if split:
        _b1 = _pil_png_bytes(main_img)
        _b2 = _pil_png_bytes(changed_native) if changed_native is not None else None
        return {"main_png": _b1, "changed_png": _b2, "png": None,
                "meta": {"layout": "hero", "split": True, "main": [MW, MH],
                         "changed": (changed_show is not None),
                         "changed_kind": changed_kind,
                         "changed_bbox": (list(bbox) if bbox else None),
                         "history": len(thumbs),
                         "main_bytes": len(_b1),
                         "changed_bytes": (len(_b2) if _b2 else 0)}}

    def _lab(draw, x, y, w, h, txt):
        if not txt or _font is None:
            return
        try:
            tb = draw.textbbox((0, 0), txt, font=_font)
            lw, lh = max(1, tb[2] - tb[0]), max(1, tb[3] - tb[1])
            bx2, by2 = x + 3, y + h - lh - 6
            draw.rectangle([bx2 - 2, by2 - 1, bx2 + lw + 3, by2 + lh + 1], fill=(0, 0, 0))
            draw.text((bx2, by2), txt, fill=(235, 235, 235), font=_font)
        except Exception:
            pass

    bottom_h = 0
    for it in ([changed_show] if changed_show is not None else []) + thumbs:
        bottom_h = max(bottom_h, it.size[1])
    canW = W + 2 * pad
    canH = MH + 2 * pad + ((bottom_h + pad) if bottom_h else 0)
    canvas = _PI.new("RGB", (canW, canH), (28, 28, 30))
    draw = _PI_Draw.Draw(canvas)
    canvas.paste(main_img, (pad, pad))
    # ★第三种（我们自己弄的 hero）手动优化：在**主图上圈出变化区**——
    #   模型看大图时能一眼定位"哪里变了"，不用拿小图反推坐标。
    if bbox:
        try:
            _sx, _sy = (MW / float(cw or 1)), (MH / float(ch or 1))
            _rx0 = pad + int(round(bbox[0] * _sx))
            _ry0 = pad + int(round(bbox[1] * _sy))
            _rx1 = pad + int(round((bbox[0] + bbox[2]) * _sx))
            _ry1 = pad + int(round((bbox[1] + bbox[3]) * _sy))
            draw.rectangle([_rx0, _ry0, max(_rx0 + 1, _rx1), max(_ry0 + 1, _ry1)],
                           outline=(255, 90, 90), width=2)
        except Exception:
            pass
    _lab(draw, pad, pad, MW, MH, "最新%s" % (("｜" + changed_label) if changed_native is None and changed_label else ""))
    if bottom_h:
        x = pad
        y2 = pad + MH + pad
        if changed_show is not None:
            canvas.paste(changed_show, (x, y2))
            _lab(draw, x, y2, changed_show.size[0], changed_show.size[1], changed_label)
            x += changed_show.size[0] + pad
        for j, th in enumerate(thumbs):
            if x + th.size[0] > canW - pad:
                break
            canvas.paste(th, (x, y2))
            _lab(draw, x, y2, th.size[0], th.size[1], "上%d" % (j + 1))
            x += th.size[0] + pad
    png = _pil_png_bytes(canvas)
    return {"png": png, "main_png": None, "changed_png": None,
            "meta": {"layout": "hero", "split": False, "main": [MW, MH],
                     "changed": (changed_show is not None),
                     "changed_kind": changed_kind,
                     "changed_bbox": (list(bbox) if bbox else None),
                     "history": len(thumbs),
                     "canvas": [canW, canH], "bytes": len(png)}}


def _vis_resume(sess):
    """把一个**已存在但已停**的会话重新拉起（沿用同一会话号/缓冲/游标，限时从原 start 时刻算）。"""
    with _VIS_LOCK:
        sess["stop"] = threading.Event()
        sess["state"] = "running"
        sess["finished"] = None
        sess["last_activity"] = time.time()
    th = threading.Thread(target=_vis_run, args=(sess,), daemon=True, name="vis-" + sess["id"])
    sess["thread"] = th
    th.start()
    return sess


def _peek_pick(spec):
    with _PEEK_LOCK:
        if not spec:
            return None
        return _PEEK.get(spec)


def _peek_start(region="fullscreen", window_title=None, rect_for_shot=None, save_path=None, after_ms=0):
    """后台单拍：抓一张就结束。**独立任务**：不占观察会话槽、不与另两种模式互覆；**不自动续**。

    `after_ms` ＝ **延迟单拍**——抓图前先等这么多毫秒（0＝立即，老行为逐字不变；上限 10 分钟）。
    完成后结果走**统一注入通道**，注入上下文**下一条消息**（`forceContext`，不唤醒回合）。
    """
    with _PEEK_LOCK:
        _PEEK_SEQ[0] += 1
        pid = "p%d" % _PEEK_SEQ[0]
        task = {"id": pid, "state": "running", "png": None, "error": "",
                "size": None, "bytes": 0, "saved": save_path or "",
                "started": time.time(), "finished": None,
                "after_ms": max(0, int(after_ms or 0))}
        _PEEK[pid] = task
        if len(_PEEK) > 8:
            for _k in list(_PEEK.keys())[:len(_PEEK) - 8]:
                _PEEK.pop(_k, None)

    def _run():
        try:
            _delay = max(0, int(after_ms or 0))
            if _delay:
                time.sleep(min(_delay, 600000) / 1000.0)   # 延迟单拍（上限 10 分钟，别挂死）
            _g, _c = _make_frame_grabber(region, window_title, rect_for_shot, prio="mid")
            try:
                _fr = _g()
            finally:
                try:
                    _c()
                except Exception:
                    pass
            if _fr is None:
                raise RuntimeError("取图失败")
            _img = _pil_of_frame_any(_fr)
            if rect_for_shot and not (isinstance(region, str) and region == "window"):
                # ★0.1.63：rect=屏幕物理坐标；图原点取本次抓图 monitor 的左上
                _mon = _shot_monitor(region, window_title) or {"left": 0, "top": 0}
                _cx, _cy, _cw, _ch, _ = _rect_abs_to_local(
                    rect_for_shot, (_mon["left"], _mon["top"]), _img.size)
                _img = _img.crop((_cx, _cy, _cx + _cw, _cy + _ch))
            _png = _pil_png_bytes(_img)
            if save_path:
                try:
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, "wb") as _sf:
                        _sf.write(_png)
                except Exception:
                    pass
            with _PEEK_LOCK:
                task["png"] = _png
                task["size"] = list(_img.size)
                task["bytes"] = len(_png)
                task["state"] = "done"
            # 完成后自动注入上下文的下一条消息（不再要求调用方 `vision_action="pull"`）；
            #   sink 不存在时静默跳过。
            _push_event("peek", {"id": pid, "after_ms": int(after_ms or 0),
                                 "meta": {"tile": list(_img.size), "bytes": len(_png),
                                          "delay_ms": int(after_ms or 0)},
                                 "png_b64": _b64_of_png(_png)})
        except Exception as _e:
            with _PEEK_LOCK:
                task["state"] = "error"
                task["error"] = str(_e)[:200]
        finally:
            task["finished"] = time.time()

    th = threading.Thread(target=_run, daemon=True, name="peek-" + pid)
    task["thread"] = th
    th.start()
    return task


def _peek_pull_text(task, want_image=True):
    import base64 as _b64p
    st = task.get("state")
    if st == "running":
        return {"content": [{"type": "text", "text":
                "后台单拍 %s：**进行中**（独立任务、不覆盖观察会话；只抓一张、不会自动续）" % task["id"]}]}
    if st == "done" and task.get("png"):
        _txt = ("后台单拍（一张，不阻塞）：%s ｜ 图 %s ｜ %d 字节 ｜ 独立任务、不自动续"
                % (task["id"], "×".join(str(x) for x in (task.get("size") or [0, 0])),
                   task.get("bytes", 0)))
        _pc = [{"type": "text", "text": _txt}]
        if want_image:
            try:
                _pc.append({"type": "image",
                            "data": _b64p.b64encode(task["png"]).decode("ascii"),
                            "mimeType": "image/png"})
            except Exception:
                pass
        return {"content": _pc}
    return {"content": [{"type": "text", "text":
            "后台单拍 %s：**%s**（%s）；单拍不会自动续，重新 peek 再取"
            % (task["id"], st, task.get("error") or "无错误")}]}


def _vis_pull(spec="", max_frames=4, want_image=True, layout="hero", split=False, auto=True,
              life_seconds=None, hero_history=None, sheet_frames=None):
    """取「自上次以来」的新变化。

    · `layout="hero"`（默认，游戏向）：最新帧整幅大图＋与上次 pull 的像素差算出的
      **变化区原生裁片**＋最多 `hero_history`（1–6，默认 2）个历史小格；没变化只回回执。
      ★历史小格取自**会话环形缓冲**（跨多次 pull 的连续历史），不是"本次增量"那几帧；
      本参数与自动注入共用同一旋钮（`start` 时定，pull 可临时覆盖）。
    · `layout="tiers"`：增量帧按档位拼图（重要帧大、次要帧小）；
      帧数上限＝`sheet_frames`（不传则沿用 `max_frames`）。
    · `layout="grid"`：增量帧等分网格（同上限）。
    · `split=True`（仅 hero）：返回两张独立图（主图＋变化裁片），token 换细节。
    · `auto=True`：**只对已 start 过的会话生效**——会话已停但**没到限时** ⇒ 自动续上采集；
      **没有会话时不会自动抓屏**（必须先 `vision_action="start"`）。
    · `life_seconds`：**中途调整限时**（立即生效；0＝不限时）：传了就改本会话的存活上限，
      按原 start 时刻计算剩余；`start` 新会话会**覆盖旧会话**（旧会话被停掉）。
    """
    def _wait_first(_s, _timeout=1.2):
        _end = time.time() + _timeout
        while time.time() < _end and _s.get("latest_raw") is None and _s.get("state") == "running":
            time.sleep(0.03)

    if spec:
        _pk = _peek_pick(spec)
        if _pk is not None:
            return _peek_pull_text(_pk, want_image=want_image)

    sess, why = _vis_pick(spec)
    if sess is None:
        return {"content": [{"type": "text", "text":
                (why or "没有可用的持续会话") +
                "｜**没有会话时 pull 不会自动抓屏**——请先 `vision_action=\"start\"`。"}], "isError": True}
    _auto_resumed = False
    _is_batch = bool(sess.get("batch_ms") or int(sess.get("batch_frames") or 0) > 1)
    if auto and sess.get("state") != "running" and not _is_batch:
        _life0 = sess.get("life") or 0
        _expired = (sess.get("state") == "expired") or (
            bool(_life0) and (time.time() - sess.get("started", time.time())) >= _life0)
        if (not _expired) and sess.get("state") in ("stopped", "timeout", "error"):
            # 已 start 过、还没到限时 ⇒ pull 自动续上（同一会话，不重置缓冲/游标）
            _vis_resume(sess)
            _auto_resumed = True
            _wait_first(sess)
    _retimed = ""
    if _is_batch:
        import base64 as _b64b
        _bstate = sess.get("state")
        _bp = sess.get("batch_progress") or {}
        if _bstate == "running":
            _bt = (("%dms" % int(sess.get("batch_ms") or 0)) if sess.get("batch_ms")
                   else ("%d 帧" % int(sess.get("batch_frames") or 0)))
            return {"content": [{"type": "text", "text":
                    "后台概览任务 %s：**进行中**（不阻塞）｜ 目标 %s ｜ 已采样 %d 次、保留 %d 帧 ｜ "
                    "完成后**自动注入上下文**（也可 pull 主动取；stop 可取消）"
                    % (sess["id"], _bt, _bp.get("samples", 0), _bp.get("kept", 0))}]}
        if _bstate == "done" and sess.get("batch_png"):
            _bm = sess.get("batch_meta") or {}
            _btxt = ("后台概览拼图（纯像素）：%s ｜ 保留 %d 帧 ｜ 单格 %d×%d ｜ 网格 %d×%d ｜ "
                     "画布 %d×%d ｜ %d 字节"
                     % (sess["id"], _bm.get("frames", 0),
                        (_bm.get("tile") or [0, 0])[0], (_bm.get("tile") or [0, 0])[1],
                        (_bm.get("grid") or [0, 0])[0], (_bm.get("grid") or [0, 0])[1],
                        (_bm.get("canvas") or [0, 0])[0], (_bm.get("canvas") or [0, 0])[1], _bm.get("bytes", 0)))
            _bc = [{"type": "text", "text": _btxt}]
            if want_image:
                try:
                    _bc.append({"type": "image",
                                "data": _b64b.b64encode(sess["batch_png"]).decode("ascii"),
                                "mimeType": "image/png"})
                except Exception:
                    pass
            return {"content": _bc}
        return {"content": [{"type": "text", "text":
                "后台概览任务 %s：**%s**（%s）；重新 start 再跑一次"
                % (sess["id"], _bstate, sess.get("error") or "无错误")}]}
    if life_seconds is not None:
        try:
            _nl = int(life_seconds)
        except Exception:
            _nl = None
        if _nl is not None:
            if _nl < 0:
                _nl = 0
            sess["life"] = _nl
            _retimed = "（**已改限时**为 %s）" % (("%ds" % _nl) if _nl else "不限时")
    sess["last_activity"] = time.time()
    try:
        _set_shot_note("")           # 会话回执不沿用上一枪的单帧标尺留痕
    except Exception:
        pass
    import base64 as _b64v
    # 两个容量旋钮：
    #   `hero_history`＝hero 版式历史小格数（1–6，默认取会话值/2）；
    #   `sheet_frames`＝tiers/grid 拼图帧数上限（不传则沿用 `max_frames`，默认 4）。
    _hero_hist = (hero_history if hero_history is not None else sess.get("hero_history", 2))
    try:
        _hero_hist = max(1, min(6, int(_hero_hist or 2)))
    except Exception:
        _hero_hist = 2
    _cap_src = sheet_frames if sheet_frames not in (None, "", 0) else max_frames
    with _VIS_LOCK:
        frames = [f for f in sess["frames"] if f["seq"] > sess.get("cursor", 0)]
        cap = max(1, min(8, int(_cap_src or 4)))
        ring_imgs = [fr["img"] for fr in (sess.get("frames") or [])][-_hero_hist:]
        omitted = 0
        if len(frames) > cap:
            omitted = len(frames) - cap
            frames = frames[-cap:]
        if frames:
            sess["cursor"] = frames[-1]["seq"]
        state = sess["state"]
        total = sess["total"]
        kept_n = sess["kept"]
        ring = len(sess["frames"])
    _life = sess.get("life") or 0
    _left_s = ((" ｜ 剩余 %ds" % max(0, int(round(_life - (time.time() - sess["started"])))))
               if _life else " ｜ 不限时")
    _auto_s = "（**已自动续上采集**）" if _auto_resumed else ""
    _st = ("持续会话 %s%s%s：%s ｜ 采样 %d 次 ｜ 累计留帧 %d ｜ 缓冲 %d 帧%s ｜ 上次取图后"
          % (sess["id"], _auto_s, _retimed, state, total, kept_n, ring, _left_s))
    if sess.get("state") == "expired":
        return {"content": [{"type": "text", "text":
                _st + " **会话已到期**（默认 %ds 限时；要长期请显式传 vision_seconds=0）——采集已停；"
                      "要用请 `vision_action=\"start\"` 开新窗口（或传更大/0 的 vision_seconds）。"
                % (_life or _VIS_LIFE_DEFAULT)}]}
    _layout = str(layout or "hero").strip().lower()

    if _layout in ("hero", "auto"):
        # 复用会话线程最近采到的那一帧（后台只允许一路采集，pull 不再二次抓屏）
        _lr = sess.get("latest_raw")
        fresh_img = _pil_of_frame_any(_lr) if _lr is not None else None
        prev_img = sess.get("last_pull_img")
        if fresh_img is None:
            if not frames:
                return {"content": [{"type": "text", "text":
                        _st + " **还没采到帧**（会话刚启动）；稍等 0.2~0.5s 再 pull，或先看 status。"}]}
            _layout = "tiers"      # 取不到当前帧 ⇒ 退回增量拼图
        else:
            _bbox = _diff_bbox_pil(prev_img, fresh_img)
            if prev_img is not None and not frames and _bbox is None:
                sess["last_pull_img"] = fresh_img
                return {"content": [{"type": "text", "text":
                        _st + " **无新画面**（当前帧与上次取图一致；静止时不出图、几乎不占 token）"}]}
            _hist = list(ring_imgs)
            try:
                _hero = _hero_sheet(fresh_img, prev_img, _hist, max_w=960, split=split,
                                    max_history=_hero_hist)
            except Exception as _e:
                return {"content": [{"type": "text", "text":
                        _st + " hero 版式失败：%s" % (str(_e)[:160],)}], "isError": True}
            sess["last_pull_img"] = fresh_img
            _m = _hero["meta"]
            if split:
                _ck = _m.get("changed_kind") or ""
                _chg_desc = ("有（%s，原生像素）" % (",".join(str(z) for z in _m["changed_bbox"]))
                             if _ck == "roi" else
                             ("有（变化范围大，未单独裁剪）" if _ck == "full" else "无"))
                _txt = (_st + " **最新帧＋变化区（两张独立图）** ｜ 主图 %dx%d ｜ 变化区 %s ｜ 历史小格 %d"
                        % (_m["main"][0], _m["main"][1], _chg_desc, _m["history"]))
                _content = [{"type": "text", "text": _txt}]
                if want_image:
                    for _b in (_hero["main_png"], _hero["changed_png"]):
                        if _b:
                            try:
                                _content.append({"type": "image",
                                                 "data": _b64v.b64encode(_b).decode("ascii"),
                                                 "mimeType": "image/png"})
                            except Exception:
                                pass
                return {"content": _content}
            _txt = (_st + " **最新帧大图%s%s** ｜ 主图 %dx%d ｜ 画布 %dx%d ｜ %d 字节"
                    % (("＋变化区原生裁片" if _m.get("changed_kind") == "roi"
                        else ("＋变化范围大半幅小图" if _m.get("changed_kind") == "full"
                              else "（本次无明显变化）")),
                       ("＋历史 %d 小格" % _m["history"]) if _m["history"] else "",
                       _m["main"][0], _m["main"][1],
                       _m["canvas"][0], _m["canvas"][1], _m["bytes"]))
            _content = [{"type": "text", "text": _txt}]
            if want_image:
                try:
                    _content.append({"type": "image",
                                     "data": _b64v.b64encode(_hero["png"]).decode("ascii"),
                                     "mimeType": "image/png"})
                except Exception:
                    pass
            return {"content": _content}

    if not frames:
        return {"content": [{"type": "text", "text":
                _st + " **无新画面**（画面没有可辨变化；静止时不出图、几乎不占 token）"}]}
    tiles = []
    _fmeta = []
    for k, fr in enumerate(frames):
        lab = "v%d +%.0fms" % (k + 1, fr["t"] * 1000.0)
        if fr.get("score") is not None:
            lab += " s%.0f" % float(fr["score"])
        tiles.append((fr["img"], lab))
        _fmeta.append({"i": k + 1, "t_ms": int(round(fr["t"] * 1000)),
                       "score": fr.get("score"), "via": fr.get("via")})
    try:
        if _layout == "grid":
            png, gmeta = _compose_sheet(tiles, cols=0, label_prefix="v")
        else:
            png, gmeta = _compose_sheet(tiles, label_prefix="v", tiers=_sheet_tiers(_fmeta),
                                        min_tile=320, max_w=960)
    except Exception as _e:
        return {"content": [{"type": "text", "text": _st + " 拼图失败：%s" % (str(_e)[:160],)}],
                "isError": True}
    txt = (_st + " **新增 %d 帧**" % len(frames)
           + (("（更早的 %d 帧已省略）" % omitted) if omitted else "")
           + " ｜ 版式=%s ｜ 最小格 %d×%d ｜ 画布 %d×%d ｜ %d 字节"
           % ("等分网格" if _layout == "grid" else "按档位（重要帧大格）",
              gmeta["tile"][0], gmeta["tile"][1], gmeta["canvas"][0],
              gmeta["canvas"][1], gmeta["bytes"]))
    content = [{"type": "text", "text": txt}]
    if want_image:
        try:
            content.append({"type": "image", "data": _b64v.b64encode(png).decode("ascii"),
                            "mimeType": "image/png"})
        except Exception:
            pass
    return {"content": content}


def _vis_stop(spec=""):
    spec_s = str(spec or "").strip().lower()
    with _VIS_LOCK:
        if spec_s in ("", "all", "*", "latest", "last"):
            items = list(_VIS.values())
        else:
            s = _VIS.get(spec_s)
            items = [s] if s else []
        for s in items:
            _VIS.pop(s["id"], None)
    for s in items:
        try:
            s["stop"].set()
        except Exception:
            pass
    if not items:
        return {"content": [{"type": "text", "text": "没有匹配的持续会话：%s" % (spec or "(全部)")}]}
    return {"content": [{"type": "text",
                         "text": "已停止 %d 个持续会话：%s%s"
                                 % (len(items), "、".join(s["id"] for s in items),
                                    "（采集线程会在下一步退出并释放取图器）")}]}


def _vis_status_text():
    with _VIS_LOCK:
        items = list(_VIS.values())
    with _PEEK_LOCK:
        _pks = list(_PEEK.values())
    if not items and not _pks:
        return "当前没有持续会话/后台单拍。"
    lines = (["持续会话（%d 个）：" % len(items)] if items else [])
    for s in items:
        _age = time.time() - s["started"]
        _life = s.get("life") or 0
        _lim = (("（限时 %ds，剩余 %ds）" % (_life, max(0, int(round(_life - _age)))))
                if _life else "（不限时）")
        _kind = ("（单个概览）" if s.get("batch_ms") else "（游戏会话）")
        lines.append("  %s%s：%s ｜ 采样 %d 次 ｜ 累计留帧 %d ｜ 缓冲 %d 帧 ｜ 已运行 %.1fs%s"
                      % (s["id"], _kind, s.get("state"), s.get("total", 0), s.get("kept", 0),
                         len(s.get("frames") or []), _age, _lim))
    if _pks:
        lines.append("后台单拍（%d 个，独立任务不占会话槽）：" % len(_pks))
        for _p in _pks[-3:]:
            lines.append("  %s：%s ｜ %s" % (_p["id"], _p.get("state"), _p.get("error") or "ok"))
    return "\n".join(lines)


def _vision_shutdown():
    """进程退出时的清理：停所有持续会话（线程都是 daemon，这里做的是**及时释放采集器、
    让它们不再抓屏**，避免退出卡住或残留抓屏）。"""
    try:
        with _VIS_LOCK:
            sessions = list(_VIS.values())
            _VIS.clear()
        for s in sessions:
            try:
                s["stop"].set()
            except Exception:
                pass
        with _PEEK_LOCK:
            _PEEK.clear()      # 后台单拍：丢弃未取走的图/任务（daemon 线程随进程退出）
    except Exception:
        pass


def _grab_pil_for_frames(region="fullscreen", window_title=None, rect_for_shot=None):
    """给抽帧/拼图用的**单帧取图**（返回 PIL.Image；不落盘、不登记最新帧、不识别）。

    `region`/`window_title` 语义与 `take_screenshot` 一致；`rect_for_shot` 只用于 `region="window"`
    （**屏幕物理坐标**：PrintWindow 整窗图 → 裁黑边 → 按 `_rect_abs_to_local` 换算再裁）。
    其余 region（含 dict monitor）走 mss；`rect_for_shot` 那条路由调用方先折成 monitor dict。
    """
    from PIL import Image as _Image
    if isinstance(region, str) and region == "window":
        hwnd = _shot_window_hwnd(window_title)
        if not hwnd:
            raise RuntimeError("窗口截图：解析不到窗口（未聚焦/标题不存在）")
        img, _tr, _g = _capture_window_for_view(hwnd)    # ★给模型看的抽帧图（底层视图变体）
        if rect_for_shot:
            _ir = _g.get("img_rect") or [0, 0, 0, 0]     # ★0.1.63：rect=屏幕物理坐标
            _cx, _cy, _cw, _ch, _ = _rect_abs_to_local(rect_for_shot, (_ir[0], _ir[1]), img.size)
            img = img.crop((_cx, _cy, _cx + _cw, _cy + _ch))
        return img
    mon = _shot_monitor(region, window_title)
    with mss.mss() as sct:
        m = mon if mon else sct.monitors[1]
        raw = sct.grab(m)
    return _Image.frombytes("RGB", (raw.width, raw.height), raw.bgra, "raw", "BGRX")


def _seq_grab_pil(region="window", rect=None):
    """序列用的单帧取图（与序列 `shot` 步同一套原语：`_grab_png_bytes`＋`_seq_crop_png`）。"""
    data = _grab_png_bytes(region, None)
    if data and rect:
        data = _seq_crop_png(data, rect)
    if not data:
        raise RuntimeError("取图失败：未聚焦窗口时 window 按设计不抓前台/全屏（传 fullscreen 可绕过）")
    return _pil_from_png_bytes(data)


# UIA 读取层与元素表缓存已整段删除（识别层不存在）。

def _ocr_cache_invalidate(hwnd=None):
    """失效缓存。hwnd 指定时只清该窗口，None 时全清。

    失效时机只有这一处（点击前／键盘输入后都会调它）⇒ 新图层缓存也挂同一时机，不另立一套。
    """
    _frame_stale("画面已变（动作/输入）")   # 附图复用通道：登记的那一帧也作废 ⇒ 附图会重截


def _clear_cache(hwnd=None):
    """作废上一轮的 UI 快照（**同一次工具调用内部仍可复用**）。

    关键点：这个函数以前只被 `try/except` 包着调用 ⇒ NameError 被静默吞掉 ⇒
      "跨调用读到旧状态"从头到尾一直没修上。判据是"它真的跑到了吗"，不是"写了吗"。
    """
    try:
        pass  # 识别缓存已随识别层删除；保留空壳兼容既有调用点
    except Exception as _e:
        # 失败**必须留痕**（本线铁律：静默失效是头号敌人）。不影响本次调用继续跑。
        print(f"[clear-cache] 快照作废失败（不影响本次调用）: {_e}", file=sys.stderr)


# ---------- 【最新帧登记】附图出口与内部判定**共用同一条复用规则** ----------
#   **只允许「同一时刻」复用**——内部那套（`_PX_LAST`：ts 距今 ≤
#   `_PX_REUSE_S`=50ms，动作边界 `_px_phase_flush()` 清空）本来就是对的，附图出口照抄同一条，
#   不再自创判据（以前它只看 stale/hwnd/pops，没有时间维度 ⇒ 可能端出上一轮调用的旧帧）。
#   ⇒ 附图复用条件（全部满足才复用，否则现截）：
#       ① 未 stale（动作/输入会标脏） ② 区域种类一致 ③ 同一扇窗 ④ 弹窗集合不变
#       ⑤ **同一时刻**（ts 距今 ≤ `_PX_REUSE_S`）——跨工具调用必然 >50ms ⇒ 一定现截。
# `kind` 记的是"这张图是什么区域"（window=窗口图 / screen=屏幕区域图），复用按 region 匹配。
_FRAME = {"path": None, "kind": "", "stale": True, "note": "", "data": None, "pops": (), "ts": 0.0}


def _surface_hwnd():
    """当前**顶层操作面**（弹窗优先，否则聚焦窗；都已失效 ⇒ None）。

    ⚠ 别和 `_resolve_target()` 混（**两个维度**）：那个返回的是**注入/回读的目标**（可能是终端
      **子控件**）；本函数要的是**顶层窗**——附图与 `region="window"` 截图取的都是顶层窗。
    """
    try:
        if _popup_hwnd and _is_window_alive(_popup_hwnd):
            return _popup_hwnd
    except Exception:
        pass
    return _focus_hwnd


def _frame_set(path, kind, note="", data=None, hwnd=None):
    """登记"最新的一帧"（截图成功时调用）。kind: "window" | "screen"。

    允许**只存字节、不存路径**（`path=None, data=b'…'`）——附图复用通道因此不再依赖盘上文件。
    `hwnd`：**这帧是哪扇窗的**。`None` ⇒ 按当前顶层操作面（`_surface_hwnd()`）记。
      只有"图 ≠ 操作面"的路必须显式传：`take_screenshot(region="window")` 与 `_capture_for_ocr(hwnd=…)`。
      配套判据在 `_frame_take()` 里（**复用前先看是不是同一扇窗**）。
    """
    if not path and not data:
        return
    _h = hwnd if hwnd is not None else _surface_hwnd()
    _FRAME.update(path=path, kind=kind or "", stale=False, note=note or "", data=data, hwnd=_h,
                  pops=_popup_sig(_h),        # ★记下这帧里"这扇窗自己的弹窗"集合（判新鲜用）
                  ts=time.time())             # ★同一时刻判据：与内部 `_PX_LAST` 同一条规则
    try:                                   # 这张图就是"当前几何的编号基准"（0.1.51）
        _geo_stamp(_h)
    except Exception:
        pass


def _frame_take(region):
    """取可复用的那一帧的**字节**；不可复用（没截过／已过期／区域不符／**不是同一扇窗**）返回 None。

    优先内存字节（不落盘那条路），没字节才回落到读文件（老的落盘那条路）。
    **第三问**：前两问是 `stale`（画面变了没）与 `kind`（window/screen），都不认"是哪扇窗" ⇒
      少了第三问，换窗之后照样复用、把上一扇窗的画面交出去。
      判据＝登记时记的那扇（`_frame_set(..., hwnd=…)`，默认＝登记当时的顶层操作面）**必须等于
      此刻的顶层操作面**；不等（含"登记时没记""窗口已换""焦点已清空"）⇒ **不复用、老实重截**。
      ⚠ 这一问刻意放在**复用函数本身**：任何调用点都不必各自打补丁。
    """
    try:
        if _FRAME.get("stale") or (not _FRAME.get("path") and not _FRAME.get("data")):
            return None
        want = "window" if region == "window" else "screen"
        if _FRAME.get("kind") != want:
            return None
        if _FRAME.get("hwnd") != _surface_hwnd():      # ★第三问：是不是"当前这扇窗"的
            return None
        # ★第四问：**这扇窗自己的弹窗集合**变了没（新弹出/关掉）——变了说明缓存帧里没有它们
        #   （实测病根：弹窗冒出来之前的旧帧仍被判"新鲜"，于是反复截图都看不到新窗口）⇒ 重截。
        if _FRAME.get("pops", ()) != _popup_sig(_surface_hwnd()):
            return None
        # ★第五问（**与内部生效判定 `_PX_LAST` 同一条规则，不自创**）：只允许「同一时刻」复用。
        #   内部那套＝`time.time() - ts > _PX_REUSE_S(50ms)` 就不复用；这里照抄同一条常数与判法。
        #   效果：同一调用内"刚截完就附图"可复用（省一次抓图）；跨工具调用必然 >50ms ⇒ 一定现截。
        if (time.time() - float(_FRAME.get("ts") or 0.0)) > _PX_REUSE_S:
            return None
        data = _FRAME.get("data")
        if not data and _FRAME.get("path"):
            with open(_FRAME["path"], "rb") as f:
                data = f.read()
        if not data:
            return None
        print(f"[附图] 复用本轮那一帧（{_FRAME.get('kind')}，{_FRAME.get('note') or '已截过'}）——不重复截图",
              file=sys.stderr)
        return data
    except Exception:
        return None


def _frame_stale(why=""):
    """画面已改变 ⇒ 登记的那一帧过期，附图必须重截（由 `_ocr_cache_invalidate` 触发）。"""
    _FRAME.update(stale=True, note=why or "")


# ══════════════════════════════════════════════════════════════════════════════
# 【坐标尺 / 棋盘线】自动标注——不是工具
#
# 形状＝**两条内存路径**：
#   · **看的那条**＝附图块出口（`_shot_item()`）上自动换成"带坐标尺/棋盘的那一张"——
#     不需要任何工具去要它；标记版字节在新画布上，**不进 `_FRAME`**；
#   · **给工具识别的那条**＝老路（干净内存帧）：本块只拿内存副本再画。
# ⇒ 接入面只有**两处**：本块＋ `_shot_item()` 里的一次调用；不加工具、不加分发分支。
# ⇒ 为什么必须分两条：标记会干扰识别（实测压上后误配、置信度峰值下降、峰位偏移）。
#
# 规格（实测）：
#   · 形态＝外部白底黑刻度尺（grid 额外把主/中刻度延伸进内容区成虚线棋盘）；
#   · 送达字号按宽自适应（14–26px 夹紧）；刻度长度按"数字墨迹底边+2px"反算；
#   · 线一律纯色（0/255，不用透明度）；棋盘段头相位锁屏幕绝对坐标。
#
# 复用与自造：抓帧/编码/坐标换算/点击/护栏/失效时机全部复用现成件。
# ══════════════════════════════════════════════════════════════════════════════
_MARK_TARGET_PIX = 1708 * 961      # 字号自适应沿用的旧经验档（≈1.64MP；**不再用它决定是否缩图**，见下）
# ★模型请求图上限（0.1.84）：DSH 的 DeepSeek 路由在送图前按 **token 网格**裁量——
#   14px patch、3:1 下采样、单图 1024 token：token = gH*(gW+1)+2，gW/gH = ceil(ceil(px/14)/3)。
#   超了平台会把**整张画布（含坐标尺边条）**再缩一次（实测 1747×1002 = 1034 token ⇒ 送到眼前是 1680×964），
#   预览就不再是原图、像素位置整体偏 3.8% ⇒ 自缩的判据必须是「最终画布」≤ 1024 token，而不是内容像素数。
# ★三个数做成**全局变量**（与 MOUSE_PX_PER_SEC_HUMAN / KBD_HUMAN_CPS 那批同风格：
#   不带下划线、文件顶部一眼可见，官方规则若变 ⇒ 直接改这三行）：
IMAGE_PATCH = 14          # 视觉 patch 边长（px）
IMAGE_CELL_RATIO = 3      # patch → token 格的下采样比
IMAGE_MAX_TOKENS = 1024   # 单图 token 上限


def _ds_grid_tokens(w, h):
    """一张图在 DeepSeek 请求里的视觉 token 数（含 2 个 framing token；公式与 DSH 源码一致）。"""
    if w <= 0 or h <= 0:
        return 0
    gw = int(math.ceil(int(math.ceil(w / float(IMAGE_PATCH))) / float(IMAGE_CELL_RATIO)))
    gh = int(math.ceil(int(math.ceil(h / float(IMAGE_PATCH))) / float(IMAGE_CELL_RATIO)))
    return gh * (gw + 1) + 2

# ★标注形态（0.1.70 起只剩 grid / center 两种；默认 center＝只画坐标尺）：
#   · center（默认）＝只画坐标尺（内容零遮挡）；局部裁剪图同样是"绝对坐标刻度"；
#   · grid＝**坐标尺 + 棋盘线**：外部白底黑刻度尺（数字＝屏幕物理坐标），
#     且把尺子的主刻度（200）与中刻度（100）**延伸进内容区**成虚线（段长可调、纯色反色）；
#     中线数字用小一号字（_MARK_MID_LABEL_SCALE）标出，与主线数字顶端对齐。
_MARK_STYLE_DEFAULT = "center"
_MARK_GRID_DASH_DEFAULT = 20       # 棋盘虚线：画 20px / 空 20px（相位锁屏幕绝对坐标）
_MARK_MID_LABEL_SCALE = 0.72       # 中线数字字号 = 主线数字 × 0.72
_MARK_EDGE_FONT_MIN = 14.0         # 送达到眼前的最小字号（px）——再小就读不动了
_MARK_EDGE_FONT_MAX = 26.0         # 送达到眼前的最大字号（px）
_MARK_EDGE_STEP_CANDS = (10, 20, 25, 50, 100, 200, 250, 400, 500, 1000)
# ★ Windows 侧只许无衬线（实测：无衬线 96–100%，衬线掉到 84%）。
_MARK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",          # 微软雅黑 Bold
    r"C:\Windows\Fonts\arialbd.ttf",         # Arial Bold（实测同族 96%）
    r"C:\Windows\Fonts\segoeuib.ttf",        # Segoe UI Bold
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Linux 侧自测用
]


def _mark_scale(w, h):
    """送图缩放 s（平台/API 侧那一步）：16:9 实测 1920→1708、2560→1708、3840→1708。"""
    if w <= 0 or h <= 0:
        return 1.0
    return min(1.0, math.sqrt(_MARK_TARGET_PIX / float(w * h)))


def _mark_font(px):
    from PIL import ImageFont
    for p in _MARK_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, px)
        except Exception:
            continue
    return ImageFont.load_default()


def _mark_geom(hwnd, img_size, region="window"):
    """批次几何快照（**离散量**——失效闸最可靠的那一层；也是"图内坐标↔物理屏幕坐标"的换算表）。

    窗口图必须用 `GetWindowRect`（与 `_capture_window` 同源）：最大化窗口实测
      `GetWindowRect=(-9,-9,1929,1089)`（＝图 1938×1098），而 `DWMWA_EXTENDED_FRAME_BOUNDS`
      ＝`(0,0,1920,1080)`（可见外框、被裁进屏幕）⇒ 混用会整体偏 8~9px。
    进程已显式 per-monitor DPI-aware ⇒ 下面全是**物理像素**。`img_rect` 按 region 取：
      · window → GetWindowRect（与附图同源）
      · fullscreen → 虚拟屏幕矩形（SM_X/YVIRTUALSCREEN、SM_CX/CYVIRTUALSCREEN）
      · monitor_N → `_shot_monitor` 的显示器矩形
    `kx=(r-l)/w, ky=(b-t)/h`：**图内像素 → 物理像素**的换算系数（正常窗口图 k=1；若某条截图
      路径改过尺寸/裁过，也能靠它算准）。`屏幕物理坐标 = (l + img_x*kx, t + img_y*ky)`。
    `rect` 仍存 DWM 可见外框（失效闸规则不变：窗口一动就作废）。"""
    try:
        l, t, rr, b = _get_physical_window_rect(hwnd)
    except Exception:
        l = t = rr = b = None
    w, h = int(img_size[0]), int(img_size[1])
    img_rect = None
    try:
        reg = str(region or "window")
        if reg == "window":
            ir = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(ir)) and (ir.right - ir.left) > 0:
                img_rect = [ir.left, ir.top, ir.right, ir.bottom]
        elif reg.startswith("monitor"):
            mon = _shot_monitor(reg)
            if mon:
                img_rect = [int(mon["left"]), int(mon["top"]),
                            int(mon["left"]) + int(mon["width"]),
                            int(mon["top"]) + int(mon["height"])]
        else:  # fullscreen（含 dict 之外的其它 region 名）
            vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
            vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
            if vw > 0 and vh > 0:
                img_rect = [vx, vy, vx + vw, vy + vh]
    except Exception:
        img_rect = None
    if not img_rect:
        img_rect = [l, t, rr, b]
    try:
        kx = (img_rect[2] - img_rect[0]) / float(w) if w else 1.0
        ky = (img_rect[3] - img_rect[1]) / float(h) if h else 1.0
    except Exception:
        kx = ky = 1.0
    try:
        dpi = int(user32.GetDpiForWindow(hwnd)) if hwnd else 0
    except Exception:
        dpi = 0
    return {"hwnd": hwnd, "rect": [l, t, rr, b], "img_rect": img_rect,
            "img": [w, h], "kx": round(kx, 6), "ky": round(ky, 6),
            "dpi": dpi, "region": str(region or "window")}


# ── 坐标尺（center 模式 · 0.1.62；0.1.70 前叫 edge）────────────────────────────────
#  形状：**白底黑刻度**，只加"上/左"两条边条；刻度数字＝**屏幕物理坐标**（不是图内像素）。
#  局部裁剪图同样是"绝对坐标"（以裁剪区 img_rect 为基准），不是从 0 开始。
#  字号/步长/边距全部自适应：送达字号夹在 [14, 26]px；步长取"标签不挤且 2–10 个"的候选；
#  边距由"角落两个首标签会不会相撞"反算（绝不重叠）。
def _trim_window_border(img, hwnd):
    """裁掉窗口自己的"屏幕外边框"（最大化窗口 PrintWindow 会抓成纯黑）。

    只裁有几何依据的宽度（GetWindowRect − DWMWA 可见框），且要求那条带 ≥95% 纯黑（<16）；
    任一边不满足就 0（绝不把深色内容当边框切）。返回 (l, t, r, b)。
    """
    try:
        import numpy as np
        a = np.asarray(img.convert("L")).astype(int)
        H, W = a.shape
        dl, dt, dr, db = _get_physical_window_rect(hwnd)
        g = _mark_geom(hwnd, img.size, region="window")
        il, it, ir, ib = g["img_rect"]
        want = [max(0, int(dl - il)), max(0, int(dt - it)),
                max(0, int(ir - dr)), max(0, int(ib - db))]
        out = []
        for side, w in zip(("l", "t", "r", "b"), want):
            w = min(int(w), 20)
            if w <= 0:
                out.append(0)
                continue
            if side == "l":
                band = a[:, :w]
            elif side == "t":
                band = a[:w, :]
            elif side == "r":
                band = a[:, W - w:]
            else:
                band = a[H - w:, :]
            out.append(w if float((band < 16).mean()) > 0.95 else 0)
        # ★只裁"几何上属于窗口外"的那几像素（GetWindowRect − DWMWA 可见框）：
        #   像素验证只用来**否决**（不是纯黑就不裁），**绝不允许往内容里多裁**——
        #   否则黑背景游戏/黑色界面会被当成"边框"啃掉一块。实测底部黑带 11px 里
        #   有 2px 是窗口真实内容（DWM 可见区内），这部分**保留**。
        return tuple(out)
    except Exception:
        return (0, 0, 0, 0)


def _view_prep(img, hwnd, region="window", crop=None):
    """**给模型看的单帧预处理 · 唯一坐标真源**：几何 → 裁窗口外黑边 → crop 覆盖。

    返回 `(img, trim, geom)`，其中 geom 已按裁后图**平移并重算 k**。
    ★刻度尺与标注坐标全部从这个 geom 派生——
      以后改裁剪/改 k **只改这一处**，三方必然一起变，不会各算各的。
    ★内部帧（识别/判定/复用/探针）不用它：那些必须保留整窗几何。
    """
    geom = _mark_geom(hwnd, img.size, region=region)
    tr = (0, 0, 0, 0)
    if crop:
        _l, _t, _r, _b = [int(v) for v in crop]
        geom["img_rect"] = [_l, _t, _r, _b]
        geom["rect"] = [_l, _t, _r, _b]
        geom["kx"] = (_r - _l) / float(img.size[0]) if img.size[0] else 1.0
        geom["ky"] = (_b - _t) / float(img.size[1]) if img.size[1] else 1.0
        geom["region"] = "crop"
        geom["crop"] = [_l, _t, _r, _b]
        return img, tr, geom
    if str(region) == "window":
        W, H = img.size
        _ir = list(geom.get("img_rect") or [0, 0, 0, 0])
        full_wh = (_ir[2] - _ir[0], _ir[3] - _ir[1])
        # ★PW 与 MSS 两条路的"图坐标语义"不同，必须按**图尺寸**判定来源：
        #   · 整窗图（PW）：尺寸 == GetWindowRect 尺寸 ⇒ 原点=GetWindowRect，可裁屏幕外黑边；
        #   · 回退图（PW 失败：_grab_window_png_padded 产出"可见区"）＝尺寸 == DWMWA 可见框尺寸
        #     ⇒ 它本来就是可见区、**没有黑边可裁**；原点必须是 DWM 左上、k=1（不能按整窗算）。
        if (W, H) != full_wh:
            try:
                _dl, _dt, _dr, _db = _get_physical_window_rect(hwnd)
            except Exception:
                _dl = _dt = _dr = _db = 0
            if (W, H) == (_dr - _dl, _db - _dt) and (W, H) != (0, 0):
                geom["img_rect"] = [_dl, _dt, _dr, _db]
                geom["rect"] = [_dl, _dt, _dr, _db]
                geom["kx"] = (_dr - _dl) / float(W)
                geom["ky"] = (_db - _dt) / float(H)
                geom["region"] = "window_visible"
                geom["view_visible"] = True
                print("[标记] 视图来源=可见区（PW 失败回退）：按 DWMWA 矩形定坐标（k=1）", file=sys.stderr)
                return img, tr, geom
            print("[标记] ⚠ 视图尺寸既非整窗也非可见区：按整窗比例兜底（%dx%d）" % (W, H), file=sys.stderr)
            return img, tr, geom
        tr = _trim_window_border(img, hwnd)
        if any(tr):
            W0, H0 = img.size
            img = img.crop((tr[0], tr[1], W0 - tr[2], H0 - tr[3]))
            try:
                _ir = list(geom.get("img_rect") or [0, 0, 0, 0])
                geom["img_rect"] = [_ir[0] + tr[0], _ir[1] + tr[1],
                                    _ir[2] - tr[2], _ir[3] - tr[3]]
                geom["img"] = [img.size[0], img.size[1]]
                geom["kx"] = ((float(geom["img_rect"][2]) - float(geom["img_rect"][0]))
                              / float(img.size[0] or 1))
                geom["ky"] = ((float(geom["img_rect"][3]) - float(geom["img_rect"][1]))
                              / float(img.size[1] or 1))
            except Exception:
                pass
    return img, tr, geom


def _capture_window_for_view(hwnd, region="window"):
    """**底层截图变体：给模型看的整窗帧**（= `_capture_window` + 裁掉窗口外黑边）。

    返回 `(img, trim, geom)`；`geom` 是**已经平移好、k 已按裁后图重算**的几何。
    调用方直接用它，不要自己拿 img.size 去套 `_mark_geom`（那会再按整窗算一次 k，偏 1%）。
    """
    _img = _capture_window(hwnd)
    try:                                   # ★：整窗帧同样要交代"图上叠了它自己的弹窗"
        _note_append_popup()
    except Exception:
        pass
    return _view_prep(_img, hwnd, region=region)


def _ruler_metrics(W, H, l0, t0, kx, ky):
    """字号 / 刻度步长 / 边距（全部自适应，带上下限）。"""
    s0 = _mark_scale(W, H)                      # 原图 → 送达 的缩放
    deliv_w = W * s0 if s0 < 0.99 else float(W)  # 1:1 的裁剪图就是原宽
    font_deliv = max(_MARK_EDGE_FONT_MIN, min(_MARK_EDGE_FONT_MAX, 0.015 * deliv_w))
    font_px = max(12, int(round(font_deliv / max(1e-6, s0))))
    font = _mark_font(font_px)

    def _tw(txt):
        try:
            b = font.getbbox(txt)
            return b[2] - b[0]
        except Exception:
            return 40

    def _th():
        try:
            b = font.getbbox("0123456789")
            return b[3] - b[1]
        except Exception:
            return int(font_px * 0.8)

    lw = _tw("8888")

    def _pick(span_px):
        for cand in _MARK_EDGE_STEP_CANDS:
            if cand < lw + 12:
                continue
            n = span_px / float(cand)
            if 2 <= n <= 10:
                return cand
        return max(100, int(span_px / 6.0))

    step_x = _pick(W * kx)
    step_y = _pick(H * ky)
    fx = int(math.ceil(l0 / float(step_x)) * step_x)   # 角落首标签（用它算边距，防重叠）
    hw = _tw(str(fx)) / 2.0
    rw = _th() + 2
    # ★刻度长度/宽度也随字号自适应（全窗缩图后不会细到看不见；局部小字号也不会显得又长又粗）
    tick_minor = max(3, int(round(font_px * 0.30)))
    tick_mid = max(4, int(round(font_px * 0.45)))
    tick_major = max(5, int(round(font_px * 0.60)))
    tick_w_major = max(2, int(round(font_px * 0.10)))
    tick_w_mid = max(1, int(round(font_px * 0.06)))
    tick_w_minor = 1
    pad_l = max(26, int(round(6 + rw + 3 + hw)))
    # 顶边距要同时容下：数字（画在 y=label_y）＋ 主刻度长度（否则数字和刻度会叠）
    pad_t = max(22, int(round(3 + _th() + 2 + max(rw / 2.0, tick_major))))
    # ★0.1.68：数字上移到 y=0（原来 y=3 会被主刻度插进底部 8px）；
    #   刻度长度改成"数字墨迹底边 + 2px 间隙"反算（放不下就**缩刻度、不加高边距**）。
    label_y = 0
    _bb_main = font.getbbox("0123456789")
    _ink_bottom = _bb_main[3]
    tick_major = max(5, min(tick_major, (pad_t - 1) - (label_y + _ink_bottom) - 2))
    tick_mid = max(4, min(tick_mid, int(round(tick_major * 0.7))))
    tick_minor = max(3, min(tick_minor, int(round(tick_major * 0.45))))
    # 中线数字：字号 = 主线 × _MARK_MID_LABEL_SCALE，且**墨迹顶端与主线数字齐平**
    mid_font_px = max(10, int(round(font_px * _MARK_MID_LABEL_SCALE)))
    mid_font = _mark_font(mid_font_px)
    try:
        _bb_mid = mid_font.getbbox("0123456789")
        mid_label_y = label_y + (_bb_main[1] - _bb_mid[1])
    except Exception:
        mid_label_y = label_y
    return {"font": font, "font_px": font_px, "font_deliv": font_deliv,
            "mid_font": mid_font, "mid_font_px": mid_font_px,
            "label_y": label_y, "mid_label_y": mid_label_y,
            "step_x": step_x, "step_y": step_y,
            "minor_x": max(1, step_x // 10), "minor_y": max(1, step_y // 10),
            "pad_l": pad_l, "pad_t": pad_t, "label_w": lw,
            "tick_minor": tick_minor, "tick_mid": tick_mid, "tick_major": tick_major,
            "tick_w_major": tick_w_major, "tick_w_mid": tick_w_mid, "tick_w_minor": tick_w_minor}


def _axis_pick_step(span, px_per_v, label_w, vertical=False):
    """在**轴坐标**里挑刻度步长：2–10 个标号、相邻标号在图上放得下。"""
    try:
        span = float(span)
        px_per_v = float(px_per_v) or 1.0
        label_w = float(label_w)
    except Exception:
        return 200, 20
    if span <= 0:
        return 10, 1
    need = label_w + (8.0 if vertical else 10.0)
    best = None
    for cand in _MARK_EDGE_STEP_CANDS:
        if cand * px_per_v < need:
            continue
        n = span / float(cand)
        if 2.0 <= n <= 10.0:
            best = cand
            break
    if best is None:
        for cand in reversed(_MARK_EDGE_STEP_CANDS):
            if cand * px_per_v >= need and span / float(cand) >= 2.0:
                best = cand
                break
    if best is None:
        best = max(10, int(round(span / 6.0)))
    minor = 1
    for d in (10, 5, 4, 2):
        if best % d == 0:
            minor = max(1, best // d)
            break
    return best, minor


def _axis_ruler_ctx(W, H, pad_l, pad_t, kx, ky, geom, inside=False):
    """刻度画在**图内坐标轴**上：
      · 整图：轴＝画布像素（0 在整张附图左上角）⇒ 位置＝轴坐标；
      · 局部图：沿用同一根轴（刻度显示轴上的绝对位置，不从 0 重来）。
    返回 {pos_x,pos_y,vx0,vx1,vy0,vy1,px_per_vx,px_per_vy}；
    没有轴信息 ⇒ None（退回老的画法，仅用于历史语境/无轴的局部图）。"""
    try:
        is_crop = (str(geom.get("region")) == "crop")
        axis = geom.get("axis") if is_crop else _AXIS
        if is_crop and not axis:
            return None
        span_l = 0 if inside else pad_l
        span_t = 0 if inside else pad_t
        if not is_crop:
            def pos_x(v):
                return float(v)
            def pos_y(v):
                return float(v)
            vx0, vx1 = 0.0, float(W + span_l)
            vy0, vy1 = 0.0, float(H + span_t)
            return {"pos_x": pos_x, "pos_y": pos_y,
                    "vx0": vx0, "vx1": vx1, "vy0": vy0, "vy1": vy1,
                    "px_per_vx": 1.0, "px_per_vy": 1.0}
        ar = [float(v) for v in (axis.get("rect") or [0, 0, 0, 0])]
        ak = [float(v) or 1.0 for v in (axis.get("k") or [1.0, 1.0])]
        ap = [float(v) for v in (axis.get("pad") or [0, 0])]
        re = [float(v) for v in (geom.get("img_rect") or [0, 0, 0, 0])]
        kxc = float(geom.get("kx") or 1.0) or 1.0
        kyc = float(geom.get("ky") or 1.0) or 1.0

        def pos_x(v):
            _scr = ar[0] + (float(v) - ap[0]) * ak[0]
            return pad_l + (_scr - re[0]) / kxc

        def pos_y(v):
            _scr = ar[1] + (float(v) - ap[1]) * ak[1]
            return pad_t + (_scr - re[1]) / kyc

        def val_x(p):
            _scr = re[0] + (float(p) - pad_l) * kxc
            return ap[0] + (_scr - ar[0]) / ak[0]

        def val_y(p):
            _scr = re[1] + (float(p) - pad_t) * kyc
            return ap[1] + (_scr - ar[1]) / ak[1]

        vx0, vx1 = val_x(0.0), val_x(float(W + span_l))
        vy0, vy1 = val_y(0.0), val_y(float(H + span_t))
        return {"pos_x": pos_x, "pos_y": pos_y,
                "vx0": vx0, "vx1": vx1, "vy0": vy0, "vy1": vy1,
                "px_per_vx": ak[0] / kxc, "px_per_vy": ak[1] / kyc}
    except Exception:
        return None


def _ruler_annotate(img, geom, grid=None, inside=False, hwnd=None):
    """在**副本**上画坐标尺，返回 (新图, meta)。

    `grid=None` ⇒ 只画尺子（center 模式：内容区零遮挡）；
    `grid={"dash":20, "color":"auto"|"black"|"white"}` ⇒ 再把**主刻度（200）与中刻度（100）**
      延伸进内容区成虚线（段长相位锁屏幕绝对坐标；中线数字用小一号字标出；
      **与内容边缘重合的那条（0）不画**，否则缩放后会盖掉尺子的分隔线）。
    `inside=True`（0.1.86）⇒ 刻度尺画到**图内**（画布＝内容，不加外部白底边条；
      刻度线贴边向内指、数字在刻度线内侧，颜色按底色**自动反色**：浅底黑/深底白）；
      棋盘线同样画在图内（继承这个设置）。
    """
    if inside:
        return _ruler_annotate_inside(img, geom, grid, hwnd=hwnd)
    from PIL import Image, ImageDraw
    W, H = img.size
    l0, t0 = int(geom["img_rect"][0]), int(geom["img_rect"][1])
    kx, ky = float(geom.get("kx") or 1.0), float(geom.get("ky") or 1.0)
    m = _ruler_metrics(W, H, l0, t0, kx, ky)
    pad_l, pad_t = m["pad_l"], m["pad_t"]
    PW, PH = W + pad_l, H + pad_t
    # ★0.1.88：刻度画在**图内坐标轴**上（整图从 0 起；局部图沿用同一根轴）
    _ctx = _axis_ruler_ctx(W, H, pad_l, pad_t, kx, ky, geom, inside=False)
    if _ctx:
        m["step_x"], m["minor_x"] = _axis_pick_step(
            _ctx["vx1"] - _ctx["vx0"], _ctx["px_per_vx"], m["label_w"])
        m["step_y"], m["minor_y"] = _axis_pick_step(
            _ctx["vy1"] - _ctx["vy0"], _ctx["px_per_vy"], m["label_w"], vertical=True)
    font = m["font"]
    mid_font = m.get("mid_font") or font
    label_y = int(m.get("label_y", 3))
    mid_label_y = int(m.get("mid_label_y", 3))
    want_grid = grid is not None

    def _x_of(p):
        return _ctx["pos_x"](p) if _ctx else (pad_l + (p - l0) / kx)

    def _y_of(p):
        return _ctx["pos_y"](p) if _ctx else (pad_t + (p - t0) / ky)

    canvas = Image.new("RGB", (PW, PH), (255, 255, 255))
    canvas.paste(img.convert("RGB"), (pad_l, pad_t))
    d = ImageDraw.Draw(canvas)
    INK, MINOR_INK, SEP = (16, 16, 16), (110, 110, 110), (196, 196, 196)
    d.line([(pad_l - 1, 0), (pad_l - 1, PH - 1)], fill=SEP, width=1)
    d.line([(0, pad_t - 1), (PW - 1, pad_t - 1)], fill=SEP, width=1)
    gx, gy = [], []       # 要延伸进内容区的刻度位置（屏幕坐标；跳过与边缘重合的 l0/t0）

    # ★0.1.88：轴坐标 → 位置（整图＝画布像素；局部图＝同一根轴的位置映射）
    _vx0, _vx1 = ((_ctx["vx0"], _ctx["vx1"]) if _ctx else (float(l0), float(l0 + W * kx)))
    _mx = max(1, int(m["minor_x"]))
    p = int(math.ceil(_vx0 / float(_mx)) * _mx)
    while p <= _vx1 + 1e-6:                       # X 轴
        x = _x_of(p)
        _lab = str(int(round(p)))
        if p % m["step_x"] == 0:
            d.line([(x, pad_t - 1), (x, pad_t - m["tick_major"])], fill=INK,
                   width=m["tick_w_major"])
            tb = d.textbbox((0, 0), _lab, font=font)
            _tw = tb[2] - tb[0]
            d.text((min(max(0.0, x - _tw / 2.0 - tb[0]), max(0.0, PW - _tw)), label_y),
                   _lab, fill=INK, font=font)
            if (_ctx is not None) or p != l0:
                gx.append(int(round(x)))
        elif m["step_x"] >= 4 and p % (m["step_x"] // 2) == 0:
            d.line([(x, pad_t - 1), (x, pad_t - m["tick_mid"])], fill=INK,
                   width=m["tick_w_mid"])
            if want_grid and ((_ctx is not None) or p != l0):
                tb = d.textbbox((0, 0), _lab, font=mid_font)
                _tw = tb[2] - tb[0]
                d.text((min(max(0.0, x - _tw / 2.0 - tb[0]), max(0.0, PW - _tw)), mid_label_y),
                       _lab, fill=INK, font=mid_font)
                gx.append(int(round(x)))
        else:
            d.line([(x, pad_t - 1), (x, pad_t - m["tick_minor"])], fill=MINOR_INK,
                   width=m["tick_w_minor"])
        p += _mx

    _vy0, _vy1 = ((_ctx["vy0"], _ctx["vy1"]) if _ctx else (float(t0), float(t0 + H * ky)))
    _my = max(1, int(m["minor_y"]))
    p = int(math.ceil(_vy0 / float(_my)) * _my)
    while p <= _vy1 + 1e-6:                       # Y 轴（数字旋转 90°，边条窄才放得下）
        y = _y_of(p)
        _lab = str(int(round(p)))
        if p % m["step_y"] == 0:
            d.line([(pad_l - 1, y), (pad_l - m["tick_major"], y)], fill=INK,
                   width=m["tick_w_major"])
            tb = font.getbbox(_lab)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            timg = Image.new("RGBA", (tw + 4, th + 6), (0, 0, 0, 0))
            ImageDraw.Draw(timg).text((2 - tb[0], 3 - tb[1]), _lab, font=font,
                                      fill=INK + (255,))
            timg = timg.rotate(90, expand=True)
            canvas.paste(timg, (5, min(max(0, int(y - timg.size[1] / 2)), max(0, PH - timg.size[1]))), timg)
            if (_ctx is not None) or p != t0:
                gy.append(int(round(y)))
        elif m["step_y"] >= 4 and p % (m["step_y"] // 2) == 0:
            d.line([(pad_l - 1, y), (pad_l - m["tick_mid"], y)], fill=INK,
                   width=m["tick_w_mid"])
            if want_grid and ((_ctx is not None) or p != t0):
                tb = mid_font.getbbox(_lab)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
                timg = Image.new("RGBA", (tw + 4, th + 6), (0, 0, 0, 0))
                ImageDraw.Draw(timg).text((2 - tb[0], 3 - tb[1]), _lab, font=mid_font,
                                          fill=INK + (255,))
                timg = timg.rotate(90, expand=True)
                canvas.paste(timg, (5, min(max(0, int(y - timg.size[1] / 2)), max(0, PH - timg.size[1]))), timg)
                gy.append(int(round(y)))
        else:
            d.line([(pad_l - 1, y), (pad_l - m["tick_minor"], y)], fill=MINOR_INK,
                   width=m["tick_w_minor"])
        p += _my

    if want_grid:
        canvas = _ruler_draw_grid(canvas, W, H, pad_l, pad_t, gx, gy, grid)
    try:
        _axis_record(geom, canvas.size, (W, H), geom.get("region"), False, hwnd)
    except Exception:
        pass
    return canvas, m


def _ruler_draw_grid(canvas, W, H, pad_l, pad_t, gx, gy, grid):
    """把刻度位置延伸进内容区成虚线（`gx/gy` 传的是**画布像素位置**）。

    "画一段、空一段"，段相位锁**画布 0 点（＝整图的图内坐标 0）**；
    对整图，画布像素＝图内坐标，所以**每个段头就是一个精确的图内坐标**。
    局部图沿用同一根轴（精确值看刻度数字，虚线只作视觉引导）。
    """
    import numpy as np
    from PIL import Image
    try:
        dash = int(grid.get("dash", _MARK_GRID_DASH_DEFAULT))
    except Exception:
        dash = _MARK_GRID_DASH_DEFAULT
    dash = max(4, min(100, dash))
    cm = str(grid.get("color") or "auto").strip().lower()
    a = np.asarray(canvas.convert("RGB")).copy()
    lum = (0.299 * a[..., 0].astype(np.float32) + 0.587 * a[..., 1].astype(np.float32)
           + 0.114 * a[..., 2].astype(np.float32))
    y0, y1 = pad_t, pad_t + H
    x0, x1 = pad_l, pad_l + W
    mask = np.zeros(lum.shape, dtype=bool)
    segs = []
    yy = y0 - (y0 % dash)
    while yy < y1:
        segs.append((max(y0, yy), min(y1, yy + dash)))
        yy += 2 * dash
    for x in gx:
        try:
            x = int(x)
        except Exception:
            continue
        if x0 <= x < x1:
            for ya, yb in segs:
                if yb > ya:
                    mask[ya:yb, x] = True
    segsx = []
    xx = x0 - (x0 % dash)
    while xx < x1:
        segsx.append((max(x0, xx), min(x1, xx + dash)))
        xx += 2 * dash
    for y in gy:
        try:
            y = int(y)
        except Exception:
            continue
        if y0 <= y < y1:
            for xa, xb in segsx:
                if xb > xa:
                    mask[y, xa:xb] = True
    if cm == "black":
        tgt = np.zeros(lum.shape, dtype=np.uint8)
    elif cm == "white":
        tgt = np.full(lum.shape, 255, dtype=np.uint8)
    else:
        tgt = np.where(lum < 128, 255, 0).astype(np.uint8)
    a[mask] = tgt[mask][:, None]
    return Image.fromarray(a)


def _ruler_annotate_inside(img, geom, grid=None, hwnd=None):
    """**图内刻度**（0.1.86）：画布＝内容（不加外部边条），刻度/棋盘画在图内边缘。

    刻度线与数字先画进一张 L 蒙版（带抗锯齿），再按**底色亮暗逐像素反色**合成：
      底色 <128 ⇒ 白；≥128 ⇒ 黑（与 `grid_color=auto` 同一条判据）——
      于是浅底黑字、深底白字，暗画面/深色主题也读得清。
    """
    from PIL import Image, ImageDraw
    import numpy as np
    W, H = img.size
    l0, t0 = int(geom["img_rect"][0]), int(geom["img_rect"][1])
    kx, ky = float(geom.get("kx") or 1.0), float(geom.get("ky") or 1.0)
    m = _ruler_metrics(W, H, l0, t0, kx, ky)
    # ★0.1.88：刻度画在图内坐标轴上（整图从 0 起；局部图沿用同一根轴）
    _ctx = _axis_ruler_ctx(W, H, 0, 0, kx, ky, geom, inside=True)
    if _ctx:
        m["step_x"], m["minor_x"] = _axis_pick_step(
            _ctx["vx1"] - _ctx["vx0"], _ctx["px_per_vx"], m["label_w"])
        m["step_y"], m["minor_y"] = _axis_pick_step(
            _ctx["vy1"] - _ctx["vy0"], _ctx["px_per_vy"], m["label_w"], vertical=True)
    font = m["font"]
    mid_font = m.get("mid_font") or font
    tick_major, tick_mid, tick_minor = m["tick_major"], m["tick_mid"], m["tick_minor"]
    want_grid = grid is not None
    # 图内布局：刻度线贴边缘向内指；数字放在刻度线内侧，避开刻度线
    label_top = tick_major + 2          # 顶部数字（含中线数字）的 y：同一行、靠 x 错开
    mid_label_top = label_top
    label_left = tick_major + 3         # 左侧数字（旋转 90°，含中线数字）的 x
    mid_label_left = label_left
    ink = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(ink)
    gx, gy = [], []

    def _x_of(p):
        return _ctx["pos_x"](p) if _ctx else ((p - l0) / kx)

    def _y_of(p):
        return _ctx["pos_y"](p) if _ctx else ((p - t0) / ky)

    def _put_text_c(x, y, txt, fnt):
        tb = d.textbbox((0, 0), txt, font=fnt)
        d.text((x - (tb[2] - tb[0]) / 2 - tb[0], y), txt, fill=255, font=fnt)

    def _put_text_rot(x, y, txt, fnt):
        tb = fnt.getbbox(txt)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        timg = Image.new("L", (tw + 4, th + 6), 0)
        ImageDraw.Draw(timg).text((2 - tb[0], 3 - tb[1]), txt, fill=255, font=fnt)
        timg = timg.rotate(90, expand=True)
        ink.paste(timg, (x, int(y - timg.size[1] / 2)), timg)

    _vx0, _vx1 = ((_ctx["vx0"], _ctx["vx1"]) if _ctx else (float(l0), float(l0 + W * kx)))
    _mx = max(1, int(m["minor_x"]))
    p = int(math.ceil(_vx0 / float(_mx)) * _mx)
    while p <= _vx1 + 1e-6:                       # X 轴（顶部）
        x = int(round(_x_of(p)))
        _lab = str(int(round(p)))
        if 0 <= x < W:
            if p % m["step_x"] == 0:
                d.line([(x, 0), (x, tick_major)], fill=255, width=m["tick_w_major"])
                _put_text_c(x, label_top, _lab, font)
                if (_ctx is not None) or p != l0:
                    gx.append(x)
            elif m["step_x"] >= 4 and p % (m["step_x"] // 2) == 0:
                d.line([(x, 0), (x, tick_mid)], fill=255, width=m["tick_w_mid"])
                if want_grid and ((_ctx is not None) or p != l0):
                    _put_text_c(x, mid_label_top, _lab, mid_font)
                    gx.append(x)
            else:
                d.line([(x, 0), (x, tick_minor)], fill=255, width=m["tick_w_minor"])
        p += _mx

    _vy0, _vy1 = ((_ctx["vy0"], _ctx["vy1"]) if _ctx else (float(t0), float(t0 + H * ky)))
    _my = max(1, int(m["minor_y"]))
    p = int(math.ceil(_vy0 / float(_my)) * _my)
    while p <= _vy1 + 1e-6:                       # Y 轴（左侧，数字旋转 90°）
        y = int(round(_y_of(p)))
        _lab = str(int(round(p)))
        if 0 <= y < H:
            if p % m["step_y"] == 0:
                d.line([(0, y), (tick_major, y)], fill=255, width=m["tick_w_major"])
                _put_text_rot(label_left, y, _lab, font)
                if (_ctx is not None) or p != t0:
                    gy.append(y)
            elif m["step_y"] >= 4 and p % (m["step_y"] // 2) == 0:
                d.line([(0, y), (tick_mid, y)], fill=255, width=m["tick_w_mid"])
                if want_grid and ((_ctx is not None) or p != t0):
                    _put_text_rot(mid_label_left, y, _lab, mid_font)
                    gy.append(y)
            else:
                d.line([(0, y), (tick_minor, y)], fill=255, width=m["tick_w_minor"])
        p += _my

    base = img.convert("RGB")
    if want_grid:
        base = _ruler_draw_grid(base, W, H, 0, 0, gx, gy, grid)
    arr = np.asarray(base).astype(np.float32)
    lum = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2])
    alpha = (np.asarray(ink).astype(np.float32) / 255.0)[..., None]
    tgt = np.where(lum < 128.0, 255.0, 0.0)[..., None]
    out = arr * (1.0 - alpha) + tgt * alpha
    return Image.fromarray(out.clip(0, 255).astype(np.uint8)), m


# ══════════════════════════════════════════════════════════════════════════════
# 【模型面坐标轴 · 0.1.88】图内相对坐标轴（左上角 0,0）＝模型唯一使用的坐标
#   · 整图（窗口/全屏）附图：轴＝整张附图（含刻度边条）的像素坐标，0 在图左上角；
#     内容区落在轴上的 [pad_l, pad_l+W] × [pad_t, pad_t+H]。
#   · 局部图（rect 裁剪）：**同一根轴继续**——刻度显示轴上的绝对位置
#     （例如从 (360,480) 裁出来的局部图，刻度就从 360/480 继续），
#     既不从 0 重来、也不是屏幕坐标。
#   · 模型只报轴坐标；本模块把它换算成屏幕物理坐标（**只在内部执行用**）。
#     **屏幕坐标绝不写进回执**。
#   · 没有轴基准（刚启动/重启）：指点类工具**回退老行为（屏幕坐标）**并写一句提示；
#     整图截一张后即启用图内坐标。
# ══════════════════════════════════════════════════════════════════════════════
_AXIS = None        # 最近一次「整图」附图的轴几何
_AXIS_WHY = ""      # 轴缺失/失效的原因
_AXIS_NOTE = ""     # 本轮坐标换算说明（_tool_call_result 追加到回执）
_AXIS0 = {}         # 本轮调用传入的原始轴坐标（回执回显用）
_AXIS_TOOLS = {"mouse_click", "mouse_move", "mouse_drag", "mouse_transfer",
               "hover", "scroll", "capture_screenshot", "sequence"}


def _axis_reset(why=""):
    global _AXIS, _AXIS_WHY
    _AXIS = None
    _AXIS_WHY = str(why or "")


def _axis_record(geom, canvas_wh, img_wh, region, inside, hwnd):
    """整图附图落盘后记轴；局部图（crop）**不换轴**（沿用现有轴）。"""
    global _AXIS, _AXIS_WHY
    try:
        if str(region) == "crop":
            return
        cw, ch = int(canvas_wh[0]), int(canvas_wh[1])
        iw, ih = int(img_wh[0]), int(img_wh[1])
        pad = (0, 0) if inside else (cw - iw, ch - ih)
        ir = [int(round(float(v))) for v in (geom.get("img_rect") or [0, 0, 0, 0])]
        kx = float(geom.get("kx") or 1.0)
        ky = float(geom.get("ky") or 1.0)
        win_rect = None
        if hwnd:
            try:
                _wr = get_window_rect(hwnd)     # (l, t, w, h) 屏幕物理
                win_rect = [int(_wr[0]), int(_wr[1]), int(_wr[2]), int(_wr[3])]
            except Exception:
                win_rect = None
        _AXIS = {"canvas": [cw, ch], "content": [iw, ih],
                 "pad": [int(pad[0]), int(pad[1])], "k": [kx, ky],
                 "rect": ir, "hwnd": hwnd, "win_rect": win_rect,
                 "region": str(region), "ts": time.time()}
        _AXIS_WHY = ""
    except Exception as _ae:
        _axis_reset("记录坐标轴失败：%s" % (str(_ae)[:80],))


def _axis_validate():
    """校验轴是否仍可用；返回 (axis, err)。"""
    a = _AXIS
    if not a:
        return None, ("还没有**图内坐标轴基准**：请先整图截图（capture_screenshot）。")
    try:
        hwnd = a.get("hwnd")
        if hwnd:
            if not _is_window_alive(hwnd):
                return None, "坐标轴的窗口已关闭/失效——请重新截图建立新轴。"
            _wr = get_window_rect(hwnd)
            old = a.get("win_rect")
            if old and (abs(int(_wr[0]) - old[0]) > 2 or abs(int(_wr[1]) - old[1]) > 2
                        or abs(int(_wr[2]) - old[2]) > 2 or abs(int(_wr[3]) - old[3]) > 2):
                return None, ("坐标轴已失效（截图后窗口被移动/改尺寸）——"
                              "请重新截图，再按新的图内坐标操作。")
        else:
            try:
                _sw = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
                _sh = user32.GetSystemMetrics(79)
                _ir = a["rect"]
                if _sw and _sh and (abs(_sw - (_ir[2] - _ir[0])) > 2
                                    or abs(_sh - (_ir[3] - _ir[1])) > 2):
                    return None, "坐标轴已失效（屏幕分辨率变化）——请重新截图。"
            except Exception:
                pass
        return a, None
    except Exception as _ve:
        return None, "坐标轴校验失败：%s" % (str(_ve)[:80],)


def _axis_pt(x, y):
    """图内坐标 → 屏幕物理坐标（内部执行用）。返回 (sx, sy)。"""
    a, err = _axis_validate()
    if err:
        raise ValueError(err)
    pl, pt = a["pad"]
    iw, ih = a["content"]
    try:
        fx, fy = float(x), float(y)
    except Exception:
        raise ValueError("坐标必须是数字：x=%r y=%r" % (x, y))
    if not (pl - 0.5 <= fx <= pl + iw + 0.5 and pt - 0.5 <= fy <= pt + ih + 0.5):
        raise ValueError(
            "图内坐标 (%.0f,%.0f) 不在附图内容区——内容区在图内 x∈[%d,%d]、y∈[%d,%d]"
            "（图内坐标 0 在整张附图左上角；刻度边条区不可点）"
            % (fx, fy, pl, pl + iw, pt, pt + ih))
    sx = a["rect"][0] + (fx - pl) * a["k"][0]
    sy = a["rect"][1] + (fy - pt) * a["k"][1]
    return int(round(sx)), int(round(sy))


def _axis_rect4(rect):
    """图内矩形 'x,y,w,h' → 屏幕物理矩形（内部裁剪用）。"""
    try:
        vals = [float(v) for v in str(rect).replace(" ", "").split(",")]
        if len(vals) != 4:
            raise ValueError
        x, y, w, h = vals
    except Exception:
        raise ValueError("rect 必须是 'x,y,w,h' 四个数字（图内坐标）")
    a, err = _axis_validate()
    if err:
        raise ValueError(err)
    pl, pt = a["pad"]
    iw, ih = a["content"]
    if x < pl - 0.5 or y < pt - 0.5 or x + w > pl + iw + 0.5 or y + h > pt + ih + 0.5:
        raise ValueError(
            "裁剪矩形 (%.0f,%.0f,%.0f,%.0f) 超出了附图内容区"
            "（图内 x∈[%d,%d]、y∈[%d,%d]）" % (x, y, w, h, pl, pl + iw, pt, pt + ih))
    sx = a["rect"][0] + (x - pl) * a["k"][0]
    sy = a["rect"][1] + (y - pt) * a["k"][1]
    sw = w * a["k"][0]
    sh = h * a["k"][1]
    return int(round(sx)), int(round(sy)), max(1, int(round(sw))), max(1, int(round(sh)))


def _axis_from_screen(sx, sy):
    """屏幕物理点 → 图内坐标（回执回显用）；无轴/越界返回 None。"""
    a = _AXIS
    if not a:
        return None
    try:
        x = a["pad"][0] + (float(sx) - a["rect"][0]) / (a["k"][0] or 1.0)
        y = a["pad"][1] + (float(sy) - a["rect"][1]) / (a["k"][1] or 1.0)
        if -0.5 <= x <= a["canvas"][0] + 0.5 and -0.5 <= y <= a["canvas"][1] + 0.5:
            return x, y
    except Exception:
        pass
    return None


def _axis_from_screen_raw(sx, sy):
    """屏幕物理点 → 图内坐标（不做范围检查；越界也返回，供窗口矩形等夹取用）。"""
    a = _AXIS
    if not a:
        return None
    try:
        return (a["pad"][0] + (float(sx) - a["rect"][0]) / (a["k"][0] or 1.0),
                a["pad"][1] + (float(sy) - a["rect"][1]) / (a["k"][1] or 1.0))
    except Exception:
        return None


def _axis_fmt(sx, sy, prefix=""):
    """屏幕点写成图内坐标文本；越界/无轴返回 None。"""
    p = _axis_from_screen(sx, sy)
    if p is None:
        return None
    return "%s图内 (%d,%d)" % (prefix, int(round(p[0])), int(round(p[1])))


def _axis_range_of_crop(axis, crop_rect, crop_k):
    """局部图内容区在轴上的区间（用于回执说明）。"""
    try:
        ax0 = axis["pad"][0] + (crop_rect[0] - axis["rect"][0]) / (axis["k"][0] or 1.0)
        ax1 = axis["pad"][0] + (crop_rect[2] - axis["rect"][0]) / (axis["k"][0] or 1.0)
        ay0 = axis["pad"][1] + (crop_rect[1] - axis["rect"][1]) / (axis["k"][1] or 1.0)
        ay1 = axis["pad"][1] + (crop_rect[3] - axis["rect"][1]) / (axis["k"][1] or 1.0)
        return ax0, ax1, ay0, ay1
    except Exception:
        return None


def _axis_note_text(kind=""):
    """回执里的坐标轴说明（只说图内坐标，不提屏幕坐标）。"""
    a = _AXIS
    if not a:
        return ""
    cw, ch = a["canvas"]
    pl, pt = a["pad"]
    iw, ih = a["content"]
    return ("坐标轴：**图内相对坐标**（0,0＝整张附图左上角，附图 %d×%d；"
            "内容区 x∈[%d,%d]、y∈[%d,%d]）——本次%s已按图内坐标解释，"
            "工具内部自动换算，不需要你心算。" % (cw, ch, pl, pl + iw, pt, pt + ih, kind or "坐标"))


def _seq_pair(v, what="坐标"):
    """序列步里的坐标：接受 [x,y] / (x,y) / "x,y"（全角逗号也认）；认不出抛 ValueError。"""
    if isinstance(v, dict):
        if v.get("x") is not None and v.get("y") is not None:
            return int(float(v["x"])), int(float(v["y"]))
        raise ValueError("%s 认不出：%r（要 [x,y] 或 \"x,y\"）" % (what, v))
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return int(float(v[0])), int(float(v[1]))
    try:
        _s = str(v).strip().replace("，", ",")
        _parts = [p for p in _s.split(",") if p.strip() != ""]
        if len(_parts) == 2:
            return int(float(_parts[0])), int(float(_parts[1]))
    except Exception:
        pass
    raise ValueError("%s 认不出：%r（要 [x,y] 或 \"x,y\"）" % (what, v))


def _seq_axis_need(steps, rel_default=True):
    """序列 steps 里有没有**绝对坐标**（需要图内→屏幕换算）；相对增量不算（那是屏幕像素增量）。"""
    if not isinstance(steps, list):
        return False
    for st in steps:
        if not isinstance(st, dict):
            continue
        if isinstance(st.get("steps"), list) and _seq_axis_need(st["steps"], bool(st.get("relative", rel_default))):
            return True
        if "move" in st and not bool(st.get("relative", rel_default)):
            return True
        if "drag" in st:
            if st.get("from") is not None or not bool(st.get("relative", rel_default)):
                return True
        if "click" in st and st.get("at") is not None:
            return True
        if "hover" in st or "transfer" in st:
            return True
        if "shot" in st and st.get("rect") is not None:
            return True
    return False


def _seq_axis_convert_steps(steps, rel_default=True):
    """把序列步里的**绝对坐标**从图内坐标换成屏幕坐标（原地改），返回转换个数。

    只换「看图指点」那一类：move 绝对步、drag 的目标与 from、click 的 at、hover 的点、
    transfer 的 from/to/safe_park、shot 的 rect。`set_window` 的位置/尺寸、`relative` 增量
    仍是屏幕值（工具面已写明，不是看图指点）。
    """
    if not isinstance(steps, list):
        return 0
    n = 0
    for st in steps:
        if not isinstance(st, dict):
            continue
        if isinstance(st.get("steps"), list):
            n += _seq_axis_convert_steps(st["steps"], bool(st.get("relative", rel_default)))
        if "move" in st and not bool(st.get("relative", rel_default)):
            _x, _y = _seq_pair(st["move"], "move")
            _sx, _sy = _axis_pt(_x, _y)
            st["move"] = [_sx, _sy]
            n += 1
        if "drag" in st:
            if not bool(st.get("relative", rel_default)):
                _x, _y = _seq_pair(st["drag"], "drag")
                _sx, _sy = _axis_pt(_x, _y)
                st["drag"] = [_sx, _sy]
                n += 1
            if st.get("from") is not None:
                _x, _y = _seq_pair(st["from"], "drag.from")
                _sx, _sy = _axis_pt(_x, _y)
                st["from"] = [_sx, _sy]
                n += 1
        if "click" in st and st.get("at") is not None:
            _x, _y = _seq_pair(st["at"], "click.at")
            _sx, _sy = _axis_pt(_x, _y)
            st["at"] = [_sx, _sy]
            n += 1
        if "hover" in st:
            _x, _y = _seq_pair(st["hover"], "hover")
            _sx, _sy = _axis_pt(_x, _y)
            st["hover"] = [_sx, _sy]
            n += 1
        if "transfer" in st and isinstance(st["transfer"], dict):
            for _k in ("from", "to", "safe_park"):
                if st["transfer"].get(_k) is not None:
                    _x, _y = _seq_pair(st["transfer"][_k], "transfer." + _k)
                    _sx, _sy = _axis_pt(_x, _y)
                    st["transfer"][_k] = [_sx, _sy]
                    n += 1
        if "shot" in st and st.get("rect") is not None:
            try:
                st["rect"] = "%d,%d,%d,%d" % _axis_rect4(st["rect"])
                n += 1
            except ValueError:
                raise
    return n


def _axis_apply(name, arguments):
    """把坐标参数按**图内坐标轴**解释并换算成执行用的屏幕坐标。

    · 有轴：全部按图内坐标（0 起）解释，换算后写回 arguments（内部用）；
    · 无轴/轴失效：**报错不执行**（绝不把图内坐标当屏幕坐标用）；唯一例外是
      「还没有任何轴时的 capture_screenshot(rect=…)」——建轴前先裁一张的引导口；
    · `relative=true`（增量语义）不换算——x,y 是屏幕像素增量，不是绝对坐标。
    返回错误文本（字符串）或 None；换算说明写 `_AXIS_NOTE`。"""
    global _AXIS_NOTE, _AXIS0
    _AXIS0 = {}
    _AXIS_NOTE = ""
    if name not in _AXIS_TOOLS:
        return None
    rel = bool(arguments.get("relative", False))
    _seq_rel = bool(arguments.get("relative", True))
    _seq_need = (name == "sequence" and _seq_axis_need(arguments.get("steps"), _seq_rel))
    if name in ("mouse_click", "mouse_move") and rel:
        _AXIS_NOTE = ("坐标说明：relative=true 是**增量**语义（x,y＝屏幕像素位移，"
                      "不是图内绝对坐标），不参与坐标轴换算。")
        return None
    a, err = _axis_validate()
    if a is None:
        _coord_keys = ("x", "y", "source", "target", "rect",
                       "start_x", "start_y", "end_x", "end_y")
        _has_coord = any(arguments.get(k) is not None for k in _coord_keys) or _seq_need
        if _AXIS is not None and _has_coord:
            # 轴在、但已失效（截图后窗口被移动/改尺寸/关闭、分辨率变化）：
            #   带坐标的调用换算不出当前屏幕位置 ⇒ **报错不执行**，绝不猜；
            #   不带坐标的调用（整图截图/滚轮翻页等）照常执行——截图本身会重建轴。
            return ((err or "图内坐标轴已失效——请重新截图。")
                    + "（本次未执行；请重新截图后按新的图内坐标操作）")
        if name == "capture_screenshot" and arguments.get("rect") not in (None, ""):
            # 唯一引导例外：还没有任何轴时，允许先按屏幕矩形裁一张（回执明确说明口径）
            _AXIS_NOTE = ("坐标说明：**尚未建立图内坐标轴**，本次 rect 按**屏幕坐标**解释；"
                          "整图截一张后即按图内坐标（0 起）解释。")
            return None
        if _has_coord:
            _why = (_AXIS_WHY or "").strip()
            return (((_why + "。") if _why else "还没有图内坐标轴基准。")
                    + "请先整图截图（capture_screenshot）建立坐标轴，再按图内坐标操作。")
        return None
    # 轴属于哪扇窗：本次 window= 指向别处 ⇒ 明确报错（否则会按错窗口的轴去点）
    _warg = arguments.get("window")
    if _warg and a.get("hwnd"):
        try:
            _w2 = _window_arg_target(_warg)
            if _w2 and _window_family_root(_w2) != _window_family_root(a.get("hwnd")):
                return ("图内坐标轴属于最近整图截图那扇窗口——本次 window= 指向的是另一扇。"
                        "请先截那扇窗的整图，再按它的图内坐标操作。")
        except Exception:
            pass
    try:
        if name == "sequence":
            # 序列步里的绝对坐标同样按图内坐标解释（relative 增量不换算）——一处换算、
            #   步内各分支只管执行；换过就把说明写进回执（模型不用心算）。
            if _seq_need:
                _nconv = _seq_axis_convert_steps(arguments.get("steps"), _seq_rel)
                if _nconv:
                    _AXIS_NOTE = _axis_note_text("步内绝对坐标")
            return None
        if name == "capture_screenshot":
            r = arguments.get("rect")
            if r not in (None, ""):
                sx, sy, sw, sh = _axis_rect4(r)
                _AXIS0["rect"] = str(r)
                arguments["rect"] = "%d,%d,%d,%d" % (sx, sy, sw, sh)
                _AXIS_NOTE = _axis_note_text("rect")
            return None
        if name in ("mouse_click", "mouse_move", "hover"):
            x, y = arguments.get("x"), arguments.get("y")
            if x is None or y is None:
                return None
            sx, sy = _axis_pt(x, y)
            _AXIS0.update({"x": x, "y": y, "sx": sx, "sy": sy})
            arguments["x"], arguments["y"] = sx, sy
            _vr = arguments.get("verify_region")
            if _vr not in (None, ""):
                try:
                    arguments["verify_region"] = "%d,%d,%d,%d" % _axis_rect4(_vr)
                except ValueError:
                    pass
            if name == "hover":
                _hr = arguments.get("rect")
                if _hr not in (None, ""):
                    try:
                        arguments["rect"] = "%d,%d,%d,%d" % _axis_rect4(_hr)
                    except ValueError:
                        pass
            _AXIS_NOTE = _axis_note_text("x,y")
            return None
        if name == "mouse_drag":
            for kx_, ky_ in (("end_x", "end_y"), ("start_x", "start_y")):
                _xv, _yv = arguments.get(kx_), arguments.get(ky_)
                if _xv is None or _yv is None:
                    continue
                sx, sy = _axis_pt(_xv, _yv)
                _AXIS0.update({kx_: _xv, ky_: _yv})
                arguments[kx_], arguments[ky_] = sx, sy
            _vr = arguments.get("verify_region")
            if _vr not in (None, ""):
                try:
                    arguments["verify_region"] = "%d,%d,%d,%d" % _axis_rect4(_vr)
                except ValueError:
                    pass
            _AXIS_NOTE = _axis_note_text("起点/终点")
            return None
        if name == "mouse_transfer":
            for key in ("source", "target", "safe_park"):
                _v = arguments.get(key)
                if _v in (None, ""):
                    continue
                _x, _y = _parse_xy(_v, key)
                sx, sy = _axis_pt(_x, _y)
                _AXIS0[key] = (_x, _y)
                arguments[key] = "%d,%d" % (sx, sy)
            _vr = arguments.get("verify_region")
            if _vr not in (None, ""):
                try:
                    arguments["verify_region"] = "%d,%d,%d,%d" % _axis_rect4(_vr)
                except ValueError:
                    pass
            _AXIS_NOTE = _axis_note_text("源/目标")
            return None
        if name == "scroll":
            x, y = arguments.get("x"), arguments.get("y")
            if x is None or y is None:
                return None
            sx, sy = _axis_pt(x, y)
            _AXIS0.update({"x": x, "y": y})
            arguments["x"], arguments["y"] = sx, sy
            _AXIS_NOTE = _axis_note_text("x,y")
            return None
    except ValueError as _ve:
        return str(_ve)
    return None


def _ruler_note(meta, geom, trim, is_crop, grid=None):
    """回执里的尺子说明：坐标怎么读，一句话说清（不让人误以为还要换算）。"""
    ir = geom.get("img_rect") or [0, 0, 0, 0]
    kx = float(geom.get("kx") or 1.0)
    ky = float(geom.get("ky") or 1.0)
    reg = {"window": "窗口", "window_visible": "窗口可见区（PW回退）",
           "fullscreen": "全屏", "crop": "裁剪区"}.get(
        geom.get("_region") or geom.get("region"),
        str(geom.get("_region") or geom.get("region")))
    tr = ("｜已裁掉窗口纯黑边框 %s" % (list(trim),)) if any(trim) else ""
    try:
        iw, ih = int(geom.get("img", [0, 0])[0]), int(geom.get("img", [0, 0])[1])
    except Exception:
        iw = ih = 0
    _grid_txt = ""
    if grid is not None:
        try:
            _d = int(grid.get("dash", _MARK_GRID_DASH_DEFAULT))
        except Exception:
            _d = _MARK_GRID_DASH_DEFAULT
        _cm = {"auto": "自动（浅底黑/深底白）", "black": "强制黑",
               "white": "强制白"}.get(str(grid.get("color") or "auto").lower(), "自动")
        _grid_txt = ("｜**棋盘线**：主刻度(每 %d)与中刻度(每 %d)延伸进内容成虚线——"
                     "段 %dpx / 空 %dpx，**段头＝屏幕坐标 %d 的整数倍（每个段头都是精确坐标）**；"
                     "中线数字=主线字号 %.0f%%、与主线顶部对齐；线色 %s、纯色不用透明度；"
                     "0 位置不画（不盖尺子分隔线）"
                     % (meta["step_x"], max(1, meta["step_x"] // 2), _d, _d, _d,
                        _MARK_MID_LABEL_SCALE * 100, _cm))
    _pv_txt = ""
    _inside = bool(geom.get("ruler_inside"))
    try:
        if geom.get("resized"):
            _cvs = geom.get("canvas") or [0, 0]
            _tok = _ds_grid_tokens(int(_cvs[0]), int(_cvs[1]))
            if _inside:
                _pv_txt = ("｜**已主动缩到内容 %d×%d**（图内刻度 ⇒ 整图＝内容、无外框；"
                           "模型请求档 %d/%d token ⇒ 预览即原图 1:1，刻度数字可直接读）"
                           % (iw, ih, _tok, IMAGE_MAX_TOKENS))
            else:
                _pv_txt = ("｜**已主动缩到内容 %d×%d**（含坐标尺边条整图 %d×%d，"
                           "模型请求档 %d/%d token ⇒ 预览即原图 1:1，刻度数字可直接读）"
                           % (iw, ih, int(_cvs[0]), int(_cvs[1]), _tok, IMAGE_MAX_TOKENS))
    except Exception:
        _pv_txt = ""
    _head_txt = ("已带**坐标轴刻度**（画在图内、按底色自动反色：浅底黑/深底白）" if _inside
                 else "已带**坐标轴刻度**（白底黑刻度）")
    # ★0.1.88：模型面只给**图内坐标轴**——屏幕坐标不写进回执（只在工具内部换算用）。
    _axis_geo = geom.get("axis") if is_crop else _AXIS
    if is_crop and _axis_geo:
        _rng = _axis_range_of_crop(_axis_geo, [int(ir[0]), int(ir[1]), int(ir[2]), int(ir[3])], None)
        _rng_txt = (("｜**图内区间** x∈[%.0f,%.0f]、y∈[%.0f,%.0f]（沿用同一根轴，不从 0 重来）"
                     % _rng) if _rng else "")
        _coord_txt = ("刻度数字是**轴上的绝对位置**（例如从 (360,480) 裁出就从 360/480 继续）——"
                      "**不是屏幕坐标、也不是从 0 重来**；要把这些数原样当坐标传，工具内部自动换算。")
    else:
        _pl, _pt = ((_AXIS["pad"] if _AXIS else (0, 0)))
        _rng_txt = ("｜**内容区**在图内 x∈[%d,%d]、y∈[%d,%d]"
                    % (_pl, _pl + iw, _pt, _pt + ih))
        _coord_txt = ("刻度数字＝**图内相对坐标**（0,0＝整张附图左上角，含刻度边条）；"
                      "要操作就把这些数原样当坐标传——工具内部换算，不用乘 k、不用加图原点。")
    return (_head_txt + "：" + _coord_txt + _rng_txt
            + "｜区域=%s（图 %d×%d）｜刻度：小 %d/格、标号每 %d｜字号 %dpx%s%s"
            % (reg, iw, ih, meta["minor_x"], meta["step_x"],
               int(round(meta["font_deliv"])), tr, _grid_txt)
            + _pv_txt)



# ★自动标记开关（默认开；要关设 MCP_MARK_ATTACH=0——与既有环境开关同形，不用改启动 bat）。
_MARK_ATTACH_ON = str(os.environ.get("MCP_MARK_ATTACH", "1")).lower() not in ("0", "false", "off", "no")
# 调试开关：模型侧已无法要原图；本地要把原图拿出来看时设 MCP_MARK_CLEAN=1（只影响附图，识别侧不变）。
_MARK_CLEAN_DEBUG = str(os.environ.get("MCP_MARK_CLEAN", "0")).lower() in ("1", "true", "on", "yes")
# ★附图显示真实光标开关（设置页控制，默认开）。关＝附图不画系统光标图标、回执也不报鼠标位置。
#   由 Node 壳启动本进程前按状态文件写进环境；不走工具面，所以改它不破提示词缓存。
_CURSOR_SHOW_ON = str(os.environ.get("DSH_DESKTOP_CURSOR_SHOW", "1")).lower() not in ("0", "false", "off", "no")


def _mark_attach_png(png, hwnd=None, region="window", crop=None,
                     style=None, grid_dash=None, grid_color=None, ruler_inside=None):
    """**唯一接入点：同一次截图，分发两条路**。

    本函数**不截图**：它只拿"本来要给模型看的那份
    字节"（＝那次唯一截图的产物）在**新画布**上画坐标尺/棋盘；`_FRAME` 里登记的那份、
    以及识别那条路手里那份，一个字节都不动（画在新画布上是隔离的**唯一**保证）。

    `style`：`grid`（默认）＝坐标尺＋棋盘虚线；`center`＝只画坐标尺。**非法值直接抛 ValueError**
    （工具面未声明的值绝不静默回落；0.1.70 起严格执行）。
    返回 `(给模型的字节, "")`。**除非法 style 外的任何异常 ⇒ 原样退回、并把原因打进 stderr**——
    绝不因为"想画标注"而把画面弄丢或弄坏（画面本身比标注重要）。
    """
    if not _MARK_ATTACH_ON or not png or not hwnd:
        _axis_reset("最近附图没有画坐标轴刻度（无操作面/标注关闭）——先 focus_window 再整图截图")
        return png, ""
    _style = _mark_style_arg(style)          # ← 校验放在 try 外：非法值必须抛出去，不被兜底吞掉
    try:
        import io as _io
        from PIL import Image as _Image
        img = _Image.open(_io.BytesIO(png)).convert("RGB")
        # ★坐标真源：与观察帧**同一个预处理**（裁窗口外黑边 / crop 覆盖 / geom 平移 + k 重算）
        #   ⇒ 刻度尺、标注与工具点击坐标永远同源；以后改规则只改 `_view_prep` 一处。
        img, trim, geom = _view_prep(img, hwnd, region=region, crop=crop)
        w, h = img.size
        # 送达档（0.1.84）：判据＝**最终画布（内容＋坐标尺边条）**必须落进模型请求图的 token
        #   网格（`_ds_grid_tokens` ≤ 1024）——平台只按整张图裁量，缩内容而不算边条仍会被再缩一次。
        #   边条宽度随尺寸变 ⇒ 用 `_ruler_metrics` 预演画布、二分找最大内容尺寸；只做数学、不重画。
        try:
            _ir0 = list(geom.get("img_rect") or [0, 0, 0, 0])
            _inside = bool(ruler_inside)

            def _canvas_wh(_w, _h):
                if _inside:      # 图内刻度：画布＝内容，没有外部边条
                    return _w, _h
                _kx = ((float(_ir0[2]) - float(_ir0[0])) / float(_w or 1))
                _ky = ((float(_ir0[3]) - float(_ir0[1])) / float(_h or 1))
                _mm = _ruler_metrics(_w, _h, int(_ir0[0]), int(_ir0[1]), _kx, _ky)
                return _w + _mm["pad_l"], _h + _mm["pad_t"]

            def _fits(_w, _h):
                _cw, _ch = _canvas_wh(_w, _h)
                return _ds_grid_tokens(_cw, _ch) <= IMAGE_MAX_TOKENS

            _s = 1.0
            if not _fits(w, h):
                _lo, _hi = 0.20, 1.0
                for _ in range(14):
                    _mid = (_lo + _hi) / 2.0
                    _mw, _mh = max(1, int(round(w * _mid))), max(1, int(round(h * _mid)))
                    if _fits(_mw, _mh):
                        _lo = _mid
                    else:
                        _hi = _mid
                _s = _lo
            _nw, _nh = max(1, int(round(w * _s))), max(1, int(round(h * _s)))
            _guard = 0
            while not _fits(_nw, _nh) and _nw > 8 and _guard < 400:
                _nw = max(1, int(round(_nw * 0.995)))
                _nh = max(1, int(round(_nw * h / float(w))))
                _guard += 1
            if _s < 0.998:
                img = img.resize((_nw, _nh), _Image.LANCZOS)
                geom["img"] = [_nw, _nh]
                geom["kx"] = ((float(_ir0[2]) - float(_ir0[0])) / float(_nw or 1))
                geom["ky"] = ((float(_ir0[3]) - float(_ir0[1])) / float(_nh or 1))
                geom["resized"] = True
                w, h = _nw, _nh
        except Exception:
            pass
        # ★center／grid＝坐标尺：白底黑刻度、内容零遮挡；grid 额外把主/中刻度延伸成虚线棋盘。
        if _style in ("center", "grid"):
            _gridcfg = None
            if _style == "grid":
                _gridcfg = {"dash": _clamp_grid_dash(grid_dash),
                            "color": _clamp_grid_color(grid_color)}
            if str(region) == "crop" and _AXIS:
                geom["axis"] = dict(_AXIS)   # 局部图沿用同一根轴（刻度继续显示轴上的绝对位置，不从 0 重来）
            geom["_region"] = region
            canvas, _meta = _ruler_annotate(img, geom, grid=_gridcfg, inside=_inside, hwnd=hwnd)
            geom["canvas"] = [canvas.size[0], canvas.size[1]]
            geom["ruler_inside"] = _inside
            buf = _io.BytesIO()
            canvas.save(buf, "PNG")
            try:
                _rn = _ruler_note(_meta, geom, trim, bool(crop), grid=_gridcfg)
                _prev_note = _LAST_SHOT_NOTE or ""
                if not _prev_note.startswith(_POPUP_NOTE_PREFIX):
                    _prev_note = ""      # 只保留「叠加弹窗」那条留痕；别的旧留痕不带过来
                _set_shot_note(((_prev_note + "；") if _prev_note else "") + _rn)
            except Exception:
                pass
            return buf.getvalue(), ""
    except Exception as e:
        print(f"[标记] 跳过（不影响画面）：{e}", file=sys.stderr)
        _axis_reset("最近附图没有画坐标轴刻度（标注失败）——请重新截图")
        return png, ""


def _clamp_grid_dash(v):
    """棋盘虚线**段长**（px）：None/空 ⇒ 默认 20；夹在 [4, 100]。非法值当没传。"""
    if v is None or v == "":
        return _MARK_GRID_DASH_DEFAULT
    try:
        return max(4, min(100, int(v)))
    except Exception:
        return _MARK_GRID_DASH_DEFAULT


def _clamp_grid_color(v):
    """棋盘**线色**：auto（逐像素反色：浅底黑/深底白）／black／white；其它值当 auto。"""
    s = str(v or "auto").strip().lower()
    return s if s in ("auto", "black", "white") else "auto"


# ── 光标图标：给「给模型看」的附图叠加真实鼠标指针（不画别的、不写字）。
def _cursor_icon_image():
    """取系统当前鼠标指针的真实图标 → (PIL RGBA 图, 热点 x, 热点 y)；失败返回 None。"""
    try:
        import ctypes
        from ctypes import wintypes as _wt
        from PIL import Image as _Im

        class _CI(ctypes.Structure):
            _fields_ = [("cbSize", _wt.DWORD), ("flags", _wt.DWORD),
                        ("hCursor", _wt.HANDLE), ("ptScreenPos", _wt.POINT)]

        class _II(ctypes.Structure):
            _fields_ = [("fIcon", _wt.BOOL), ("xHotspot", _wt.DWORD),
                        ("yHotspot", _wt.DWORD), ("hbmMask", _wt.HANDLE),
                        ("hbmColor", _wt.HANDLE)]

        class _BIH(ctypes.Structure):
            _fields_ = [("biSize", _wt.DWORD), ("biWidth", _wt.LONG),
                        ("biHeight", _wt.LONG), ("biPlanes", _wt.WORD),
                        ("biBitCount", _wt.WORD), ("biCompression", _wt.DWORD),
                        ("biSizeImage", _wt.DWORD), ("biXPelsPerMeter", _wt.LONG),
                        ("biYPelsPerMeter", _wt.LONG), ("biClrUsed", _wt.DWORD),
                        ("biClrImportant", _wt.DWORD)]

        _u = ctypes.windll.user32
        _g = ctypes.windll.gdi32
        _u.GetCursorInfo.argtypes = [ctypes.c_void_p]
        _u.GetCursorInfo.restype = _wt.BOOL
        _u.GetIconInfo.restype = _wt.BOOL
        _g.CreateCompatibleDC.argtypes = [_wt.HDC]
        _g.CreateCompatibleDC.restype = _wt.HDC
        _g.CreateDIBSection.argtypes = [_wt.HDC, ctypes.c_void_p, _wt.UINT,
                                        ctypes.POINTER(ctypes.c_void_p), _wt.HANDLE, _wt.DWORD]
        _g.CreateDIBSection.restype = _wt.HANDLE
        _g.SelectObject.argtypes = [_wt.HDC, _wt.HANDLE]
        _g.SelectObject.restype = _wt.HANDLE
        _g.DeleteObject.argtypes = [_wt.HANDLE]
        _g.DeleteDC.argtypes = [_wt.HDC]
        ci = _CI()
        ci.cbSize = ctypes.sizeof(_CI)
        if not _u.GetCursorInfo(ctypes.byref(ci)):
            return None
        if not (int(ci.flags) & 0x1):          # CURSOR_SHOWING
            return None
        ii = _II()
        _u.GetIconInfo.argtypes = [_wt.HANDLE, ctypes.c_void_p]
        if not _u.GetIconInfo(ci.hCursor, ctypes.byref(ii)):
            return None
        hx, hy = int(ii.xHotspot), int(ii.yHotspot)
        cx = int(_u.GetSystemMetrics(13)) or 32       # SM_CXCURSOR
        cy = int(_u.GetSystemMetrics(14)) or 32       # SM_CYCURSOR
        bmi = _BIH()
        bmi.biSize = ctypes.sizeof(_BIH)
        bmi.biWidth = cx
        bmi.biHeight = -cy
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        hdc = _g.CreateCompatibleDC(0)
        bits = ctypes.c_void_p()
        hbmp = _g.CreateDIBSection(0, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = _g.SelectObject(hdc, hbmp)
        if bits:
            ctypes.memset(bits, 0, cx * cy * 4)
        _u.DrawIconEx.argtypes = [_wt.HDC, ctypes.c_int, ctypes.c_int, _wt.HANDLE,
                                  ctypes.c_int, ctypes.c_int, _wt.UINT, ctypes.c_void_p, _wt.UINT]
        _u.DrawIconEx.restype = _wt.BOOL
        ok = _u.DrawIconEx(hdc, 0, 0, ci.hCursor, cx, cy, 0, None, 0x0003)
        img = None
        if ok and bits:
            raw = ctypes.string_at(bits, cx * cy * 4)
            img = _Im.frombytes("RGBA", (cx, cy), raw, "raw", "BGRA")
            if img.getchannel("A").getextrema()[1] == 0:
                # 图标没有 alpha 通道：按非黑像素补齐，避免整张透明
                px = img.load()
                for yy in range(cy):
                    for xx in range(cx):
                        r, gg, b, aa = px[xx, yy]
                        if aa == 0 and (r or gg or b):
                            px[xx, yy] = (r, gg, b, 255)
        _g.SelectObject(hdc, old)
        if hbmp:
            _g.DeleteObject(hbmp)
        if hdc:
            _g.DeleteDC(hdc)
        for _h in (ii.hbmMask, ii.hbmColor):
            if _h:
                _g.DeleteObject(_h)
        return (img, hx, hy) if img is not None else None
    except Exception:
        return None


def _draw_cursor_icon(png, hwnd, region, sx, sy, croprect=None):
    """鼠标在附图范围内 ⇒ 叠加真实光标图标（不加文字），否则原样返回。"""
    if not png or not _CURSOR_SHOW_ON:
        return png
    try:
        import io as _io
        from PIL import Image as _Image
        img = _Image.open(_io.BytesIO(png)).convert("RGBA")
        w, h = img.size
        if croprect:
            l, t, r, b = [int(v) for v in croprect]
        elif str(region) == "fullscreen":
            l, t, r, b = _screen_virtual_rect()
        else:
            if not hwnd:
                return png
            l, t, r, b = _get_physical_window_rect(hwnd)
        vw, vh = max(r - l, 1), max(b - t, 1)
        cx = (float(sx) - l) * w / vw
        cy = (float(sy) - t) * h / vh
        if cx < -8 or cy < -8 or cx > w + 8 or cy > h + 8:
            return png
        _cur = _cursor_icon_image()
        if not _cur:
            return png
        icon, hx, hy = _cur
        px = int(round(cx)) - int(hx)
        py = int(round(cy)) - int(hy)
        if px < 0 or py < 0 or px >= w or py >= h:
            return png
        icon = icon.crop((0, 0, min(icon.width, w - px), min(icon.height, h - py)))
        base = img.copy()
        base.alpha_composite(icon, (px, py))
        buf = _io.BytesIO()
        base.convert("RGB").save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return png


_sys_timing = {}   # 调试：系统调用耗时 {"screenshot": [elapsed, ...], "enum": [...], ...}
_debug_timing = True   # 调试开关：为 True 时每轮工具调用输出耗时明细
                       #   接好的计时点由 `_timing_add()` 汇总，回执会多出
                       #   `[SYS: enum:N次 X.Xs, uia:…, screenshot:…, classify:…]` 一行。

def _timing_add(cat, elapsed):
    _sys_timing.setdefault(cat, []).append(elapsed)

def _sys_stats_block():
    """返回系统调用统计"""
    if not _sys_timing:
        return ""
    parts = []
    # 原表只有 screenshot/enum/uia/classify 四类 ⇒ `focus_window` 链路里剩下那 0.5~1.0s 是黑箱。
    #   下面这批新类别就是黑箱的切面（见 handle_tool_call 的 focus_window 分支）。
    #   纯显示、零行为影响。
    for cat in ["screenshot", "enum", "uia", "classify",
                "findwin", "activate", "popups", "status", "shot1",
                "buttons", "mergeuo", "yolo", "render", "tracked", "attach"]:
        if cat in _sys_timing:
            vals = _sys_timing[cat]
            parts.append(f"{cat}:{len(vals)}次:{sum(vals):.1f}s")
    return f"\n[SYS: {', '.join(parts)}]"

# OCR 相关实现已整段删除（_get_physical_window_rect 保留，标尺/编号仍用它）。


def _get_physical_window_rect(hwnd):
    """获取窗口物理屏幕矩形（不受 DPI 虚拟化影响）。
    用 DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS=9) 得到物理像素坐标。
    回退到 GetWindowRect（可能受 DPI 影响，但 SendInput 用 MOUSEEVENTF_ABSOLUTE 物理空间）。"""
    rect = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS = 9
    try:
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
        if result == 0 and (rect.right - rect.left) > 0 and (rect.bottom - rect.top) > 0:
            return rect.left, rect.top, rect.right, rect.bottom
    except:
        pass
    # Fallback: GetWindowRect（若进程非 DPI-aware 则返回虚拟化坐标）
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


# （ 去识别）ocr_to_screen_coords 已删（OCR/图标层的图像→屏幕坐标换算，唯一调用方已删）。


# （ 去识别）_mask_content_area / _detect_buttons_from_data / _detect_buttons /
#   _get_class_name 已整段删除（OCR 遮罩＋Canny 图标按钮检测＋窗口类名仅在识别里用）。




# （ 去识别）parse_ui_elements 已整段删除（OCR 元素解析主入口）。


# （ 去识别）_parse_ui_with_fallback / _link_disabled_to_checkboxes / _format_element_col /
#   _merge_icon_buttons / _group_elements_by_rows 已整段删除。


# （ 去识别）_classify_elements 已整段删除（OCR/UIA 元素的角色分类器）。


# （ 去识别）capture_ui_changes 已整段删除（点击后整帧 OCR/UIA 变化对比＝回读）。


# （ 去识别）extract_terminal_text 已整段删除（UIA+OCR 并行终端取文）。


def _pil_png_bytes(img):
    """PIL.Image → PNG 字节（**在内存里编一次**）。

    这一份字节在内存里编一次后供附图复用通道等内存消费者使用；
      老路是「`img.save(路径,'PNG')` 落盘、谁要谁去读」；现在盘上不留。
    """
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# OCR 截图与文字回读、弹窗按钮扫描已删除；动作回执只给「纯像素生效判定 + 窗口变化」。


# ══════════════════════════════════════════════════════════════════════════════
# 【按键注入分流表】不同的按键要用不同的方法，才能稳定生效。与窗口级的 `_INPUT_RULES` 同构。
#   **默认通道一个字节不动**（有线终端依赖 keybd_event）；只给**实测过的**例外开定向通道。
#
#   按键                  走哪条通道              凭据（都是实测出来的）
#   ────────────────────  ─────────────────────  ──────────────────────────────────────
#   默认（字母/符号/组合键） keybd_event（老 API）   有线终端依赖它，红线，不许换
#   Win 家族 0x5B/5C/5D    SendInput·纯扫描码        keybd_event 根本发不进系统（定案）
#   Shift 0x10（要用它切中英）SendInput·虚拟键        keybd_event 的 Shift 输入法不理会；
#                                                  SendInput 的 Shift 切得动（运行框 A/B）
# ══════════════════════════════════════════════════════════════════════════════


def _si_key(vk, keyup=False):
    """SendInput **虚拟键**形态（wVk=vk、不带 KEYEVENTF_SCANCODE）。
    实测：这是**唯一能触发输入法中/英切换**的形态——
      keybd_event 的 Shift 输入法不理会（x→卞、py→朋友照转），SendInput 的 Shift 切动了。
    与 Win 家族那条（SendInput 纯扫描码 wVk=0）是**两条不同的通道**：各自适配不同的"收件人"。"""
    try:
        flags = KEYEVENTF_KEYUP if keyup else 0
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki = KEYBDINPUT(vk, _vk_scan_code(vk), flags, 0, None)
        return user32_si.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    except Exception:
        return 0


def _ime_toggle_sendi():
    """点一下裸 Shift ＝ 输入法中/英**翻转**（`_si_key` ⇒ **SendInput、VK 形态**；不是 scancode 形态）。

    · **注入的 Shift 在记事本这类窗口上照样切得动**：探针报英文态（无声）
        → `send_shortcut {SHIFT}`（走 `_key_event`＝**keybd_event**）→ 再探**报中文态**，
        且 `ri␣` 落成**「日」**（汉字）⇒ 就是那一下切过去的；
    · 「只有 SendInput 切得动」这句**是错的**：它源自 shell 对话框那族（`#32770`）的结论，那族才切不动；
    · 形态规则（以免再混）：分野是 **API**（`keybd_event` vs `SendInput`），**不是** VK／扫描码。
    """
    _si_key(0x10, False)
    time.sleep(0.03)
    _si_key(0x10, True)
    time.sleep(0.15)


# ========== 鼠标操作（PostMessage 后台，不抢焦点） ==========

_focus_hwnd = None          # focus_window 设置的全局聚焦句柄（主窗口）
_focus_child_hwnd = None   # focus_window 解析出的终端子控件句柄
_focus_pid = 0             # ★聚焦那一刻**记下的进程号**：窗口关了还能凭它找回同进程的窗（见 _recover_focus_target）
_LAST_FOCUS_NOTE = ""      # 本轮「聚焦窗失联 ⇒ 自动接管」的说明（每轮重置，由 _tool_call_result 带出）
_popup_hwnd = None          # 最近一次新发现的弹窗句柄（快捷引用）
_tracked_hwnds = set()      # 跟踪中的所有弹窗句柄（自动清理已关闭的）
# _last_scan_elements 已随扫描层删除。
# Win32 窗口样式常量
GWL_STYLE = -16
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000


def _is_window_alive(hwnd):
    """窗口句柄有效且可见。"""
    return bool(user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd))


def _is_offscreen(left, top):
    """坐标为离屏区域（虚拟桌面坐标 <= -32000）。"""
    return left <= -32000 or top <= -32000


def _rect_size(rect):
    """从 RECT 结构体返回 (width, height)。"""
    return rect.right - rect.left, rect.bottom - rect.top


def _is_main_window(hwnd):
    """Win32 样式判断是否为主窗口（非弹窗、非子窗口）。"""
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    return not (style & WS_POPUP) and not (style & WS_CHILD)


def _is_popup_window(hwnd):
    """Win32 样式判断是否为弹窗。"""
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    return bool(style & WS_POPUP)


def _set_popup(hwnd):
    """安全设置 _popup_hwnd（设为最近发现的弹窗）。"""
    global _popup_hwnd
    _popup_hwnd = hwnd


def _clear_popup():
    """安全清除 _popup_hwnd。"""
    global _popup_hwnd
    _popup_hwnd = None


def _discover_popups(focused_hwnd):
    """实时发现聚焦窗口的弹窗（基于 WS_POPUP 样式 + owner 关系）。
    同 PID 优先，owner 关系兜底（跨进程弹窗也纳入）。
    返回 [(hwnd, title, rect, owner_hwnd), ...]。"""
    popups = []
    try:
        pid_buf = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(focused_hwnd, ctypes.byref(pid_buf))
        focused_pid = pid_buf.value
    except:
        return popups
    if not focused_pid:
        return popups
    GW_OWNER = 4
    for w in enum_all_windows():
        wh = w["hwnd"]
        if wh == focused_hwnd:
            continue
        if not user32.IsWindowVisible(wh):
            continue
        if not _is_popup_window(wh):
            continue
        rect = wintypes.RECT()
        if not user32.GetWindowRect(wh, ctypes.byref(rect)):
            continue
        if _is_offscreen(rect.left, rect.top):
            continue
        # 跳过全屏大小的窗口（非弹窗，是独立主窗口）
        pw, ph = _rect_size(rect)
        if pw > 1600 and ph > 900:
            continue
        # 跳过极小窗口（隐藏/工具窗口，< 50x50）
        if pw < 50 or ph < 50:
            continue
        try:
            pid_buf2 = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(wh, ctypes.byref(pid_buf2))
            pid_match = (pid_buf2.value == focused_pid)
            # 检查 owner 关系（跨进程弹窗兜底）
            owner = user32.GetWindow(wh, GW_OWNER) or 0
            owner_match = (owner == focused_hwnd or any(owner == p[0] for p in popups))
            if pid_match or owner_match:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(wh, buf, 256)
                title = buf.value or "(无标题)"
                popups.append((wh, title, rect, owner))
        except:
            pass
    return popups


def _popup_set(focused_hwnd):
    """当前可见弹窗集合 {hwnd: title}（Win32：WS_POPUP + owner，不碰 OCR）。
    用于「动作前后弹窗差集」——右键菜单/下拉框/对话框属于独立窗口，
    像素差分看不见它们，但这里能看见。"""
    out = {}
    if not focused_hwnd:
        return out
    try:
        for wh, title, _rect, _owner in _discover_popups(focused_hwnd):
            out[int(wh)] = str(title or "(无标题)")
    except Exception:
        pass
    return out


def _popup_delta_line(before, after):
    """动作前后弹窗差集 → 一行回执文本；无变化返回空串。

    这层是「新弹窗/弹窗关闭检测」的 **Win32 复刻版**
    （老实现靠 OCR，已随识别层删除；发现/跟踪件一直保留，这里只是把差集算出来）。"""
    try:
        added = [v for k, v in (after or {}).items() if k not in (before or {})]
        closed = [v for k, v in (before or {}).items() if k not in (after or {})]
        if not added and not closed:
            return ""
        bits = []
        if added:
            bits.append("新增 " + "、".join("'%s'" % t for t in added[:3]))
        if closed:
            bits.append("关闭 " + "、".join("'%s'" % t for t in closed[:3]))
        return "弹窗变化：" + "；".join(bits)
    except Exception:
        return ""


def _popup_probe_begin():
    """动作前：记下当前操作面的弹窗集合（分发层用）。"""
    return _popup_set(_focus_hwnd)


def _popup_probe_line(before):
    """动作后：清理已关闭窗口 → 重算弹窗集合 → 返回差集行（无变化返回空串）。"""
    try:
        _prune_tracked_popups()
    except Exception:
        pass
    return _popup_delta_line(before, _popup_set(_focus_hwnd))


def _build_popup_tree(focused_hwnd, popups):
    """根据 owner 关系构建窗口树。
    popups: [(hwnd, title, rect, owner_hwnd), ...]
    返回: {hwnd: {"title":..., "rect":..., "children":[hwnd,...], "is_current":bool}} 字典。"""
    tree = {}
    # 主窗口节点
    frect = wintypes.RECT()
    user32.GetWindowRect(focused_hwnd, ctypes.byref(frect))
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(focused_hwnd, buf, 256)
    tree[focused_hwnd] = {"title": buf.value or "(无标题)",
                          "rect": frect, "children": [], "is_current": False}
    # 弹窗节点
    for ph, ptitle, prect, powner in popups:
        tree[ph] = {"title": ptitle, "rect": prect, "children": [],
                    "is_current": (ph == _popup_hwnd)}
    # 建父子关系
    for ph, _, _, powner in popups:
        parent = powner if powner and powner in tree else focused_hwnd
        if parent not in tree:
            parent = focused_hwnd
        tree[parent]["children"].append(ph)
    return tree


def _get_active_surface(focused_hwnd=None):
    """确定当前操作面（顶层弹窗/主窗口）。同时同步 _popup_hwnd。
    返回 {"hwnd": int, "title": str, "type": "popup"|"main", "rect": {...}}
    弹窗栈：最深层弹窗=唯一可交互面。其下所有窗口（主窗口+外层弹窗）均被阻挡。"""
    global _popup_hwnd
    fh = focused_hwnd or _focus_hwnd
    if not fh:
        return None
    popups = _discover_popups(fh)
    if not popups:
        _popup_hwnd = None
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(fh, buf, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(fh, ctypes.byref(rect))
        return {"hwnd": fh, "title": buf.value or "(无标题)", "type": "main",
                "rect": {"left": rect.left, "top": rect.top,
                         "right": rect.right, "bottom": rect.bottom}}

    tree = _build_popup_tree(fh, popups)
    # 找最深子节点（栈最顶层=唯一可交互）。
    # GetForegroundWindow 最可靠——弹窗出现必然抢焦点。
    def _deepest(node_hwnd):
        fg = user32.GetForegroundWindow()
        if fg and fg in tree and fg != node_hwnd:
            return fg
        node = tree.get(node_hwnd)
        if not node or not node["children"]:
            return node_hwnd
        children = [c for c in node["children"] if c in tree]
        return _deepest(children[-1]) if children else node_hwnd

    deepest = _deepest(fh)
    if deepest == fh:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(fh, buf, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(fh, ctypes.byref(rect))
        return {"hwnd": fh, "title": buf.value or "(无标题)", "type": "main",
                "rect": {"left": rect.left, "top": rect.top,
                         "right": rect.right, "bottom": rect.bottom}}

    node = tree.get(deepest, {})
    rect = node.get("rect", wintypes.RECT())
    _popup_hwnd = deepest  # 同步最深弹窗
    return {"hwnd": deepest, "title": node.get("title", "(无标题)"),
            "type": "popup",
            "rect": {"left": rect.left, "top": rect.top,
                     "right": rect.right, "bottom": rect.bottom}}


# （ 去识别）弹窗按钮扫描与其轮次令牌（_scan_popup_buttons/_popup_scan_tok/_popup_btn_memo/
#   _popup_scan_tok_bump）已整段删除；弹窗"发现/跟踪"保留（输入落点仍用弹窗优先）。


def _tracked_popup_names(pre_elements=None):
    """窗口树形展示，标记当前弹窗。pre_elements 增强已有元素。"""
    _prune_tracked_popups()
    if not _focus_hwnd:
        return []
    popups = _discover_popups(_focus_hwnd)
    if not popups:
        # 无弹窗：_status_block 用空，底部独立窗口树负责显示
        return [] if pre_elements is None else []
    tree = _build_popup_tree(_focus_hwnd, popups)
    names = []
    root = tree.get(_focus_hwnd)
    if not root:
        return []
    r = root["rect"]
    names.append(f"{root['title']} (主窗口)")

    def _fmt_node(hwnd, depth, is_last):
        node = tree[hwnd]
        r = node["rect"]
        prefix = "  " * depth + ("└ " if is_last else "├ ")
        marker = " ◀ 当前" if node["is_current"] else ""
        names.append(f"{prefix}{node['title']} @{r.left},{r.top} {r.right-r.left}x{r.bottom-r.top}{marker}")
        children = [c for c in node["children"] if c in tree]
        for i, c in enumerate(children):
            _fmt_node(c, depth + 1, i == len(children) - 1)

    children = [c for c in root["children"] if c in tree]
    for i, c in enumerate(children):
        _fmt_node(c, 1, i == len(children) - 1)
    return names


_last_status_text = ""  # 窗口树去重缓存


def _focus_surface_line():
    """每个回执都要说清「当前操作面」在哪里。

    为什么非它不可：回执原来只报「前台窗口」。而按不回退设计，**前台可能已被新窗口抢走、
    操作面根本没变**（实测：传 window="Edge" 后发 {WIN+R}，「运行」弹出抢走前台，
    实际作用的仍是 Edge）。两句话不是一回事，只报一句就会让人误判
    「附图/回读怎么落在别的窗口上」。
    """
    h = _surface_hwnd()     # 与帧那边共用同一份表达式
    kind = "弹窗" if (h and _popup_hwnd and h == _popup_hwnd) else "聚焦窗"
    if not h:
        return "操作面: （无 —— 尚未聚焦；按设计不回退，下一步须先 focus_window）"
    if not _is_window_alive(h):
        return "操作面: （已失效 —— 那扇窗已关闭；按设计不回退，下一步须先 focus_window）"
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, buf, 256)
        title = buf.value or "(无标题)"
    except Exception:
        title = "(标题读取失败)"
    line = f"操作面: {title} [{_fg_proc_name(h) or '?'}]（{kind}；注入/回读/附图都只取自这一扇）"
    try:
        fg = user32.GetForegroundWindow()
        if fg and fg != h and _is_window_alive(fg):
            buf2 = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(fg, buf2, 256)
            line += f"｜系统前台另有:「{buf2.value or '?'}」（不回退：仍作用于操作面）"
    except Exception:
        pass
    return line


# ── 操作面几何校准（0.1.51）
#   坐标图/窗口图都是"当时那扇窗的矩形"；窗口一改尺寸或位置，图内坐标就失效了。
#   ⇒ 截图时记基准，`_status_block()` 里比对；变了就在回执里顶一句"重新取坐标图校准"。
_GEO_MEMO = {}                  # hwnd -> (l, t, w, h)
_GEO_TOL = 2                    # 忽略 ≤2px 的抖动（DWM 边框/取整）


def _geo_now(hwnd=None):
    """当前操作面矩形 (l,t,w,h)；取不到返回 None。纯 Win32，不枚举。"""
    try:
        h = int(hwnd or _surface_hwnd() or _focus_hwnd or 0)
        if not h or not _is_window_alive(h):
            return None
        r = wintypes.RECT()
        if not user32.GetWindowRect(h, ctypes.byref(r)):
            return None
        return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))
    except Exception:
        return None


def _geo_stamp(hwnd=None):
    """截图/校准后登记基准（不产生提醒）——坐标图对应的窗口几何就是这一次的。"""
    try:
        h = int(hwnd or _surface_hwnd() or _focus_hwnd or 0)
        g = _geo_now(h)
        if h and g:
            _GEO_MEMO[h] = g
    except Exception:
        pass


def _geo_change_line():
    """操作面几何变了 → 一行提醒（并把基准更新成现值，只提醒一次）。"""
    try:
        h = int(_surface_hwnd() or _focus_hwnd or 0)
        g = _geo_now(h)
        if not h or not g:
            return ""
        prev = _GEO_MEMO.get(h)
        if prev is None:
            _GEO_MEMO[h] = g
            return ""
        if (abs(g[0] - prev[0]) <= _GEO_TOL and abs(g[1] - prev[1]) <= _GEO_TOL
                and abs(g[2] - prev[2]) <= _GEO_TOL and abs(g[3] - prev[3]) <= _GEO_TOL):
            return ""
        _GEO_MEMO[h] = g
        return ("⚠ 操作面几何变了：基准 %d×%d@(%d,%d) → 现在 %d×%d@(%d,%d)"
                " ⇒ 旧图坐标已失效，**请重新 capture_screenshot 校准**；按旧图推算的坐标可能偏。"
                % (prev[2], prev[3], prev[0], prev[1], g[2], g[3], g[0], g[1]))
    except Exception:
        return ""


def _status_block():
    """统一的状态块：**操作面（始终输出）** + 窗口树（只在上次不同时输出，避免 AI 噪声）。

    + 操作面几何变了 ⇒ 顶部顶一句「重新取坐标图校准」（新增）。
    """
    global _last_status_text
    surface = _focus_surface_line()
    _geo = _geo_change_line()
    if _geo:
        surface = _geo + "\n" + surface
    tracked = _tracked_popup_names()
    if not tracked:
        _last_status_text = ""
        return "\n\n" + surface
    text = "窗口树:\n  " + "\n  ".join(tracked)
    if text == _last_status_text:
        return "\n\n" + surface  # 窗口树未变化 → 只报操作面
    _last_status_text = text
    return "\n\n" + surface + "\n\n" + text


# （ 去识别）_append_status_block 已删（唯一调用方 click_element 已删）。


def _prune_tracked_popups():
    """遍历跟踪列表，移除已关闭/隐藏/offscreen 的窗口。同时更新 _popup_hwnd。"""
    global _popup_hwnd, _tracked_hwnds
    dead = set()
    for h in _tracked_hwnds:
        if not _is_window_alive(h):
            dead.add(h)
            continue
        rect = wintypes.RECT()
        if user32.GetWindowRect(h, ctypes.byref(rect)):
            if _is_offscreen(rect.left, rect.top):
                dead.add(h)
    _tracked_hwnds -= dead
    if _popup_hwnd and _popup_hwnd in dead:
        _popup_hwnd = None
    return dead


SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
HWND_TOP = 0
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102


def _mouse_lparam(hwnd, x, y):
    """将屏幕坐标转为 PostMessage 鼠标消息的 lParam（客户端坐标）"""
    if x is not None and y is not None:
        pt = wintypes.POINT(x, y)
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
        return (pt.y << 16) | (pt.x & 0xFFFF)
    return 0


def _make_lparam(vk, up=False):
    """构造键盘消息的 lParam"""
    scan = user32.MapVirtualKeyW(vk, 0)
    if up:
        return 1 | (1 << 30) | (1 << 31) | (scan << 16)
    return 1 | (scan << 16)


def _post_key(hwnd, vk):
    """Post WM_KEYDOWN + WM_KEYUP 到指定窗口（不激活窗口）"""
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, _make_lparam(vk))
    user32.PostMessageW(hwnd, WM_KEYUP, vk, _make_lparam(vk, True))


def _post_text(hwnd, text):
    """
    通过 PostMessage 向窗口发送文本。不激活窗口，不影响当前前台操作。
    - 普通字符: 只发 WM_CHAR（终端类程序会同时处理 WM_KEYDOWN）
    - 换行: WM_KEYDOWN/UP Enter
    - Ctrl+字母: WM_KEYDOWN + WM_CHAR + WM_KEYUP
    - 退格: WM_KEYDOWN/UP Backspace
    """
    _edit_like = _is_text_edit_hwnd(hwnd)
    # `\r\n`/`\r` 归一**只对编辑控件族**做（防止一张回车变两行）；
    #   **终端族保持原行为**（既有实现逐字把 `\r` 当 WM_CHAR 0x0D 发，不改它）。
    _body = text.replace("\r\n", "\n").replace("\r", "\n") if _edit_like else text
    for ch in _body:
        code = ord(ch)
        if ch == '\n':
            # 实测修正（本机 Win11 记事本 Edit 子窗 · **无间隔连发**）：
            #   **Edit/RichEdit 族**：换行必须发 `WM_CHAR 0x0D` —— 夹在正文中间也按顺序生效（`A1⏎A2` ✓）；
            #   原来发 `WM_KEYDOWN(VK_RETURN)` 时换行会**延后到后续文字之后**（`B1B2⏎` ✗ 顺序错）。
            #   **终端族/其它**：保持原路 `WM_KEYDOWN/UP VK_RETURN`（既有实现一直这么发，不许为了记事本改终端）。
            if _edit_like:
                user32.PostMessageW(hwnd, WM_CHAR, 0x0D, 0)
            else:
                _post_key(hwnd, 0x0D)  # 老路：终端等窗口族原样
        elif ch == '\b':
            _post_key(hwnd, 0x08)  # VK_BACK —— 退格必须走 KEYDOWN（WM_CHAR 0x08 实测被无视）
        elif 1 <= code <= 26:
            user32.PostMessageW(hwnd, WM_KEYDOWN, code, 1)
            user32.PostMessageW(hwnd, WM_CHAR, code, 0)
            user32.PostMessageW(hwnd, WM_KEYUP, code, 1)
        elif code > 0xFFFF:
            # 非 BMP（emoji…）：WM_CHAR 的 wParam 只有 16 位，直接发 ord() 会截断
            #   ⇒ 按 UTF-16 拆代理对，两张 WM_CHAR（同一消息队列，顺序不变）。
            _c = code - 0x10000
            user32.PostMessageW(hwnd, WM_CHAR, 0xD800 + (_c >> 10), 0)
            user32.PostMessageW(hwnd, WM_CHAR, 0xDC00 + (_c & 0x3FF), 0)
        else:
            user32.PostMessageW(hwnd, WM_CHAR, code, 0)


_TERMINAL_CLASSES = set(
    os.environ.get("MCP_TERMINAL_CLASSES",
                   "CMoTTY,PuTTY,ConsoleWindowClass,Vim,Mintty,Terminal,AfxFrameOrView140u").split(","))


def _find_terminal_child(parent_hwnd):
    """递归查找父窗口下的终端子控件，返回句柄或 None。"""
    result = None

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(child, lparam):
        nonlocal result
        if result:
            return False
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, buf, 256)
        if buf.value in _TERMINAL_CLASSES and user32.IsWindowVisible(child):
            rect = wintypes.RECT()
            user32.GetWindowRect(child, ctypes.byref(rect))
            w, h = _rect_size(rect)
            if w >= 50 and h >= 50:
                result = child
                return False
        user32.EnumChildWindows(child, enum_cb, 0)
        return True

    user32.EnumChildWindows(parent_hwnd, enum_cb, 0)
    return result


def _resolve_target():
    """统一目标窗口选择。
    - 弹窗存在（任何类型）：返回弹窗
    - 有终端子控件：终端子控件 > 主窗口
    - 无：直接主窗口"""
    if _popup_hwnd and _is_window_alive(_popup_hwnd):
        return _popup_hwnd
    if _focus_child_hwnd:
        child = _find_terminal_child(_focus_hwnd) if _focus_hwnd else None
        return child or _focus_hwnd
    # 3. 其他类型 → 直接主窗口（浏览器/编辑器/通用软件）
    return _focus_hwnd


def _post_mouse_move(hwnd, x, y, duration=0.3):
    """通过 PostMessage 后台移动鼠标"""
    _ = duration
    lp = _mouse_lparam(hwnd, x, y)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    return {"x": x, "y": y}


def _post_drag(hwnd, end_x, end_y, start_x=None, start_y=None, button="left", duration=0.5):
    """通过 PostMessage 后台拖拽鼠标"""
    _ = duration
    dn = {"left": (WM_LBUTTONDOWN, WM_LBUTTONUP),
          "right": (WM_RBUTTONDOWN, WM_RBUTTONUP),
          "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP)}
    down, up = dn.get(button, dn["left"])
    sx = start_x if start_x is not None else end_x
    sy = start_y if start_y is not None else end_y
    start_lp = _mouse_lparam(hwnd, sx, sy)
    end_lp = _mouse_lparam(hwnd, end_x, end_y)
    user32.PostMessageW(hwnd, down, 1, start_lp)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 1, end_lp)
    user32.PostMessageW(hwnd, up, 0, end_lp)
    return {"to": {"x": end_x, "y": end_y}, "button": button}


def _post_scroll(hwnd, clicks, x=None, y=None):
    """通过 PostMessage 后台滚动鼠标滚轮。

    x/y ＝ **屏幕坐标**（不传则退到窗口中心）；返回值带上**实际目标坐标**。
    说明：后台本来就用 x/y，但旧返回里没有坐标字段，分发层会把回执写成
    「光标位置」，看起来像"x/y 没生效"，实际正好相反。"""
    delta = 120 * clicks
    wparam = (delta << 16) & 0xFFFF0000
    if x is not None and y is not None:
        tx, ty = int(x), int(y)
    else:
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        tx = (rect.left + rect.right) // 2
        ty = (rect.top + rect.bottom) // 2
    user32.PostMessageW(hwnd, WM_MOUSEWHEEL, wparam, (ty << 16) | (tx & 0xFFFF))
    return {"clicks": clicks, "direction": "up" if clicks > 0 else "down",
            "method": "postmessage", "x": tx, "y": ty}


def _shell_open_desktop_item(keyword, verb="open"):
    """通过 Shell.Application COM 枚举桌面图标，用 os.startfile(.lnk) 独立启动。
    用 os.startfile 而非 InvokeVerb 避免子进程随本进程退出。"""
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except:
        pass
    try:
        import win32com.client
        shell = win32com.client.Dispatch("Shell.Application")
        desktop = shell.Namespace(0x00000000)
        for item in desktop.Items():
            name = str(item.Name)
            if keyword.lower() in name.lower():
                path = item.Path  # .lnk 文件全路径
                if path and os.path.exists(path):
                    os.startfile(path)
                    return {"found": True, "item": name, "path": path, "method": "os.startfile"}
        return {"found": False, "keyword": keyword}
    finally:
        try:
            pythoncom.CoUninitialize()
        except:
            pass


def _shell_scan_desktop():
    """扫描桌面所有项，返回名称和路径列表，不打开任何东西。"""
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except:
        pass
    try:
        import win32com.client
        shell = win32com.client.Dispatch("Shell.Application")
        desktop = shell.Namespace(0x00000000)
        items = []
        for item in desktop.Items():
            name = str(item.Name)
            path = item.Path
            items.append({"name": name, "path": path if path and os.path.exists(path) else ""})
        return items
    except Exception as e:
        return []
    finally:
        try:
            pythoncom.CoUninitialize()
        except:
            pass


# _shell_click_by_position 已删。


def _is_desktop_hwnd(hwnd):
    """是否桌面窗口（Progman/WorkerW）——原版 _sendinput_click 里 is_desktop 的语义。"""
    try:
        if not hwnd or hwnd == user32.GetDesktopWindow():
            return True
        _b = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, _b, 64)
        return _b.value in ("Progman", "WorkerW")
    except Exception:
        return False


def _click_mouse_event(x, y, button="left", click_count=1, human=False, speed=None, precise=False,
                       pre_click_hook=None, dwell_ms=None):
    """mouse_event 点击。**恢复原设计**：只有首击确实被拿去激活窗口时，才补第二击。

    原设计（见 docs/ARCHITECTURE.md「WM_MOUSEACTIVATE 激活补偿」）：
        if focus_gained and button == "left" and click_count == 1 and not is_desktop:
            time.sleep(0.1); _si_click(x, y, button, click_count)   # 补偿点击【另一次单击】
    本文件曾把 focus_gained 条件丢了、还做成 20ms 连击 ⇒ Windows 视作【双击】，
    而标准 Win32 按钮与标题栏 ✕ **不响应双击** ⇒ 关闭/取消/浏览全无反应（实测：
    MobaXterm 的自绘按钮能用，正因自绘控件把双击也当单击；explorer 标准控件全废）。
    现按原设计恢复三要素：① 先判定 focus_gained ② 命中才补 ③ 补的是 0.1s 后的独立单击。

    `dwell_ms`：落点后、按下前的停顿（自由传，0~2000ms）；不传＝老行为。"""
    _ensure_foreground(_focus_hwnd)
    fg_before = user32.GetForegroundWindow()
    # 首击会被"点击激活窗口"吃掉 ⇒ 需要补偿；目标已是前台 ⇒ 单击就是有效点击
    focus_gained = bool(_focus_hwnd) and fg_before != _focus_hwnd
    _mouse_event_click(x, y, button, click_count, human=human, speed=speed, precise=precise,
                       pre_click_hook=pre_click_hook, dwell_ms=dwell_ms)
    if focus_gained and button == "left" and click_count == 1 and not _is_desktop_hwnd(_focus_hwnd):
        time.sleep(0.1)                                  # 等激活完成（原版即 0.1s）
        _mouse_event_click(x, y, button, click_count, human=human, speed=speed, precise=precise,
                           dwell_ms=dwell_ms)             # 补偿点击（真正的第二次单击）
    time.sleep(0.1)
    return {"x": x, "y": y, "button": button, "clicks": click_count,
            "method": "mouse_event", "focus_gained": focus_gained,
            "note": ("首击被拿去激活窗口 → 已补第二击" if focus_gained else "")}


# _uia_invoke_by_text 已整段删除。


def _hwnd_text(h):
    b = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(h, b, 512)
    return b.value


def _hwnd_cls(h):
    b = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(h, b, 64)
    return b.value


def _dlg_kids(dlg):
    out = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumChildWindows(dlg, CB(lambda h, l: (out.append(h), True)[1]), 0)
    return out


def _si_kbd_raw(vk, scan, flags):
    """SendInput 发一个键（可指定虚拟键/扫描码/UNICODE 三种形态）。"""
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(vk, scan, flags, 0, None)
    user32_si.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))






# ══════════════════════════════════════════════════════════════════════════════
# 【纯像素观察/交互增强】
#   本块只加**能力与验证**，不写"某类应用勿用"。
#   为什么要有：动作回执只说「已投递」，调用方拿不到
#   「这一步到底生效没有」⇒ 只能靠"移动光标停放"掩盖失败，反而把失败放大成误触。
#   本块全部**纯像素**（截图 → 灰度 → 差分；不碰 OCR/UIA）。
#   ★默认值与老行为：**不传新参数 ＝ 老行为逐字不变**（verify_effect 默认 true 只影响
#   click/drag/transfer 的回执内容，不改变它们原有的动作语义）。
# ══════════════════════════════════════════════════════════════════════════════
_PX_PIXEL_DELTA = 8          # 单像素灰度差 > 8 才算「变了」（滤掉压缩/抖动噪声）
_PX_CHANGE_RATIO = 0.002     # 默认「生效」判据：变化像素占区域 ≥ 0.2%
_PX_SETTLE_MS = 120          # 动作后等画面稳定的默认毫秒数
_EXTRA_IMG = None            # 本轮动作产生的可选小图（PNG bytes + 说明）；handle_tool_call 入口清空

# 哪些动作类工具的回执要**固定带窗口矩形**（窗口尺寸会变、回执却不带
# rect ⇒ 之前算好的坐标全作废）。mouse_drag／set_window 的回执本来就报窗口位置，不再重复。
_ACTION_RECT_TOOLS = {"mouse_click", "mouse_move", "mouse_transfer", "hover", "scroll",
                      "send_keystrokes", "send_shortcut", "open_desktop_item",
                      "sequence"}


def _take_extra_image():
    """取走本轮「可选小图」（`effect_image` 的变化区域 / `hover` 的局部图）——附图出口消费一次。"""
    global _EXTRA_IMG
    _it = _EXTRA_IMG
    _EXTRA_IMG = None
    return _it


def _set_extra_image(png_bytes, note):
    """登记本轮可选小图（PNG bytes）；附图出口（`_tool_call_result`）会把它作为**唯一附图**返回。"""
    global _EXTRA_IMG
    try:
        import base64 as _b64
        _EXTRA_IMG = {"item": {"type": "image", "data": _b64.b64encode(png_bytes).decode("ascii"),
                               "mimeType": "image/png"}, "note": str(note or "")}
    except Exception:
        _EXTRA_IMG = None


def _px_rect_or_none(rect):
    """把 `verify_region`／`hover.rect` 解析成「可交给 `_px_snapshot` 的 rect」；空 ⇒ None。
    格式错**当场抛 ValueError**（不静默退回整屏——「要局部却拿到全屏」比报错更坏）。"""
    if rect in (None, "", {}, [], ()): 
        return None
    _parse_rect4(rect)      # 仅校验；失败由它抛 ValueError
    return rect


def _px_bgra_scan_hsv(arr_bgra):
    """BGRA 整帧 → 64×36 网格 HSV（各通道 0–255）——变化分统一走这一层（与 watch 模式同一规则）。"""
    import numpy as np
    h, w = arr_bgra.shape[:2]
    ys = np.clip(((np.arange(_SEE_SCAN_H) + 0.5) * h / _SEE_SCAN_H).astype(np.int32), 0, h - 1)
    xs = np.clip(((np.arange(_SEE_SCAN_W) + 0.5) * w / _SEE_SCAN_W).astype(np.int32), 0, w - 1)
    sub = arr_bgra[np.ix_(ys, xs)][..., [2, 1, 0]].astype(np.float32) / 255.0  # BGRA→RGB(0–1)
    r, g, b = sub[..., 0], sub[..., 1], sub[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    hh = np.zeros_like(mx)
    m = d > 0
    rm = (mx == r) & m
    gm = (mx == g) & m & ~rm
    bm = m & ~rm & ~gm
    dd = np.where(m, d, 1.0)
    hh[rm] = ((g[rm] - b[rm]) / dd[rm]) % 6
    hh[gm] = (b[gm] - r[gm]) / dd[gm] + 2
    hh[bm] = (r[bm] - g[bm]) / dd[bm] + 4
    hh = (hh * 60.0) % 360.0
    hue = hh / 360.0 * 255.0
    sat = np.where(mx == 0, 0.0, d / np.maximum(mx, 1e-6)) * 255.0
    val = mx * 255.0
    return np.stack([hue, sat, val], axis=-1)


_PX_LAST = {"rect": None, "ts": 0.0, "gray": None}
_PX_REUSE_S = 0.05        # 50ms 内同一块可直接从最近一帧裁（「一次抓屏、两处用」）


def _px_last_crop(rect):
    """从「最近一次抓屏」里裁出同一块（≤50ms 内有效）；不满足返回 None。

    为什么要有：生效判定（整屏 / verify_region）和局部判定（动作点邻域）各抓一次屏，
    但它们**可以共用一张图**——局部就是从大图里裁出来的。这里只裁灰度（局部差分只用灰度）。
    """
    try:
        if not rect or _PX_LAST.get("gray") is None:
            return None
        if (time.time() - float(_PX_LAST.get("ts") or 0.0)) > _PX_REUSE_S:
            return None
        import numpy as np
        x, y, w, h = _parse_rect4(rect)
        l0, t0, w0, h0 = _PX_LAST["rect"]
        if x < l0 or y < t0 or x + w > l0 + w0 or y + h > t0 + h0:
            return None
        crop = _PX_LAST["gray"][y - t0: y - t0 + h, x - l0: x - l0 + w]
        return {"gray": np.asarray(crop, dtype=np.uint8).copy(),
                "rect": (int(x), int(y), int(w), int(h)),
                "hsv": None, "source": "reuse"}
    except Exception:
        return None


def _px_phase_flush():
    """**动作边界**：作废「50ms 复用」缓存——动作之后的快照绝不许复用动作之前的帧。

    为什么必须：复用只允许发生在**同一瞬间**（局部判定与生效判定看的是同一次抓屏）；
      动作前/动作后是两个不同瞬间。若动作很快（相对点击、滚轮、dwell_ms=0）且两帧间隔 <50ms，
      后一帧会裁到动作前那张 ⇒ 差分恒为 0（假「未检出变化」、假「没生效」）。
      ⇒ 动作一开始就清掉缓存，保证 before/after 必定是两次不同的抓取。
    """
    try:
        _PX_LAST["ts"] = 0.0
        _PX_LAST["rect"] = None
        _PX_LAST["gray"] = None
    except Exception:
        pass


def _px_pw_grab(rect):
    """**PW 优先**：用 PrintWindow 抓整窗再裁出这块（返回 PIL RGB）；不适用/失败返回 None ⇒ 调用方回退 mss。

    为什么和 `capture_screenshot(region="window")` 同一条策略：窗口内容以窗口自己的画面为准，
      PW 被遮挡/在后台也能拿到；mss 只是兜底。**不再靠 `WindowFromPoint` 猜遮挡**——
      分层窗口/透明覆盖/全屏游戏/DWM 幽灵窗都会让覆盖判定出错，PW 直接拿窗口画面更可靠。
    """
    try:
        import numpy as np
        if not _shot_window_pw_enabled():
            return None                      # 与窗口截图共用同一开关（MCP_SHOT_WINDOW_PW=0 ⇒ 全局回退 mss）
        hwnd = int(_focus_hwnd or 0)
        if not hwnd or not _is_window_alive(hwnd):
            return None
        if _is_fullscreen_window(hwnd):
            return None                       # 全屏窗口：PW 可能给旧帧 ⇒ 回退 mss（实时画面）
        x, y, w, h = _parse_rect4(rect)
        wl, wt, ww, wh = _geo_now(hwnd) or (0, 0, 0, 0)
        if ww <= 0 or wh <= 0:
            return None
        if x < wl or y < wt or x + w > wl + ww or y + h > wt + wh:
            return None                       # 探针跨出窗口 ⇒ 不裁（回退 mss，不拿错内容）
        img = _capture_window(hwnd, None)     # 自己抓整窗（不共用截图函数）
        if img is None:
            return None
        crop = img.crop((x - wl, y - wt, x - wl + w, y - wt + h))
        arr = np.asarray(crop)
        if arr.ndim != 3 or arr.shape[2] < 3:
            return None
        if float(arr[:, :, :3].std()) < 1.0:  # 黑图/空图 ⇒ 不冒充结果
            return None
        return crop
    except Exception:
        return None


def _px_snapshot(rect=None, fresh=False, allow_pw=True):
    """**纯像素快照**（不碰 OCR/UIA）：返回
    `{"gray": uint8 二维, "rect": (l,t,w,h), "hsv": 64×36 HSV}`。

    rect＝`"x,y,w,h"`（也接受 4 元序列 / dict）＝**屏幕物理像素矩形（绝对坐标，不是相对窗口）**；
    不传 ⇒ **虚拟屏幕整屏**。失败抛异常（调用方决定降级还是报「判定不可用」——绝不静默当「没变化」）。

    两条复用/兜底（新增）：
      · **50ms 内同一块**直接从最近一帧裁（`_px_last_crop`）——生效判定与局部判定共用一次抓屏；
      · 窗口内区域**PW 优先**（`_px_pw_grab`，与 `capture_screenshot(region="window")` 同策略）：
        PW 拿不到（无窗口/跨出窗口/PW 黑图或失败）才回退 mss——否则 mss 会抓到遮挡者。
      `fresh=True` ⇒ 强制真抓（动态判定的第二帧用，绝不允许复用同一帧）。
    """
    import numpy as np
    if rect and not fresh:
        _re = _px_last_crop(rect)
        if _re is not None:
            return _re
    if rect and allow_pw:
        _pwcrop = _px_pw_grab(rect)
        if _pwcrop is not None:
            _rgb = np.asarray(_pwcrop)[:, :, :3].astype(np.uint8)
            _bgr = _rgb[:, :, ::-1]
            _gray = (_bgr[:, :, 2].astype(np.uint32) * 299 + _bgr[:, :, 1].astype(np.uint32) * 587
                     + _bgr[:, :, 0].astype(np.uint32) * 114) // 1000
            try:
                _a = np.dstack([_bgr, np.full(_rgb.shape[:2] + (1,), 255, np.uint8)])
                _hsv = _px_bgra_scan_hsv(_a)
            except Exception:
                _hsv = None
            _x, _y, _w, _h = _parse_rect4(rect)
            _snap = {"gray": _gray.astype(np.uint8),
                     "rect": (int(_x), int(_y), int(_w), int(_h)),
                     "hsv": _hsv, "source": "printwindow"}
            try:
                _PX_LAST.update(rect=_snap["rect"], ts=time.time(), gray=_snap["gray"])
            except Exception:
                pass
            return _snap
    with mss.mss() as sct:
        if rect:
            x, y, w, h = _parse_rect4(rect)
            mon = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
        else:
            _m = sct.monitors[0]          # 0＝虚拟屏幕（多屏/负原点也覆盖）
            mon = {"left": int(_m["left"]), "top": int(_m["top"]),
                   "width": int(_m["width"]), "height": int(_m["height"])}
        with _CAP_LOCK:                 # 同一时刻只允许一路抓屏（会话/任务/单帧共用）
            grab = sct.grab(mon)
    arr = np.frombuffer(grab.bgra, dtype=np.uint8)
    if arr.size != grab.width * grab.height * 4:
        raise RuntimeError("像素快照尺寸异常（%d ≠ %dx%dx4）" % (arr.size, grab.width, grab.height))
    arr = arr.reshape(grab.height, grab.width, 4)
    gray = (arr[..., 2].astype(np.uint32) * 299 + arr[..., 1].astype(np.uint32) * 587
            + arr[..., 0].astype(np.uint32) * 114) // 1000
    try:
        hsv = _px_bgra_scan_hsv(arr)
    except Exception:
        hsv = None
    _snap = {"gray": gray.astype(np.uint8),
             "rect": (int(mon["left"]), int(mon["top"]), int(mon["width"]), int(mon["height"])),
             "hsv": hsv, "source": "mss"}
    try:                                # 「一次抓屏、两处用」：供 50ms 内的同一块裁剪
        _PX_LAST.update(rect=_snap["rect"], ts=time.time(), gray=_snap["gray"])
    except Exception:
        pass
    return _snap


def _px_diff(before, after, pixel_delta=_PX_PIXEL_DELTA):
    """两个快照做差分 → `{diff_ratio, hsv_score, changed_px, bbox, shape_changed}`。

    `diff_ratio`＝灰度差超过阈值的像素占比（给 bbox 用）；`hsv_score`＝64×36 HSV 加权差
    （0–255，与 watch 模式同一把尺；对「同亮度换色」也能看见）。
    bbox 是**相对快照左上角**的 `(x, y, w, h)`；形状不同（窗口/分辨率变了）⇒ 直接判变化。
    """
    if not before or not after:
        return None
    a = before["gray"]
    b = after["gray"]
    if a.shape != b.shape:
        return {"diff_ratio": 1.0, "hsv_score": 255.0, "changed_px": int(a.size), "bbox": None,
                "shape_changed": True,
                "note": "快照形状变了（%s → %s）⇒ 直接判变化" % (a.shape, b.shape)}
    import numpy as np
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    mask = d > int(pixel_delta)
    n = int(mask.sum())
    ratio = n / float(mask.size or 1)
    bbox = None
    if n:
        ys, xs = np.nonzero(mask)
        bbox = (int(xs.min()), int(ys.min()),
                int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))
    hsv_s = None
    try:
        if before.get("hsv") is not None and after.get("hsv") is not None:
            hsv_s = _see_score(before["hsv"], after["hsv"])
    except Exception:
        hsv_s = None
    return {"diff_ratio": round(ratio, 6), "hsv_score": (None if hsv_s is None else round(hsv_s, 2)),
            "changed_px": n, "bbox": bbox, "shape_changed": False}


def _px_region_text(reg, source=None):
    """区域的一行描述：PW 窗口帧写「窗口」、mss 写「屏幕」、拿不准写「区域」；
    坐标按**图内坐标轴**写（屏幕坐标不写回执）。"""
    try:
        _tag = "窗口" if source == "printwindow" else ("屏幕" if source == "mss" else "区域")
        _p1 = _axis_from_screen_raw(reg[0], reg[1])
        _p2 = _axis_from_screen_raw(reg[0] + reg[2], reg[1] + reg[3])
        if _p1 is None or _p2 is None:
            return "%s（坐标见附图）" % _tag
        return "%s图内(%d,%d %dx%d)" % (_tag, int(round(_p1[0])), int(round(_p1[1])),
                                         int(round(_p2[0] - _p1[0])), int(round(_p2[1] - _p1[1])))
    except Exception:
        return "区域未知"


def _px_gray_png(arr):
    """灰度数组 → PNG bytes（PIL 惰性导入，本文件约定）。失败抛 RuntimeError。"""
    try:
        import io as _io
        from PIL import Image as _Image
        _im = _Image.fromarray(arr)
        _buf = _io.BytesIO()
        _im.save(_buf, "PNG")
        return _buf.getvalue()
    except Exception as _e:
        raise RuntimeError("灰度小图编码失败：%s" % (str(_e)[:60],))


def _px_rgb_png(rect):
    """彩色局部图（RGB PNG bytes）。hover 这类"给人看颜色/文字"的局部图走这条；
    差分仍用 _px_gray_png，二者职责分开——灰度图会压掉颜色与对比度。

    ★被别的窗盖住时改走 **PrintWindow 抓整窗再裁**（`_px_pw_grab`）——否则返回的
      "局部图"其实是遮挡者的画面（对模型是误导）；没被盖住仍走 mss（更快）。
    """
    import mss
    import mss.tools as _mss_tools
    _pwcrop = _px_pw_grab(rect)
    if _pwcrop is not None:
        return _pil_png_bytes(_pwcrop)
    x, y, w, h = _parse_rect4(rect)
    mon = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
    with mss.mss() as sct:
        with _CAP_LOCK:
            grab = sct.grab(mon)
    return _mss_tools.to_png(grab.rgb, grab.size)


def _px_gray_rgb_img(gray, tile_max=480):
    """灰度数组 → RGB PIL（用于拼「前后对比条带」；只缩放、不做增强）。"""
    from PIL import Image as _Image
    im = _Image.fromarray(gray).convert("RGB")
    w, h = im.size
    sc = min(1.0, float(tile_max) / float(max(w, h) or 1))
    if sc < 1.0:
        im = im.resize((max(16, int(round(w * sc))), max(16, int(round(h * sc)))), _Image.BILINEAR)
    return im


def _px_effect_region(rect=None):
    """生效判定的抓取区域：给了 rect 就用它；没给 ⇒ **聚焦窗口矩形**（PW 优先，与局部探针/返回图同通道）；
    无聚焦窗/取不到窗口矩形 ⇒ None（整屏，走 mss 回退）。"""
    if rect:
        return rect
    try:
        g = _geo_now(_surface_hwnd() or _focus_hwnd)
        if g and int(g[2]) > 0 and int(g[3]) > 0:
            return "%d,%d,%d,%d" % (int(g[0]), int(g[1]), int(g[2]), int(g[3]))
    except Exception:
        pass
    return None


def _px_effect_begin(rect=None):
    """动作前快照；失败返回 None（本轮判「不可用」，动作照常执行——绝不因判定失败而中止动作）。

    ★（0.1.58）：默认区域从「整屏 mss」改成「聚焦窗口矩形，PW 优先、失败回退 mss」——
      与局部探针/返回图同一条策略、同一通道；同一判定里 before/after 必须同通道（见 finish）。
    """
    try:
        _snap = _px_snapshot(_px_effect_region(rect))
        try:                                   # 供 B 帧（到位/按下前）直接裁，零额外抓取
            _PX_EFF_LAST.update(snap=_snap, ts=time.time())
        except Exception:
            pass
        return _snap
    except Exception:
        return None


def _px_rect_around(x, y, size=96):
    """以 (x,y) 为中心的屏幕矩形字符串（夹到虚拟屏幕内）；失败 None。"""
    try:
        import mss
        with mss.mss() as sct:
            m = sct.monitors[0]
            L, T, W, H = int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"])
        w = h = max(16, int(size))
        l = max(L, min(int(x) - w // 2, L + W - w))
        t = max(T, min(int(y) - h // 2, T + H - h))
        return "%d,%d,%d,%d" % (l, t, w, h)
    except Exception:
        return None


def _window_at_text(x, y):
    """光标处窗口的一句话（标题 + 类名）；纯 Win32，不做识别。"""
    try:
        hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return "空处"
        cb = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cb, 256)

        def _txt(h):
            b = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, b, 256)
            return (b.value or "").strip()

        title = _txt(hwnd) or _txt(user32.GetAncestor(hwnd, 2) or hwnd)
        return ("%s [%s]" % (title, cb.value)) if title else ("[%s]" % cb.value)
    except Exception:
        return "未知"


def _patch_match_score(src_gray, dst_gray):
    """源小图在目标区域里的最佳相似度（0–1）与偏移；纯像素（numpy），不做识别。"""
    try:
        import numpy as np
        from numpy.lib.stride_tricks import sliding_window_view
        s = np.asarray(src_gray, dtype=np.float32)[::2, ::2]
        d = np.asarray(dst_gray, dtype=np.float32)[::2, ::2]
        sh, sw = s.shape
        dh, dw = d.shape
        if sh < 6 or sw < 6 or dh < sh or dw < sw:
            return None
        win = sliding_window_view(d, (sh, sw))
        s0 = s - s.mean()
        s0n = float((s0 * s0).sum()) or 1.0
        dw0 = win - win.mean(axis=(2, 3), keepdims=True)
        num = (dw0 * s0).sum(axis=(2, 3))
        den = np.sqrt((dw0 * dw0).sum(axis=(2, 3)) * s0n)
        corr = num / np.maximum(den, 1e-6)
        idx = np.unravel_index(int(np.argmax(corr)), corr.shape)
        return round(float(corr[idx]), 3), (int(idx[1]) * 2, int(idx[0]) * 2)
    except Exception:
        return None


# ══════════ 模板匹配 ＋ 条件等待引擎（0.1.91） ══════════
#   模板来源：① 文件路径（PNG/JPG） ② "rect:x,y,w,h"（按最近附图的图内坐标，从当前屏幕抓这块）
#            ③ base64 PNG 字符串
#   匹配：纯 numpy FFT 归一化互相关（本机 Python 3.13 的 cv2 与 numpy 2.x ABI 不兼容，故不依赖 cv2）

def _tmpl_grab_rgb(rect=None):
    """抓一帧 RGB（numpy HxWx3）。rect='x,y,w,h'（屏幕物理）；None ⇒ 整个虚拟屏幕。"""
    import numpy as np
    import mss
    r = rect
    if r:
        try:
            x, y, w, h = _parse_rect4(r)
            mon = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
            with mss.mss() as sct:
                with _CAP_LOCK:
                    g = sct.grab(mon)
            return np.frombuffer(g.rgb, dtype=np.uint8).reshape((int(h), int(w), 3))
        except Exception:
            pass
    with mss.mss() as sct:
        with _CAP_LOCK:
            g = sct.grab(sct.monitors[0])
    return np.frombuffer(g.rgb, dtype=np.uint8).reshape((int(g.height), int(g.width), 3))


def _tmpl_gray(rgb):
    import numpy as np
    a = np.asarray(rgb, dtype=np.float32)
    return a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114


def _tmpl_load(spec):
    """模板加载（文件路径 / 'rect:x,y,w,h' / base64 PNG）→ np RGB；失败抛 ValueError。"""
    import base64 as _b64
    import io as _io
    import numpy as np
    from PIL import Image as _Image
    s = str(spec if spec is not None else "").strip()
    if not s:
        raise ValueError("template 不能为空")
    try:
        low = s.lower()
        if low.startswith("rect:"):
            _sr = _axis_rect4(s[5:].strip())
            _png = _px_rgb_png("%d,%d,%d,%d" % _sr)
        elif low.startswith("data:image") or (len(s) > 256 and " " not in s[:64] and not os.path.exists(s)):
            _b = s.split(",", 1)[-1] if "," in s else s
            _png = _b64.b64decode(_b)
        else:
            with open(s, "rb") as _f:
                _png = _f.read()
        _im = _Image.open(_io.BytesIO(_png)).convert("RGB")
        return np.array(_im, dtype=np.uint8)
    except ValueError:
        raise
    except Exception as _e:
        raise ValueError("template 加载失败（%r）：%s" % (s[:80], str(_e)[:120]))


def _tmpl_color_factor(patch, tmpl):
    """颜色一致性因子（0..1）：灰度 NCC 只比结构，红/蓝同形的图案会都算 1.0
    —— 这里按**色度差**补一刀（只对**本身有彩度**的模板生效；灰/白/黑模板不判颜色，避免误伤）。
    返回 1.0＝颜色一致；0.0＝颜色完全不同（该候选按不命中处理）。"""
    try:
        import numpy as np
        T = np.asarray(tmpl, dtype=np.float32)
        P = np.asarray(patch, dtype=np.float32)
        if P.shape != T.shape:
            return 1.0
        tc = np.stack([T[..., 0] - T[..., 1], T[..., 2] - T[..., 1]], axis=-1)
        if float(tc.std()) < 6.0:          # 模板基本无彩度（灰阶图标/文字）⇒ 不判颜色
            return 1.0
        pc = np.stack([P[..., 0] - P[..., 1], P[..., 2] - P[..., 1]], axis=-1)
        d = float(np.abs(tc - pc).mean()) * 2.0
        return max(0.0, 1.0 - d / 200.0)
    except Exception:
        return 1.0


def _tmpl_locate(rgb, tmpl, threshold=0.85, max_hits=8):
    """在 rgb 帧里找 tmpl（归一化互相关，FFT）。返回 [(score, x, y, w, h)]，帧像素坐标，score 降序。

    灰度 NCC 之后再按**色度**补一刀（`_tmpl_color_factor`）：同形不同色的图案不再互相冒充，
    `score` 是两者相乘后的值（所以"看着像但颜色不对"的候选会掉到阈值以下）。"""
    import numpy as np
    if rgb is None or tmpl is None:
        return []
    ih, iw = rgb.shape[:2]
    th, tw = tmpl.shape[:2]
    if th < 4 or tw < 4 or th > ih or tw > iw:
        return []
    I = _tmpl_gray(rgb)
    T = _tmpl_gray(tmpl)
    t = T - T.mean()
    tn = float(np.sqrt((t * t).sum()))
    if tn <= 1e-6:
        return []
    H, W = ih + th - 1, iw + tw - 1
    from numpy.fft import rfft2, irfft2
    # 互相关＝I 与 T 反转后的卷积；峰位对齐关系：conv[y,x] = corr[y-(th-1), x-(tw-1)]
    conv = irfft2(rfft2(I, s=(H, W)) * rfft2(t[::-1, ::-1], s=(H, W)), s=(H, W))[th - 1:ih, tw - 1:iw]

    def _win(x):
        c = np.cumsum(np.cumsum(x, axis=0), axis=1)
        c = np.pad(c, ((1, 0), (1, 0)))
        return c[th:, tw:] - c[:-th, tw:] - c[th:, :-tw] + c[:-th, :-tw]

    s1 = _win(I)
    s2 = _win(I * I)
    n = float(th * tw)
    # conv 里已经用的是**去均值后的模板** t ⇒ 分子直接就是 Σ I·(T-mean)
    num = conv
    var = np.maximum(s2 - (s1 * s1) / n, 0.0)
    den = np.sqrt(var) * tn
    # 近纯色区域局部方差≈0 ⇒ NCC 分母塌缩会给出假分数：方差太弱一律判 -1
    score = np.where(den < max(1e-6, tn * 0.5), -1.0, num / np.maximum(den, 1e-6))
    score = np.clip(score, -1.0, 1.0)
    flat = score.ravel()
    order = np.argsort(-flat)[: max(1, int(max_hits) * 8)]
    hits = []
    for idx in order:
        if flat[idx] < float(threshold):
            break
        y, x = divmod(int(idx), score.shape[1])
        if any(abs(x - hx) < max(4, tw * 0.6) and abs(y - hy) < max(4, th * 0.6) for _, hx, hy, _, _ in hits):
            continue
        _sc = float(flat[idx])
        try:                                   # 色度一刀：同形不同色 ⇒ 按不命中处理
            _pf = _tmpl_color_factor(rgb[y:y + th, x:x + tw], tmpl)
        except Exception:
            _pf = 1.0
        _sc2 = _sc * _pf
        if _sc2 < float(threshold):
            continue
        hits.append((round(_sc2, 3), int(x), int(y), int(tw), int(th)))
        if len(hits) >= max(1, int(max_hits) * 3):
            break
    hits.sort(key=lambda r: -r[0])
    return hits[: int(max_hits)]


def _tmpl_resize(arr, w, h):
    """把 numpy RGB 图缩到 w×h（LANCZOS，保纹理）。"""
    import numpy as np
    from PIL import Image as _Image
    im = _Image.fromarray(np.asarray(arr, dtype=np.uint8))
    if im.size == (int(w), int(h)):
        return np.asarray(im, dtype=np.uint8)
    return np.asarray(im.resize((int(w), int(h)), _Image.LANCZOS), dtype=np.uint8)


def _tmpl_locate_fast(rgb, tmpl, threshold=0.85, max_hits=8):
    """两段式定位：大帧先缩到 700 长边**粗搜**，再回原分辨率在命中点附近**精修**。

    为什么要它：整屏 1920×1080 直接跑 FFT 互相关要 0.5–0.8s，等图轮询/连点都会拖慢；
    粗搜把 FFT 尺寸降到 1/3 面积，精修只在几个小窗口里跑 ⇒ 目标 <200ms 且**召回不降**。
    小图（长边 ≤760）或模板短边 <12（缩小会糊）⇒ 直接全分辨率跑，不做两段（宁慢不误）。
    返回格式与 `_tmpl_locate` 完全一致：[(score, x, y, w, h)]，帧像素坐标，score 降序。
    """
    if rgb is None or tmpl is None:
        return []
    ih, iw = rgb.shape[:2]
    th, tw = tmpl.shape[:2]
    if th < 4 or tw < 4 or th > ih or tw > iw:
        return []
    if max(ih, iw) <= 760 or min(th, tw) < 12:
        return _tmpl_locate(rgb, tmpl, threshold, max_hits)
    s = 700.0 / float(max(ih, iw))
    fw, fh = max(1, int(round(iw * s))), max(1, int(round(ih * s)))
    sw, sh = max(4, int(round(tw * s))), max(4, int(round(th * s)))
    try:
        frame_s = _tmpl_resize(rgb, fw, fh)
        tmpl_s = _tmpl_resize(tmpl, sw, sh)
    except Exception:
        return _tmpl_locate(rgb, tmpl, threshold, max_hits)
    # 粗搜阈值放宽一点（缩图会丢高频细节，真实命中常掉到 0.7 左右），精修时再按真阈值判；
    # 候选数**不跟 max_hits 走**（调用方只要 1 个时也要给足候选——缩图粗搜里真命中排到第 7 名开外
    # 是实测过的，只取 6 个会漏）。精修只在几个小窗口里跑，代价很小。
    coarse = _tmpl_locate(frame_s, tmpl_s, max(0.30, float(threshold) - 0.20),
                          max(8, min(24, int(max_hits) * 3)))
    if not coarse:
        # 粗搜没命中不等于没有——退回全分辨率跑一次（正确率优先；这一步只在大帧偶发）
        return _tmpl_locate(rgb, tmpl, threshold, max_hits)
    margin = int(round(max(8.0, 6.0 / s)))
    hits = []
    for _sc, cx, cy, cw, ch in coarse:
        _ccx = int(round((cx + cw / 2.0) / s))
        _ccy = int(round((cy + ch / 2.0) / s))
        x0 = max(0, _ccx - tw // 2 - margin)
        y0 = max(0, _ccy - th // 2 - margin)
        x1 = min(iw, _ccx + tw // 2 + margin + 1)
        y1 = min(ih, _ccy + th // 2 + margin + 1)
        sub = rgb[y0:y1, x0:x1]
        if sub.shape[0] < th or sub.shape[1] < tw:
            continue
        for fs, fx, fy, fw2, fh2 in _tmpl_locate(sub, tmpl, threshold, 1):
            hits.append((fs, x0 + fx, y0 + fy, fw2, fh2))
    hits.sort(key=lambda r: -r[0])
    out = []
    for h in hits:
        if any(abs(h[1] - p[1]) < max(4, tw * 0.6) and abs(h[2] - p[2]) < max(4, th * 0.6) for p in out):
            continue
        out.append(h)
        if len(out) >= int(max_hits):
            break
    if not out:
        # 没有一个候选精修后过阈值 ⇒ 别信缩图，回全分辨率跑一次（宁可慢，不许漏）
        return _tmpl_locate(rgb, tmpl, threshold, max_hits)
    return out


def _tmpl_search_frame(region=None):
    """取一帧用于搜索：region（图内坐标 rect）优先，否则**整个虚拟屏幕**（找图默认全屏）。
    返回 (rgb, origin_x, origin_y)——origin 是帧左上角的**屏幕物理**坐标；
    region 给了但坐标轴不可用时直接抛 ValueError（不静默改成全屏）。"""
    if region:
        _sr = "%d,%d,%d,%d" % _axis_rect4(region)
        x, y, w, h = _parse_rect4(_sr)
        return _tmpl_grab_rgb(_sr), int(x), int(y)
    try:
        import mss
        with mss.mss() as sct:
            m = sct.monitors[0]
        return _tmpl_grab_rgb(None), int(m["left"]), int(m["top"])
    except Exception:
        return _tmpl_grab_rgb(None), 0, 0


def _wait_find_window(spec):
    """窗口条件：按标题子串 / 进程名 / hwnd 找一扇可见窗口；找不到返回 None。"""
    s = str(spec if spec is not None else "").strip()
    if not s:
        return None
    try:
        wins = enum_all_windows(False)
    except Exception:
        return None
    try:
        if s.lower().startswith("0x"):
            hw = int(s, 16)
            for w in wins:
                if int(w.get("hwnd") or 0) == hw:
                    return w
        if s.isdigit():
            for w in wins:
                if int(w.get("hwnd") or 0) == int(s):
                    return w
    except Exception:
        pass
    low = s.lower()
    for w in wins:
        t = str(w.get("title") or "").lower()
        p = str(w.get("process") or "").lower()
        if low in t or low in p:
            return w
    return None


def _wait_diff_ratio(a, b, tol=12):
    """两帧（同尺寸 RGB）变化像素占比。"""
    import numpy as np
    if a is None or b is None or a.shape != b.shape:
        return 1.0
    return float((np.abs(_tmpl_gray(a) - _tmpl_gray(b)) > float(tol)).mean())


def _wait_norm_conditions(conds):
    """条件规范化：允许单个对象或数组；校验 mode；返回 list。"""
    if conds in (None, "", []):
        return []
    if isinstance(conds, dict):
        conds = [conds]
    if not isinstance(conds, list):
        raise ValueError("conditions 必须是对象或数组")
    out = []
    valid = ("window_appear", "window_gone", "image_appear", "image_gone",
             "region_changed", "region_stable")
    for i, c in enumerate(conds):
        if not isinstance(c, dict):
            raise ValueError("conditions[%d] 必须是对象" % i)
        mode = str(c.get("mode") or "").strip().lower()
        if mode not in valid:
            raise ValueError("conditions[%d].mode 只支持 %s（收到 %r）" % (i, " / ".join(valid), c.get("mode")))
        c = dict(c)
        c["mode"] = mode
        if mode.startswith("window_") and not c.get("window"):
            raise ValueError("conditions[%d] 是 %s，必须给 window（标题子串/进程名/hwnd）" % (i, mode))
        if mode.startswith("image_") and not c.get("template"):
            raise ValueError("conditions[%d] 是 %s，必须给 template（文件路径 / rect:x,y,w,h / base64）" % (i, mode))
        out.append(c)
    return out


def _wait_race(conds, seconds, match="any", abort_check=None):
    """多条件监听：`match="any"`＝谁先达成立刻返回谁（竞速）；`"all"`＝全部同时满足才返回。
    `seconds` 是总上限（纯等待时长；conditions 为空时不走这里）。
    `abort_check`（可选，默认 None）＝每轮调一次，真值即收工——序列里用它接总超时/ESC/中止位。
    """
    _match = str(match or "any").strip().lower()
    if _match not in ("any", "all"):
        raise ValueError("match 只认 any / all（收到 %r）" % (match,))
    t0 = time.time()
    deadline = t0 + float(seconds)
    interval = 0.1
    for c in conds:
        c["_done"] = False
        c["_res"] = None
        if c["mode"].startswith("image_"):
            c["_tmpl"] = _tmpl_load(c["template"])
            c["_threshold"] = float(c.get("threshold", 0.85) or 0.85)
        if c["mode"].startswith("region_"):
            c["_threshold"] = float(c.get("threshold", 0.002) or 0.002)
            c["_stable_ms"] = int(c.get("stable_ms", 300) or 300)
            c["_base"] = None
            c["_prev"] = None
            c["_stable_since"] = None
    ticks = 0
    while True:
        ticks += 1
        _frames = {}       # 同一轮所有条件共用一次抓帧（窗口条件根本不抓图）

        def _frame_of(region):
            _k = str(region or "")
            if _k not in _frames:
                _frames[_k] = _tmpl_search_frame(region)
            return _frames[_k]

        def _el():
            return round(time.time() - t0, 2)

        for c in conds:
            if c.get("_done"):
                continue
            mode = c["mode"]
            _done = False
            _info = None
            if mode in ("window_appear", "window_gone"):
                w = _wait_find_window(c.get("window"))
                found = bool(w)
                c["_last"] = ("已出现：" + str(w.get("title") or "")[:40]) if found else "未出现"
                if (mode == "window_appear" and found) or (mode == "window_gone" and not found):
                    _done = True
                    _info = {"kind": "window", "window": w or {}}
            elif mode in ("image_appear", "image_gone"):
                frame, _fox, _foy = _frame_of(c.get("region"))
                hits = _tmpl_locate_fast(frame, c["_tmpl"], c.get("_threshold", 0.85), 1)
                c["_last"] = ("相似度 %.2f" % hits[0][0]) if hits else "未找到（0.00）"
                if (mode == "image_appear" and hits) or (mode == "image_gone" and not hits):
                    hit = None
                    if hits:
                        hit = [_fox + hits[0][1], _foy + hits[0][2], hits[0][3], hits[0][4]]
                    _done = True
                    _info = {"kind": "image", "hit": hit,
                             "score": (hits[0][0] if hits else 0.0),
                             "template": c.get("template")}
            elif mode == "region_changed":
                frame, _fox, _foy = _frame_of(c.get("rect"))
                if c["_base"] is None:
                    c["_base"] = frame
                    c["_last"] = "已建基线"
                else:
                    _r = _wait_diff_ratio(c["_base"], frame)
                    c["_last"] = "变化 %.2f%%" % (_r * 100.0)
                    if _r >= c["_threshold"]:
                        _done = True
                        _info = {"kind": "region_changed", "ratio": round(_r, 4)}
            elif mode == "region_stable":
                frame, _fox, _foy = _frame_of(c.get("rect"))
                if c["_prev"] is None:
                    c["_prev"] = frame
                    c["_stable_since"] = time.time()
                    c["_last"] = "等静止…"
                else:
                    _r = _wait_diff_ratio(c["_prev"], frame)
                    c["_prev"] = frame
                    if _r >= c["_threshold"]:
                        c["_stable_since"] = time.time()
                        c["_last"] = "仍在动（%.2f%%）" % (_r * 100.0)
                    else:
                        _stable_s = (time.time() - (c["_stable_since"] or time.time())) * 1000.0
                        c["_last"] = "静止 %.0fms" % _stable_s
                        if _stable_s >= c["_stable_ms"]:
                            _done = True
                            _info = {"kind": "region_stable", "stable_ms": int(_stable_s)}
            if _done:
                c["_done"] = True
                c["_res"] = _info
                if _match == "any":
                    _info["ticks"] = ticks
                    _info["elapsed"] = _el()
                    return True, c, _info
        if _match == "all" and all(c.get("_done") for c in conds):
            return True, None, {"kind": "all", "ticks": ticks, "elapsed": _el(),
                                "results": [{"mode": c["mode"], "info": c.get("_res")} for c in conds]}
        if abort_check is not None:
            try:
                if abort_check():
                    return False, None, {"ticks": ticks, "elapsed": _el(), "aborted": True,
                                         "states": [(c["mode"], c.get("_last", "?")) for c in conds]}
            except Exception:
                pass
        if time.time() >= deadline:
            return False, None, {"ticks": ticks, "elapsed": _el(),
                                 "states": [(c["mode"], c.get("_last", "?")) for c in conds]}
        time.sleep(max(0.0, min(interval, deadline - time.time())))


def _wait_attach_image(hit=None, red=1):
    """成功附图：抓一张带坐标轴刻度的当前帧，把命中处画框。

    `hit` 可以是单个屏幕矩形 [x,y,w,h]、矩形列表、或带 `screen` 键的行列表；
    前 `red` 个画**红框**（＝本次真正选中/点中的），其余画**绿框**（其他候选，方便核对）。
    图里同时带坐标轴刻度，可以直接按图里数字继续操作。
    """
    try:
        import base64 as _b64
        import io as _io
        from PIL import Image as _Image, ImageDraw as _ImageDraw
        if hit is None:
            _rects = []
        elif isinstance(hit, dict):
            _rects = [hit.get("screen")] if hit.get("screen") else []
        elif isinstance(hit, (list, tuple)) and hit and isinstance(hit[0], dict):
            _rects = [h.get("screen") for h in hit if h.get("screen")]
        elif isinstance(hit, (list, tuple)) and hit and not isinstance(hit[0], (list, tuple)):
            _rects = [hit]
        else:
            _rects = list(hit)
        item = _shot_item(region="window")
        if not item:
            item = _shot_item(region="fullscreen")
        if not item:
            return None
        im = _Image.open(_io.BytesIO(_b64.b64decode(item["data"]))).convert("RGB")
        _draw = _ImageDraw.Draw(im)
        for _i, _r in enumerate(_rects):
            try:
                if not _r or len(_r) < 4:
                    continue
                p1 = _axis_from_screen_raw(_r[0], _r[1])
                p2 = _axis_from_screen_raw(_r[0] + _r[2], _r[1] + _r[3])
                if p1 and p2:
                    _hot = _i < int(max(0, red))
                    _draw.rectangle([int(p1[0]), int(p1[1]), int(p2[0]), int(p2[1])],
                                    outline=((255, 0, 0) if _hot else (0, 190, 0)),
                                    width=(3 if _hot else 2))
            except Exception:
                continue
        buf = _io.BytesIO()
        im.save(buf, "PNG")
        item["data"] = _b64.b64encode(buf.getvalue()).decode("ascii")
        return item
    except Exception:
        return None


def _cursor_screen_xy():
    """当前光标屏幕坐标；取不到返回 None（pick=near 用）。"""
    try:
        _pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(_pt))
        return int(_pt.x), int(_pt.y)
    except Exception:
        return None


def _tmpl_rows(hits, ox, oy):
    """命中（帧内坐标）→ 行：相似度／屏幕矩形／图内中心点（轴未建立时 center=None）。"""
    rows = []
    for _sc, _hx, _hy, _hw, _hh in hits:
        _sx, _sy = int(ox + _hx), int(oy + _hy)
        _pc = _axis_from_screen_raw(_sx + _hw / 2.0, _sy + _hh / 2.0)
        rows.append({"score": _sc, "screen": [_sx, _sy, int(_hw), int(_hh)], "center": _pc,
                     "cx": int(_sx + _hw // 2), "cy": int(_sy + _hh // 2)})
    return rows


def _tmpl_pick_rows(rows, pick, dist=None):
    """按 pick 选行（单选）；`all`＝全部。

    · `best`＝相似度最高（默认）；
    · `near`／`far`＝离当前光标最近／最远（便捷档）；
    · **`dist=<数值>`＝按「距离值」选**：取离光标距离**最接近这个值**的那一个
      —— 给 0（最小）就是最近、给个很大的数（最大）就是最远、给中间值就是第 2/3/4 个
      （距离值由调用方看图估算）。给了 `dist` 时它优先于 `near/far/best` 的单选。
    """
    if not rows:
        return []
    _p = str(pick or "best").strip().lower()
    if _p == "all":
        return list(rows)
    _cur = _cursor_screen_xy()
    if dist is not None and _cur:
        try:
            _want = max(0.0, float(dist))
            return [min(rows, key=lambda r: abs(
                float((((r["cx"] - _cur[0]) ** 2 + (r["cy"] - _cur[1]) ** 2) ** 0.5)) - _want))]
        except Exception:
            pass
    if _p in ("near", "far") and _cur:
        _sorted = sorted(rows, key=lambda r: (r["cx"] - _cur[0]) ** 2 + (r["cy"] - _cur[1]) ** 2)
        if _p == "far":
            _sorted = list(reversed(_sorted))
        return [_sorted[0]]
    return [max(rows, key=lambda r: r["score"])]


def _row_loc(row):
    """一行的图内坐标文字（回执用；轴没建立就明说）。"""
    _p = (row or {}).get("center")
    return ("图内 (%d,%d)" % (int(round(_p[0])), int(round(_p[1])))) if _p else "（轴未建立，先整图截图）"


def _find_click_err(msg):
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _find_click_do(arguments):
    """find_click 工具实现：按模板**找图 / 按图点击 / 按两张图搬运**。

    · action=find：只找位置（所有匹配的**图内中心**＋相似度），不点击；
    · action=click：找到就点（`pick` 决定点最像的 / 离光标最近的 / **全部**），每处带局部纯像素判定；
    · action=transfer：`source`＋`target` 两张图＝起点/终点，等价 `mouse_transfer`（mode=drag|click|auto）。
    **不在里面等图**——等它出现/消失请用 `wait` 的 image_appear / image_gone 条件。
    附图默认开（`include_image=false` 可关）：红框＝本次选中/点中的，绿框＝其他候选。
    """
    _act = str(arguments.get("action") or "find").strip().lower()
    if _act not in ("find", "click", "transfer"):
        return _find_click_err("find_click：action 只认 find / click / transfer（收到 %r）"
                               % (arguments.get("action"),))
    try:
        _thr = float(arguments.get("threshold", 0.85) or 0.85)
    except Exception:
        return _find_click_err("threshold 必须是数字（0–1）")
    if not (0.0 < _thr <= 1.0):
        return _find_click_err("threshold 必须在 (0,1] 之间（收到 %r）" % (arguments.get("threshold"),))
    _pick = str(arguments.get("pick") or "best").strip().lower()
    if _pick not in ("best", "near", "far", "all"):
        return _find_click_err("pick 只认 best / near / far / all（收到 %r）" % (arguments.get("pick"),))
    _dist = arguments.get("dist")
    if _dist is not None:
        try:
            _dist = max(0.0, float(_dist))
        except Exception:
            return _find_click_err("dist（距离值）必须是数字（像素，≥0；给 0≈最近、给很大的数≈最远、中间值≈第 2/3/4 个）")
    _include = bool(arguments.get("include_image", True))
    _button = str(arguments.get("button") or "left").strip().lower()
    if _button not in ("left", "right"):
        return _find_click_err("button 只认 left / right（收到 %r）" % (arguments.get("button"),))
    _human = bool(arguments.get("human", False))
    _precise = bool(arguments.get("precise", False))
    _speed = arguments.get("speed")
    _region = arguments.get("region")
    # ── 一次抓帧（给了 region 但坐标轴不可用 ⇒ 明确报错，不静默改全屏）──
    try:
        _frame, _ox, _oy = _tmpl_search_frame(_region)
    except ValueError as _ve:
        return _find_click_err("find_click：%s" % _ve)
    if _frame is None:
        return _find_click_err("find_click：抓帧失败（检查屏幕/截图权限）")

    def _locate(spec, what):
        try:
            _tp = _tmpl_load(spec)
        except ValueError as _ve:
            raise ValueError("%s：%s" % (what, _ve))
        return _tmpl_rows(_tmpl_locate_fast(_frame, _tp, _thr, 8), _ox, _oy)

    # ══════ action=transfer：两张图＝起点/终点 ══════
    if _act == "transfer":
        if not arguments.get("source") or not arguments.get("target"):
            return _find_click_err("transfer 需要 source 与 target 两张模板（起点图/终点图）——"
                                   "等价 mouse_transfer，只是坐标换成图片中心")
        try:
            _src_rows = _locate(arguments.get("source"), "source")
            _tgt_rows = _locate(arguments.get("target"), "target")
        except ValueError as _ve:
            return _find_click_err("find_click：%s" % _ve)
        if not _src_rows:
            return _find_click_err("没找到起点模板（阈值 %.2f）——**没动手**（没找到就不点，避免盲操作）" % _thr)
        if not _tgt_rows:
            return _find_click_err("找到起点、没找到终点模板（阈值 %.2f）——**没动手**" % _thr)
        if _pick == "all":
            return _find_click_err("transfer 不支持 pick=all（起点/终点各取一个）；要逐个搬请分开调用")
        _s = _tmpl_pick_rows(_src_rows, _pick, _dist)[0]
        _t = _tmpl_pick_rows(_tgt_rows, _pick, _dist)[0]
        _mode = str(arguments.get("mode") or "auto").strip().lower()
        if _mode not in ("drag", "click", "auto"):
            return _find_click_err("mode 只认 drag / click / auto（收到 %r）" % (arguments.get("mode"),))
        try:
            _show_indicator(window=_get_focus_title(), action="按图搬运")
        except Exception:
            pass
        try:
            _res = mouse_transfer((_s["cx"], _s["cy"]), (_t["cx"], _t["cy"]),
                                  mode=_mode, button=_button, human=_human, speed=_speed, precise=_precise,
                                  verify_effect=bool(arguments.get("verify_effect", True)),
                                  settle_ms=int(arguments.get("settle_ms", _PX_SETTLE_MS) or 0),
                                  safe_park=arguments.get("safe_park", "") or "",
                                  undo=arguments.get("undo", "auto"))
        except Exception as _e:
            return _find_click_err("按图搬运失败：%s" % (str(_e)[:160],))
        _lines = ["按图搬运：起点 %s（相似度 %.3f）→ 终点 %s（相似度 %.3f）"
                  % (_row_loc(_s), _s["score"], _row_loc(_t), _t["score"]),
                  "请求模式=%s｜实际=%s｜%s"
                  % (_res["mode_requested"], _res["mode_used"],
                     "**生效（changed=true）**" if _res["changed"] else "⚠ **未生效（changed=false）**")]
        for _stg in (_res.get("stages") or []):
            _lines.append("  " + (_stg.get("line") or ""))
        if _res.get("undo"):
            _lines.append("撤销记录：" + _res["undo"])
        if _res.get("safe_park"):
            _lines.append(_res["safe_park"])
        _content = [{"type": "text", "text": "\n".join(_lines) + _status_block()}]
        if _include:
            _img = _wait_attach_image([_s, _t], red=1)   # 红框＝起点，绿框＝终点
            if _img:
                _content.append(_img)
        return {"content": _content}

    # ══════ action=find / click：单张模板 ══════
    if not arguments.get("template"):
        return _find_click_err("find/click 需要 template（文件路径 / rect:x,y,w,h / base64）；"
                               "两张图搬运用 action=transfer + source/target")
    try:
        _rows = _locate(arguments.get("template"), "template")
    except ValueError as _ve:
        return _find_click_err("find_click：%s" % _ve)
    if not _rows:
        return {"content": [{"type": "text", "text":
                "没找到模板（阈值 %.2f）%s" % (_thr, _status_block())}], "isError": (_act != "find")}
    if _act == "find":
        _lines = ["找到 %d 处（阈值 %.2f%s）" % (len(_rows), _thr,
                                             ("，只在 region 内找" if _region else ""))]
        for _i, _r in enumerate(_rows[:8]):
            _lines.append("  #%d 相似度 %.3f｜中心 %s｜大小 %dx%d"
                          % (_i, _r["score"], _row_loc(_r), _r["screen"][2], _r["screen"][3]))
        _lines.append("（要点击用 action=click；pick=best 最像／near 最近／far 最远／all 全部；"
                      "要挑中间那个就给 `dist`＝距离值，取离该值最近的一个）")
        _content = [{"type": "text", "text": "\n".join(_lines) + _status_block()}]
        if _include:
            _img = _wait_attach_image(_rows, red=1)
            if _img:
                _content.append(_img)
        return {"content": _content}
    # ── click：按 pick／dist 选目标，逐个「走到→（驻留）→点」，每处带局部纯像素判定 ──
    _sel = _tmpl_pick_rows(_rows, _pick, _dist)
    _count = max(1, int(arguments.get("count", 1) or 1))
    _dwell = max(0, min(2000, int(arguments.get("dwell_ms", 0) or 0)))
    _verify = bool(arguments.get("verify_effect", True))
    try:
        if _focus_hwnd:
            _ensure_foreground(_focus_hwnd)
    except Exception:
        pass
    try:
        _show_indicator(window=_get_focus_title(), action="按图点击")
    except Exception:
        pass
    _lines = ["按图点击：%d 处（pick=%s｜阈值 %.2f｜button=%s｜count=%d%s）"
              % (len(_sel), _pick, _thr, _button, _count,
                 ("｜dist=%.0fpx" % _dist) if _dist is not None else "")]
    if _dist is not None:
        _cur0 = _cursor_screen_xy()
        if _cur0 and _sel:
            _d0 = float((((_sel[0]["cx"] - _cur0[0]) ** 2 + (_sel[0]["cy"] - _cur0[1]) ** 2) ** 0.5))
            _lines.append("按距离值挑选：请求 dist=%.0fpx｜选中目标离光标 %.0fpx（候选 %d 个）"
                          % (_dist, _d0, len(_rows)))
    for _i, _r in enumerate(_sel):
        _lines.append("  #%d 相似度 %.3f｜中心 %s｜大小 %dx%d"
                      % (_i, _r["score"], _row_loc(_r), _r["screen"][2], _r["screen"][3]))
        _pxr = _before = None
        if _verify:
            _pxr, _before = _local_snapshot(_r["cx"], _r["cy"], 96)
        try:
            _glide_to(_r["cx"], _r["cy"], human=_human,
                      speed=(None if _speed is None else float(_speed)), precise=_precise)
            if _dwell:
                time.sleep(_dwell / 1000.0)
            _px_phase_flush()
            for _n in range(_count):
                _mouse_event_click_here(_button, 1)
                if _n + 1 < _count:
                    time.sleep(0.03)
        except Exception as _ce:
            _lines.append("  ⚠ 点击失败：%s" % (str(_ce)[:120],))
            return {"content": [{"type": "text", "text": "\n".join(_lines) + _status_block()}], "isError": True}
        if _verify and _pxr:
            _after = _local_after(_pxr, _before, retry_ms=200)
            _sem = _local_sem_line("点击点邻域", _before, _after,
                                   "命中：" + _window_at_text(_r["cx"], _r["cy"]), rect=_pxr)
            if _sem:
                _lines.append("  " + _sem)
    _content = [{"type": "text", "text": "\n".join(_lines) + _status_block()}]
    if _include:
        _boxes = list(_sel) + [r for r in _rows if all(r is not s for s in _sel)]
        _img = _wait_attach_image(_boxes, red=len(_sel))
        if _img:
            _content.append(_img)
    return {"content": _content}


def _local_snapshot(x, y, size=96):
    """调用前快照（**真抓**，不复用缓存）——动作边界的 before 必须是此刻画面。"""
    rect = _px_rect_around(x, y, size)
    if not rect:
        return None, None
    try:
        return rect, _px_snapshot(rect, fresh=True)
    except Exception:
        return rect, None


def _local_after(rect, before=None, retry_ms=0):
    """调用后快照（**真抓**）；给 before＋retry_ms ⇒ 画面还没更新就重抓，直到测到变化或超时。

    为什么重抓：渲染是异步的（滚轮/Chromium 合成帧滞后），抓太快会拿到动作前的画面
    ⇒ 本应「有变化」被报成 0.0%。重抓只在「未测到变化」时发生，最多 retry_ms。
    """
    if not rect:
        return None
    try:
        snap = _px_snapshot(rect, fresh=True)
    except Exception:
        return None
    if not before or not retry_ms:
        return snap
    try:
        t0 = time.time()
        while (time.time() - t0) * 1000.0 < float(retry_ms):
            _d = _px_diff(before, snap)
            if _d and float(_d.get("diff_ratio") or 0.0) >= 0.005:
                return snap
            time.sleep(0.1)
            try:
                snap = _px_snapshot(rect, fresh=True)
            except Exception:
                continue
        return snap
    except Exception:
        return snap


# ── 动作前/后判定的共用件（0.1.58）
#   A＝分发层调用前真抓；B＝动作内部"到位/停顿后、按下前"帧（从生效-before 帧裁，零额外抓）；
#   C＝动作结束后的**最终一帧**（就是附图要返回的那张，登记进 _FRAME 供附图复用）——判定裁它，不再单独抓。
_PX_EFF_LAST = {"snap": None, "ts": 0.0}
_LOCAL_CTX = {}


def _px_crop_snap(snap, rect):
    """从已经抓到的快照里裁出一块（屏幕绝对坐标）；裁不到返回 None。"""
    try:
        import numpy as np
        x, y, w, h = _parse_rect4(rect)
        _s = snap["rect"]
        l0, t0, w0, h0 = int(_s[0]), int(_s[1]), int(_s[2]), int(_s[3])
        if x < l0 or y < t0 or x + w > l0 + w0 or y + h > t0 + h0:
            return None
        crop = snap["gray"][y - t0: y - t0 + h, x - l0: x - l0 + w]
        return {"gray": np.asarray(crop, dtype=np.uint8).copy(),
                "rect": (int(x), int(y), int(w), int(h)),
                "hsv": None, "source": snap.get("source") or "mss"}
    except Exception:
        return None


def _final_window_frame(hwnd=None):
    """动作结束后的**最终一帧**（窗口 PW、内存）：登记 `_FRAME` 供附图复用，返回灰度快照。

    为什么这样省：工具附图本来就要截一张（`_shot_item` → `_frame_take` 复用通道）；
      判定直接裁这一张，就不再单独抓"后帧"。
    """
    try:
        _h = int(hwnd or _surface_hwnd() or _focus_hwnd or 0)
        if not _h or not _is_window_alive(_h):
            return None
        img = _capture_window(_h, None)
        if img is None:
            return None
        try:
            _frame_set(None, "window", "动作后帧", data=_pil_png_bytes(img), hwnd=_h)
        except Exception:
            pass
        import numpy as np
        arr = np.asarray(img)
        if arr.ndim != 3 or arr.shape[2] < 3:
            return None
        bgr = arr[:, :, :3][:, :, ::-1].astype(np.uint8)
        gray = (bgr[:, :, 2].astype(np.uint32) * 299 + bgr[:, :, 1].astype(np.uint32) * 587
                + bgr[:, :, 0].astype(np.uint32) * 114) // 1000
        return {"gray": gray.astype(np.uint8),
                "rect": _geo_now(_h),
                "hsv": None, "source": "printwindow"}
    except Exception:
        return None


def _after_from_final(rect, before=None, retry_ms=0):
    """动作后的判定"后帧"：优先用最终窗口截图（与附图同一张）；没测到变化就重截，最后一张仍复用。

    抓不到窗口（无聚焦/PW 失败）⇒ 退化为局部真抓（`_local_after`），不假装。
    """
    snap = _final_window_frame()
    crop = _px_crop_snap(snap, rect) if snap else None
    if crop is None:
        return _local_after(rect, before, retry_ms)
    if not before or not retry_ms:
        return crop
    try:
        t0 = time.time()
        while (time.time() - t0) * 1000.0 < float(retry_ms):
            _d = _px_diff(before, crop)
            if _d and float(_d.get("diff_ratio") or 0.0) >= 0.005:
                return crop
            time.sleep(0.1)
            snap = _final_window_frame()
            crop = _px_crop_snap(snap, rect) if snap else None
        return crop
    except Exception:
        return crop


def _local_diff_line(label, before, after, hit_note=""):
    """邻域前后差一行判定；失败返回空串。

    ★措辞口径：检出侧写「有变化」，未检出侧写「未检出变化」——
    像素差分为零只说明**这个邻域里没测到变化**，不等于"目标程序没接受操作"。
    """
    d = _px_diff(before, after) if (before and after) else None
    if d is None:
        return ""
    r = d.get("diff_ratio") or 0.0
    verdict = ("%s%s（%.1f%%）" % (label, "有变化" if r >= 0.005 else "未检出变化", r * 100))
    return verdict + (("；" + hit_note) if hit_note else "")


# ---------------------------------------------------------------- 局部变化语义（新增）
#
# 为什么加：原先只有「邻域有/没变化」一句话——
#   ① 像素差分为 0 时读起来像"确定没生效"，但画面里可能只是被动画掩着；
#   ② 有变化时只说一个百分比，说不清"哪一块变了"。
# 所以补两个**只加不改**的语义（基础判定 `_local_diff_line` 行为不变）：
#   · 变化块：16px 格内变化像素占比 → 连通域 → 屏幕绝对坐标的矩形（比一个大 bbox 更能说明位置）
#   · 邻域动态：隔 80ms 再抓一帧，判"这个邻域还在动吗"——**只回答动没动，不回答操作生效没**。
# 阈值一律用**比例**，不用"某个应用调出来的绝对像素数"（通用性）。

_LOCAL_GRID = 16            # 变化块的格边长（像素）
_LOCAL_CELL_RATIO = 0.12    # 一格内「变化像素占比」超过它，这格算变
_LOCAL_BLOCK_MAX = 3        # 回执最多列几块
_LOCAL_MOTION_GAP_MS = 80   # 动态判定两帧间隔
_LOCAL_MOTION_DELTA = 24    # 动态判定单像素宽阈值（滤压缩噪点）
_LOCAL_MOTION_RATIO = 0.01  # 动态判定：变化像素占比 ≥1% 才算"还在动"


def _local_blocks(before, after, rect, grid=None, cell_ratio=None,
                  min_cells=1, limit=None):
    """把邻域里的变化并成矩形块（纯像素，零识别）；返回屏幕绝对坐标 [(x,y,w,h,格数), ...]。

    与 `_px_diff` 的 bbox 的区别：先按格判"这格动不动"，再把连在一起的格子并成块，
    因此能说"左上和右下各变了一块"，而不是一个大框。全部被过滤时退回总框，绝不丢信息。
    `rect` 既收 `"x,y,w,h"` 字符串（`_px_rect_around` 的产物）也收 4 元序列。
    """
    try:
        import numpy as np
        a = before["gray"] if before else None
        b = after["gray"] if after else None
        if a is None or b is None or getattr(a, "shape", None) != getattr(b, "shape", None):
            return []
        try:
            if isinstance(rect, str):
                ox, oy = _parse_rect4(rect)[:2]
            else:
                ox, oy = int(rect[0]), int(rect[1])
        except Exception:
            return []
        h, w = a.shape
        g = max(4, int(grid or _LOCAL_GRID))
        rows, cols = h // g, w // g
        if rows < 2 or cols < 2:
            return []
        aa = a[: rows * g, : cols * g].astype(np.int16)
        bb = b[: rows * g, : cols * g].astype(np.int16)
        cell = np.abs(aa - bb) > int(_PX_PIXEL_DELTA)
        ratio = cell.reshape(rows, g, cols, g).mean(axis=(1, 3))
        changed = ratio > float(cell_ratio if cell_ratio is not None else _LOCAL_CELL_RATIO)
        if not changed.any():
            return []
        seen = np.zeros_like(changed, dtype=bool)
        groups = []
        ys, xs = np.nonzero(changed)
        for sy, sx in zip(ys.tolist(), xs.tolist()):
            if seen[sy, sx]:
                continue
            stack = [(sy, sx)]
            seen[sy, sx] = True
            y0 = y1 = sy
            x0 = x1 = sx
            n = 0
            while stack:
                cy, cx = stack.pop()
                n += 1
                if cy < y0: y0 = cy
                if cy > y1: y1 = cy
                if cx < x0: x0 = cx
                if cx > x1: x1 = cx
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < rows and 0 <= nx < cols and changed[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            groups.append((n, (x0, y0, x1 - x0 + 1, y1 - y0 + 1)))
        groups.sort(key=lambda t: -t[0])
        lim = int(limit if limit is not None else _LOCAL_BLOCK_MAX)
        out = []
        for n, (x0, y0, cw, ch) in groups[:lim]:
            if n < int(min_cells) and len(groups) > 1:
                continue
            out.append((ox + x0 * g, oy + y0 * g, cw * g, ch * g, n))
        if not out:                    # 全被过滤 ⇒ 退回"所有变化格的总框"，信息不丢
            n, (x0, y0, cw, ch) = groups[0]
            out = [(ox + x0 * g, oy + y0 * g, cw * g, ch * g, n)]
        return out
    except Exception:
        return []


def _local_blocks_line(blocks):
    """变化块 → 一行短描述（坐标按**图内坐标轴**）；空返回空串。"""
    if not blocks:
        return ""
    parts = []
    for b in blocks[:3]:
        p1 = _axis_from_screen(b[0], b[1])
        p2 = _axis_from_screen(b[0] + b[2], b[1] + b[3])
        if p1 is None or p2 is None:
            continue
        parts.append("图内 %d,%d %d×%d"
                     % (int(round(p1[0])), int(round(p1[1])),
                        int(round(p2[0] - p1[0])), int(round(p2[1] - p1[1]))))
    if not parts:
        return ""
    return "变化块 %d 处：%s" % (len(parts), "；".join(parts))


def _local_motion(rect, gap_ms=None, pixel_delta=None, moving_ratio=None):
    """同一邻域隔一会儿再抓一帧：判「这个邻域还在动吗」。

    **只回答动没动，不回答操作生效没**（两件事必须分开，否则会误导）。
    宽阈值滤压缩噪点；阈值是比例（1%），不写死像素数。失败返回 None ⇒ 调用方当"判定不可用"。
    """
    if not rect:
        return None
    try:
        gap = max(0, int(gap_ms if gap_ms is not None else _LOCAL_MOTION_GAP_MS))
        a = _px_snapshot(rect)
        time.sleep(gap / 1000.0)
        b = _px_snapshot(rect, fresh=True)   # 第二帧必须真抓（否则两帧同源 ⇒ 永远"静止"）
    except Exception:
        return None
    d = _px_diff(a, b, pixel_delta=int(pixel_delta if pixel_delta is not None else _LOCAL_MOTION_DELTA)) if (a and b) else None
    if not d:
        return None
    r = float(d.get("diff_ratio") or 0.0)
    mr = float(moving_ratio if moving_ratio is not None else _LOCAL_MOTION_RATIO)
    return {"ratio": r, "moving": bool(r >= mr), "gap_ms": gap}


def _local_sem_line(label, before, after, hit_note="", rect=None, motion=False):
    """局部判定一行 = 老判定 ＋（变化块）＋（该邻域还在动/已静止）。

    - 基础判定走 `_local_diff_line`，**行为与旧版一致**；
    - 传 `rect` 才加变化块（屏幕绝对坐标，不会和局部图坐标混）；
    - `motion=True` 只在基础判定是「未检出变化」时**才**多抓一帧判动态
      ⇒ 正常路径（检出变化）零额外开销；判出来只做"可信度提示"，不改基础结论。
    """
    line = _local_diff_line(label, before, after, hit_note)
    if not line or not rect:
        return line
    try:
        btxt = _local_blocks_line(_local_blocks(before, after, rect))
    except Exception:
        btxt = ""
    if btxt:
        line += "；" + btxt
    if motion and "未检出变化" in line:
        m = _local_motion(rect)
        if m:
            if m.get("moving"):
                line += "；该邻域仍在动（%.1f%%），结论仅供参考" % (float(m.get("ratio") or 0.0) * 100.0)
            else:
                line += "；该邻域静止"
    return line


def _drag_probe_begin(src, dst):
    """拖拽前：起点邻域 96、落点邻域 200（给模板匹配留搜索范围）。"""
    sr, ss = _local_snapshot(src[0], src[1], 96)
    dr, ds = _local_snapshot(dst[0], dst[1], 200)
    if not sr:
        return None
    return {"src_rect": sr, "dst_rect": dr, "src": ss, "dst": ds}


def _drag_probe_report(probe):
    """拖拽后：起点/落点邻域前后差 + 起点图形在落点的相似度。

    ★0.1.60：有分段帧就分段报——
      · `arrive`（到位/按下前）⇒ 起点段＝「移动前→按下前」；
      · `pre_release`/`final`（松手前/松手后）⇒ 落点段＝「松手前→松手后」；
      否则退回老口径（起点/落点 = 动作前 → 返回图那帧）。
    """
    lines = []
    if not probe:
        return lines
    _snap_fin = _final_window_frame()
    _arr = probe.get("arrive")        # B：到位/按下前
    _pre = probe.get("pre_release")   # C：松手前
    _fin = probe.get("final")         # D：松手后（返回图那张的裁剪）
    if _arr is not None:
        src_line = _local_sem_line("移动前→按下前", probe.get("src"), _arr, rect=probe.get("src_rect"))
    else:
        _ss = (_px_crop_snap(_snap_fin, probe.get("src_rect")) if (_snap_fin and probe.get("src_rect"))
               else _local_after(probe.get("src_rect"), probe.get("src"), 200))
        src_line = _local_sem_line("起点邻域", probe.get("src"), _ss, rect=probe.get("src_rect"))
    if _pre is not None and _fin is not None:
        dst_line = _local_sem_line("松手前→松手后", _pre, _fin, rect=probe.get("dst_rect"))
        _dst_for_match = _fin
    else:
        _ds = (_px_crop_snap(_snap_fin, probe.get("dst_rect")) if (_snap_fin and probe.get("dst_rect"))
               else _local_after(probe.get("dst_rect"), probe.get("dst"), 200))
        dst_line = _local_sem_line("落点邻域", probe.get("dst"), _ds, rect=probe.get("dst_rect"))
        _dst_for_match = _ds
    if src_line:
        lines.append(src_line)
    if dst_line:
        lines.append(dst_line)
    if probe.get("src") and _dst_for_match:
        m = _patch_match_score(probe["src"]["gray"], _dst_for_match["gray"])
        if m:
            lines.append("起点图形在落点区域的相似度：%.2f（偏移 %+d,%+d）"
                         % (m[0], m[1][0], m[1][1]))
    return lines


def _transfer_probe_report(probe):
    """搬移（mouse_transfer）的局部变化：按**动作原语分段**（0.1.61）。

    · click 模式＝**拾取段**（移动前→拾取后，源邻域）＋**放置段**（放置前→放置后，目标邻域）；
    · drag 模式＝**移动前→按下前**（源邻域）＋**松手前→松手后**（目标邻域），与 mouse_drag 同口径；
    · 源/目标不是同一块画面，**不硬造"移动段"**——"东西跟到没有"由起点图形在落点的模板相似度回答；
    · 分段帧不足（像素判定关闭／auto 撤销后换模式／抓帧失败）⇒ 退回老口径（起点/落点＝动作前→返回图），
      **不拿错帧凑**。
    """
    lines = []
    if not probe:
        return lines
    _snap_fin = _final_window_frame()
    _used = probe.get("mode_used")
    _src = probe.get("src")
    _dst = probe.get("dst")
    _src_rect = probe.get("src_rect")
    _dst_rect = probe.get("dst_rect")
    _arr = probe.get("arrive")          # drag：按下前（从生效-before 帧裁，零额外抓取）
    _pick = probe.get("pick_after")     # click：拾取后（钩子真抓）
    _pre = probe.get("pre_release")     # drag：松手前（钩子真抓）
    _pb = probe.get("place_before")     # click：放置前（第二次生效-before 帧裁）
    _fin = probe.get("final")           # 松手后/放置后（返回图那张的裁剪）
    if _used == "click" and _pick is not None:
        _l1 = _local_sem_line("拾取段（移动前→拾取后）", _src, _pick, rect=_src_rect)
    elif _used == "drag" and _arr is not None:
        _l1 = _local_sem_line("移动前→按下前", _src, _arr, rect=_src_rect)
    else:
        _ss = (_px_crop_snap(_snap_fin, _src_rect) if (_snap_fin and _src_rect)
               else _local_after(_src_rect, _src, 200))
        _l1 = _local_sem_line("起点邻域", _src, _ss, rect=_src_rect)
    if _l1:
        lines.append(_l1)
    if _used == "drag" and _pre is not None and _fin is not None:
        _l2 = _local_sem_line("松手前→松手后", _pre, _fin, rect=_dst_rect)
        _dst_for_match = _fin
    elif _used == "click" and _pb is not None and _fin is not None:
        _l2 = _local_sem_line("放置段（放置前→放置后）", _pb, _fin, rect=_dst_rect)
        _dst_for_match = _fin
    else:
        _ds = (_px_crop_snap(_snap_fin, _dst_rect) if (_snap_fin and _dst_rect)
               else _local_after(_dst_rect, _dst, 200))
        _l2 = _local_sem_line("落点邻域", _dst, _ds, rect=_dst_rect)
        _dst_for_match = _ds
    if _l2:
        lines.append(_l2)
    if _src and _dst_for_match:
        m = _patch_match_score(_src["gray"], _dst_for_match["gray"])
        if m:
            lines.append("起点图形在落点区域的相似度：%.2f（偏移 %+d,%+d）"
                         % (m[0], m[1][0], m[1][1]))
    return lines


def _px_effect_finish(before, rect=None, settle_ms=_PX_SETTLE_MS, threshold=None, effect_image=False,
                      samples=1, span_ms=0, strip=False):
    """动作后采样 + 差分 → 生效判定 dict（失败也返回 dict，带 unavailable；绝不抛）。

    `samples>1` ＝**动作后短时连拍**：在 `settle_ms` 起、总跨度 `span_ms` 内再取若干帧，
    取变化最大的一帧作判定（避免动画/延迟把一次真实生效误判成「没变化」）。
    `strip=True` ⇒ 把「前帧＋各后帧」拼成一张小条带图随回执返回（与抽帧拼图共用 `_compose_sheet`）。
    """
    try:
        time.sleep(max(0.0, min(5.0, float(settle_ms or 0) / 1000.0)))
    except Exception:
        pass
    _th = _PX_CHANGE_RATIO if threshold is None else max(0.0, float(threshold))
    if before is None:
        return {"unavailable": "动作前快照不可用（像素判定已跳过）", "threshold": _th}
    # ★（0.1.58）：after 必须与 before 同区域＋同通道（PW↔PW / mss↔mss），否则渲染差异会假报变化。
    if rect is None and before.get("rect"):
        rect = "%d,%d,%d,%d" % tuple(int(v) for v in before["rect"])
    _allow_pw = (before.get("source") == "printwindow")
    _n = max(1, min(8, int(samples or 1)))
    _span = max(0.0, min(5.0, float(span_ms or 0) / 1000.0))
    afters = []
    times = []
    for _k in range(_n):
        if _k:
            _extra = _span * _k / float(max(1, _n - 1))
            if _extra > 0:
                time.sleep(_extra)
        try:
            afters.append(_px_snapshot(rect, fresh=True, allow_pw=_allow_pw))   # 连拍每帧必须真抓（绝不许复用上一帧）
            _off = (_span * _k / float(max(1, _n - 1))) if _n > 1 else 0.0
            times.append(int(round(float(settle_ms or 0) + _off)))
        except Exception as _e:
            afters.append(None)
            times.append(None)
    best = None
    best_i = -1
    scores = []
    for _i, _aft in enumerate(afters):
        _rep = _px_diff(before, _aft)
        if _rep is None:
            scores.append(None)
            continue
        scores.append({"diff_ratio": _rep.get("diff_ratio"), "hsv_score": _rep.get("hsv_score")})
        if best is None:
            best, best_i = _rep, _i
        else:
            _cur_key = (float(_rep.get("diff_ratio") or 0.0), float(_rep.get("hsv_score") or 0.0))
            _best_key = (float(best.get("diff_ratio") or 0.0), float(best.get("hsv_score") or 0.0))
            if _cur_key > _best_key:
                best, best_i = _rep, _i
    if best is None:
        return {"unavailable": "动作后快照全部失败", "threshold": _th}
    rep = best
    rep["threshold"] = _th
    if not rep.get("shape_changed"):
        rep["changed"] = bool(float(rep.get("diff_ratio") or 0.0) >= _th
                              or float(rep.get("hsv_score") or 0.0) >= _SEE_CUT_ABS)
    rep["region"] = tuple(int(v) for v in before["rect"])
    rep["source"] = before.get("source")   # 判定用的通道（窗口PW / 屏幕mss）——回执要如实写
    rep["samples"] = _n
    rep["peak_at_ms"] = times[best_i]
    rep["frame_scores"] = scores
    _best_after = afters[best_i]
    if strip and rep.get("changed"):
        try:
            _tiles = [(_px_gray_rgb_img(before["gray"]), "before")]
            for _i, _aft in enumerate(afters):
                if _aft is None:
                    continue
                _sc = scores[_i]
                _lab = "after%d" % (_i + 1)
                if _sc and _sc.get("hsv_score") is not None:
                    _lab += " s%.0f" % float(_sc["hsv_score"])
                _tiles.append((_px_gray_rgb_img(_aft["gray"]), _lab))
            _png, _meta = _compose_sheet(_tiles, label_prefix="")
            _set_extra_image(_png, "生效判定·前后对比条带：%d 帧 ｜ 单格 %d×%d ｜ 网格 %d×%d（%s）"
                             % (_meta["frames"], _meta["tile"][0], _meta["tile"][1],
                                _meta["grid"][0], _meta["grid"][1],
                                "；".join("f%d s=%s" % ((_i + 1), (scores[_i] or {}).get("hsv_score"))
                                          for _i in range(len(scores)) if scores[_i])))
            rep["strip"] = _meta
        except Exception:
            pass
    elif effect_image and rep.get("changed") and rep.get("bbox") and _best_after is not None:
        try:
            _arr = _best_after["gray"]
            _bx, _by, _bw, _bh = rep["bbox"]
            _pad = 16
            _x0 = max(0, _bx - _pad); _y0 = max(0, _by - _pad)
            _x1 = min(_arr.shape[1], _bx + _bw + _pad)
            _y1 = min(_arr.shape[0], _by + _bh + _pad)
            _crop = _arr[_y0:_y1, _x0:_x1]
            _sx = int(before["rect"][0]) + _x0
            _sy = int(before["rect"][1]) + _y0
            _set_extra_image(_px_gray_png(_crop),
                             "生效判定·变化区域小图：屏幕 (%d,%d) %dx%d ｜ diff=%.2f%% ｜ 区域 %s"
                             % (_sx, _sy, int(_crop.shape[1]), int(_crop.shape[0]),
                                float(rep["diff_ratio"]) * 100.0, _px_region_text(rep["region"])))
            rep["image_screen_rect"] = [_sx, _sy, int(_crop.shape[1]), int(_crop.shape[0])]
        except Exception:
            pass
    return rep


def _px_effect_line(rep):
    """把生效判定 dict 渲染成回执里的一行（多行也行）。失败/不可用都**如实写**。"""
    if not rep:
        return "（像素判定不可用）"
    if rep.get("unavailable"):
        return "（生效判定不可用：%s；动作本身已执行）" % (rep["unavailable"],)
    _pct = float(rep.get("diff_ratio") or 0.0) * 100.0
    _thp = float(rep.get("threshold") or _PX_CHANGE_RATIO) * 100.0
    _reg = _px_region_text(rep.get("region"), rep.get("source"))
    _bb = rep.get("bbox")
    _bbtxt = ("；变化 bbox 图内(%d,%d) %dx%d" % _bb) if _bb else ""
    _hsv_txt = ""
    if rep.get("hsv_score") is not None:
        _hsv_txt = "；变化分 %.1f/255" % float(rep["hsv_score"])
    _sample_txt = ""
    if int(rep.get("samples") or 1) > 1:
        _sample_txt = "；连拍 %d 帧，峰值在 +%sms" % (int(rep["samples"]), rep.get("peak_at_ms"))
    if rep.get("changed"):
        return ("**changed=true**（变化像素 %.2f%% ≥ 阈值 %.2f%%%s%s；区域 %s）"
                % (_pct, _thp, _hsv_txt, _sample_txt, _reg))
    return ("**changed=false** —— 区域 %s 内没有可见变化（%.2f%% < 阈值 %.2f%%%s%s）。\n"
            "    ⚠ 目标可能没接受这次动作（例如仍处于「拾取/按住」状态、或落在不可交互处）；"
            "**建议先别移动光标**：重试一次，或先取消（点回/ESC）确认状态后再继续。"
            % (_reg, _pct, _thp, _hsv_txt, _sample_txt))


def _parse_xy(v, what="坐标"):
    """把 `"x,y"`、`[x,y]`、`{"x":..,"y":..}` 解析成 `(int, int)`；失败抛 ValueError。"""
    try:
        if isinstance(v, dict):
            return int(v["x"]), int(v["y"])
        if isinstance(v, (list, tuple)):
            if len(v) != 2:
                raise ValueError
            return int(v[0]), int(v[1])
        _parts = [p for p in re.split(r"[,\s]+", str(v).strip()) if p != ""]
        if len(_parts) != 2:
            raise ValueError
        return int(float(_parts[0])), int(float(_parts[1]))
    except Exception:
        raise ValueError("%s 必须是「x,y」/ [x,y] / {x,y}，收到 %r" % (what, v))


def _window_rect_line():
    """窗口矩形一行（**纯 Win32 读数，不做窗口枚举**）——动作回执固定带它；
    坐标按**图内坐标轴**写（屏幕上不写数字；轴外则只给标题/进程）。"""
    try:
        h = _surface_hwnd() or _focus_hwnd
        if not h or not _is_window_alive(h):
            return ""
        r = wintypes.RECT()
        if not user32.GetWindowRect(h, ctypes.byref(r)):
            return ""
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, buf, 256)
        title = buf.value or "(无标题)"
        _p1 = _axis_from_screen_raw(r.left, r.top)
        _p2 = _axis_from_screen_raw(r.right, r.bottom)
        if _p1 is None or _p2 is None:
            return "窗口：%s [%s]" % (title, _fg_proc_name(h) or "?")
        if _AXIS:
            _cw, _ch = _AXIS["canvas"]
            _p1 = (min(max(_p1[0], 0.0), float(_cw)), min(max(_p1[1], 0.0), float(_ch)))
            _p2 = (min(max(_p2[0], 0.0), float(_cw)), min(max(_p2[1], 0.0), float(_ch)))
        return ("窗口在图内 (%d,%d)–(%d,%d)（%d×%d）｜%s [%s]"
                % (int(round(_p1[0])), int(round(_p1[1])),
                   int(round(_p2[0])), int(round(_p2[1])),
                   int(round(_p2[0] - _p1[0])), int(round(_p2[1] - _p1[1])),
                   title, _fg_proc_name(h) or "?"))
    except Exception:
        return ""


def _window_query_matches(hwnd, keyword="", class_name="", process="", pid=None,
                          exact=False, case_sensitive=False, regex=False):
    """判断「已经记住的那扇窗」是否满足本次查询（规则沿用 `find_window_by_title` 的匹配规则）。

    ★ 用于 `focus_window` 的**已聚焦短路**：只读 GetWindowTextW／GetClassNameW／pid，
    **不做任何窗口枚举**（实战反馈：一次 focus_window 偶发 5~6s，大头就是重复枚举）。
    """
    try:
        if pid is not None:
            _p = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(_p))
            return int(_p.value) == int(pid)
        _b = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, _b, 512)
        _c = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, _c, 256)
        _proc = _fg_proc_name(hwnd) or ""
        for _fv, _sv in ((_b.value, keyword), (_c.value, class_name), (_proc, process)):
            if not _sv:
                continue
            _fv = _strip_invisible(_fv)
            _sv = _strip_invisible(_sv)
            if regex:
                import re as _re
                _flags = 0 if case_sensitive else _re.IGNORECASE
                try:
                    if _re.search(_sv, _fv, _flags):
                        return True
                except Exception:
                    pass
            elif exact:
                if (_fv == _sv) if case_sensitive else (_fv.lower() == _sv.lower()):
                    return True
            else:
                if (_sv in _fv) if case_sensitive else (_sv.lower() in _fv.lower()):
                    return True
        return False
    except Exception:
        return False


def hover(x, y, dwell_ms=600, rect=None, human=False, speed=None, precise=False, on_arrive=None):
    """**悬停**：把光标移到 (x,y) 并停留 `dwell_ms`，用于读取悬停提示/高亮/预览。

    ★ P0（实战反馈：读 tooltip 要「move → wait → 截图 → 裁剪」四步，于是调用方
    下意识就省了）⇒ 收成一步。**纯像素**：只移动＋等待＋截图，不点击、不识别（OCR/UIA 不碰）。
    `rect` 可选：`"x,y,w,h"`（屏幕物理像素）——给了就只回那一小块（省 token）；不传＝照常回整张附图。
    """
    _require_focus_target()
    _ensure_foreground(_focus_hwnd)
    _px_rect = _px_rect_or_none(rect)
    _t0 = time.perf_counter()
    _mv = _glide_to(x, y, human=human, speed=speed, precise=precise)
    try:                                   # B 帧（到达后、停留前）——分辨"到达就有反应"还是"停留才出提示"
        if on_arrive is not None:
            on_arrive()
    except Exception:
        pass
    _dwell = max(0, min(5000, int(dwell_ms or 0)))
    time.sleep(_dwell / 1000.0)
    out = {"x": int(x), "y": int(y), "dwell_ms": _dwell, "method": "SetCursorPos",
           "elapsed": round(time.perf_counter() - _t0, 3),
           "moved": {k: _mv.get(k) for k in ("duration", "steps", "elapsed")}}
    if _px_rect:
        try:
            _reg = _parse_rect4(_px_rect)
            _set_extra_image(_px_rgb_png(_px_rect),
                             "hover 局部图（彩色原图，悬停保持不动）：屏幕 (%d,%d) %dx%d ｜ dwell=%dms"
                             % (int(_reg[0]), int(_reg[1]), int(_reg[2]), int(_reg[3]), _dwell))
            out["image_rect"] = [int(_reg[0]), int(_reg[1]), int(_reg[2]), int(_reg[3])]
        except Exception as _e:
            out["image_error"] = str(_e)[:80]
    return out


def mouse_transfer(source, target, mode="auto", button="left", human=False, speed=None, precise=False,
                   verify_effect=True, settle_ms=_PX_SETTLE_MS, verify_region="", change_threshold=None,
                   effect_image=False, effect_samples=1, effect_span_ms=0, effect_strip=False,
                   safe_park="", undo="auto", on_pick_done=None, pre_release_hook=None):
    """**通用「搬移对象」原语**：drag（按下→移动→松开）／click（点起→移动→点落）／auto（按验证切换）。

    不写"某类应用勿用"，把三种语义都提供，**用生效判定
    让调用方自己看结果**（每个阶段前后各取一帧做纯像素差分）。

    · `mode="auto"`：先试 `drag`；回执 `changed=false` 时做**安全撤销**（松键/点回起点/可选 ESC，
      由 `undo` 决定）再试 `click`；两种结果**都如实回**。
    · `click` 模式分两段：**拾取**（点起点）→ **放置**（点终点）。auto 的第二次尝试在「拾取阶段
      无变化」时**不会往目标盲点**（直接记失败）。
    · `verify_effect=false` ⇒ 只执行动作、不做像素判定（`changed` 记 true，回执注明判定已关闭）。
    · `safe_park` 可选：结束后把光标停到调用方给的空场坐标（**不点击**）。
    """
    src = _parse_xy(source, "source")
    tgt = _parse_xy(target, "target")
    mode = str(mode or "auto").strip().lower()
    if mode not in ("drag", "click", "auto"):
        raise ValueError("mode 只支持 drag / click / auto（收到 %r）" % (mode,))
    button = str(button or "left").strip().lower()
    if button not in ("left", "right"):
        raise ValueError("mouse_transfer 只支持 button=left / right（收到 %r）" % (button,))
    undo_v = str(undo or "auto").strip().lower()
    if undo_v not in ("auto", "release", "click_back", "escape", "none"):
        raise ValueError("undo 只支持 auto / release / click_back / escape / none（收到 %r）" % (undo,))
    _px_rect = _px_rect_or_none(verify_region)
    _require_focus_target()
    _ensure_foreground(_focus_hwnd)

    def _do_drag():
        _glide_to(src[0], src[1], human=human, speed=speed, precise=precise)
        _human_pause(0.03, 0.10, 0.06)
        before = _px_effect_begin(_px_rect) if verify_effect else None
        _px_phase_flush()      # 动作边界：清复用缓存
        if button == "left":
            down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        else:
            down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
        user32_si.mouse_event(down, 0, 0, 0, 0)
        _held_mark_btn(button, True)
        try:
            _human_pause(0.08, 0.28, 0.13)
            _glide_to(tgt[0], tgt[1], human=human, speed=speed, drag=True, precise=precise)
            _human_pause(0.05, 0.20, 0.09)
            try:                                   # C 帧（松手前、落点邻域）——给"松手前→松手后"用
                if pre_release_hook is not None:
                    pre_release_hook()
            except Exception:
                pass
        finally:
            user32_si.mouse_event(up, 0, 0, 0, 0)
            _held_mark_btn(button, False)
        if not verify_effect:
            return None
        return _px_effect_finish(before, _px_rect, settle_ms=settle_ms,
                                 threshold=change_threshold, effect_image=effect_image,
                                 samples=effect_samples, span_ms=effect_span_ms,
                                 strip=effect_strip)

    def _do_click(require_pick):
        _glide_to(src[0], src[1], human=human, speed=speed, precise=precise)
        _human_pause(0.03, 0.10, 0.05)
        _b1 = _px_effect_begin(_px_rect) if verify_effect else None
        _px_phase_flush()      # 动作边界：清复用缓存
        _mouse_event_click_here(button, 1)
        _eff_pick = None
        if verify_effect:
            _eff_pick = _px_effect_finish(_b1, _px_rect, settle_ms=settle_ms,
                                          threshold=change_threshold, effect_image=False,
                                          samples=effect_samples, span_ms=effect_span_ms, strip=False)
        try:                                   # B 帧（拾取后、源邻域）——给"拾取段"用
            if on_pick_done is not None:
                on_pick_done()
        except Exception:
            pass
        if require_pick and _eff_pick is not None and not _eff_pick.get("changed"):
            return _eff_pick, None
        _glide_to(tgt[0], tgt[1], human=human, speed=speed, precise=precise)
        _human_pause(0.03, 0.10, 0.05)
        _b2 = _px_effect_begin(_px_rect) if verify_effect else None
        _px_phase_flush()      # 动作边界：清复用缓存
        _mouse_event_click_here(button, 1)
        _eff_place = None
        if verify_effect:
            _eff_place = _px_effect_finish(_b2, _px_rect, settle_ms=settle_ms,
                                           threshold=change_threshold, effect_image=effect_image,
                                           samples=effect_samples, span_ms=effect_span_ms,
                                           strip=effect_strip)
        return _eff_pick, _eff_place

    def _undo_action():
        _n = []
        try:
            _rel = _held_release_mouse_all()
            if _rel:
                _n.append("已松开残留按键：%s" % _rel)
        except Exception:
            pass
        if undo_v in ("auto", "click_back"):
            try:
                _glide_to(src[0], src[1], human=human, speed=speed, precise=precise)
                _human_pause(0.03, 0.10, 0.05)
                _mouse_event_click_here(button, 1)
                _n.append("已点回起点（撤销拾取）")
                time.sleep(max(0.0, min(0.5, float(settle_ms or 0) / 1000.0)))
            except Exception as _e:
                _n.append("点回起点失败：%s" % (str(_e)[:60],))
        if undo_v in ("auto", "escape"):
            try:
                _send_combo("{ESC}")
                _n.append("已发 ESC")
            except Exception as _e:
                _n.append("ESC 发送失败：%s" % (str(_e)[:60],))
        return "；".join(_n)

    stages = []
    used = None
    final_ok = False
    undo_note = ""
    order = ["drag", "click"] if mode == "auto" else [mode]
    for _i, _m in enumerate(order):
        if _m == "drag":
            _eff = _do_drag()
            _ok = True if not verify_effect else bool(_eff and _eff.get("changed"))
            stages.append({"mode": "drag", "effect": _eff, "changed": _ok,
                           "line": "阶段·drag（按下→移动→松开）："
                                   + (_px_effect_line(_eff) if verify_effect else "（已关闭像素判定）")})
        else:
            _ep, _eq = _do_click(require_pick=(_i > 0))
            if verify_effect:
                _ok = bool(_eq and _eq.get("changed"))
                _pk = "拾取阶段：" + (_px_effect_line(_ep) if _ep else "（未执行）")
                _pl = ("放置阶段：" + _px_effect_line(_eq)) if _eq else \
                      "放置阶段：（拾取阶段无变化 ⇒ 已跳过，不往目标盲点）"
                stages.append({"mode": "click", "effect": _eq, "changed": _ok,
                               "line": "阶段·click（点起→移动→点落）：\n    " + _pk + "\n    " + _pl})
            else:
                _ok = True
                stages.append({"mode": "click", "effect": None, "changed": True,
                               "line": "阶段·click（点起→移动→点落）：（已关闭像素判定）"})
        used = _m
        final_ok = bool(_ok)
        if final_ok or mode != "auto":
            break
        undo_note = _undo_action()
        if _i + 1 < len(order):
            stages.append({"mode": "undo", "effect": None, "changed": True,
                           "line": "撤销：" + (undo_note or "（无动作）") + " —— 换下一种模式再试"})

    park_note = ""
    if safe_park not in (None, "", {}, [], ()):
        try:
            _px_park = _parse_xy(safe_park, "safe_park")
            try:
                _held_release_mouse_all()
            except Exception:
                pass
            _glide_to(_px_park[0], _px_park[1], human=human, speed=speed, precise=precise)
            park_note = "结束后光标已停放到空场 (%d,%d)（未点击）" % (_px_park[0], _px_park[1])
        except Exception as _e:
            park_note = "safe_park 无效（%s），未移动光标" % (str(_e)[:60],)

    return {"source": list(src), "target": list(tgt), "mode_requested": mode,
            "mode_used": used, "changed": final_ok, "stages": stages,
            "undo": undo_note, "safe_park": park_note, "verify": bool(verify_effect)}


def mouse_click(x, y, button="left", click_count=1, verify=True, ocr_timeout=30, fast=False, relative=False, human=False, speed=None, precise=False,
                verify_effect=True, settle_ms=_PX_SETTLE_MS, verify_region="", change_threshold=None, effect_image=False,
                effect_samples=1, effect_span_ms=0, effect_strip=False, dwell_ms=None):
    """鼠标点击。需先 focus_window。mouse_event 前台点击。verify=false 跳过OCR回读提速。

    ★ 新增 **relative=True（相对位移）**：先按**增量 (x,y)** 相对移动光标（`mouse_event`，
      **不 SetCursorPos**），再**在当前位置**点击（`_mouse_event_click_here`：不走近路、不做人手化随机等待）。
      相对位移时请配 `verify=false`（省掉整帧 OCR 回读）。x=y=0 ⇒ 纯「就地点击」，不产生多余移动。
      默认 relative=False ⇒ **老路一个字节没动**（`_click_mouse_event` → 接近路 + 双击补偿）。
    ★ 补 **human（鼠标两档 · 与 `mouse_move`／`mouse_drag` 同形）**：只影响**接近路**的速度档——
      `human=True` ⇒ 人影档（≈2500 px/s）；默认 False ⇒ 快档（≈4000 px/s）＝旧行为。`relative=True` 时不走接近路，故不受它影响。
    ★ **dwell_ms（落点后、按下前的停顿，0~2000ms，自由传）**：不传＝老行为（随机 30~100ms，中位 50ms）。
      远控/RDP 建议 200~300；游戏串流 300~500。relative=True 时同样生效（移动完先停再按）。"""
    _require_focus_target()
    # ★ P0：纯像素生效判定（动作前后各一帧；不碰 OCR/UIA）——详见文件里 `_px_*` 块。
    _px_rect = _px_rect_or_none(verify_region)
    _eff_holder = {"before": None}

    def _px_hook():
        if verify_effect:
            _eff_holder["before"] = _px_effect_begin(_px_rect)
        try:                                   # B 帧（到位/停顿后、按下前）——必须在清缓存**之前**取
            _r = _LOCAL_CTX.get("rect")
            if _r:
                _LOCAL_CTX["hook"] = _px_snapshot(_r)
        except Exception:
            pass
        _px_phase_flush()          # 动作边界：清复用缓存（后帧绝不许裁到前帧；verify 关掉时也要清）

    # 识别层已移除：不再采动作前窗口快照、不做 OCR/UIA「UI 变化回读」；窗口树走 Win32 基础信息。
    if _popup_hwnd and _is_window_alive(_popup_hwnd):
        _ocr_cache_invalidate(_popup_hwnd)  # 跟踪弹窗存在，点的是弹窗
    else:
        _ocr_cache_invalidate(_focus_hwnd)  # 无弹窗，点的是主窗口
    if relative:
        # ★ 两档规则同 `mouse_move`：默认人手化切块（抖动）；快速档 precise=true 才一次直发；
        #   人影档不许 precise（要精确/瞬移请用快速档）。
        if int(x or 0) or int(y or 0):
            if precise and human:
                raise ValueError("人影档不允许 precise=true：人影档必须走人手化轨迹（抖动）；"
                                 "要精确/瞬移请用快速档（human=false）")
            if precise:
                _mouse_event_move_rel(x, y)
            else:
                _rel_move_paced(x, y, _mouse_speed(human, False, speed)[0], human=True)
        _px_hook()                     # 纯像素：移动完成后、按下之前取「前帧」
        _dv_rel = _clamp_dwell_ms(dwell_ms)
        if _dv_rel is not None:
            time.sleep(_dv_rel / 1000.0)   # 落点后、按下前的停顿（relative 路径同样生效）
        _mouse_event_click_here(button, click_count)
        result = {"x": x, "y": y, "button": button, "clicks": click_count,
                  "method": "mouse_event-rel", "relative": True, "clicked_at_cursor": True,
                  "dwell_ms": (None if _dv_rel is None else round(_dv_rel)),
                  "tier": ("人影档" if human else "快速档"), "precise": bool(precise)}
    else:
        result = _click_mouse_event(x, y, button, click_count, human=human, speed=speed, precise=precise,
                                    pre_click_hook=_px_hook, dwell_ms=dwell_ms)
    if verify_effect:
        # 动作已投递 ⇒ 等 settle 再取「后帧」；判定失败只写 unavailable，绝不影响动作结果。
        result["effect"] = _px_effect_finish(_eff_holder["before"], _px_rect, settle_ms=settle_ms,
                                             threshold=change_threshold, effect_image=effect_image,
                                             samples=effect_samples, span_ms=effect_span_ms,
                                             strip=effect_strip)
    changes_dict = {"new": [], "old": [], "blocked": [], "all": [], "raw_ocr": "",
                    "active_surface": {"type": "窗口", "title": _get_focus_title() or "?"}}
    result["ui_changes"] = changes_dict
    result["change_count"] = 0
    _prune_tracked_popups()
    result["tracked"] = _tracked_popup_names(None)
    return result


def _do_send_keystrokes(text="", hotkeys=None, press_enter=False, press=None, release=None, hold_duration=None, scroll_clicks=None, mode=None, human=False, cps=None, ime_mode="temp", unicode_text=False):
    """统一的按键/文本发送函数。
    - 文本输入：优先 UIA ValuePattern.SetValue（精准），失败后回退 PostMessage/SendInput。
    - 热键 + Enter：仍走原有流程。
    - press: 按下不放（KEYDOWN），需 release 解除。
    - release: 释放已按下的键（KEYUP）。
    - hold_duration: 长按毫秒数（按下→等待→释放）。
    - scroll_clicks: 鼠标滚轮（正数上滚，负数下滚），前台 SendInput 方式。
    - mode: None=auto, True=foreground, False=background, 或字符串
    供 send_keystrokes handler 和 mouse_click text 原子操作共用。
    返回: {"success": bool, "method": ..., "verified": bool, "text": str, ...}"""
    hotkeys = hotkeys or []
    popup_hwnd = _popup_hwnd
    target = _resolve_target()
    prof = _input_profile(target)      # ★按窗口族分流：{} = 默认通道（原样），见 _INPUT_RULES
    # （进函数先记一次"目标窗当前是不是前台"）：本次到底走前台还是后台，回执要如实报；
    #   且后台模式的红线＝**全程不许抢前台**（不切前台、不发 SendInput、不动前台窗口的输入法）。
    try:
        _fg0 = user32.GetForegroundWindow()
        # ★与 `_ensure_foreground` 用**同一条判据**（含"前台是目标自己拥有的弹窗/同族窗"）：
        #   以前这里只比 `_fg0 == target || IsChild`，于是出现"实际一步没激活、回执却说
        #   已把该窗切到前台"的**虚报**（实测：IDA 主窗 + 它的文件对话框在前台）。
        _fg_before = bool(target and _foreground_satisfies(target))
    except Exception:
        _fg0, _fg_before = None, False
    is_cmd = press_enter or ("\n" in text) or (text.strip() and " " in text.strip())
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # **物理输入没有可靠的后台路**：按下/松开/长按/滚轮走
    #   SendInput／keybd_event，只会打到前台窗口；PostMessage 那条后台鼠标实测太局限
    #   （PostMessage 鼠标点击在多数应用里被忽略/不可靠）。规则＝
    #   这几个动作**忽略 mode、一律先切前台再执行**（打字仍按 mode 走后台，两者互不影响）。

    # ── 滚轮 ──
    if scroll_clicks is not None:
        _ensure_foreground(target)
        _mouse_event_scroll(scroll_clicks)
        direction = "↑" if scroll_clicks > 0 else "↓"
        _show_indicator(window=_get_focus_title(), action=f"滚轮 {direction}{abs(scroll_clicks)}")
        return {"success": True, "method": "mouse_event-wheel", "verified": False,
                "text": "", "action": "scroll", "clicks": scroll_clicks}

    # ── 按下/松开/长按 ──
    if press:
        _ensure_foreground(target)
        _si_key_down(*press)
        _show_indicator(window=_get_focus_title(), action=f"按下: {'+'.join(press)}")
        return {"success": True, "method": "keybd_event", "verified": False,
                "text": "", "actual_value": "", "action": "press", "keys": press}

    if release:
        _ensure_foreground(target)
        _si_key_up(*release)
        _show_indicator(window=_get_focus_title(), action=f"松开: {'+'.join(release)}")
        return {"success": True, "method": "keybd_event", "verified": False,
                "text": "", "actual_value": "", "action": "release", "keys": release}

    if hold_duration:
        _ensure_foreground(target)
        hold_keys = hotkeys if hotkeys else ([text] if text else [])
        if not hold_keys:
            return {"success": False, "method": "none", "verified": False,
                    "text": "", "actual_value": "", "error": "长按需要指定按键"}
        _si_hold_key(hold_duration, *hold_keys)
        _show_indicator(window=_get_focus_title(), action=f"长按 {hold_duration}ms: {'+'.join(hold_keys)}")
        return {"success": True, "method": "keybd_event", "verified": False,
                "text": "", "actual_value": "", "action": "hold", "keys": hold_keys, "duration_ms": hold_duration}

    # ── **UIA 整篇写已全面禁止**（`ValuePattern.SetValue` 一失手就清空整篇）──
    #   为什么禁：`ValuePattern.SetValue` 的语义是「**设值**」＝**整篇覆盖**，不是"在光标处打字"——
    #   一失手就把窗口里已有的内容清空（真机实录：文档 `kjaa bbcc dd`，一发 `send_keystrokes`
    #   之后**只剩新写的字**）。而 UIA **没有**"在光标处插入"的 API ⇒ 这条路**从机制上就只能整篇写**
    #   ⇒ **整个撤掉**。打字**一律走键盘路**（真打字＝在光标处追加，语义正确）。
    #   ⚠ **已知取舍（如实记）**：有些窗"键盘注入进不去、只有 UIA 写值进得去" ⇒ 那种窗从此**写不进去**
    #     ——宁可写不进去，也绝不整篇覆盖。
    #   **判据**：本文件里 `SetValue(` **0 命中**；静态闸门里有一条同义断言（防复发）。
    uia_result = None  # 恒 None（UIA 写值已禁）——下面键盘 / PostMessage 那条路照常跑

    # ── 回退：PostMessage / SendInput ──
    if uia_result is None:
        if is_cmd and text:
            _show_indicator(window=_get_focus_title(), command=text.replace("\n", "↵"))
        elif text:
            _show_indicator(window=_get_focus_title(), action=f"输入 {text}")
        elif hotkeys:
            _show_indicator(window=_get_focus_title(), action=f"热键: {hotkeys}")

        # 统一输入策略：mode=None → auto, True→foreground, False→background
        if mode is True:
            pref = "foreground"
        elif mode is False:
            pref = "background"
        elif isinstance(mode, str):
            # **别名归一到两个值**（工具面写 foreground/background，老写法 fg/bg 也认）——
            #   原来 "bg" 会落进 auto 的判定 ⇒ 可能**悄悄按前台发**，与"要后台"的意图正好相反。
            pref = {"fg": "foreground", "fore": "foreground",
                    "bg": "background"}.get(mode.strip().lower(), mode.strip().lower())
        else:
            surf = _get_active_surface()
            pref = "foreground" if (surf and surf.get("type") == "popup") else "auto"
        use_foreground = (_resolve_input_strategy(target, mode=pref) == "foreground")

        # **直发非 ASCII 默认不允许**（中文走输入法拼音上屏）。
        #   要发就显式 `unicode_text=true`（走 Unicode 注入）。**动手前就拦**：一个字不发、
        #   一个键不按、绝不碰输入法——原来会静默丢字，还把目标窗输入法中/英态翻转。
        if text and not hotkeys and not _text_is_ascii(text) and not unicode_text:
            return {"success": False, "method": "none", "verified": False,
                    "text": text, "actual_value": "",
                    "error": ("直发非 ASCII 文本默认不允许（设计规则）。要发请显式传 "
                              "`unicode_text=true`（走 Unicode 注入、不经过输入法）；"
                              "中文也可以走 `ime_mode=cn` ＋ 拼音，由输入法上屏。")}

        # ★定向改道（分流表命中）：默认通道对这族窗口实测进不去（见 _INPUT_RULES 的 why）
        #   ⇒ 纯扫描码"真打字" + 真回车，并把「值有没有真进去」读回来验证。
        #   默认通道与其它窗口（终端等）一个字节不动——这是红线。
        # 输入法守卫：注入纯 ASCII 前把
        #   **目标窗口**切英文（按窗口切，不是全局切），打完恢复；中文文本一个字不动。
        # **一个字符串＝一条有序打字流**。
        #   · 正文逐字发；认得的 `{KEY}`（同 send_shortcut 的 {} 语言，如 {ENTER}/{TAB}/{CTRL+C}）
        #     按位置当按键发；**认不出的花括号按字面输入（不报错）**；
        #   · 文本里的裸换行（\n）就按换行发——和正文同一条注入流，顺序天然正确；
        #   · `press_enter=true` ＝兼容糖：等价于在末尾追加一个换行（同一流）。
        #   ⇒ 不再有"正文走注入 + 回车另用 PostMessage 补"的两条队列（那会抢跑，真机抓到
        #     中文输入法下「你好⏎世界」）。三种来源（press_enter／\n／{ENTER}）收敛成一条有序流。
        _type_text = (text or "") + ("\n" if press_enter else "")
        _chan = "foreground" if use_foreground else "background"
        # 打字流与输入法规则：
        #   · **标准文本控件窗（记事本这类）**：保持原有自动切换（temp 临时切＋打完还原；perm 永久切）
        #     —— 这类窗的键真的进控件，守卫一直有效，**不要改**；
        #   · **自管 IME 的窗（WPS 这类：没有标准 Edit 子控件、IME 状态读不准）**：**不再自动切换**
        #     —— 只读一次状态写进回执，并要求**以截图核对是否被输入法干扰**（识别层已删，截图＝唯一判据）。
        _ime_std = bool(_text_input_child(target))
        ime_guarded = False
        _ime_note = ""
        if use_foreground and str(ime_mode).lower() != "off":
            if _ime_std:
                ime_guarded = _ime_guard_typed(text, target, mode=ime_mode)
                _mixed_uni = bool(unicode_text and _type_text and not _text_is_ascii(_type_text))
                if _mixed_uni and not ime_guarded:
                    ime_guarded = _ime_mixed_guard(target, ime_mode)
                if str(ime_mode).lower() == "perm" and text and _text_is_ascii(text):
                    _ime_note = "已切英文（**perm 永久切换：不还原**，该窗从此为英文）"
            else:
                # 自管 IME：**不再读状态**（读出来不可信）⇒ 只报"自管 IME、不自动切"，**不拦输入**。
                _ime_note = ("未自动切换输入法（该窗自管 IME；不自动切，避免把英文翻成中文）"
                             " ⇒ **请以截图核对是否被输入法干扰**（需要时手动 `send_shortcut {SHIFT}`）")
        # ★：扫描码"真打字"是**前台专属**（SendInput 扫描码只会打到前台窗口）——
        #   显式 `mode=background` 时**绝不走这条**（否则就成"要后台、却把窗口切到前台"）。
        #   自动模式（auto）仍照旧：它的语义就是"决定走前台 ⇒ 前台一定切到位"。
        if not hotkeys and prof.get("name") == "scancode" and _type_text and use_foreground:
            _ensure_foreground(target)
            uia_wrote = False
            if _type_text:
                # 修正（按实测的通道表）：**扫描码"真打字"优先**——
                #   这族 shell 对话框只认"真输入"：只有扫描码打字能把「确定」从【禁用】翻成【可用】；
                #   UIA SetValue 能写字但**点不亮按钮**（对话框认为"没有输入过"）⇒ 写了也交不上去。
                # 显式 `unicode_text=true` 且含非 ASCII ⇒ 走混排注入——SendInput 的
                #   Unicode 形态同样是"真输入"（能点亮 shell 对话框「确定」），而扫描码打不出中文。
                if unicode_text and not _text_is_ascii(_type_text):
                    _parse_sendinput_text(_type_text, interval=None,
                                          typing_interval=_kbd_gap_speed(human, cps)[0],
                                          unicode_text=True)
                else:
                    # {} 语言按位置发（ENTER 走扫描码回车）；认不出的花括号按字面打字（不报错）
                    for _is_tok, _piece in _split_key_parts(_type_text):
                        if _is_tok:
                            if _piece.strip().upper() == "ENTER":
                                _scancode_enter()
                            else:
                                _send_combo(_piece)
                        else:
                            # 裸换行（press_enter 追加的／文本里自带的）⇒ 真回车，同一扫描码流、顺序正确
                            for _si, _seg in enumerate(_piece.split("\n")):
                                if _si:
                                    _scancode_enter()
                                if _seg:
                                    _type_scancode(_seg, gap=_kbd_gap_speed(human, cps)[0])
                # 原来这里在"真打字没进去／被输入法转掉"时用 `_uia_setvalue` **整篇写**
                #   兜底校正文本——按"禁止整篇写"**已撤**：
                #   宁可如实报 verified=False、让调用方自己决定，也绝不用"整篇覆盖"去纠正文。
                #   `uia_wrote` 恒 False（回执照实报 scancode）。
                uia_wrote = False
            # 识别/读取层已删（UIA 整层移除）：**不再读回正文**。
            #   Win32 `WM_GETTEXT` 只对标准 Edit/RichEdit 子窗有效（UWP/WinUI/Chromium/Office 那类
            #   没有 Edit 子窗；跨权限被 UIPI 挡；跨进程卡死要超时兜底）⇒ 半吊子能力，按"不靠"处理：
            #   留空、如实报 verified=False，绝不伪装成功。
            actual = ""
            verified = False
            _show_indicator(window=_get_focus_title(),
                            action=("UIA写入" if uia_wrote else "扫描码") + f"→已输入 {text}" + ("  {ENTER}" if press_enter else ""))
            if ime_guarded:
                _ime_guard_restore(target)      # 用完还原成"切换前那个模式"（精确还原，见该函数）
            return {"success": True, "method": ("uia-setvalue" if uia_wrote else "scancode"),
                    "verified": verified, "text": text, "actual_value": actual or "",
                    "profile": prof.get("name"), "profile_why": prof.get("why", ""),
                    "ime_guarded": ime_guarded, "ime_note": _ime_note,
                    "channel": "foreground", "foreground_before": _fg_before,
                    "foreground_ensured": (not _fg_before)}

        fallback_method = "keybd_event" if use_foreground else "postmessage"
        if _type_text:
            if use_foreground:
                # ★统一一条有序流：正文逐字 + `{KEY}` 键 + 非 ASCII（unicode_text=true）按字符串顺序发
                _parse_sendinput_text(_type_text, interval=None,
                                      typing_interval=_kbd_gap_speed(human, cps)[0],
                                      unicode_text=unicode_text)
                fallback_method = ("keybd_event+unicode"
                                   if (unicode_text and not _text_is_ascii(_type_text)) else "keybd_event")
            else:
                # 后台：同一套 {} 语言——正文走 WM_CHAR（发**文本输入子控件**），`{KEY}` 走 WM_KEYDOWN/UP
                _post_text_tokens(_text_input_child(target) or target, _type_text)
        if ime_guarded:
            _ime_guard_restore(target)          # 用完还原成"切换前那个模式"（精确还原，见该函数）
        if hotkeys:
            if use_foreground:
                hk_lower_list = [hk.lower() for hk in hotkeys]
                if len(hk_lower_list) == 1 and hk_lower_list[0] in ("ctrl","shift","alt","win","lwin","rwin"):
                    pass
                else:
                    _foreground_send_keys(target, *hotkeys)
            else:
                # 后台模式：逐键 PostMessage（Ctrl+C 等控制信号在后台模式下无效，需前台）
                hk_lower_list = [hk.lower() for hk in hotkeys]
                mods = set()
                keys = []
                for hk in hk_lower_list:
                    if hk in ("ctrl","shift","alt","win","lwin","rwin"):
                        mods.add(hk)
                    else:
                        keys.append(hk)
                # 先按修饰键
                for m in mods:
                    vk = _SI_VK_MAP.get(m, 0)
                    if vk:
                        user32.PostMessageW(target, WM_KEYDOWN, vk, _make_lparam(vk))
                # 按普通键
                for k in keys:
                    vk = _si_parse_key(k)
                    if vk is not None:
                        user32.PostMessageW(target, WM_KEYDOWN, vk, _make_lparam(vk))
                        user32.PostMessageW(target, WM_KEYUP, vk, _make_lparam(vk, True))
                # 释放修饰键（逆序）
                for m in reversed(list(mods)):
                    vk = _SI_VK_MAP.get(m, 0)
                    if vk:
                        user32.PostMessageW(target, WM_KEYUP, vk, _make_lparam(vk, True))
        # 回退方法：前台用 _last_key_verified，后台始终未验证
        uia_result = {
            "success": True, "method": fallback_method,
            "verified": _last_key_verified if use_foreground else False,
            "text": text, "actual_value": "",
            # ★：**本次到底走的哪条通道**（回执如实报；后台＝未抢前台、未动输入法）
            "channel": _chan, "foreground_before": _fg_before,
            "foreground_ensured": bool(use_foreground and not _fg_before),
            "ime_note": _ime_note,
        }

    return uia_result


# ───── 移动速度策略 ─────
# 判据：默认**按距离自适应**——短距离不磨蹭、长距离看得见轨迹；显式传 duration 时以参数为准；
#       0 = 瞬移（保留原契约）。回执报「设定 vs 实测」，够不够慢由数字说话。
# 速度值已统一收进文件顶部的【全局速度设置】块（MOUSE_* / DRAG_*），这里只留说明，勿再就地写死。


def _auto_move_duration(dist, px_per_sec=MOUSE_PX_PER_SEC, lo=MOUSE_MIN_SEC, hi=MOUSE_MAX_SEC):
    """按距离算默认时长：dist/速度，夹在 [lo, hi] 之间。"""
    return max(lo, min(hi, dist / px_per_sec))


# ───── 人手化轨迹 ─────
# 判据：检测方看的是**分布**——匀速、等间隔、均匀随机抖动，各自都是机器指纹。
# 所以四个成分都按真人轨迹的实测特征来：缓入缓出 / 步间对数正态 / 抖动成段出现 / 起点终点不抖。
def _human_trace(sx, sy, ex, ey, duration, sigma=0.18, jitter_px=2.5,
                 burst_chance=0.45, pause_chance=0.04, rnd=None):
    """把一段直线路径变成人手化轨迹。返回 (points, meta)。

    points = [(x, y, delay_sec), ...]，最后一点就是目标（终点精确命中）。
      · 缓入缓出：min-jerk 曲线 f(t)=t³(10-15t+6t²)，两端速度为 0（人手起步与收势）
      · 步间延迟：基准 × exp(N(0, sigma)) —— 对数正态（多数步接近基准、少数明显慢）；
        另有 pause_chance 概率"手顿一下"（+40~90ms）。实测总时长会略长于设定，回执照实报。
      · 小抖动：路径切 3~6 段，每段按 burst_chance 概率带一段平滑摆动（正弦进出、
        幅度与摆向随机）⇒ **有的段抖、有的段干净**；起点两步与终点不抖。
      · 随机弧线（**按距离缩放**）：一条随机弓形（两端归零，约 1.2%~4.5% 距离、上限 28px）+ 一条常驻
        低频上下慢摆 ⇒ 真人不会压着几何直线走，且**每次的几何形状都不同**（离线实测过：只加缓动时，
        同一条 A→B 两次有 64/80 步位置一模一样）。
      · 到手微调：过冲一点点 → 修正 →（一半概率）1px 微调 → 精确落定 ⇒「到了之后手还在动」
        （人不可能控制得那么精确）。最后一点仍是目标，终点精确命中不变。
    """
    rnd = rnd or random.Random()
    n = max(8, int(duration / 0.01))
    dx, dy = float(ex - sx), float(ey - sy)
    dist = (dx * dx + dy * dy) ** 0.5
    ux, uy = (dx / dist, dy / dist) if dist else (0.0, 0.0)
    nx, ny = -uy, ux                                  # 路径法向 = 抖动方向
    ease = lambda t: t * t * t * (10.0 + t * (-15.0 + 6.0 * t))
    w = [math.exp(rnd.gauss(0.0, sigma)) for _ in range(n)]
    k = duration / sum(w)
    segs = rnd.randint(3, 6)                          # 抖动分段：有的抖、有的不抖
    bursts = {}
    for s in range(segs):
        if rnd.random() < burst_chance:
            bursts[s] = (rnd.uniform(0.4, 1.0) * jitter_px, rnd.uniform(0.8, 2.2),
                         rnd.choice((-1.0, 1.0)), rnd.uniform(0.0, 2 * math.pi))
    # 两条机器痕迹，都在这里修：
    #   ① 「移动的时候不可能是一条直线，会带一点弧度」——原来固定 ±2px，千像素级移动上等于几何
    #      直线（0.2%，看不出）；现在**按距离缩放**（约 1.2%~4.5%，上限 28px，随机取一侧）。
    #   ② 「也有可能有一点上下波动」——常驻低频慢摆（不是只在"抖动段"里才有）。
    bow_amp = min(max(dist * rnd.uniform(0.012, 0.045), 1.0), 28.0)
    bow = bow_amp * rnd.choice((-1.0, 1.0))
    und_amp = min(max(dist * 0.004, 0.6), 3.5)
    und_cyc = rnd.uniform(0.8, 1.8)
    und_ph = rnd.uniform(0.0, 2 * math.pi)
    pts, pauses = [], 0
    for i in range(1, n + 1):
        t = i / n
        px, py = sx + dx * ease(t), sy + dy * ease(t)
        if dist > 8 and 0 < i < n:
            b = bow * math.sin(math.pi * t)           # 弓形：中段最大、两端为 0
            u = und_amp * math.sin(2 * math.pi * und_cyc * t + und_ph) * (1.0 - t)
            px, py = px + nx * (b + u), py + ny * (b + u)
        if bursts and 2 < i < n and dist > 8:
            s = min(segs - 1, int(t * segs))
            if s in bursts:
                amp, cycles, sgn, ph = bursts[s]
                tau = t * segs - s
                wig = (amp * sgn * math.sin(2 * math.pi * cycles * tau + ph)
                       * math.sin(math.pi * tau) * (1.0 - t))
                px, py = px + nx * wig, py + ny * wig
        d = w[i - 1] * k
        if i < n and rnd.random() < pause_chance:
            d += rnd.uniform(0.04, 0.09)
            pauses += 1
        pts.append((int(round(px)), int(round(py)), d))

    # 到手微调：「人到达目的地后，会有一点随机小移动，不可能控制得那么精确。」
    #   过冲一点点 → 修正回来（还差 1px 量级）→（一半概率）再 1px 微调 → **最后一点仍严格是 (ex,ey)**：
    #   终点精确命中这条契约不能破，否则点击/拖拽会落偏。
    settle_steps, over_px = 0, 0.0
    if dist >= 15:
        over_px = min(max(dist * rnd.uniform(0.004, 0.012), 1.0), 6.0)
        side = rnd.choice((-1.0, 1.0)) * rnd.uniform(0.0, 1.5)
        micro = [(int(round(ex + ux * over_px + nx * side)),
                  int(round(ey + uy * over_px + ny * side)))]                  # ① 冲过一点点
        jx = rnd.choice((-1.0, 1.0)) * rnd.uniform(0.0, 1.2)
        micro.append((int(round(ex + nx * jx)), int(round(ey + ny * jx))))     # ② 修正回来（还差 1px 量级）
        if rnd.random() < 0.5:                                                 # ③ 手还有点没停稳
            j2 = rnd.choice((-1.0, 1.0)) * rnd.uniform(0.0, 1.0)
            micro.append((int(round(ex + nx * j2)), int(round(ey + ny * j2))))
        micro.append((int(ex), int(ey)))                                       # ④ 落定（精确命中）
        # ★这几步的总时长**按本次移动时长缩放**（约 18%，夹 0.03~0.25s）——否则短距离会被
        #   微调主导成"慢动作收尾"（离线实测过：60px 移动原写法 设定 0.027s / 实际 0.225s，8 倍）。
        budget = min(max(duration * 0.18, 0.03), 0.25)
        ws = [rnd.uniform(0.6, 1.4) for _ in micro]
        tot_w = sum(ws)
        pts.extend((mx, my, budget * w / tot_w) for (mx, my), w in zip(micro, ws))
        settle_steps = len(micro)
    # 自检：整条路径相对直线最大侧偏（弓形有没有真加上、加对方向）——实测值，不是计划值
    max_off = max(abs((p[0] - sx) * nx + (p[1] - sy) * ny) for p in pts) if dist > 8 else 0.0
    meta = {"steps": len(pts), "base_steps": n, "segs": segs, "burst_segs": len(bursts), "pauses": pauses,
            "bow_px": round(bow_amp, 1), "und_px": round(und_amp, 1), "bow_obs_px": round(max_off, 1),
            "settle_steps": settle_steps, "overshoot_px": round(over_px, 1),
            "delay_min_ms": round(min(p[2] for p in pts) * 1000),
            "delay_max_ms": round(max(p[2] for p in pts) * 1000),
            "delay_mean_ms": round(sum(p[2] for p in pts) / len(pts) * 1000, 1)}
    return pts, meta


def _plain_trace(sx, sy, ex, ey, duration, n_min=8):
    """**确定性直线轨迹**（快速档 `precise=true` 专用）—— 不抖、不弧、无到手微调。

    两档规则：**两档默认都走 `_human_trace()`**；只有**快速档**显式传 `precise=true` 才走本函数：
      与 `_human_trace` **同一条 min-jerk 曲线、同样步数**，但**等间隔、零侧偏、零摆动、
      零到手微调**，**终点严格等于 (ex,ey)**（点击/拖拽要落准的核心契约）。

    与 `_human_trace` 的关系：**不是替换、不是改它**——那个函数一个字节没动。
    """
    dx, dy = float(ex - sx), float(ey - sy)
    n = max(int(n_min), int(duration / 0.01))          # 与 `_human_trace` 同一条步数公式
    ease = lambda t: t * t * t * (10.0 + t * (-15.0 + 6.0 * t))   # 同一条 min-jerk
    step = duration / n
    pts = []
    for i in range(1, n + 1):
        t = i / n
        px, py = sx + dx * ease(t), sy + dy * ease(t)
        if i == n:
            px, py = float(ex), float(ey)              # 终点精确命中（不受取整/浮点影响）
        pts.append((int(round(px)), int(round(py)), step))
    meta = {"steps": len(pts), "base_steps": n, "segs": 1, "burst_segs": 0, "pauses": 0,
            "bow_px": 0.0, "und_px": 0.0, "bow_obs_px": 0.0,
            "settle_steps": 0, "overshoot_px": 0.0,
            "delay_min_ms": round(step * 1000, 1), "delay_max_ms": round(step * 1000, 1),
            "delay_mean_ms": round(step * 1000, 1), "precise": True}
    return pts, meta


def _human_pause(lo, hi, mu=None, sigma=0.32):
    """人手化停顿（秒），返回实际停顿时长。

    给 mu 用对数正态（真人的停顿是"多数短、偶尔明显长"，右偏；均匀随机反而像机器），
    否则区间均匀。下限 lo 保证不短到被系统/控件丢掉，上限 hi 保证不拖沓。

    **连分布参数本身也每次重抽**：中位 ×[0.88, 1.12]、
      形状 σ ∈ [0.24, 0.42]。理由：参数固定 ⇒ 每次跑出来的是**同一个统计指纹**（那和等间隔
      只差一层皮）；重抽之后，两次运行既不共享中心、也不共享散布。
      **取值范围仍由 [lo, hi] 夹住 ⇒ "多久到多久"一个字没变**（变的是分布形状）。
    """
    if mu:
        _mu = mu * random.uniform(0.88, 1.12)
        _sig = random.uniform(0.24, 0.42)
        d = random.lognormvariate(math.log(_mu), _sig)
    else:
        d = random.uniform(lo, hi)
    d = max(lo, min(hi, d))
    time.sleep(d)
    return d


def _trace_note(r):
    """回执里的轨迹自证一行：「够不够像人、够不够慢」由数字说话，不靠自述。

    「步间(计划)」= 生成器打算睡多久；「步间(真实)」= 实测每步真睡了多久。
    两者差距大 ⇒ Windows 时钟粒度把抖动压平了（检测方看到的是真实那一列）。"""
    if not r.get("steps"):
        return ("精确落点（precise，未走轨迹）" if r.get("precise") else "瞬移（duration=0）")
    _pre = "精确(无抖动) · " if r.get("precise") else ""
    return (_pre + f"距离 {r.get('distance')}px · {r['steps']} 步 · 设定 {r.get('duration')}s"
            f" · 实测 {r.get('elapsed')}s · 步间计划 {r.get('delay_min_ms')}~{r.get('delay_max_ms')}ms"
            f" · 步间真实 {r.get('obs_min_ms')}~{r.get('obs_max_ms')}ms(均{r.get('obs_mean_ms')}ms)"
            f" · 抖动段 {r.get('burst_segs')}/{r.get('segs')} · 手顿 {r.get('pauses')}"
            f" · 弧 {r.get('bow_px')}px(实测侧偏{r.get('bow_obs_px')}px) · 荡 {r.get('und_px')}px"
            f" · 到手微调 {r.get('settle_steps')}步(过冲{r.get('overshoot_px')}px)")


def _click_timing_note():
    """回执自证：最近一次点击的时序（人手化后「按下」是 50~200ms 的随机值，不再固定 20ms），
    含接近路（`_glide_to`）走了多少步/多久——原来那一步是瞬移。"""
    t = _last_click_timing or {}
    if not t.get("hold_ms"):
        return ""
    holds = "/".join(str(h) for h in t["hold_ms"])
    note = f" · 按下 {holds}ms(落下停顿{t.get('settle_ms')}ms)"
    if t.get("glide_steps"):
        note += f" · 接近路 {t['glide_steps']}步/{t.get('glide_elapsed')}s"
    return note


def _glide_to(x, y, duration=None, min_dist=1.0, human=False, speed=None, drag=False, precise=False):
    """人手化把光标移到 (x,y) —— **所有要移动光标的地方都走这里**。

    每个要移动光标的地方都走这里（原来是三处各自瞬移：mouse_move、点击前、拖拽前，改一条漏两条）。
    光标本来就在原地（dist < min_dist）或 duration=0 时，退化为直接落点（不做无谓的 0.4 秒）。
    返回 distance/duration/elapsed/steps/步间计划/步间真实/抖动段，供回执自证。

    ★：`human` 之上再接 **`speed`（像素/秒，只有人影档可传）** —— 判定与夹取都在**源头**
      （`_mouse_speed(human, drag, speed)`），故凡是走本函数的路（移动/点击接近路/拖拽/序列绝对步）
      **一律自动获得调速能力**；`drag=True` 表示按**拖拽档**的上限夹（1600 而非 2500）。
      显式传了 `duration` ⇒ 以 duration 为准（speed 不参与，回执里也不报）。
    两档规则：**两档默认都走 `_human_trace()`（抖动轨迹）**；
      `precise=True`（**仅快速档 human=False**）⇒ 走 `_plain_trace()`：确定性直线、等间隔、
      零侧偏、零到手微调、终点精确命中。**人影档传 precise ⇒ 抛错**；**人影档传 duration=0（瞬移）
      ⇒ 抛错**（人影档不许有"不像人"的一段）。两道判据都在函数最开头，**先于任何 OS 调用**。"""
    if precise and human:
        raise ValueError("人影档不允许 precise=true：人影档必须走人手化轨迹（抖动）；"
                         "要精确/瞬移请用快速档（human=false）")
    if human and duration is not None and float(duration) <= 0:
        raise ValueError("人影档不允许瞬移（duration=0）：人影档必须走人手化轨迹；"
                         "要瞬移请用快速档（human=false）")
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    sx, sy = pt.x, pt.y
    dist = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
    _sp = None
    if duration is None:
        _sp = _mouse_speed(human, drag, speed)      # ★源头：这里判定"能不能调"并夹取
        duration = _auto_move_duration(dist, *_sp)
    if dist < min_dist or duration <= 0:
        _me_start_move(x, y)
        _skip = {"from_x": sx, "from_y": sy, "distance": int(dist),
                 "duration": 0.0, "elapsed": 0.0, "steps": 0,
                 "tier": ("人影档" if human else "快速档"), "precise": bool(precise)}
        if speed is not None:
            _skip["speed_requested_px_per_sec"] = float(speed)
            _skip["speed_note"] = "距离过近/瞬移：本次没走轨迹，速度未生效"
        return _skip
    if precise:
        pts, meta = _plain_trace(sx, sy, x, y, duration)   # 快速档·精确：确定性直线（不走"抖动"）
    else:
        pts, meta = _human_trace(sx, sy, x, y, duration)   # 默认（两档）：人手化轨迹（抖动）
    t0 = time.perf_counter()
    obs = []
    for (mx, my, d) in pts:
        _me_start_move(mx, my)
        s0 = time.perf_counter()
        time.sleep(d)
        obs.append(time.perf_counter() - s0)          # 真实睡眠时长（检测方看到的就是这一列）
    elapsed = time.perf_counter() - t0
    out = {"from_x": sx, "from_y": sy, "distance": int(dist),
           "duration": round(float(duration), 3), "elapsed": round(elapsed, 3),
           "tier": ("人影档" if human else "快速档"), "precise": bool(precise)}
    if _sp is not None and speed is not None:
        # ★夹取必须如实报（本线铁律：不静默）——请求多少、实取多少、为什么夹
        _req, _got = float(speed), float(_sp[0])
        out["speed_requested_px_per_sec"] = _req
        out["speed_used_px_per_sec"] = round(_got, 1)
        if abs(_got - _req) > 0.5:
            out["speed_note"] = ("⚠ 请求 %g ⇒ 实取 %g 像素/秒（%s；人影档上限＝移动 %g／拖拽 %g）"
                                 % (_req, _got, "超出人影档上限" if _req > _got else "低于下限",
                                    MOUSE_PX_PER_SEC_HUMAN, DRAG_PX_PER_SEC_HUMAN))
    out.update(meta)
    out.update(obs_min_ms=round(min(obs) * 1000, 1),
               obs_max_ms=round(max(obs) * 1000, 1),
               obs_mean_ms=round(sum(obs) / len(obs) * 1000, 1))
    return out


def mouse_move(x, y, duration=None, human=False, relative=False, nocoalesce=False, speed=None, precise=False):
    """SetCursorPos 移动鼠标（人手化轨迹：缓入缓出 + 步间抖动 + 时有时无的小摆动）。

    duration=None→按距离自适应（约 1000px/s，夹在 0.4~1.6s）；0→瞬移；>0→指定秒数。
    返回实测耗时与步间区间（供回执自证"够不够慢、像不像人"）。需先 focus_window。

    ★ 新增 **relative=True（相对位移）**：此时 x,y 是**增量 (dx,dy)**，走 `mouse_event`
      相对位移（`_mouse_event_move_rel`）——只认 raw delta 的应用（典型是游戏），而 `SetCursorPos` **一个输入事件都不产生**；
      且**无屏幕边界**（绝对坐标转两下就贴边，增量可以一直转）。
      ⚠ 相对位移受系统指针加速影响（官方 MOUSEINPUT：可能被放大约 4 倍）⇒ 回执只报「请求位移」。
      默认 relative=False ⇒ **老路一个字节没动**（`_glide_to` → `_me_start_move`＝SetCursorPos）。
    ★ 新增 **`speed`（像素/秒，只有 human=true 时可传）**：在**人影档上限（2500）之内**自选速度
      ——例 300＝很慢、600＝正常、1000＝偏快；超出上限夹到上限、低于下限夹到下限，**夹取写进回执**。
      `human=false` 传 `speed` ⇒ 报错（快档没有调速这个选项）。
      ★`relative=true` 也能调速：它没有"轨迹时长"，改成**把增量切成小块、按时间铺开**送出
      （`_rel_move_paced`，块间≈一帧 16ms；末块精确落在 (Δx,Δy) 不漂移）——同样**只在 human=true 下开放**。"""
    _require_focus_target()
    _ensure_foreground(_focus_hwnd)
    if relative:
        # 两档规则（抖动＝`_human_trace()` 轨迹算法；**两档默认都抖**，
        #   快速档可传 precise=true 要"直接精确、不走抖动"；人影档不许 precise）：
        #   · 默认 ⇒ 相对位移也走人手化轨迹（`_rel_move_paced(..., human=True)`：切块＋抖动＋弧线，
        #     末块精确落在 (dx,dy)）；
        #   · precise=true（**仅快速档**）⇒ 原来的"一次 raw 直发"（瞬时增量）。
        if precise and human:
            raise ValueError("人影档不允许 precise=true：人影档必须走人手化轨迹（抖动）；"
                             "要精确/瞬移请用快速档（human=false）")
        t0 = time.perf_counter()
        if precise:
            _mouse_event_move_rel(x, y, nocoalesce=bool(nocoalesce))
            return {"x": x, "y": y, "method": "mouse_event-rel", "relative": True, "precise": True,
                    "tier": ("人影档" if human else "快速档"),
                    "elapsed": round(time.perf_counter() - t0, 4),
                    "note": "增量位移（precise=一次直发、不走人手化轨迹）；受系统指针加速影响，可能被放大（官方：最多约 4 倍）"}
        _pace = _rel_move_paced(x, y, _mouse_speed(human, False, speed)[0],
                                nocoalesce=bool(nocoalesce), human=True)
        return {"x": x, "y": y, "method": "mouse_event-rel", "relative": True,
                "tier": ("人影档" if human else "快速档"), "precise": False,
                "elapsed": round(time.perf_counter() - t0, 4), "paced": _pace,
                "note": "增量位移（默认人手化切块＝抖动）；受系统指针加速影响，可能被放大（官方：最多约 4 倍）"}
    out = _glide_to(x, y, duration, human=human, speed=speed, precise=precise)
    out.update({"x": x, "y": y, "method": "SetCursorPos"})
    return out


def set_window(x=None, y=None, width=None, height=None):
    """设置窗口位置和/或尺寸。x/y/width/height 全可选，传什么改什么。
    不抢焦点。自动选择弹窗或主窗口。返回新的 rect。"""
    _prune_tracked_popups()
    target = _popup_hwnd if _popup_hwnd else _focus_hwnd
    if not target:
        raise RuntimeError("未聚焦窗口，请先调用 focus_window（窗口调整不回退到其他窗口）")
    if not _is_window_alive(target):
        raise RuntimeError(f"聚焦窗口已失效（hwnd={target}），请重新调用 focus_window（按设计不回退、窗口调整已中止）")
    rect = wintypes.RECT()
    user32.GetWindowRect(target, ctypes.byref(rect))
    cur_x, cur_y = rect.left, rect.top
    cur_w, cur_h = rect.right - rect.left, rect.bottom - rect.top
    new_x = x if x is not None else cur_x
    new_y = y if y is not None else cur_y
    new_w = width if width is not None else cur_w
    new_h = height if height is not None else cur_h
    flags = 0x0010 | 0x0004  # SWP_NOACTIVATE | SWP_NOZORDER
    if x is None and y is None:
        flags |= 0x0002  # SWP_NOMOVE
    if width is None and height is None:
        flags |= 0x0001  # SWP_NOSIZE
    user32.SetWindowPos(target, 0, new_x, new_y, new_w, new_h, flags)
    user32.GetWindowRect(target, ctypes.byref(rect))
    return {"hwnd": target, "left": rect.left, "top": rect.top,
            "width": rect.right - rect.left, "height": rect.bottom - rect.top,
            "tracked": _tracked_popup_names()}


def mouse_drag(end_x=None, end_y=None, start_x=None, start_y=None, button="left", duration=None, human=False, speed=None, precise=False, hold=False, release=False,
               verify_effect=True, settle_ms=_PX_SETTLE_MS, verify_region="", change_threshold=None, effect_image=False,
               effect_samples=1, effect_span_ms=0, effect_strip=False, pre_release_hook=None):
    """SendInput 前台拖拽（按住的那一段是人手化轨迹）。start 未指定时自动取窗口标题栏中心。

    duration=None→按距离自适应（约 700px/s，夹在 0.5~2.0s）；0=直接落点；>0→指定秒数。
    `speed`（像素/秒，只有 `human=true` 可传）——**拖拽档的上限是 1600**（不是移动的 2500）；
      接近路按移动档上限夹，按住那段按拖拽档上限夹；夹取写进回执。

    拖拽两态：
      `hold=False`（**默认**）＝按住拖到终点后**自动松手**（松手发生在拖拽完成之后，更通用）；
      `hold=True`＝拖完**保持按住不松**（刻意保持，跨调用保留；松开用 `release=True` 只松手、不拖）。
      异常时**任何模式都先松手**。"""
    if release:
        # `release=true` ＝ **只松开**之前 hold=true 刻意按住的鼠标键（不移动、不新按）。
        rel = _held_release_mouse_all()
        return {"method": "mouse_event-release", "released": rel, "held_after": _held_desc()}
    if end_x is None or end_y is None:
        raise ValueError("mouse_drag：拖拽必须给 end_x／end_y；只想松开请传 release=true")
    _require_focus_target()
    _ensure_foreground(_focus_hwnd)
    _px_rect = _px_rect_or_none(verify_region)   # ★ P0：纯像素生效判定（失败当场报错，不静默）
    if start_x is None or start_y is None:
        rect = wintypes.RECT()
        user32.GetWindowRect(_focus_hwnd, ctypes.byref(rect))
        sx = start_x if start_x is not None else (rect.left + rect.right) // 2
        sy = start_y if start_y is not None else rect.top + 15  # 标题栏
    else:
        sx, sy = start_x, start_y
    dist = ((end_x - sx) ** 2 + (end_y - sy) ** 2) ** 0.5
    if duration is None:
        # ★：拖拽档也能调速（上限＝拖拽人影档 1600，而非移动的 2500）——源头同样是 `_mouse_speed`
        duration = _auto_move_duration(dist, *_mouse_speed(human, drag=True, speed=speed))
    _glide_to(sx, sy, human=human, speed=speed, precise=precise)  # 人手化走到抓取点（统一入口 `_glide_to`，不再是瞬移）
    _human_pause(0.03, 0.10, 0.06)  # 到位后停一下再按
    # ★ P0：纯像素「前帧」——放在**走到抓取点之后、按下之前**（指针扫过的 hover
    #   变化不算这一次拖拽的成效）。失败只写 unavailable，绝不影响拖拽本身。
    _px_before = _px_effect_begin(_px_rect) if verify_effect else None
    _px_phase_flush()          # 动作边界：清复用缓存（拖拽时长通常够，但不赌）
    if button == "left":
        down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    else:
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    user32_si.mouse_event(down, 0, 0, 0, 0)
    _held_mark_btn(button, True)
    _ok = False
    try:
        # 拖拽两态：
        #   `hold=False`（默认）：拖到终点后**自动松手**（老行为）；`hold=True`：**拖完保持按住不松**
        #   （刻意保持、跨调用保留；松开用 `release=true`）。**异常时任何模式都先松手**（防卡桌面）。
        _show_indicator(window=_get_focus_title(),
                        action=f"按住拖拽中：{button} 键（{'hold：拖完保持按住' if hold else '拖完自动松手'}）")
        grip = _human_pause(0.08, 0.28, 0.13)   # 按下后先"抓稳"再拖（滑块验证码常看这一段）
        glide = _glide_to(end_x, end_y, duration if duration > 0 else 0, human=human, speed=speed, drag=True, precise=precise)   # 按住状态下的移动也走统一入口（★拖拽档上限）
        _human_pause(0.05, 0.20, 0.09)          # 到位后瞄一下再松手
        try:                                   # C 帧（松手前、落点邻域）——给"松手前→松手后"用
            if pre_release_hook is not None:
                pre_release_hook()
        except Exception:
            pass
        _ok = True
    finally:
        if _ok and hold:
            _held_mark_intentional(button, True)          # 刻意保持：入口安全网**不自动松**
        else:
            user32_si.mouse_event(up, 0, 0, 0, 0)
            _held_mark_btn(button, False)                 # 普通完成 / 异常 ⇒ 都松手
    _show_indicator(window=_get_focus_title(),
                    action=(f"拖拽 ({sx},{sy})→({end_x},{end_y})"
                            + ("｜**保持按住**（未松开，release=true 松开）" if hold else "")))
    out = {"x": end_x, "y": end_y, "start_x": sx, "start_y": sy, "method": "mouse_event",
           "grip_ms": round(grip * 1000), "hold": bool(hold)}   # 抓握停顿（滑块验证码常看这一段）
    out.update(glide)                       # distance/duration/elapsed/steps/步间/抖动段（统一来自 _glide_to）
    if verify_effect:
        out["effect"] = _px_effect_finish(_px_before, _px_rect, settle_ms=settle_ms,
                                          threshold=change_threshold, effect_image=effect_image,
                                          samples=effect_samples, span_ms=effect_span_ms,
                                          strip=effect_strip)
    return out


def scroll(clicks, x=None, y=None, mode=None):
    """滚轮操作。mode='background'→后台, 'foreground'→前台, None→自动(终端后台/其他前台)。"""
    _require_focus_target()
    use_background = (mode == "background") or (mode is None and _is_terminal_or_child())
    if use_background:
        target = _resolve_target()
        return _post_scroll(target, clicks, x, y)
    _ensure_foreground(_focus_hwnd)
    _mouse_event_scroll(clicks)
    _at = None
    try:                                   # 前台滚轮按「当前光标位置」投递（x/y 不生效）⇒ 回执报真实位置
        _pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(_pt))
        _at = (int(_pt.x), int(_pt.y))
    except Exception:
        _at = None
    return {"clicks": clicks, "x": x, "y": y, "method": "mouse_event", "at_cursor": _at}


def get_mouse_position():
    """获取鼠标坐标 + 当前位置的窗口信息"""
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    hwnd = user32.WindowFromPoint(pt)
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    class_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buf, 256)
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc_name = ""
    try:
        import psutil
        proc_name = psutil.Process(pid.value).name()
    except Exception:
        try:
            r = subprocess.run(['tasklist', '/FI', f'PID eq {pid.value}', '/FO', 'CSV', '/NH'], capture_output=True, text=True, timeout=2)
            if r.stdout:
                proc_name = r.stdout.strip().split(',')[0].strip('"')
        except:
            pass
    return {"x": pt.x, "y": pt.y, "hwnd": hex(hwnd),
            "title": buf.value, "class": class_buf.value, "pid": pid.value, "process": proc_name}


# ========== MCP 协议 ==========

# ══════ 输出双视图 ══════
#   一个真相源 `elements` → 两个渲染器：文本（给「看」的布局）+ NDJSON（给「点」的精确数）。
#   三条硬规则：① `[i]` 编号对齐 ② 两边顺序一致（自上而下、自左而右）③ 截断留痕 count/truncated。
#   ★第四条·只加不减：**文字画面＝截图的替身**（截图没给到时全靠它"看"），所以它带 [i]、带角色/名字、
#     **带取整坐标**，一个字不许减；NDJSON 是**加**在旁边的精确层（树里取整、精确数去 NDJSON 拿），
#     同样只加不减。
#   成本旋钮：>60 个元素时文本视图转「粗绘」；树内坐标取整，精确数去 NDJSON 拿。
#   NDJSON 也设行数上限——大窗口不然会把上下文灌爆（超出给 truncated=true）。
# 双视图（_dv_el_line / _dual_view_block）与 _DV_* 常量已整段删除。


# 纯像素生效判定的参数形状（click / drag / mouse_transfer 共用一套）——
#   一处定义、三处注入（见 TOOLS 之后的统一循环）。全部**不碰 OCR/UIA**，只做动作前后帧差分。
_EFFECT_PROPS = {
    "verify_effect": {
        "type": "boolean",
        "description": "纯像素差分判定：动作前后取帧对比，回执给 changed/diff_ratio/变化 bbox（不碰 OCR/UIA）。false＝只执行不判定，更快。",
        "default": True,
    },
    "settle_ms": {
        "type": "integer",
        "description": "动作后等画面稳定的毫秒数，再取后帧；动画/载入慢就调大。",
        "default": _PX_SETTLE_MS,
    },
    "verify_region": {
        "type": "string",
        "description": "限定对比区域 'x,y,w,h'（图内坐标）；不传＝最近附图的内容区。",
        "default": "",
    },
    "change_threshold": {
        "type": "number",
        "description": "判定 changed 的最小变化像素占比（0~1；越小越敏感）。",
        "default": _PX_CHANGE_RATIO,
    },
    "effect_image": {
        "type": "boolean",
        "description": "true＝只回变化区小图（替代整张附图，省 token；无变化不出图）。",
        "default": False,
    },
    "effect_samples": {
        "type": "integer",
        "description": "连拍帧数：>1 时在 effect_span_ms 内取多帧、取变化最大的一帧判定（防动画/延迟误判）。",
        "default": 1,
    },
    "effect_span_ms": {
        "type": "integer",
        "description": "连拍总跨度毫秒（0＝自动；effect_samples>1 时按 400ms 摊开）。",
        "default": 0,
    },
    "effect_strip": {
        "type": "boolean",
        "description": "true＝把前帧＋各后帧拼成一张前后对比条带随回执返回（需变化才出图）。",
        "default": False,
    },
}

# ── 工具面瘦身：公共说明「一处定义」，其余工具只留一行引用（只改描述文本；参数、默认值、行为一律不变）──
#   完整说明只出现在载体工具：capture_screenshot（附图/坐标）、mouse_move（鼠标档位）、
#   mouse_click（生效判定）、focus_window（window 参数）、send_keystrokes（输入法/文本）。
_IMG_DESC_REF = "是否回图（默认 true；false 关，省 token）。"
_MS_DESC_REF = "标注样式，见 capture_screenshot。"
_GD_DESC_REF = "标注参数，见 capture_screenshot。"
_GC_DESC_REF = "标注参数，见 capture_screenshot。"
_RI_DESC_REF = "标注参数，见 capture_screenshot。"
_WIN_DESC_REF = "目标窗口（标题/hwnd）；不传＝当前操作面。见 focus_window。"
_EFF_DESC_REF = {
    "verify_effect": "纯像素差分判定（默认 true）。同 mouse_click。",
    "settle_ms": "动作后稳定等待（默认 120ms）。同 mouse_click。",
    "verify_region": "对比区域 'x,y,w,h'（图内坐标）。同 mouse_click。",
    "change_threshold": "判定变化最小占比（默认 0.002）。同 mouse_click。",
    "effect_image": "true＝只回变化区小图。同 mouse_click。",
    "effect_samples": "连拍帧数。同 mouse_click。",
    "effect_span_ms": "连拍总跨度。同 mouse_click。",
    "effect_strip": "前后对比条带图。同 mouse_click。",
}
_TIER_DESC_REF = {
    "human": "同 mouse_move。",
    "speed": "同 mouse_move。",
    "precise": "同 mouse_move。",
}
_GUIDE_REF = "回执默认带图（include_image=false 关）；多步用 sequence。"
_AXIS_REF = "坐标看图内刻度（见 capture_screenshot）。"
_COORD_REF = "坐标＝图内坐标（见 capture_screenshot）。"

TOOLS = [

    # ---- 桌面通用 ----
    {
        "name": "capture_screenshot",
        "description": "【截图—只读，不抢焦点】截取全屏/窗口/显示器像素画面，默认带图返回（include_image=false 关）、默认不落盘（传 save_path 才写）。不做 OCR/识别。主路径＝看整图按图内坐标一次点完；只有小目标/核对细节才传 rect='x,y,w,h' 取 1:1 局部（多一次调用，别反复叠局部图）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "save_path": {"type": "string", "description": "要存图才传：写到该路径并回路径。"},
                "region": {"type": "string",
                           "description": ("'fullscreen'全屏 / 'window'窗口（PrintWindow 优先，被遮挡也能抓该窗画面） / 'monitor_N'。"
                                           "不传＝按上下文自动：传了 window/window_title，或只给 rect 且最近截图是窗口图 ⇒ window"
                                           "（rect 归该窗裁、不抓前台遮挡者）；否则 fullscreen。")},
                "rect": {"type": "string", "description": "小目标/核对细节才传：'x,y,w,h'（图内坐标），只回这一块（1:1 不缩、沿用同轴；越界夹边、格式错报错）。", "default": ""},
                "window_title": {"type": "string", "description": "窗口标题关键字（region='window'时指定）", "default": ""},
                "include_image": {"type": "boolean", "description": "是否把图片本身一并返回（默认 true）", "default": True},
                "mark_style": {"type": "string", "enum": ["grid", "center"],
                               "default": _MARK_STYLE_DEFAULT,
                               "description": ("附图标注方式：center（默认）＝只画刻度（内容零遮挡）；"
                                               "grid＝刻度＋棋盘虚线（数字＝**图内坐标**；"
                                               "主/中刻度延伸进内容成虚线）。一次只出一张图。")},
                "grid_dash": {"type": "integer", "minimum": 4, "maximum": 100,
                              "description": ("（只对 mark_style=grid 生效）棋盘虚线**段长**（px，默认 20）："
                                              "画一段、空一段，段长＝空长；段头相位锁图内坐标的整数倍。")},
                "grid_color": {"type": "string", "enum": ["auto", "black", "white"],
                               "description": ("（只对 mark_style=grid 生效）棋盘线颜色：auto（默认）＝逐像素反色"
                                               "（浅底黑线、深底白线）；black／white＝强制单色。")},
                "ruler_inside": {"type": "boolean", "default": False,
                                 "description": ("把刻度尺挪到**图内**（默认 false＝图外白底边条）；true＝刻度线/数字画在"
                                                 "内容边缘内侧、不加外部边条（画布＝内容，预览即原图 1:1）；"
                                                 "颜色按底色自动反色（浅底黑/深底白）。对 center/grid 都生效。")},
                "fps": {"type": "number", "description": "采样率（最快 16 帧/秒）", "default": 8},
                "sensitivity": {"type": "number",
                                "description": "变化灵敏度：>1 更密、<1 更省；细调见 cut_abs/integral_sec/dup_min。",
                                "default": 1.0},
                "cut_abs": {"type": "number", "description": "切点绝对地板（0–255；默认按灵敏度算）"},
                "integral_sec": {"type": "number", "description": "积分基准秒数（默认按灵敏度缩放）"},
                "dup_min": {"type": "number", "description": "差异小于它就不留帧"},
                "watch_ms": {"type": "integer",
                             "description": "start 用：后台概览采样这么多毫秒后拼一张图（tiers）；立即返回，完成后自动注入；受 vision_seconds 限时。",
                             "default": 0},
                "frames": {"type": "integer",
                           "description": "start 用：固定间隔抽 N 帧拼概览（>1 生效）。", "default": 1},
                "interval_ms": {"type": "integer", "description": "抽帧间隔毫秒（配合 frames + start）。", "default": 200},
                "min_frames": {"type": "integer", "description": "概览：凑够几帧。", "default": 3},
                "max_frames": {"type": "integer", "description": "概览：最多几帧（sheet_frames 优先）。", "default": 6},
                "sheet_frames": {"type": "integer",
                                 "minimum": 1, "maximum": 12,
                                 "description": "tiers 最多装几帧；不传沿用 max_frames/vision_max_frames。"},
                "hero_history": {"type": "integer",
                                 "minimum": 1, "maximum": 6,
                                 "description": "hero 历史小格数（1–6，默认 2）；start 时定，pull 可临时覆盖。"},
                "push_interval_ms": {"type": "integer", "minimum": 0, "maximum": 120000,
                                     "description": "自动注入最短间隔（默认 10000ms；0＝不节流）。"},
                "grid_cols": {"type": "integer", "description": "概览拼图列数（0＝自动）", "default": 0},
                "tile_max": {"type": "integer", "description": "概览拼图单格最长边（0＝自动）", "default": 0},
                "vision_action": {"type": "string",
                                  "description": "start＝后台观察（配 watch_ms/frames 为概览，否则持续会话）；pull＝取增量；stop/status；peek＝后台单拍（独立、不自动续）。到期/空闲超时会自动注入提示。",
                                  "default": ""},
                "vision_id": {"type": "string", "description": "持续会话号（pull/stop 用；不传＝最近一个）。", "default": ""},
                "after_ms": {"type": "integer",
                             "description": "peek 用：延迟这么多毫秒再抓一张（上限 10 分钟）；抓完自动注入下一条消息。",
                             "default": 0},
                "vision_max_frames": {"type": "integer", "description": "pull 一次最多回几帧。", "default": 4},
                "vision_ring": {"type": "integer", "description": "会话环形缓冲保留多少帧（旧的自动丢）。", "default": 12},
                "vision_seconds": {"type": "integer",
                                   "description": "持续会话存活上限（秒）；0＝不限时。start 时定新会话限时，pull 时传＝中途改（按原 start 算剩余）；新 start 覆盖旧会话。",
                                   "default": 120},
                "layout": {"type": "string", "enum": ["hero", "tiers", "grid"],
                           "description": "pull 取图版式：hero（默认，最新帧大图＋变化区裁片＋历史小格）/ tiers（按档位拼图）/ grid（等分网格）。",
                           "default": "hero"},
                "hero_split": {"type": "boolean",
                               "description": "仅 hero：true＝返回两张图（最新帧＋裁片）；默认合成一张。",
                               "default": False},
            },
            "required": [],
        },
    },
    {
        "name": "send_keystrokes",
        "description": "【输入文本/执行命令—自动前/后台】向已聚焦窗口发送文本：有终端子控件走 PostMessage 后台，否则 SendInput 前台。先用 focus_window 或用 window= 一步指定。press_enter=true 末尾回车；组合键用 send_shortcut；human=true 慢速逐字（≈90ms/字）；shell 对话框（如「运行」框）走扫描码真打字。通道由 mode 决定并写进回执：background 全程 PostMessage、不抢前台、不碰输入法；foreground/auto 会先切前台。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "text": {"type": "string", "description": "要输入的文本"},
                "unicode_text": {"type": "boolean", "description": "允许直发非 ASCII（不走输入法）；默认 false＝非 ASCII 报错、一个字不发（中文走 ime_mode）。DirectInput 类应用通常收不到。", "default": False},
                "press_enter": {"type": "boolean", "description": "输入后按回车（与正文同一条有序流，不会抢跑到字前面）。", "default": False},
                "mode": {"type": "string", "description": "auto（默认，自动判后台/前台）/ foreground（SendInput 前台并保证该窗在前台）/ background（PostMessage 后台：不抢前台、不切前台、不动输入法；回车也走后台队列）。物理按键（press/release/hold_duration/scroll_clicks）一律走前台。"},
                "human": {"type": "boolean", "description": "true＝逐字≈90ms（≈10 字/秒）；不传＝快档≈10ms/字。", "default": False},
                "speed": {"type": "number", "description": "打字速度（字/秒，仅 human=true；夹在 0.5~10，超上限夹住并报）。"},
                "ime_mode": {"type": "string", "description": "temp（默认，切英文→打完还原）/ perm / auto（读得到中文态才切）/ off（不碰）。只对纯 ASCII 有效；意图中文⇒off；自管 IME 的窗（WPS 等）不自动切并提示截图核对。", "default": "temp"},
            },
            "required": [],
        },
    },
    {
        "name": "send_shortcut",
        "description": "【发送按键—仅前台】用 {KEY} 语法发组合键/特殊键（如 {CTRL+L}、{CTRL+ENTER+ESC}，也可带普通字符，如 'ihello{ESC}:wq{ENTER}'）。先用 focus_window 或用 window= 一步指定；仅前台，interval/typing_interval 控速。⚠ 别用 {ALT+F4} 关窗（容易关错），关窗用 mouse_click 点标题栏 ✕。中文→英文前先发 {ESC} 清残留，否则首个空格被当作提交键吃掉。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "text": {"type": "string", "description": "按键文本。{ESC}{ENTER}{TAB}{BS}{CTRL+C}{ALT+F4}{F1-F12}{UP}{DOWN} 等均可嵌套"},
                "unicode_text": {"type": "boolean", "description": "同 send_keystrokes。", "default": False},
                "interval": {"type": "number", "description": "块间间隔秒数（不传＝人手化）。"},
                "typing_interval": {"type": "number", "description": "字符间间隔秒数（不传≈25ms/字）。"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "focus_window",
        "description": "【聚焦窗口—前置步骤】按标题/类名/进程名/PID 查找并激活窗口，多结果取第一个并列出全部；返回窗口详情(class/pid/进程名)。keyword/class/process/pid 至少一个。只激活可见窗口：命中全是 DWM 隐藏幽灵窗时拒绝并列出（聚焦幽灵窗拉不出前台）；要列全部（含隐藏、带标记）用 list_windows。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "窗口标题关键字（部分匹配）"},
                "class": {"type": "string", "description": "窗口类名（部分匹配）"},
                "process": {"type": "string", "description": "进程名（部分匹配，如 'Xshell.exe'）"},
                "pid": {"type": "integer", "description": "进程 ID（精确匹配）"},
                "exact": {"type": "boolean", "description": "true=精确匹配，默认 false=子串匹配", "default": False},
                "case_sensitive": {"type": "boolean", "description": "true=区分大小写，默认 false", "default": False},
                "regex": {"type": "boolean", "description": "true=正则匹配，默认 false", "default": False},
                "index": {"type": "integer", "description": "多结果时选择第几个（1-based），默认 1"},
                "app_type": {"type": "string", "description": "应用类型: 'terminal'(默认) / 'vnc' / 'general'"},
                "target": {"type": "string", "description": "目标: 'auto'(默认), 'popup', 'main'"},
                "capture_region": {"type": "string", "description": "截图区域：'window'（默认，操作面窗口）/ 'fullscreen'（全屏）", "default": "window"},
            },
            "required": [],
        },
    },
    # ---- 鼠标操作（mouse_event 前台，需先 focus_window） ----
    {
        "name": "mouse_click",
        "description": "【点击鼠标—mouse_event 前台，不受UIPI限制】在**图内坐标 (x,y)** 点击（自动激活窗口＋点中心聚焦，左键自动双击补偿，点击后验证变化）。必须：先调用过 focus_window 或用 window= 一步指定。需要就地点击时用 relative=true：按增量 (Δx,Δy) 相对移动后在当前位置点击（Δ=0 即纯就地点击）；建议配 verify_effect=false 省差分。human=true 走人影档（≈2500px/s，夹 0.14~0.60s），只影响接近路速度；太快 Windows 可能丢掉点击 ⇒ 没反应就改 human=true。两档默认都走人手化轨迹，档位只决定速度；人影档不许瞬移、不许 precise；precise=true（仅快速档）＝精确直线。dwell_ms＝落点后按下前停顿（ms，0~2000）——远控/RDP 200~300、串流 300~500 更稳。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "x": {"type": "integer", "description": "屏幕 x 坐标"},
                "y": {"type": "integer", "description": "屏幕 y 坐标"},
                "button": {"type": "string", "description": "按键: 'left'(默认) / 'right' / 'middle'", "default": "left"},
                "click_count": {"type": "integer", "description": "点击次数: 1(单击) / 2(双击)", "default": 1},
                "method": {"type": "string", "description": "已废弃，仅 mouse_event 前台点击", "default": "sendinput"},
                "text": {"type": "string", "description": "点击后自动输入文本(可选)，省去单独send_keystrokes"},
                "unicode_text": {"type": "boolean", "description": "点击后自动输入文本时是否直发非 ASCII；同 send_keystrokes。", "default": False},
                "press_enter": {"type": "boolean", "description": "输入文本后按回车(配合text使用)", "default": False},
                "relative": {"type": "boolean", "description": "就地点击：true 时按增量 (Δx,Δy) 相对移动后在当前位置点击（不走近路）；Δ=0 即纯就地点击。", "default": False},
                "human": {"type": "boolean", "description": "人影档：true=接近路走慢一档（≈2500 像素/秒，夹 0.14~0.60 秒）；轨迹算法不变，只是变慢。默认 false＝快档（≈4000 像素/秒）。★只影响接近路；relative=true 时不走接近路，故不受它影响", "default": False},
                "speed": {"type": "number", "description": "★**只与 human=true 搭配**：人影档内自选接近路速度（像素/秒），上限＝移动人影档上限 2500；超出夹到上限并写进回执。接近路按它走；**relative=true 时就地点击前那段增量也按它切块铺时间**"},
                "precise": {"type": "boolean", "description": "★两档区别：**人影档与快速档默认都走人手化轨迹（抖动＝那套轨迹算法）**，档位只决定速度（人影档更慢）。本参数**只给快速档**：true＝**精确移动、不走抖动**（确定性直线、终点精确命中）；**人影档传它 ⇒ 报错**；**瞬移（duration=0）同样只在快速档可用**。", "default": False},
                 "dwell_ms": {"type": "integer", "description": "落点后按下前停顿（ms，0~2000）；远控/RDP 200~300、串流 300~500 更稳。", "minimum": 0, "maximum": 2000},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "mouse_move",
        "description": "【移动鼠标—人手化轨迹·前台】把光标走到 (x,y)（缓入缓出＋抖动＋按距离缩放的弧线，到手有微调）。duration：不传按距离自适应（≈4000px/s，夹 0.08~0.40s）；0＝瞬移；>0＝秒数。必须：先调用过 focus_window 或用 window= 一步指定。human=true 人影档（≈2500px/s，夹 0.14~0.60s），轨迹不变只变慢。需要相对位移时用 relative=true：x,y 是增量 (dx,dy)，走 mouse_event（只认 raw delta 的应用才吃这条；无屏幕边界；受指针加速影响可能放大，回执只报请求位移）。两档默认都走人手化轨迹；人影档不许瞬移、不许 precise；precise=true（仅快速档）＝不走抖动。回执自报实测耗时与步间区间。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "x": {"type": "integer", "description": "目标 x 坐标"},
                "y": {"type": "integer", "description": "目标 y 坐标"},
                "duration": {"type": "number", "description": "移动秒数；不传＝按距离自适应；0＝瞬移（relative 时忽略）。"},
                "human": {"type": "boolean", "description": "true＝人影档（≈2500px/s）；轨迹不变只变慢。", "default": False},
                "speed": {"type": "number", "description": "人影档速度 px/s（仅 human=true；上限 2500；relative 时同样有效）。"},
                "relative": {"type": "boolean", "description": "相对位移：x,y 是增量 (dx,dy)，走 mouse_event；无屏幕边界；受指针加速影响可能放大。", "default": False},
                "nocoalesce": {"type": "boolean", "description": "仅 relative=true：附加 MOVE_NOCOALESCE，只抑制 WM_MOUSEMOVE 合并，不影响 raw input。", "default": False},
                "precise": {"type": "boolean", "description": "仅快速档：true＝精确不走抖动；人影档传报错；瞬移仅快速档可用。", "default": False},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "mouse_drag",
        "description": "【拖拽鼠标—人手化轨迹·前台】从起点按住拖到终点松开，按住段同样人手化轨迹。终点必填；起点默认取已聚焦窗口标题栏中心。duration：不传按距离自适应（≈2500px/s，夹 0.12~0.60s）；0＝直接落点。必须：先调用过 focus_window 或用 window= 一步指定。human=true 人影档（≈1600px/s，夹 0.20~0.80s）。⚠ 拖拽也吃速度：太快可能没拖起来 ⇒ 先传 human=true。两档默认都走人手化轨迹；人影档不许瞬移、不许 precise；precise=true（仅快速档）＝不走抖动。拖拽两态：默认按住→拖到终点→自动松手；hold=true 拖完保持按住（跨调用保留，回执提醒，release=true 只松手）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "end_x": {"type": "integer", "description": "终点 x 坐标"},
                "end_y": {"type": "integer", "description": "终点 y 坐标"},
                "start_x": {"type": "integer", "description": "起点 x 坐标（可选）"},
                "start_y": {"type": "integer", "description": "起点 y 坐标（可选）"},
                "button": {"type": "string", "description": "按键: 'left'(默认) / 'right' / 'middle'", "default": "left"},
                "duration": {"type": "number", "description": "拖拽秒数；不传＝按距离自适应；0＝直接落点。"},
                "human": {"type": "boolean", "description": "true＝人影档（≈1600px/s）；轨迹不变只变慢。", "default": False},
                "speed": {"type": "number", "description": "人影档拖拽速度 px/s（仅 human=true；上限 1600）。"},
                "precise": {"type": "boolean", "description": "仅快速档：true＝精确不走抖动；人影档传报错。", "default": False},
                "hold": {"type": "boolean", "description": "true＝拖完保持按住（跨调用保留；回执提醒；松开用 release=true）。", "default": False},
                "release": {"type": "boolean", "description": "true＝只松开之前 hold 住的键（不移动、不新按）。", "default": False},
            },
            "required": [],
        },
    },
    {
        "name": "mouse_transfer",
        "description": "【搬移对象—通用三模式，纯像素验证】把一个位置的对象搬到另一个位置。mode：drag＝按下→移动→松开；click＝点起→移动→点落；auto＝先试 drag，未生效则安全撤销再试 click（默认）。每阶段都做纯像素生效判定（changed/diff_ratio/bbox，不触发 OCR/UIA）。safe_park＝结束后光标停到空场坐标（不点击）。必须：先调用过 focus_window 或用 window= 一步指定。verify_effect=false 可整体关判定。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "source": {"type": "string", "description": "起点坐标 'x,y'（图内坐标）。"},
                "target": {"type": "string", "description": "终点坐标 'x,y'（图内坐标）。"},
                "mode": {"type": "string", "enum": ["drag", "click", "auto"],
                         "description": "drag＝拖；click＝点起→点落；auto＝先 drag，未生效撤销后试 click（默认）。",
                         "default": "auto"},
                "button": {"type": "string", "description": "'left'(默认) / 'right'", "default": "left"},
                "human": {"type": "boolean", "description": "人影档：true=全程放慢一档（移动≈2500／拖拽≈1600 像素/秒）；轨迹算法不变", "default": False},
                "speed": {"type": "number", "description": "★**只与 human=true 搭配**：人影档内的速度（像素/秒），上限由移动/拖拽各自档位夹取并写进回执"},
                "precise": {"type": "boolean", "description": "★两档区别：**两档默认都走人手化轨迹（抖动）**；本参数**只给快速档**：true＝精确、不走抖动（确定性直线）；**人影档传它 ⇒ 报错**", "default": False},
                **_EFFECT_PROPS,
                "safe_park": {"type": "string", "description": "结束后把光标停到空场坐标（不点击）。", "default": ""},
                "undo": {"type": "string", "enum": ["auto", "release", "click_back", "escape", "none"],
                         "description": "auto 模式未生效时的撤销方式：auto/release/click_back/escape/none。",
                         "default": "auto"},
            },
            "required": ["source", "target"],
        },
    },
    {
        "name": "hover",
        "description": "【悬停—移动→停留→出图】把光标移到 (x,y) 并停留 dwell_ms（默认 600ms），用于读悬停提示/高亮/预览；"
                       "纯像素（只移动＋等待＋截图，不点击、不识别）。rect 可选：只回那一小块（图内坐标，省 token）；不传＝整张附图。"
                       "必须：先调用过 focus_window 或用 window= 一步指定。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "x": {"type": "integer", "description": "屏幕 x 坐标"},
                "y": {"type": "integer", "description": "屏幕 y 坐标"},
                "dwell_ms": {"type": "integer", "description": "停留毫秒数（0~5000）。", "default": 600},
                "rect": {"type": "string", "description": "只回这一小块 'x,y,w,h'（图内坐标，省 token）。", "default": ""},
                "human": {"type": "boolean", "description": "人影档：true=接近路放慢一档（≈2500 像素/秒）；轨迹算法不变", "default": False},
                "speed": {"type": "number", "description": "★**只与 human=true 搭配**：人影档内的接近路速度（像素/秒），上限 2500"},
                "precise": {"type": "boolean", "description": "★两档区别：**两档默认都走人手化轨迹（抖动）**；本参数**只给快速档**：true＝精确直线（终点精确）；**人影档传它 ⇒ 报错**", "default": False},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "set_window",
        "description": "【调整窗口位置/大小—不抢焦点】设置已聚焦窗口的位置和尺寸，x/y/width/height 全可选，传什么改什么，不传保持不变。必须：先调用过 focus_window 或用 window= 一步指定。自动选弹窗优先，不抢焦点。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "x": {"type": "integer", "description": "左上角 x 坐标（可选，不传保持不变）"},
                "y": {"type": "integer", "description": "左上角 y 坐标（可选，不传保持不变）"},
                "width": {"type": "integer", "description": "窗口宽度（可选，不传保持不变）"},
                "height": {"type": "integer", "description": "窗口高度（可选，不传保持不变）"},
            },
            "required": [],
        },
    },
    {
        "name": "scroll",
        "description": "【滚动鼠标】在已聚焦窗口滚轮，clicks 正数向上、负数向下；终端→PostMessage 后台，其他→mouse_event 前台。必须：先调用过 focus_window 或用 window= 一步指定。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "clicks": {"type": "integer", "description": "滚动格数: 正数向上翻，负数向下翻"},
                "x": {"type": "integer", "description": "滚动位置 x（可选）"},
                "y": {"type": "integer", "description": "滚动位置 y（可选）"},
                "mode": {"type": "string", "description": "手动选择: 'background'(后台PostMessage) / 'foreground'(前台mouse_event)，默认空=自动"},
                "verify": {"type": "boolean", "description": "是否回读验证滚动结果，默认 false", "default": False},
            },
            "required": ["clicks"],
        },
    },
    {
        "name": "get_mouse_position",
        "description": "【获取鼠标坐标—纯只读】返回当前鼠标指针的**图内坐标**（0 起，工具自动换算）。不改焦点。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "open_desktop_item",
        "description": "【操作桌面图标—COM 直连】通过 COM 操作桌面快捷方式（无需截图）：按 keyword 模糊匹配并打开桌面图标，或 mode='scan' 列出全部桌面项。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "桌面图标名关键字（模糊匹配）。scan 模式时可省略"},
                "mode": {"type": "string", "description": "操作模式: 'open'(默认,直接打开) / 'scan'(列出所有桌面项)", "default": "open"},
                "verb": {"type": "string", "description": "Shell 动词: open(默认)/edit/properties/runas", "default": "open"},
            },
            "required": [],
        },
    },
    # ---- 系统信息 ----
    {
        "name": "list_windows",
        "description": "【列举窗口—只读】列窗口（进程名、标题、位置、尺寸、子窗口树），可按 keyword 过滤，不改焦点。默认只列屏幕上真正显示着的窗口（Win32 可见且未被 DWM cloaked）；include_hidden=true 连幽灵窗一起列（标注 [DWM隐藏]）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "按关键字过滤窗口标题（可选）"},
                "include_hidden": {"type": "boolean", "description": "默认 false=只列显示着的窗口；true=连 DWM 隐藏的幽灵窗一起列（会标注 [DWM隐藏]）"},
            },
            "required": [],
        },
    },
    # ---- 等待工具 ----
    {
        "name": "wait",
        "description": "【等待 / 条件监听】两种用法：① 只传 seconds＝纯等待（睡满，ESC+` 可取消）；"
                       "② 再加 conditions（1 个或多个条件同时监听；seconds 是总上限，提前达成立即返回，超时报每个条件最后状态）。"
                       "match：any（默认）先到先返／all 全部满足才返。"
                       "条件：window_appear/gone、image_appear/gone（模板匹配）、region_changed（开始变化）、region_stable（静止）。"
                       "template 支持图片文件路径、rect:x,y,w,h（最近附图裁块）、base64。"
                       "成功默认附图（include_image=false 关）；本工具不点击、不搬运（找图/点图用 find_click）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number",
                            "description": "等待秒数（可小数）：纯等待＝睡满；条件模式＝总上限。",
                            "default": 1},
                "conditions": {
                    "type": "array",
                    "description": "条件数组（也可只传一个对象）：每个条件 {mode, window?|template?|rect?, threshold?, stable_ms?}；多个同时监听。"
                                   "mode＝window_appear/gone、image_appear/gone、region_changed/stable；"
                                   "window 条件给 window；image 给 template（文件/rect:x,y,w,h/base64）＋threshold（默认 0.85）；"
                                   "region 给 rect（图内坐标）＋threshold（默认 0.002）＋stable_ms（默认 300）。",
                    "items": {"type": "object"},
                },
                "match": {"type": "string", "enum": ["any", "all"],
                          "description": "any（默认）先到先返／all 全部满足才返。",
                          "default": "any"},
                "window": {"type": "string", "description": "可选：本次操作面窗口（标题子串或 hwnd）。不传＝当前聚焦窗口。"},
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "find_click",
        "description": "【寻找点击—模板匹配找图/按图点/按图搬】拿模板图在屏幕上找它（纯像素，不做 OCR/识别），"
                       "再按 action 找位置/点过去/搬运。模板来源：图片文件路径、rect:x,y,w,h（最近附图上裁一块）、base64。"
                       "action：find（默认，返回所有匹配的图内中心坐标＋相似度）；click（找到就点，pick 选哪个）；"
                       "transfer（两张图＝起点/终点 source/target，等价 mouse_transfer，mode=drag|click|auto）。"
                       "pick：best（默认，最像）/near（离光标最近）/far（最远）/all（全点）；挑中间那个给 dist＝距离值（取离光标距离最接近该值的一个，0≈最近、很大≈最远）。"
                       "click/transfer 默认带纯像素判定，verify_effect=false 关。"
                       "附图默认开（include_image=false 关）：红框＝选中、绿框＝其他候选。"
                       "不在里面等图——等图用 wait 的 image_* 条件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template": {"type": "string",
                             "description": "单张模板（find/click）：图片文件路径｜rect:x,y,w,h（最近附图上裁）｜base64 PNG。"},
                "source": {"type": "string",
                           "description": "transfer 起点模板（来源同 template），从其中心开始搬。"},
                "target": {"type": "string",
                           "description": "transfer 终点模板（来源同 template），搬到其中心。"},
                "action": {"type": "string", "enum": ["find", "click", "transfer"],
                           "description": "find＝找位置（返回图内中心＋相似度）；click＝找到就点；transfer＝两张图搬运。",
                           "default": "find"},
                "pick": {"type": "string", "enum": ["best", "near", "far", "all"],
                         "description": "click 时点哪个：best（默认，最像）/near（最近）/far（最远）/all（全点）；挑中间用 dist。transfer 只取一个（all 不允许）。",
                         "default": "best"},
                "dist": {"type": "number",
                         "description": "按距离值选（像素，相对光标）：取离光标距离最接近该值的一个；0≈最近、很大≈最远；优先于 pick 单选（pick=all 仍全点）。"},
                "threshold": {"type": "number", "description": "相似度阈值（0–1，默认 0.85；越小越宽松）。", "default": 0.85},
                "region": {"type": "string", "description": "只在这块区域内找（图内坐标 x,y,w,h；不传＝整屏）。"},
                "mode": {"type": "string", "enum": ["drag", "click", "auto"],
                         "description": "transfer 用：drag/click/auto（auto＝先 drag，未生效撤销再 click）。",
                         "default": "auto"},
                "button": {"type": "string", "enum": ["left", "right"], "description": "用哪个键（默认 left）。", "default": "left"},
                "count": {"type": "integer", "description": "click 时每处点几下（默认 1）。", "default": 1},
                "dwell_ms": {"type": "integer", "description": "走到命中点后、按下前停顿（ms，0–2000；远控/串流建议 200–500）。"},
                "human": {"type": "boolean", "description": "同 mouse_move。", "default": False},
                "speed": {"type": "number", "description": "同 mouse_move。"},
                "precise": {"type": "boolean", "description": "同 mouse_move。", "default": False},
                "verify_effect": {"type": "boolean", "description": "click/transfer 纯像素差分（默认 true；false＝只执行不判定）。", "default": True},
                "settle_ms": {"type": "integer", "description": "判定稳定等待（默认 120ms）。"},
                "safe_park": {"type": "string", "description": "transfer 结束后光标停到这块空场（图内坐标，不点击）。"},
                "undo": {"type": "string", "enum": ["auto", "release", "click_back", "escape", "none"],
                         "description": "auto 模式 drag 未生效时的撤销方式：auto/release/click_back/escape/none。", "default": "auto"},
                "window": {"type": "string", "description": "可选：本次操作面窗口（标题子串或 hwnd）。"},
            },
            "required": [],
        },
    },
    {
        "name": "sequence",
        "description": "【输入序列 / 鼠标宏 · 键盘宏】把一段按时间轴编排的鼠标＋键盘动作一次跑完（短＝边想边发；长＝宏）。宏单槽：action=save 存、saved 看、run_saved 跑（可传 steps 临时改几步、不覆盖槽位）。"
                       "元素三分：数字＝停顿（秒，唯一时间写法）｜字符串＝文字输入（原样打出，含 {ctrl+c} 这类组合键；「0.5」＝打这三个字）｜对象＝动作或容器。"
                       "步型覆盖全部工具的动作用法——键鼠：key_down/key_up/key/hold/combo、move（[dx,dy] 默认相对；步内 relative=false 绝对，可加 duration，0＝瞬移）、"
                       "click（当前光标处；at＝图内坐标先走过去）、drag（可带 from）、transfer（from→to）、wheel、hover、mouse_pos、text（完整打字）；"
                       "窗口与系统：focus、set_window、open、windows、shot（途中截图，可 after_ms 延迟）；感知与等待：find_click、wait（条件等待）。"
                       "容器：带 track＝轨（多轨同时跑）；带 steps＝块（repeat/gap_ms）；都不写＝单轨严格顺序。"
                       "坐标：绝对步按最近一次附图的图内坐标解释；relative 增量仍是屏幕像素。先用 focus_window 或用 window= 一步指定（聚焦只在开头一次）。"
                       "安全：timeout_ms 默认 60000 总超时；超时/ESC+`/异常立刻停并抬起按下的键；相对位移受指针加速影响（回执只报请求位移）。"
                       "两档默认都走人手化轨迹，档位只决定速度；precise=true（仅快速档）＝精确；human=true 走人影档，绝对步可配 speed。"
                       "action：run（默认同步）｜append（排队）｜overwrite（顶掉旧的）｜list｜shot（取当前画面）｜stop（id=all 停全部）｜save｜saved｜run_saved。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "可选：指定本次作用的窗口（标题子串或 hwnd）。不是当前记住的那扇 → 先聚焦并记住再执行；已经是它 → 跳过聚焦（不重复抢前台）。不传＝照旧用记住的窗口。"},
                "steps": {
                    "type": "array",
                    "description": "动作与停顿的序列（唯一形状＝数组）：① 步骤：数字＝停顿（秒）；字符串＝文字输入（含 {ctrl+c} 组合键；「0.5」是三个字不是时间）；或动作对象。② 容器：带 track＝轨（多轨同时跑、各自从 0 开始）；带 steps＝块（就地重复 {steps, repeat, gap_ms}）；都不写＝单轨严格顺序。",
                    "items": {"description": "动作对象（key_down/key_up/key/hold/combo/move/click/drag/transfer/hover/wheel/text/focus/set_window/open/windows/shot/mouse_pos/find_click/wait）、数字（停顿，秒）、字符串（文字输入）、一条轨（{track, steps}）或一个块（{steps, repeat, gap_ms}）。"},
                },
                "timeout_ms": {"type": "integer", "description": "总超时毫秒（默认 60000），防卡死。", "default": 60000},
                "relative": {"type": "boolean", "description": "move 步默认语义：true＝增量，false＝绝对坐标；步内可覆盖。", "default": True},
                "human": {"type": "boolean", "description": "鼠标档位（只对绝对移动步有效）：true＝人影档（≈2500 像素/秒，夹 0.14~0.60 秒）；默认 false＝快档（≈4000 像素/秒）。步内可写 human 单独覆盖；相对步不受影响。人影档不许 precise、不许瞬移（duration=0）。", "default": False},
                "precise": {"type": "boolean", "description": "只给快速档：true＝精确、不走抖动（绝对步＝确定性直线；相对步＝一次直发 raw delta）；人影档传它报错；瞬移（duration=0）也只在快速档可用。步内可写 precise 逐条覆盖。", "default": False},
                "speed": {"type": "number", "description": "只与 human=true 搭配：人影档内自选速度（像素/秒，例 300 很慢／600 正常／1000 偏快），上限＝移动 2500／拖拽 1600，超出夹到上限并写进回执。步内可写 speed；human=false 传它报错。绝对步按它算轨迹时长；相对步按它切块铺时间。"},
                "ime_ascii": {"type": "boolean", "description": "打字前的输入法守卫：纯 ASCII 文本注入前把目标窗切英文、打完恢复（默认 true，与 send_keystrokes 同规则）。目标窗中文态下 `r3 alpha ` 会被打成「加 alpha 加 beta」。要故意用输入法打字（拼音→汉字）就传 false；步内可写 ime_ascii:false 单步关掉；mode=background 走 WM_CHAR、不经过输入法，那条不守。", "default": True},
                "unicode_text": {"type": "boolean", "description": "允许序列里直发非 ASCII（中文/emoji 等，走 KEYEVENTF_UNICODE、不经过输入法）。默认 false＝字符串步/text 步出现非 ASCII 当场报错（不静默丢字）。步内可写 unicode_text:true 覆盖。混排时先切英文再打 ASCII 段。", "default": False},
                "ime_mode": {"type": "string", "description": "整条序列的输入法切换规则：temp（默认，每次打字前切英文、打完还原）｜perm（切了不还原）｜auto（**读着判断**：读得到中文态才切、读不到不碰；切了按 temp 规则还原）｜off（不碰输入法）。只做「防被吃」一个方向。步内可写 ime_mode 单步覆盖。", "default": "temp"},
                # action 一个开关管「跑/排队/顶掉/查」；list/shot/stop 不需要 steps ⇒ required 放宽成 []，
                # 由分发层判 action∈{run,append,overwrite} 时 steps 必填（报错比静默好）。
                "action": {"type": "string", "enum": ["run", "append", "overwrite", "list", "shot", "stop", "save", "saved", "run_saved"],
                           "description": "run（默认同步）｜append（排队）｜overwrite（顶掉旧的）｜list｜shot（取当前画面）｜stop（id=all 停全部）｜save（存单槽）｜saved｜run_saved（跑已存，可传 steps 临时改）。",
                           "default": "run"},
                "note": {"type": "string", "description": "action=save 用：一句话备注（saved 时显示）。"},
                "id": {"type": "string", "description": "stop 用：要停的 id（all＝全部）。"},
                "with_steps": {"type": "boolean", "description": "list 用：true＝连内容一起列（默认）；false＝只状态。", "default": True},
                "shot_region": {"type": "string", "description": "shot 用：window（默认）/fullscreen；内存取图不落盘。", "default": "window"},
            },
            "required": [],
        },
    },
]


# `no_recognize` 快速通道已整套删除（识别层不存在，参数没有意义）。
#   截图仍由 `include_image` 控制、带编号/原图由 `clean` 控制。
# 动作类工具统一注入纯像素生效判定参数（一处定义、三处生效；不碰 OCR/UIA）。
for _t in TOOLS:
    if _t["name"] in ("mouse_click", "mouse_drag", "mouse_transfer"):
        _pr = _t.setdefault("inputSchema", {}).setdefault("properties", {})
        for _k, _v in _EFFECT_PROPS.items():
            _pr.setdefault(_k, dict(_v))
        if _t["name"] != "mouse_click":          # 生效判定八参数的完整说明只在 mouse_click
            for _k in _EFFECT_PROPS:
                if _k in _pr:
                    _pr[_k]["description"] = _EFF_DESC_REF[_k]
        if _t["name"] == "mouse_click":
            _t["description"] += "回执带纯像素生效判定（参数见本工具 verify_effect 等）。"
        elif _t["name"] == "mouse_drag":
            _t["description"] += "回执带纯像素生效判定（参数同 mouse_click）。"

# 观察能力写进工具面（只剩非阻塞的两条：单帧 / 持续会话；一次性拼图已取消）
for _t in TOOLS:
    if _t["name"] == "capture_screenshot":
        _t["description"] += ("观察三用法（全非阻塞）：① 单帧（不传观察参数）；"
                              "② 后台概览：vision_action=\"start\" + watch_ms/frames，完成自动注入拼图；"
                              "③ 持续会话：vision_action=\"start\"，有新内容自动注入（layout=hero）。")
    if _t["name"] == "sequence":
        _t["description"] += "抽帧拼图已取消；回看用持续会话 vision_action=start。"

# 工具面软提醒：动作类回执通常已附图，不必再单独截图；不需要看结果时可 include_image=false 跳过。
# 只加一句提示，不改变参数与行为。
_GUIDE_DESC_TOOLS = {"focus_window", "mouse_click", "mouse_move", "mouse_drag", "mouse_transfer",
                     "hover", "set_window", "scroll", "send_keystrokes", "send_shortcut",
                     "open_desktop_item", "wait", "sequence"}
_TOOL_GUIDE = "回执默认带图（include_image=false 关）；多步用 sequence。"
_CAPTURE_GUIDE = ("动作回执通常已带图，不必重复截图；连续观察用 vision_action=start"
                  "（或 watch_ms 概览、after_ms 单拍），完成后自动注入。")
for _t in TOOLS:
    if _t["name"] == "capture_screenshot":
        # 唯一的完整通用说明放在截图工具上（其余工具只留一行引用）
        _t["description"] += _CAPTURE_GUIDE + _TOOL_GUIDE
    elif _t["name"] in _GUIDE_DESC_TOOLS:
        _t["description"] += _GUIDE_REF

_INCLUDE_IMAGE_PROP = {
    "type": "boolean",
    "description": "true＝本次调用结束后附一张当前画面（动作类默认 true，只读类默认 false）；false 可关。"
}

_CLEAN_IMAGE_PROP = {
    "type": "boolean",
    "description": "默认 false＝回带标注的图（标注＝坐标尺）；true＝回原图（不标注），看细节/认字时用。"
}

_MARK_STYLE_PROP = {
    "type": "string",
    "enum": ["grid", "center"],
    "default": _MARK_STYLE_DEFAULT,
    "description": "center（默认）＝只画刻度；grid＝刻度＋棋盘虚线（数字＝图内坐标）。一次只出一张图。"
}

_GRID_DASH_PROP = {
    "type": "integer",
    "minimum": 4,
    "maximum": 100,
    "description": "（grid 用）棋盘虚线段长 px（默认 %d）；段头相位锁图内坐标。" % _MARK_GRID_DASH_DEFAULT
}

_GRID_COLOR_PROP = {
    "type": "string",
    "enum": ["auto", "black", "white"],
    "description": "（grid 用）auto（默认）＝逐像素反色；black/white＝强制单色。"
}

_RULER_INSIDE_PROP = {
    "type": "boolean",
    "default": False,
    "description": "true＝刻度尺画在图内（默认 false＝图外白底条）；颜色按底色自动反色。center/grid 都生效。"
}

def _mark_style_arg(v):
    """mark_style 唯一校验：只认工具面声明的 `grid` / `center`；其它（含已删的 `edge`）**直接报错**。

    刻意**不再**"认不出就回落默认"——工具面没有的值，后端就不该接受；静默回落会让模型
    以为那个值生效了（0.1.68 埋的坑，0.1.70 拆掉）。
    """
    s = str(v if v is not None else _MARK_STYLE_DEFAULT).strip().lower()
    if s not in ("center", "grid"):
        raise ValueError("mark_style 只认 grid / center（收到 %r）——工具面只有这两个值，不做兼容回落。" % (v,))
    return s


# 动作类工具默认附图：它们会改变屏幕/焦点，图最值钱；只读类默认关，避免每次调用都烧一张图。
_IMAGE_DEFAULT_ON = {"mouse_click", "mouse_drag", "send_keystrokes",
                     "send_shortcut", "scroll", "set_window", "open_desktop_item"}


def _want_image(name, args):
    """本次调用要不要附图。默认全部附图（include_image=false 可关）。"""
    v = args.get("include_image")
    if isinstance(v, bool):
        return v
    return True


def _shot_item(region="window", clean=False, mark_click=None,
               mark_style=None, grid_dash=None, grid_color=None, ruler_inside=None):
    """把**当前画面**按 image content 返回。

    ★附图出口与内部生效判定**共用同一条复用规则**（只允许「同一时刻」复用）：
      `_frame_take()` 五问全过才复用（含 ts 距今 ≤ `_PX_REUSE_S`＝50ms，同内部 `_PX_LAST` 一条常数），
      否则**现截**。跨工具调用必然 >50ms ⇒ 一定现截，不会端上一轮的旧帧。
      实测病根：`send_shortcut` 的附图与上一次 `mouse_click` 的附图**字节完全相同**——
      动作效果根本看不见（"点了像没点"）；旧版附图出口只看 stale/hwnd/pops，**没有时间维度**。
    region="window"（默认，跟随已聚焦窗口）或 "fullscreen"。失败返回 None，不影响工具本身结果。"""
    try:
        import base64
        data = _frame_take(region)                 # 同一时刻才复用（跨调用必然落空）
        _reused = bool(data)
        if not data:
            data = _grab_png_bytes(region, None)   # 现截：内存抓图，不落盘（见 _grab_png_bytes）
        if not data:
            return None
        # 唯一接入点：上面这份 `data` 就是那一次截图的产物；下面只把它分发给两条内存路径：
        #   · 给模型看的这条 ⇒ 在新画布上嵌编号（`_mark_attach_png` 不截图、不改原件）；
        #   · 识别/复用那条 ⇒ `_FRAME` 里那份字节保持干净。
        #   失败/关掉开关 ⇒ 原样返回。clean=True 走"原图"这条（不嵌号）；默认＝嵌号的那份。
        if not (clean or _MARK_CLEAN_DEBUG):
            try:
                data, _mbatch = _mark_attach_png(data, _focus_hwnd, region=region,
                                                 style=mark_style,
                                                 grid_dash=grid_dash, grid_color=grid_color,
                                                 ruler_inside=ruler_inside)
            except Exception as _me:
                print(f"[标记] 跳过（不影响附图）：{_me}", file=sys.stderr)
        else:
            _axis_reset("最近附图是原图直出（未画坐标轴刻度）——请重新截图")
        # 附图：鼠标在截图范围内就画真实光标图标（不写字、不遮挡）；坐标走文字回执。
        #   设置页「附图显示真实光标」关掉时，图标和鼠标位置文字一起不出（应用自带光标时避免冲突）。
        if _CURSOR_SHOW_ON:
            try:
                _prev_note = _LAST_SHOT_NOTE
                _cp = get_mouse_position()
                _mx, _my = int(_cp.get("x", 0)), int(_cp.get("y", 0))
                _marked = _draw_cursor_icon(data, _focus_hwnd, region, _mx, _my)
                if _marked is not data:
                    data = _marked
                if _AXIS is None:
                    _mpos = "🖱 鼠标位置：（尚未建立图内坐标轴）"
                else:
                    _mpos = _axis_fmt(_mx, _my, "🖱 鼠标") or "🖱 光标不在本图内"
                if _reused:                       # 复用了就写明白（不静默：让人知道这次没重抓）
                    _mpos += "（附图：同一时刻复用，未重复抓屏）"
                _set_shot_note(((_prev_note + "；") if _prev_note else "") + _mpos)
            except Exception as _cme:
                print(f"[鼠标图标] 跳过（不影响附图）：{_cme}", file=sys.stderr)
        else:
            # 未叠光标：改成「怎么读应用自己画的光标」，别让模型去找插件画的箭头。
            try:
                _prev_note = _LAST_SHOT_NOTE
                _mpos = "🖱 未叠光标，以图里应用自己的光标为准，点击触发区自己看图判断"
                if _reused:
                    _mpos += "（附图：同一时刻复用，未重复抓屏）"
                _set_shot_note(((_prev_note + "；") if _prev_note else "") + _mpos)
            except Exception as _cme:
                print(f"[鼠标读法] 跳过（不影响附图）：{_cme}", file=sys.stderr)
        return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"}
    except Exception:
        return None


# 给常用工具补上 include_image ＋ mark_style ＋ mark_n ＋ 棋盘两参数（分发层统一处理）
#   ★（0.1.62）：**原图参数（clean）已从工具面收掉**——模型永远只拿带标定的图；
#     原图那条函数路径保留（识别/复用/内部调用都依赖它），要调试可用环境开关 MCP_MARK_CLEAN=1。
for _t in TOOLS:
    if _t["name"] in ("focus_window", "mouse_click", "mouse_drag", "mouse_move", "send_keystrokes",
                      "send_shortcut", "scroll", "set_window", "wait", "find_click",
                      "open_desktop_item", "sequence", "mouse_transfer", "hover"):
        _props = _t.setdefault("inputSchema", {}).setdefault("properties", {})
        _props["include_image"] = dict(_INCLUDE_IMAGE_PROP, description=_IMG_DESC_REF)
        _props["mark_style"] = dict(_MARK_STYLE_PROP, description=_MS_DESC_REF)
        _props["grid_dash"] = dict(_GRID_DASH_PROP, description=_GD_DESC_REF)
        _props["grid_color"] = dict(_GRID_COLOR_PROP, description=_GC_DESC_REF)
        _props["ruler_inside"] = dict(_RULER_INSIDE_PROP, description=_RI_DESC_REF)

# 完整说明的载体＝capture_screenshot：它自己的同名参数换成完整版（全文件唯一一份全文）
for _t in TOOLS:
    if _t["name"] == "capture_screenshot":
        _p = _t["inputSchema"]["properties"]
        _p["include_image"] = dict(_INCLUDE_IMAGE_PROP, default=True)
        _p["mark_style"] = dict(_MARK_STYLE_PROP)
        _p["grid_dash"] = dict(_GRID_DASH_PROP)
        _p["grid_color"] = dict(_GRID_COLOR_PROP)
        _p["ruler_inside"] = dict(_RULER_INSIDE_PROP)

for _t in TOOLS:
    # 附图刻度说明：完整一份只在 capture_screenshot，其余工具一行引用
    if _t["name"] == "capture_screenshot":
        _t["description"] += ("附图＝坐标轴刻度（默认）：刻度数字即图内坐标，原样传参；"
                              "rect 裁片沿用同一根轴（绝对位置，不从 0 重来）。"
                              "mark_style=center 只画刻度；ruler_inside=true 把刻度尺画进图内。")
    else:
        _t["description"] += _AXIS_REF

# ★0.1.88：会「传坐标」的工具统一口径——坐标＝最近一次附图的**图内坐标**（左上 0 起）
_AXIS_COORD_TOOLS = {"capture_screenshot", "mouse_click", "mouse_move", "mouse_drag",
                     "mouse_transfer", "hover", "scroll", "find_click", "sequence"}
for _t in TOOLS:
    if _t["name"] == "capture_screenshot":
        # 坐标口径的完整一份只在 capture_screenshot，其余工具一行引用
        _t["description"] += ("坐标参数一律按最近一次附图的图内坐标解释，越界报错；"
                              "优先整图一次完成，rect 只在小目标/核对细节时用。"
                              "（relative=true 的 x,y 是增量（屏幕像素），不参与图内坐标轴。）")
    elif _t["name"] in _AXIS_COORD_TOOLS:
        _t["description"] += _COORD_REF

# 工具面清理：描述里不写版本号、不放星号
for _t in TOOLS:
    try:
        _t["description"] = (_t["description"].replace("★", "")
                             .replace("（0.1.88）", "").replace("(0.1.88)", ""))
        _sch = _t.get("inputSchema") or {}
        for _pname, _pv in (_sch.get("properties") or {}).items():
            if isinstance(_pv, dict) and isinstance(_pv.get("description"), str):
                _pv["description"] = (_pv["description"].replace("★", "")
                                      .replace("（0.1.88）", "").replace("(0.1.88)", ""))
    except Exception:
        pass

# ── 公共参数说明归并（window／鼠标档位／输入法）＋死参数清理 ──
#   只改描述文本与工具面参数暴露；代码读取逻辑、默认值、行为一律不变。
for _t in TOOLS:
    _pn = _t["name"]
    _props = ((_t.get("inputSchema") or {}).get("properties")) or {}
    # window：完整说明只在 focus_window
    if _pn != "focus_window" and "window" in _props:
        _props["window"]["description"] = _WIN_DESC_REF
    # 鼠标档位 human/speed/precise：完整说明只在 mouse_move（send_keystrokes 是打字档位，保留自己的）
    if _pn in ("mouse_click", "mouse_drag", "mouse_transfer", "hover", "find_click", "sequence"):
        for _k, _ds in _TIER_DESC_REF.items():
            if _k in _props:
                _props[_k]["description"] = _ds
    # 输入法/直发参数：完整说明在 send_keystrokes
    if _pn == "sequence":
        for _k in ("ime_ascii", "ime_mode", "unicode_text"):
            if _k in _props:
                _props[_k]["description"] = "同 send_keystrokes 的同名参数（可步内覆盖）。"
    elif _pn == "send_shortcut" and "unicode_text" in _props:
        _props["unicode_text"]["description"] = "同 send_keystrokes 的 unicode_text。"
    elif _pn == "mouse_click" and "unicode_text" in _props:
        _props["unicode_text"]["description"] = "点击后自动输入文本时是否直发非 ASCII；同 send_keystrokes。"
    # 死参数：verify＝识别层时代的 OCR 回读开关（识别层已整层删除，代码不再读取它）
    if _pn in ("send_keystrokes", "send_shortcut", "mouse_click", "scroll"):
        _props.pop("verify", None)

# 识别层（OCR / UIA / 图标 YOLO / 沙箱）已整层删除——视觉只靠截图。


def handle_tool_call(name, arguments):

    global _EXTRA_IMG
    # ★mark_style 严格校验（0.1.70）：工具面只声明 grid / center；旧值（edge／被删的编号环 center）
    #   一律**显式报错**，绝不静默回落——回落＝后端接受了工具面没声明的值，模型会以为它生效了。
    if isinstance(arguments, dict) and arguments.get("mark_style") is not None:
        try:
            arguments["mark_style"] = _mark_style_arg(arguments.get("mark_style"))
        except ValueError as _ve:
            return {"content": [{"type": "text", "text": str(_ve)}], "isError": True}
    # 可选小图（effect_image 的变化区域 / hover 的局部图）只属于当轮动作，绝不跨调用泄漏——
    #   每轮入口先清空；由附图出口消费一次。
    _EXTRA_IMG = None
    # 上一轮若留下鼠标按住（异常/超时/被强杀等）⇒ 本次入口自动松开并告警；
    #   本轮按住状态由 _tool_call_result 统一追加到回执（跨调用可见）。
    global _HELD_AUTO_NOTE, _LAST_FOCUS_NOTE
    _HELD_AUTO_NOTE = ""
    _LAST_FOCUS_NOTE = ""
    try:
        # 只松"非刻意"的残留——mouse_drag(hold=true) 刻意保持的不自动松。
        #   （否则"按住不松手"活不过下一轮工具调用）；刻意 hold 的由回执提醒，用 `release=true` 松开。
        _auto_rel = _held_release_mouse_all(stale_only=True)
        if _auto_rel:
            _HELD_AUTO_NOTE = ("（⚠ 检测到上一次调用遗留的鼠标按住状态：%s —— 已在本次调用入口自动松开）"
                               % _auto_rel)
            print("[held] 入口自动松开残留鼠标按住：%s" % _auto_rel, file=sys.stderr)
    except Exception:
        pass
    _sys_timing.clear()
    reset_cancel()  # 每次新工具调用清除上一轮的取消标志
    try:
        global _focus_hwnd, _focus_child_hwnd, _focus_pid
        # ★ 修（缓存陷阱实测）：**每次新工具调用一律作废上一轮的 UI/OCR 快照**——
        #   缓存的设计规则本来就是"同工具调用内复用"（见 _scan_uia_elements/_ocr_cache 注释）；
        #   跨调用复用会读到旧状态（实测：写完字后扫到的还是「确定=禁用」，差点据此下错结论）。
        #   放在这里＝一处生效、所有工具都吃到；同一次调用内部的复用不受影响。
        # ⚠ 位置红线：这段必须留在 `global _focus_hwnd` **之后**——放前面会炸
        #   SyntaxError: name '_focus_hwnd' is used prior to global declaration（ 已炸过一次）。
        try:
            if _focus_hwnd or _focus_child_hwnd:
                _clear_cache(_focus_hwnd)
                _clear_cache(_focus_child_hwnd)
            else:
                _clear_cache(None)
        except Exception:
            pass
        # ★0.1.88：坐标一律按**图内坐标轴**解释（整图从 0 起）——这里换算成执行用坐标
        _ax_err = _axis_apply(name, arguments)
        if _ax_err:
            return {"content": [{"type": "text", "text": "⚠ " + _ax_err}], "isError": True}
        # ── window= 前置：要用的窗口不是当前记住的（或还没记住）才先聚焦 ──
        _w_arg = arguments.get("window")
        if _w_arg and name in _WINDOW_ARG_TOOLS:
            _w_hwnd = _window_arg_target(_w_arg)
            if not _w_hwnd:
                return {"content": [{"type": "text", "text": f"window 参数没能解析出窗口：{_w_arg!r}（可用标题子串或 hwnd）"}], "isError": True}
            # 已是它 ⇒ 跳过聚焦（即使传了参数也不真调聚焦）。
            # ★ 同族判定：UWP 一个标题两扇窗（ApplicationFrameWindow / CoreWindow）
            #   是**同一扇**——族根相同就视为"已是它"，不再重复聚焦。
            _same_family = bool(_w_hwnd and _focus_hwnd and _is_window_alive(_focus_hwnd)
                                and _window_family_root(_w_hwnd) == _window_family_root(_focus_hwnd))
            if not _same_family:
                _focus_and_remember(_w_hwnd)
                # ★ 实测：**聚焦完成 → 第一个按键之间必须留稳定时间**——否则第一个
                #   组合键会被吃掉（实况：带 window= 的那次 {WIN+R} 只认到 Win 键，弹出「资讯和兴趣」；
                #   不带 window=、用已聚焦窗口直接发，一次就成功）。120~200ms：太短会被吃、太长拖慢动作。
                time.sleep(0.15)

        if name == "capture_screenshot":
            _show_indicator(window=_get_focus_title(), action="截图中")
            # **持续观察会话**（边看边玩）：start/pull/stop/status；先判、不走截图路径。
            _vact = str(arguments.get("vision_action") or "").strip().lower()
            if _vact:
                if _vact == "start":
                    # ★缺省 region 也按上下文推断（修：以前不管传没传 window= 都默认 fullscreen，
                    #   窗口参数的意图被忽略 ⇒ 持续会话抓的是屏幕/遮挡者）
                    _v_region, _v_auto = _shot_region_default(arguments)
                    _v_rect = arguments.get("rect", "") or ""
                    _v_inner = None
                    if _v_rect:
                        try:
                            if isinstance(_v_region, str) and _v_region == "window":
                                _v_inner = _v_rect
                            else:
                                _v_region, _ = _shot_rect_monitor(
                                    _v_region, arguments.get("window_title") or None, _v_rect)
                        except Exception as _ve:
                            return {"content": [{"type": "text", "text":
                                    "⚠ rect 参数无效，未启动持续会话：%s" % (str(_ve)[:160],)}], "isError": True}
                    _vs = _vis_start(_v_region, arguments.get("window_title") or None, _v_inner,
                                     fps=float(arguments.get("fps", 8) or 8),
                                     sensitivity=float(arguments.get("sensitivity", 1.0) or 1.0),
                                     cut_abs=arguments.get("cut_abs"),
                                     integral_sec=arguments.get("integral_sec"),
                                     dup_min=arguments.get("dup_min"),
                                     ring_max=int(arguments.get("vision_ring", 12) or 12),
                                     life_seconds=_parse_vision_life(arguments.get("vision_seconds")),
                                     batch_ms=int(arguments.get("watch_ms", 0) or 0),
                                     batch_frames=int(arguments.get("frames", 1) or 1),
                                     batch_interval_ms=int(arguments.get("interval_ms", 200) or 200),
                                     batch_min_frames=int(arguments.get("min_frames", 3) or 3),
                                     batch_max_frames=int(arguments.get("max_frames", 6) or 6),
                                     tile_max=int(arguments.get("tile_max", 0) or 0),
                                     grid_cols=int(arguments.get("grid_cols", 0) or 0),
                                     hero_history=int(arguments.get("hero_history") or 2),
                                     sheet_frames=int(arguments.get("sheet_frames") or 0),
                                     push_interval_ms=int(arguments.get("push_interval_ms", 10000)
                                                          if arguments.get("push_interval_ms") not in (None, "")
                                                          else 10000))
                    _vs_batch = bool(_vs.get("batch_ms") or int(_vs.get("batch_frames") or 0) > 1)
                    if _vs_batch:
                        _bt = (("%dms" % int(_vs.get("batch_ms") or 0)) if _vs.get("batch_ms")
                               else ("%d 帧" % int(_vs.get("batch_frames") or 0)))
                        _capn = (("；每图最多 %d 帧（sheet_frames）" % int(_vs["sheet_frames"]))
                                 if _vs.get("sheet_frames") else
                                 ("；每图最多 %d 帧（max_frames）" % int(_vs.get("batch_max_frames") or 6)))
                        return {"content": [{"type": "text", "text":
                                ("后台概览任务已启动（**不阻塞**，立即返回）：会话 **%s** ｜ 目标 %s ｜ 限时 %ds%s ｜ "
                                 "**完成后自动注入上下文**（tiers 拼图，不用 pull；pull 仍可主动取）%s%s。"
                                 % (_vs["id"], _bt, int(_vs.get("life") or 0), _capn,
                                    (("；**已覆盖旧会话** %s" % "、".join(_vs.get("replaced") or []))
                                     if _vs.get("replaced") else ""),
                                    (("｜" + _v_auto) if _v_auto else "")))}]}
                    _hn = int(_vs.get("hero_history") or 2)
                    _piv = int(_vs.get("push_interval_ms") or 0)
                    _piv_txt = ("%ds" % max(1, int(round(_piv / 1000.0)))) if _piv else "不节流"
                    return {"content": [{"type": "text", "text":
                            ("持续观察已启动（**后台常驻**，立即返回）：会话 **%s** ｜ 请求 %sfps ｜ %s ｜ "
                             "hero 历史小格 %d ｜ 自动注入间隔 %s ｜ 后台仅此一路采集%s%s。\n"
                             "**有新内容才自动注入**（hero 版式：最新帧大图＋变化区＋历史小格；静止不出图；"
                             "节流期间攒帧，到点推一张），也可 `vision_action=\"pull\"` 主动取；"
                             "**到期或空闲超时会自动注入一条到期提示**；"
                             "`stop` 关闭、`status` 看状态。"
                             % (_vs["id"], _vs["fps"],
                                ("限时 %ds" % _vs["life"]) if _vs.get("life") else "**不限时**（vision_seconds=0）",
                                _hn, _piv_txt,
                                (("；**已覆盖旧会话** %s" % "、".join(_vs.get("replaced") or []))
                                 if _vs.get("replaced") else ""),
                                (("｜" + _v_auto) if _v_auto else "")))}]}
                if _vact == "peek":
                    _v_region, _v_auto = _shot_region_default(arguments)
                    _v_rect = arguments.get("rect", "") or ""
                    _v_inner = None
                    if _v_rect:
                        try:
                            if isinstance(_v_region, str) and _v_region == "window":
                                _v_inner = _v_rect
                            else:
                                _v_region, _ = _shot_rect_monitor(
                                    _v_region, arguments.get("window_title") or None, _v_rect)
                        except Exception as _ve:
                            return {"content": [{"type": "text", "text":
                                    "⚠ rect 参数无效，未启动后台单拍：%s" % (str(_ve)[:160],)}], "isError": True}
                    _after_ms = int(arguments.get("after_ms") or arguments.get("delay_ms") or 0)
                    _pk = _peek_start(_v_region, arguments.get("window_title") or None, _v_inner,
                                      after_ms=_after_ms)
                    _dly_txt = ("**延迟 %d ms 后**再抓这一张" % _after_ms) if _after_ms > 0 else "立即抓这一张"
                    return {"content": [{"type": "text", "text":
                            ("后台单拍已提交（**独立任务、不覆盖观察会话**，立即返回）：**%s** ｜ %s、**不会自动续**；"
                             "完成后**自动注入上下文的下一条消息**（统一注入通道 forceContext，不唤醒回合），无需 pull。%s"
                             % (_pk["id"], _dly_txt,
                                (("｜" + _v_auto) if _v_auto else "")))}]}
                if _vact == "pull":
                    return _vis_pull(arguments.get("vision_id") or "",
                                     max_frames=int(arguments.get("vision_max_frames", 4) or 4),
                                     want_image=bool(arguments.get("include_image", True)),
                                     layout=arguments.get("layout", "hero"),
                                     split=bool(arguments.get("hero_split", False)),
                                     auto=True,
                                     life_seconds=_parse_vision_life(arguments.get("vision_seconds")),
                                     hero_history=arguments.get("hero_history"),
                                     sheet_frames=arguments.get("sheet_frames"))
                if _vact in ("stop", "close"):
                    return _vis_stop(arguments.get("vision_id") or "")
                if _vact in ("status", "list"):
                    return {"content": [{"type": "text", "text": _vis_status_text()}]}
                return {"content": [{"type": "text",
                                     "text": "vision_action 只认 start / pull / stop / status（收到 %r）" % (_vact,)}],
                        "isError": True}
            # ：后台观察任务层已删除（被持续会话覆盖）——旧参数给明确指引，不走截图路径。
            _old_watch_keys = [k for k in ("watch_nowait", "watch_result", "watch_cancel", "watch_overwrite")
                               if k in arguments]
            if _old_watch_keys:
                return {"content": [{"type": "text", "text":
                        "后台观察任务层已删除：观察请用 `vision_action=\"start\"`／`\"pull\"`／`\"stop\"`（持续会话）；"
                        "一次性拼图直接传 `watch_ms`／`frames`，本调用会**同步等结果**。"}]}
            save_path = arguments.get("save_path", "")
            window_title = arguments.get("window_title", "") or None
            # region 缺省值**按上下文推断**（`_shot_region_default`）：显式 window/window_title ⇒ window；
            #   只给 rect 且最近截图的轴属于某扇窗 ⇒ window；其余 ⇒ fullscreen。
            #   不许让"忘传 region"把窗口内的局部截图掉进全屏 mss 路径（被遮挡时会抓到前台别的窗口）。
            region, _region_auto = _shot_region_default(arguments)
            _region_arg = arguments.get("region")
            # 夜新增：`rect="x,y,w,h"` ⇒ **只回那一块**（屏幕物理坐标，0.1.63 起）。
            #   为什么：图大时送达端会缩小（送达档≈1708×961）⇒ 细节会损失；
            #   **用 rect 裁局部 ⇒ 1:1 不缩**，细节保真。
            #   不传 ⇒ **逐字老行为**；解析失败 ⇒ **当场报错、不截图**（绝不静默退回全屏）。
            _rect = arguments.get("rect", "")
            # ★0.1.62：`clean` 已从工具面收掉 ⇒ **默认一律带标定图**（rect 局部也给绝对坐标尺）；
            #   只有调试开关 MCP_MARK_CLEAN=1 或显式传 clean=true（内部/老调用）才回原图。
            _clean_arg = arguments.get("clean")
            _clean_eff = bool(_clean_arg is True) or _MARK_CLEAN_DEBUG
            _rect_note = ""
            _frame_kind = None
            _rect_for_shot = None            # ★只有 window 那条路要把 rect 传下去（PW 抓整窗后按屏幕坐标裁）
            _crop_phys = None                # ★编号图/标尺用：裁剪区的**屏幕物理矩形**（= 实取，非请求值）
            if _rect not in (None, ""):
                _frame_kind = "screen_rect"  # ★防被附图通道当成全屏帧复用
                if region == "window":
                    try:
                        _parse_rect4(_rect)      # 先解析一次：格式错 ⇒ 当场报错、不截图
                    except Exception as _rerr2:
                        return {"content": [{"type": "text",
                                             "text": f"⚠ rect 参数无效，未截图：{_rerr2}"
                                                     f"（格式 'x,y,w,h'，**屏幕物理坐标**＝刻度尺读数）"}]}
                    # ★0.1.63：rect 一律**屏幕物理坐标** —— 换算由 `_rect_abs_to_local`/take_screenshot 做，
                    #   这里不再按 GetWindowRect 预平移（旧版"+窗口原点"就是两套坐标系并存的根源）。
                    #   实取矩形（可能被夹）由 take_screenshot 写进 `_LAST_SHOT_RECT`，标尺只认它。
                    _rect_for_shot = _rect
                    _rect_note = ""
                else:
                    try:
                        _rm, _rect_note = _shot_rect_monitor(region, window_title, _rect)
                        region = _rm             # dict ⇒ `_shot_monitor` 直接采用（老能力，未改一行）
                        _crop_phys = [_rm["left"], _rm["top"],
                                      _rm["left"] + _rm["width"], _rm["top"] + _rm["height"]]
                    except Exception as _rerr:
                        return {"content": [{"type": "text",
                                             "text": f"⚠ rect 参数无效，未截图：{_rerr}"
                                                     f"（格式 'x,y,w,h'，**屏幕物理坐标**＝刻度尺读数）"}]}
            # **不留任何会阻塞的**：一次性拼图（watch_ms/frames/…）已取消。
            #   回看一段时间改用持续会话（start 立即返回 → pull 取增量），两条路都不阻塞。
            _block_keys = [k for k in ("watch_ms", "min_frames", "max_frames", "frames", "interval_ms",
                                       "grid_cols", "tile_max", "sensitivity", "cut_abs",
                                       "integral_sec", "dup_min") if k in arguments]
            if _block_keys:
                return {"content": [{"type": "text", "text":
                        "一次性拼图（会阻塞）已取消：回看请用持续会话——先 `vision_action=\"start\"`（立即返回），"
                        "再 `vision_action=\"pull\"`（`layout=tiers` 概览／`layout=hero` 游戏向）；单帧截图不传这些参数。"}],
                        "isError": True}
            _watch_ms = 0
            _frames_n = 1
            if False:      # 旧一次性拼图块：保留代码但不可达（停用；不留会阻塞的）
                _inner_rect = _rect_for_shot if (isinstance(region, str) and region == "window") else None
                _vrun = _vis_running()
                if _vrun:
                    return {"content": [{"type": "text", "text":
                            "已有持续会话 %s 在采集（后台只允许一路）；先 `vision_action=\"stop\"` 再试。"
                            % _vrun[0]["id"]}]}
                _set_shot_note("")           # 清掉上一枪的附图留痕（拼图不沿用单帧标尺行）
                _grab_fn, _grab_close = _make_frame_grabber(region, window_title, _inner_rect)
                try:
                    if _watch_ms > 0:
                        _sheet_png, _sheet_meta = _watch_frames(
                            _grab_fn, watch_ms=_watch_ms,
                            fps=float(arguments.get("fps", 6) or 6),
                            min_frames=int(arguments.get("min_frames", 3) or 3),
                            max_frames=int(arguments.get("max_frames", 6) or 6),
                            tile_max=int(arguments.get("tile_max", 0) or 0),
                            grid_cols=int(arguments.get("grid_cols", 0) or 0),
                            label_prefix="f",
                            sensitivity=float(arguments.get("sensitivity", 1.0) or 1.0),
                            cut_abs=arguments.get("cut_abs"),
                            integral_sec=arguments.get("integral_sec"),
                            dup_min=arguments.get("dup_min"))
                    else:
                        _sheet_png, _sheet_meta = _fixed_frames(
                            _grab_fn, frames=_frames_n,
                            interval_ms=int(arguments.get("interval_ms", 200) or 0),
                            tile_max=int(arguments.get("tile_max", 0) or 0),
                            grid_cols=int(arguments.get("grid_cols", 0) or 0),
                            label_prefix="f")
                except Exception as _sheet_e:
                    return {"content": [{"type": "text", "text":
                            "⚠ 抽帧拼图失败（未出图）：%s" % (str(_sheet_e)[:200],)}], "isError": True}
                finally:
                    try:
                        _grab_close()
                    except Exception:
                        pass
                _lay_kind = _sheet_meta.get("layout") or "grid"
                _stxt = ("抽帧拼图（纯像素）：%s ｜ 保留 %d 帧 ｜ %s %d×%d ｜ 网格 %d×%d ｜ 画布 %d×%d ｜ %d 字节"
                         % ("watch 模式（按画面变化挑帧）" if _sheet_meta.get("mode") == "watch" else "固定间隔",
                            _sheet_meta["frames"],
                            ("最小格" if _lay_kind == "tiers" else "单格"),
                            _sheet_meta["tile"][0], _sheet_meta["tile"][1],
                            _sheet_meta["grid"][0], _sheet_meta["grid"][1],
                            _sheet_meta["canvas"][0], _sheet_meta["canvas"][1], _sheet_meta["bytes"]))
                if _lay_kind == "tiers":
                    _stxt += "｜版式=按档位（重要帧大格）"
                if _sheet_meta.get("mode") == "watch":
                    _fms = _sheet_meta.get("frames_meta") or []
                    _stxt += ("｜采样 %d 次（失败 %d）｜watch %dms @ 请求 %sfps／实采 %s 帧每秒 ｜ 切点 %d／积分 %d／兜底 %d ｜ 灵敏度 %s"
                              "（cut_abs %s／积分 %s／dup %s）"
                              % (_sheet_meta.get("samples", 0), _sheet_meta.get("fails", 0),
                                 _sheet_meta.get("watch_ms", 0), _sheet_meta.get("fps", 0),
                                 _sheet_meta.get("eff_fps", "?"),
                                 sum(1 for f in _fms if f.get("via") == "cut"),
                                 sum(1 for f in _fms if f.get("via") == "mark"),
                                 sum(1 for f in _fms if f.get("via") == "floor"),
                                 _sheet_meta.get("sensitivity", 1.0),
                                 _sheet_meta.get("cut_abs", "?"),
                                 _sheet_meta.get("integral_need", "?"),
                                 _sheet_meta.get("dup_min", "?")))
                _flist = []
                for _fm in (_sheet_meta.get("frames_meta") or []):
                    _bit = "帧%d +%dms" % (_fm.get("i", 0), _fm.get("t_ms", 0))
                    if _fm.get("score") is not None:
                        _bit += " s%.1f" % float(_fm["score"])
                    if _fm.get("via"):
                        _bit += "（%s）" % _fm["via"]
                    _flist.append(_bit)
                if _flist:
                    _stxt += "｜" + " ｜ ".join(_flist)
                _stxt += ("｜每格左下角角标＝帧序号/相对时刻/变化分；**这是时序拼图、不是坐标图**："
                          "图上位置不能当屏幕坐标用（要坐标请单帧 clean=false 看标尺图）")
                if _sheet_meta.get("mode") == "watch" and _sheet_meta.get("kept", 0) <= 1:
                    _stxt += "｜⚠ 这段几乎没有可辨的画面变化，只保留了基线帧"
                if save_path:
                    try:
                        os.makedirs(os.path.dirname(save_path), exist_ok=True)
                        with open(save_path, "wb") as _sf2:
                            _sf2.write(_sheet_png)
                        _stxt += "｜已保存: %s" % save_path
                    except Exception as _sfe:
                        _stxt += "｜⚠ 保存失败：%s" % (str(_sfe)[:80],)
                _sheet_content = [{"type": "text", "text": _stxt + _status_block()}]
                if arguments.get("include_image", True):
                    try:
                        import base64 as _b64s
                        _sheet_content.append({"type": "image",
                                               "data": _b64s.b64encode(_sheet_png).decode("ascii"),
                                               "mimeType": "image/png"})
                    except Exception:
                        pass
                _axis_reset("最近附图是时序拼图，不是坐标轴图——请重新截图")
                return {"content": _sheet_content}
            _set_shot_note("")               # ★本次截图的留痕先清空（take_screenshot 里会写实情）
            # **默认不落盘**：**不传 `save_path` ⇒ 一个文件都不写**，图只回给你；
            #   要存盘请显式传 `save_path`（老行为：传了就写、回路径）。
            # ★0.1.67：region=window 的目标由底层 `_shot_window_hwnd` 统一解析
            #   （标题 → **记住的操作面** → 前台兜底）；这里不再单独穿 hwnd，避免两套语义。
            _shot = take_screenshot(save_path, region, window_title, _frame_kind, _rect_for_shot)
            _shot_actual = _LAST_SHOT_NOTE   # ★实际走了哪条路（PW / 回退）——由 take_screenshot 写
            _crop_phys = _LAST_SHOT_RECT or _crop_phys   # ★0.1.63：标尺/编号只认真实取到的矩形
            _want_img = arguments.get("include_image", True)      # 默认就返回图
            if save_path:
                _txt = f"截图已保存: {_shot}"
                _data = None
                if _want_img:
                    try:
                        with open(_shot, "rb") as f:
                            _data = f.read()
                    except Exception:
                        _data = None
            else:
                _txt = ("截图（**未落盘**：按默认不写文件；要存图请传 `save_path`）"
                        f" ｜ {len(_shot) if _shot else 0} 字节 PNG")
                _data = _shot if _want_img else None
            # 截图：鼠标在范围内就画真实光标图标（不写字）；坐标走文字回执。
            #   设置页「附图显示真实光标」关掉时，图标和鼠标位置文字一起不出。
            if _data and _CURSOR_SHOW_ON:
                try:
                    _cp2 = get_mouse_position()
                    _mx2, _my2 = int(_cp2.get("x", 0)), int(_cp2.get("y", 0))
                    _m2c = _draw_cursor_icon(_data, _focus_hwnd, region, _mx2, _my2,
                                             croprect=(_crop_phys if _crop_phys else None))
                    if _m2c is not _data:
                        _data = _m2c
                    if _AXIS is None:
                        _txt += "｜🖱 鼠标位置：（尚未建立图内坐标轴）"
                    else:
                        _txt += "｜" + (_axis_fmt(_mx2, _my2, "🖱 鼠标") or "🖱 光标不在本图内")
                except Exception:
                    pass
            elif _data:
                _txt += "｜🖱 未叠光标，以图里应用自己的光标为准，点击触发区自己看图判断"
            if _rect_note:
                _txt += "｜" + _rect_note
            if _shot_actual and _shot_actual != _rect_note:
                _txt += "｜" + _shot_actual
            if _region_auto:
                _txt += "｜" + _region_auto
            if _rect not in (None, "") and _clean_arg is None:
                _txt += "｜rect 局部图：刻度**沿用同一根轴**（显示轴上的绝对位置，不从 0 重来）"
            # ★ 没有操作面（尚未聚焦）时，window 截图会抓到前台**别的**窗口（实测：没聚焦时截到了 DSH 自己，
            #   看起来却像正常结果）⇒ 直接不出图并说清楚，避免误判。要全屏请显式 region='fullscreen'。
            if (str(region) == "window") and not _focus_hwnd:
                _txt += ("｜⚠ 当前没有操作面（尚未聚焦）：**未附图**（避免截到前台其他窗口）"
                         "——先 focus_window 指定窗口，或传 region='fullscreen' 明确要全屏")
                _data = None
            content = [{"type": "text", "text": _txt + _status_block()}]
            if _data:
                try:
                    import base64
                    # 这条是**自己出图**的路径（不走中央附图），同样要"默认带号、
                    #   clean=true 给原图"——与 `_shot_item` 那边同一套判据（所有会返回
                    #   图片的路径都要有这个参数）。
                    # 三态：有 rect 默认原图；显式 clean=false 才编号（crop 几何按裁剪图）。
                    if not _clean_eff:
                        _data, _mb = _mark_attach_png(_data, _focus_hwnd, region=region,
                                                      style=arguments.get("mark_style"),
                                                      grid_dash=arguments.get("grid_dash"),
                                                      grid_color=arguments.get("grid_color"),
                                                      ruler_inside=arguments.get("ruler_inside"),
                                                      crop=_crop_phys)
                    else:
                        _axis_reset("最近附图是原图直出（未画坐标轴刻度）——请重新截图")
                    content.append({"type": "image", "data": base64.b64encode(_data).decode("ascii"),
                                    "mimeType": "image/png"})
                except Exception:
                    pass
            return {"content": content}

        elif name == "send_keystrokes":
            if not _focus_hwnd or not _is_window_alive(_focus_hwnd):
                return {"content": [{"type": "text", "text": "未聚焦窗口或聚焦已失效，请先调用 focus_window（按设计不回退到其他窗口）"}], "isError": True}
            if (cancel := _check_cancel()): return cancel
            text = arguments.get("text", "")
            press_enter = arguments.get("press_enter", False)
            press = arguments.get("press")
            release = arguments.get("release")
            hold_duration = arguments.get("hold_duration")
            scroll_clicks = arguments.get("scroll_clicks")
            mode = arguments.get("mode")  # None=auto, "fg"|"foreground", "bg"|"background"
            human = bool(arguments.get("human", False))   # 人影档：打字放慢到 ≈90ms/字（像真人敲）
            # **单发这条路也要报「打字速度／夹紧」**：原来只有序列那条回执打 `kbd_notes`，
            #   而 `_do_send_keystrokes` 里两处都只取元组的 `[0]`、把说明丢了 ⇒ 「夹了必须报」在这一半是空的。
            #   这里一次性算好（纯函数、无副作用；`cps` 为 None ⇒ 说明为空串、老行为逐字不变），
            #   顺带把「快档传 speed ⇒ 抛错」提到**动手之前**（与序列步同一规则）。
            _knote = _kbd_gap_speed(human, arguments.get("speed"))[1]
            _pre_win = _win_snapshot_light()
            input_info = _do_send_keystrokes(text, [], press_enter, press, release, hold_duration, scroll_clicks, mode, human,
                                             cps=arguments.get("speed"),
                                             ime_mode=str(arguments.get("ime_mode") or "temp").lower(),
                                             unicode_text=bool(arguments.get("unicode_text", False)))
            # 直发非 ASCII 默认被拦：**如实报错**，不静默、不伪装成功。
            if input_info and input_info.get("error"):
                return {"content": [{"type": "text", "text": "⚠ " + str(input_info["error"]) + _status_block()}],
                        "isError": True}
            # 键盘输入可能改变弹窗内容，失效缓存
            if _popup_hwnd and _is_window_alive(_popup_hwnd):
                _ocr_cache_invalidate(_popup_hwnd)
            # 按下/松开/长按/滚轮
            if input_info and input_info.get("action"):
                action = input_info["action"]
                keys = input_info.get("keys", [])
                if action == "press":
                    desc = f"按下: {'+'.join(keys)}"
                elif action == "release":
                    desc = f"松开: {'+'.join(keys)}"
                elif action == "hold":
                    desc = f"长按 {input_info.get('duration_ms',0)}ms: {'+'.join(keys)}"
                elif action == "scroll":
                    direction = "↑" if input_info.get("clicks", 0) > 0 else "↓"
                    desc = f"滚轮 {direction}{abs(input_info.get('clicks', 0))}"
                else:
                    desc = "发送"
            elif input_info and input_info.get("text"):
                verified = input_info.get("verified", False)
                method = input_info.get("method", "?")
                if text and press_enter:
                    desc = f"执行 {text.replace(chr(10),'↵')} [{method}]"
                else:
                    desc = f"输入 {text} [{method}]"
                # **temp 不进回执**（打完自动还原，报出来会被误读成"输入法被改了"）；
                #   只有 perm（不还原）与"切换失败"才写，且**不拦输入**。
                _imnote = input_info.get("ime_note")
                if _imnote:
                    desc += " · 输入法：" + _imnote
                _ch = input_info.get("channel")
                if _ch == "background":
                    desc += " · 通道：后台（PostMessage，未抢前台、未动输入法）"
                elif _ch == "foreground":
                    desc += (" · 通道：前台（已把该窗切到前台）" if input_info.get("foreground_ensured")
                             else " · 通道：前台（该窗本来就在前台）")
                if _knote:       # 「夹了必须报」：单发这条路原来把这句丢了（只有序列那条报）
                    desc += " · 打字：" + _knote
                # 把「值有没有真进去」直接写进回执，不再靠猜、也不只藏在 verified 字段里：
                _av = input_info.get("actual_value")
                if input_info.get("verified"):
                    desc += " · 读回✓"
                elif _av:
                    desc += f" · 读回✗「{str(_av)[:60]}」"
                elif method == "scancode":
                    desc += " · 读回：读不到编辑框值"
            else:
                desc = "发送"
            desc += _win_delta_text(_pre_win)
            return {"content": [{"type": "text", "text": f"{desc}{_status_block()}"}]}

        elif name == "send_shortcut":
            if not _focus_hwnd or not _is_window_alive(_focus_hwnd):
                return {"content": [{"type": "text", "text": "未聚焦窗口或聚焦已失效，请先调用 focus_window（按设计不回退到其他窗口）"}], "isError": True}
            text = arguments.get("text", "")
            if not text:
                return {"content": [{"type": "text", "text": "请提供 text 参数"}], "isError": True}
            if (cancel := _check_cancel()): return cancel
            target = _resolve_target()
            _ensure_foreground(target)
            # **快捷键也要显示操作指示条**。
            #   实测：其它工具（聚焦/点击/打字/滚轮/拖拽/等待…）都有，唯独 `send_shortcut` 这一支**从来没有**
            #   —— 原实现这条也没有（历史遗漏）。
            #   形状沿用 `_do_send_keystrokes` 的热键分支（action=f"热键: {hotkeys}"）：只加一条，不改既有行为。
            _show_indicator(window=_get_focus_title(), action=f"热键: {text}")
            interval = arguments.get("interval")
            typing_interval = arguments.get("typing_interval")
            # 非 ASCII 默认**不允许直发**（与 send_keystrokes 同一规则）：
            #   含 `{KEY}` 块的组合键文本里出现中文也一样：**动手前报错**，绝不静默丢字、不碰输入法。
            _unicode_text = bool(arguments.get("unicode_text", False))
            if not _unicode_text and not _text_is_ascii(text):
                return {"content": [{"type": "text", "text":
                        "⚠ 直发非 ASCII 文本默认不允许（设计规则）。要发请显式传 `unicode_text=true`"
                        "（Unicode 注入、不经过输入法）；中文也可以走 `ime_mode` ＋ 拼音由输入法上屏。"
                        + _status_block()}], "isError": True}
            _pre_win = _win_snapshot_light()
            # **输入法守卫**（这条原来也没守）。**只对"纯文本"守**：`send_shortcut` 的主业
            #   是发组合键（`{ctrl+c}` 这类），而守卫在直设失败时会**真的点一下 Shift**（`_ime_toggle_sendi`）
            #   ⇒ 往热键序列里插一个 Shift 有把热键改坏的风险，得不偿失。判据：**含 `{}` 块就不守**
            #   （那是热键形态）；非纯 ASCII 也不守（中文一个字都不许动，闸自己会挡）。
            _hguard = False
            if "{" not in text and "}" not in text:
                _hguard = _ime_guard_typed(text, target,
                                           mode=str(arguments.get("ime_mode") or "temp").lower())
            try:
                _parse_sendinput_text(text, interval, typing_interval, unicode_text=_unicode_text)
            finally:
                if _hguard:
                    _ime_guard_restore(target)
            # 键盘输入可能改变弹窗内容，失效缓存
            if _popup_hwnd and _is_window_alive(_popup_hwnd):
                _ocr_cache_invalidate(_popup_hwnd)
            method = _last_key_method
            desc = f"发送: {text} [{method}]"
            desc += _win_delta_text(_pre_win)
            desc += _status_block()
            return {"content": [{"type": "text", "text": desc}]}

        elif name == "focus_window":
            keyword = arguments.get("keyword", "")
            class_name = arguments.get("class", "")
            process = arguments.get("process", "")
            pid = arguments.get("pid")
            exact = arguments.get("exact", False)
            case_sensitive = arguments.get("case_sensitive", False)
            regex = arguments.get("regex", False)
            index = arguments.get("index")
            app_type = arguments.get("app_type")
            target = arguments.get("target", "auto")
            capture_region = arguments.get("capture_region", "window")
            # 插件没有识别层，把该语义落到**附图区域**上，
            #   否则这个参数只会改个「[全屏模式]」标签、图还是窗口图（显式传了却不生效＝静默错路径）。
            if str(capture_region) not in ("window", "fullscreen"):
                return {"content": [{"type": "text",
                                     "text": "capture_region 只认 window / fullscreen"
                                             "（收到 %r）" % (capture_region,)}], "isError": True}
            arguments["include_image_region"] = str(capture_region)
            if not (keyword or class_name or process or pid is not None):
                return {"content": [{"type": "text", "text": "请提供 keyword/class/process/pid 参数"}], "isError": True}
            if (cancel := _check_cancel()): return cancel
            # ★ P0：**已聚焦短路**——目标就是当前操作面时，不再重复枚举/激活
            #   （实战反馈：一次 focus_window 偶发 5~6s，大头就是重复的窗口枚举）。
            #   只在「不扫描 + 未指定 index + target=auto + 当前前台就是已记住的操作面 +
            #   本次查询条件确实命中它」时生效；其余一律走老路（行为不变）。
            if (index is None and (target or "auto") == "auto"
                    and _focus_hwnd and _is_window_alive(_focus_hwnd)
                    and user32.GetForegroundWindow() == _focus_hwnd
                    and _window_query_matches(_focus_hwnd, keyword, class_name, process, pid,
                                              exact, case_sensitive, regex)):
                _rr = wintypes.RECT()
                user32.GetWindowRect(_focus_hwnd, ctypes.byref(_rr))
                _tb = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(_focus_hwnd, _tb, 256)
                return {"content": [{"type": "text", "text":
                        "已是操作面（**短路**：目标就是当前已聚焦窗口，未重复枚举/激活/扫描）\n"
                        "  [%s] class=%s | %s\n"
                        "  窗口矩形: %d,%d,%d,%d（%d×%d）"
                        % (_fg_proc_name(_focus_hwnd) or "?", _hwnd_cls(_focus_hwnd),
                           _tb.value or "(无标题)", _rr.left, _rr.top, _rr.right, _rr.bottom,
                           _rr.right - _rr.left, _rr.bottom - _rr.top)}]}
            _tp_fw = time.time()
            windows = find_window_by_title(keyword, class_name=class_name, process=process, pid=pid,
                                            first_only=False, exact=exact, case_sensitive=case_sensitive, regex=regex)
            _timing_add("findwin", time.time()-_tp_fw)
            if not windows:
                # **列表端宽松、动作端严格**：可见窗里没有 ⇒ 只在隐藏层里**报个明白**，不聚焦它。
                #   依据（实测）：聚焦幽灵窗拉不出前台（仍 [DWM隐藏]、抓图全黑），只会让**操作面被一扇
                #   看不见的窗占住**、坐标动作再被护栏拦 ⇒ 纯负担。
                _hid_hits = find_window_by_title(keyword, class_name=class_name, process=process, pid=pid,
                                                 first_only=False, exact=exact, case_sensitive=case_sensitive,
                                                 regex=regex, include_hidden=True)
                if _hid_hits:
                    lines = [f"找到 {len(_hid_hits)} 个匹配，但**全是 DWM 隐藏的幽灵窗**（屏幕上你看不到它）"
                             f"——按设计**不聚焦隐藏窗**（聚焦它 = 操作面被看不见的窗占住，坐标动作还会被护栏拦）："]
                    for w in _hid_hits[:10]:
                        lines.append(f"  [幽灵窗] [{w.get('process','?')}] pid={w.get('pid','?')} "
                                     f"class={w.get('class','?')} | {w['title']}")
                    lines.append("  要动它：让它自己显示出来（重开那个应用）；要关它：借执行链"
                                 "（运行框跑 taskkill /IM <进程>.exe /F）；只想看它：list_windows。")
                    return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": True}
            if not windows:
                all_w = enum_all_windows(include_hidden=True)
                lines = [f"未找到匹配窗口（规则：全量·含隐藏），当前共有 {len(all_w)} 个窗口:"]
                for w in all_w[:30]:
                    lines.append(f"  [{w.get('process','?')}] pid={w.get('pid','?')} class={w.get('class','?')} ({w['left']},{w['top']} {w['width']}x{w['height']}) | {w['title']}")
                if len(all_w) > 30:
                    lines.append(f"  ... 及其他 {len(all_w)-30} 个")
                return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": True}
            # 选择结果
            sel = 0
            if index is not None and 1 <= index <= len(windows):
                sel = index - 1
            if len(windows) > 1:
                lines = [f"找到 {len(windows)} 个匹配窗口，{'已选择第 ' + str(index) + ' 个' if index else '已自动选择第一个'}:"]
                for i, w in enumerate(windows):
                    marker = " ←" if i == sel else ""
                    lines.append(f"  [{i+1}] [{w.get('process','?')}] pid={w.get('pid','?')} class={w.get('class','?')} ({w['left']},{w['top']} {w['width']}x{w['height']}) | {w['title']}{marker}")
            w = windows[sel]
            hwnd = w["hwnd"]
            title = w["title"]
            child_hwnd = _find_terminal_child(hwnd)
            _tp_act = time.time()
            _focus_st = focus_window(hwnd, app_type)
            _show_indicator(window=title, action="聚焦")
            _timing_add("activate", time.time()-_tp_act)
            _focus_hwnd = hwnd
            _focus_child_hwnd = child_hwnd if child_hwnd and child_hwnd != hwnd else None
            _focus_pid = _pid_of(hwnd)
            # 窗口信息
            win_info = f"[{w.get('process','?')}] pid={w.get('pid','?')} class={w.get('class','?')}"
            _tracked_hwnds.clear()
            _clear_popup()
            _tp_pop = time.time()
            popups_list = _discover_popups(hwnd)
            _timing_add("popups", time.time()-_tp_pop)
            for ph, ptitle, prect, _ in popups_list:
                _tracked_hwnds.add(ph)
                # 不设 _popup_hwnd — 聚焦时发现的弹窗不一定是当前操作面
            _tp_st = time.time()
            tree_block = _status_block()
            _timing_add("status", time.time()-_tp_st)

            # === 自动扫描当前操作面 ===（识别层已移除：不再跑 OCR/UIA/图标/坐标树，只看截图）

            mode_note = " [全屏模式]" if capture_region == "fullscreen" else ""
            _type_label = "terminal" if _focus_child_hwnd else "gui"
            result_text = f"激活窗口: {title} {win_info} [{_type_label}]{mode_note}"
            if _focus_st == "still_minimized":
                result_text += "｜⚠ 窗口仍处于最小化，可能要先手动切一下"
            elif _focus_st == "not_foreground":
                result_text += "｜⚠ 窗口已恢复，但没能切到前台"
            result_text += tree_block
            # 底部独立窗口树（不受弹窗警告影响）
            _tp_tk = time.time()
            tracked = _tracked_popup_names(None)
            _timing_add("tracked", time.time()-_tp_tk)
            if not tracked and _focus_hwnd:
                r = wintypes.RECT()
                user32.GetWindowRect(_focus_hwnd, ctypes.byref(r))
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(_focus_hwnd, buf, 256)
                tracked = [f"{buf.value or '(无标题)'} (主窗口) @{r.left},{r.top} {r.right-r.left}x{r.bottom-r.top}"]
            if tracked and "窗口树" not in tree_block:      # ★别重复打印：`_status_block()` 已经出过一次
                result_text += "\n\n窗口树:\n  " + "\n  ".join(tracked)
            return {"content": [{"type": "text", "text": result_text}]}

        elif name == "open_desktop_item":
            """通过 Shell.Application COM 操作桌面图标。"""
            mode = arguments.get("mode", "open")
            keyword = arguments.get("keyword", "")
            verb = arguments.get("verb", "open")

            if mode == "scan":
                items = _shell_scan_desktop()
                names = [it["name"] for it in items]
                if items:
                    return {"content": [{"type": "text", "text": f"桌面图标 ({len(items)} 个):\n" + "\n".join(f"  - {n}" for n in names) + _status_block()}]}
                else:
                    return {"content": [{"type": "text", "text": "未找到桌面图标"}]}

            # mode="open"
            if not keyword:
                return {"content": [{"type": "text", "text": "open 模式需要 keyword 参数"}], "isError": True}
            _show_indicator(window=_get_focus_title(), action=f"打开 {keyword}")
            result = _shell_open_desktop_item(keyword, verb)
            if result["found"]:
                return {"content": [{"type": "text", "text": f"✓ 已打开: {result['item']} (verb={verb}){_status_block()}"}]}
            else:
                return {"content": [{"type": "text", "text": f"✗ 未找到包含 '{keyword}' 的名字，请检查名称"}]}

        elif name == "mouse_click":
            if (cancel := _check_cancel()): return cancel
            x = arguments.get("x")
            y = arguments.get("y")
            if x is None or y is None:
                return {"content": [{"type": "text", "text": "请提供 x 和 y 坐标"}], "isError": True}
            button = arguments.get("button", "left")
            click_count = arguments.get("click_count", 1)
            text = arguments.get("text")  # 可选：点击后输入文本
            _rel = bool(arguments.get("relative", False))     # ★ 相对：增量移动后就地点击
            _show_indicator(window=_get_focus_title(),
                            action=(f"相对点击 (Δ{x},{y})" if _rel else f"点击 ({x},{y})"))
            # 语义探针（纯像素/Win32，不做识别）：动作前先取点击点邻域快照，动作后对比。
            _clk_x, _clk_y = int(x), int(y)
            if _rel:
                _pt0 = wintypes.POINT()
                try:
                    user32.GetCursorPos(ctypes.byref(_pt0))
                    _clk_x, _clk_y = int(_pt0.x), int(_pt0.y)
                except Exception:
                    pass
            _clk_rect, _clk_before = _local_snapshot(_clk_x, _clk_y, 96)
            _pops_before = _popup_probe_begin()   # 动作前弹窗集合（Win32；右键菜单/下拉框靠它看得见）
            _LOCAL_CTX.clear()
            _LOCAL_CTX["rect"] = _clk_rect        # 让 _px_hook 抓 B 帧（到位/按下前）
            result = mouse_click(x, y, button, click_count, verify=arguments.get("verify", True), ocr_timeout=arguments.get("ocr_timeout", 30), fast=arguments.get("fast", False), relative=_rel, human=bool(arguments.get("human", False)), speed=(None if arguments.get("speed") is None else float(arguments.get("speed"))), precise=bool(arguments.get("precise", False)),
                                 verify_effect=bool(arguments.get("verify_effect", True)),
                                 settle_ms=int(arguments.get("settle_ms", _PX_SETTLE_MS) or 0),
                                 verify_region=arguments.get("verify_region", "") or "",
                                 change_threshold=(None if arguments.get("change_threshold") is None else float(arguments.get("change_threshold"))),
                                 effect_image=bool(arguments.get("effect_image", False)),
                                 effect_samples=int(arguments.get("effect_samples", 1) or 1),
                                 effect_span_ms=int(arguments.get("effect_span_ms", 0) or 0),
                                 effect_strip=bool(arguments.get("effect_strip", False)),
                                 dwell_ms=_clamp_dwell_ms(arguments.get("dwell_ms")))
            # 点击后验证窗口是否关闭
            closed_info = ""
            if _focus_hwnd and not user32.IsWindow(_focus_hwnd):
                time.sleep(0.1)
                new_fg = user32.GetForegroundWindow()
                new_title = ""
                try:
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(new_fg, buf, 256)
                    new_title = buf.value or "?"
                except:
                    pass
                closed_info = f"\n⚠ 原窗口已关闭，焦点落至: {new_title}"
                _focus_hwnd = None  # 清除已失效的句柄
            # 点击后输入文本（原子操作）
            if text:
                time.sleep(0.25)  # 等输入框获得焦点
                input_info = _do_send_keystrokes(text=text, press_enter=arguments.get("press_enter", False),
                                                 unicode_text=bool(arguments.get("unicode_text", False)))
                result["input_info"] = input_info
            # 格式化输出 — 只留：点击结果 + 纯像素生效判定 + 窗口树（Win32 基础信息）
            lines = []
            _cx0, _cy0 = _AXIS0.get("x"), _AXIS0.get("y")
            _clk_disp = (("图内 (%s,%s)" % (_cx0, _cy0))
                         if (_cx0 is not None and _cy0 is not None)
                         else "(%s,%s)" % (result.get('x'), result.get('y')))
            summary = f"点击成功（点击{_clk_disp} {result.get('button','left')}）"
            if result.get("note"):
                summary += f"（{result['note']}）"
            summary += _click_timing_note()
            lines.append(summary)
            if result.get("effect") is not None:
                lines.append("生效判定（纯像素）: " + _px_effect_line(result.get("effect")))
            if _clk_rect:
                _clk_b = _LOCAL_CTX.pop("hook", None)          # B：到位/停顿后、按下前
                _clk_from = _clk_b or _clk_before
                _clk_after = _after_from_final(_clk_rect, _clk_from, 200)   # C：返回图那张裁
                if _clk_b is not None and not _rel:            # 移动段：A→B（真移动过才给）
                    _mv_seg = _local_sem_line("移动前→到位后", _clk_before, _clk_b,
                                              rect=_clk_rect)
                    if _mv_seg:
                        lines.append("移动段（纯像素）：" + _mv_seg)
                _seg_label = "点击前→点击后" if _clk_b is not None else "调用前→调用后"
                _clk_sem = _local_sem_line(_seg_label, _clk_from, _clk_after,
                                           "命中：" + _window_at_text(_clk_x, _clk_y),
                                           rect=_clk_rect, motion=True)
                if _clk_sem:
                    lines.append("局部判定（纯像素）：" + _clk_sem)
            _pops_line = _popup_probe_line(_pops_before)   # 弹窗差集：右键菜单/下拉框/对话框
            if _pops_line:
                lines.append(_pops_line)
            if text:
                ii = result.get("input_info", {})
                if ii.get("error"):
                    # ★：直发非 ASCII 被拦/失败 ⇒ 如实报，不伪装成"输入了"
                    lines.append("   ⚠ 输入未执行：" + str(ii["error"]))
                else:
                    # ★：识别层已删 ⇒ 键盘路**没有读回**（verified 恒 False）——
                    #   以前这里会打一个「✗」，看着像"没输进去"，其实是**没有读回能力**。
                    #   判据照 send_keystrokes 那边：只有"读到了值但对不上"才打 ✗；读不到 ⇒ 不打标记。
                    _av = ii.get("actual_value")
                    v = " ✓" if ii.get("verified") else (" ✗" if _av else "")
                    lines.append(f"   输入: [{text}]{v} ({ii.get('method', '?')})")
            tracked = result.get("tracked", [])
            if tracked:
                lines.append("窗口树:\n  " + "\n  ".join(tracked))
            if closed_info:
                lines.append(closed_info)
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        elif name == "mouse_move":
            if (cancel := _check_cancel()): return cancel
            x = arguments["x"]
            y = arguments["y"]
            dur = arguments.get("duration")
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            sx, sy = pt.x, pt.y
            human = bool(arguments.get("human", False))   # 人影档：移动放慢（≈2500 像素/秒），轨迹算法不变
            # ★ 修（全文件复扫查出的**静默失效**）：原来这里**没把 relative/nocoalesce 传下去**
            #   ⇒ 工具面 relative=true 被吞掉、照旧走绝对移动——而"SetCursorPos 一个输入事件都不产生"
            #   正是视角类操作"点得到、视角转不动"的成因。对照组：mouse_click 一直是传的（relative=_rel）。
            #   判据：**工具面开关必须真到达函数**；单测原先只测函数层，漏的就是这一跳。
            _rel = bool(arguments.get("relative", False))
            _noco = bool(arguments.get("nocoalesce", False))
            _show_indicator(window=_get_focus_title(),
                            action=(f"相对移动 Δ({x},{y})" if _rel else f"移动鼠标 ({x},{y})"))
            # 局部像素变化测量（与 click 同一套）：移动前后各取一块邻域做差分。
            #   目的：判断"这次移动/悬停有没有让界面起反应"（远控下尤其——远端光标可能根本没动）。
            _mv_x = int(sx) if _rel else int(x)
            _mv_y = int(sy) if _rel else int(y)
            _mv_rect, _mv_before = _local_snapshot(_mv_x, _mv_y, 96)
            _mv_pops = _popup_probe_begin()
            _px_phase_flush()      # 动作边界：清复用缓存（后帧绝不许裁到前帧）
            result = mouse_move(x, y, dur, human, _rel, _noco,
                                speed=(None if arguments.get("speed") is None else float(arguments.get("speed"))),
                                precise=bool(arguments.get("precise", False)))
            _mv_after = _after_from_final(_mv_rect, _mv_before, 200) if _mv_rect else None
            _mv_sem = _local_sem_line("落点邻域", _mv_before, _mv_after,
                                      "命中：" + _window_at_text(_mv_x, _mv_y),
                                      rect=_mv_rect, motion=True) if _mv_rect else ""
            # 落点实测（GetCursorPos）：绝对移动报偏差，相对移动报实际位移。
            _mv_landed = ""
            try:
                _pt2 = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(_pt2))
                if _rel:
                    _mv_landed = "光标实际位移 Δ(%+d,%+d)" % (_pt2.x - int(sx), _pt2.y - int(sy))
                else:
                    _mv_to = _axis_fmt(int(x), int(y)) or "（图外）"
                    _mv_landed = "落点实测 %s" % (_axis_fmt(_pt2.x, _pt2.y) or "（图外）")
            except Exception:
                pass
            _prune_tracked_popups()
            # ★ 修：相对位移**不是"移动到 (x,y)"**——原来两条路共用同一句，会打出
            #   「鼠标 (960,540)→(8,0)」＋轨迹自证「瞬移（duration=0）」（相对路没有 steps），读起来全错。
            if result.get("method") == "mouse_event-rel":
                _mtxt = (f"鼠标 **相对位移 Δ({x},{y})** [{result.get('method')}]"
                         f"（未移动光标到绝对坐标；受系统指针加速影响，可能被放大 ~4×）")
                if result.get("paced"):
                    _pm = result["paced"]
                    _mtxt += (f"\n  切块铺时间：{_pm['steps']} 步 × {_pm['step_px']}px"
                              f"（间隔 {_pm['interval_ms']}ms ｜ 实速 {_pm['speed_effective']} px/s"
                              f" ｜ 用时 {_pm['elapsed']}s）"
                              + (("  " + _pm["pace_note"]) if _pm.get("pace_note") else ""))
            else:
                _mv_from = _axis_fmt(sx, sy, "从 ")
                _mv_to = _axis_fmt(int(x), int(y), "到 ")
                _mtxt = f"鼠标 {_mv_from or '（图外）'}→{_mv_to} [{result.get('method','?')}]\n{_trace_note(result)}"
            if _mv_landed:
                _mtxt += "\n" + _mv_landed
            if _mv_sem:
                _mtxt += "\n局部判定（纯像素）：" + _mv_sem
            _mv_pl = _popup_probe_line(_mv_pops)
            if _mv_pl:
                _mtxt += "\n" + _mv_pl
            return {"content": [{"type": "text", "text": f"{_mtxt}{_status_block()}"}]}

        elif name == "mouse_drag":
            if (cancel := _check_cancel()): return cancel
            # 拖拽两态：
            #   `hold=false`（默认）＝拖完自动松手；`hold=true`＝拖完保持按住；`release=true`＝只松开（不拖）。
            if bool(arguments.get("release", False)):
                result = mouse_drag(release=True)
                _rel = result.get("released") or ""
                return {"content": [{"type": "text", "text":
                        ("鼠标松开：%s%s" % (_rel if _rel else "（当前没有按住的鼠标键）", _status_block()))}]}
            ex = arguments.get("end_x")
            ey = arguments.get("end_y")
            if ex is None or ey is None:
                return {"content": [{"type": "text", "text":
                        "mouse_drag：拖拽必须给 end_x／end_y；只想松开请传 release=true"}], "isError": True}
            ex, ey = int(ex), int(ey)
            _hold = bool(arguments.get("hold", False))   # 默认自动松手（更通用）；传 hold=true 才拖完保持按住
            sx = arguments.get("start_x")
            sy = arguments.get("start_y")
            btn = arguments.get("button", "left")
            dur = arguments.get("duration")
            human = bool(arguments.get("human", False))   # 人影档：拖拽放慢（≈1600 像素/秒），轨迹算法不变
            # 语义探针（纯像素，不做识别）：起点邻域小、落点邻域大（给模板匹配留搜索范围）。
            _drag_probe = None
            _drag_dst_rect = _px_rect_around(int(ex), int(ey), 200)
            _drag_c = {"snap": None}

            def _drag_pre_release():
                try:
                    if _drag_dst_rect:
                        _drag_c["snap"] = _px_snapshot(_drag_dst_rect)
                except Exception:
                    pass

            if sx is not None and sy is not None:
                _drag_probe = _drag_probe_begin((int(sx), int(sy)), (ex, ey))
            _pops_before = _popup_probe_begin()   # 动作前弹窗集合（Win32）
            result = mouse_drag(ex, ey, sx, sy, btn, dur, human,
                                speed=(None if arguments.get("speed") is None else float(arguments.get("speed"))),
                                precise=bool(arguments.get("precise", False)), hold=_hold,
                                verify_effect=bool(arguments.get("verify_effect", True)),
                                settle_ms=int(arguments.get("settle_ms", _PX_SETTLE_MS) or 0),
                                verify_region=arguments.get("verify_region", "") or "",
                                change_threshold=(None if arguments.get("change_threshold") is None else float(arguments.get("change_threshold"))),
                                effect_image=bool(arguments.get("effect_image", False)),
                                 effect_samples=int(arguments.get("effect_samples", 1) or 1),
                                 effect_span_ms=int(arguments.get("effect_span_ms", 0) or 0),
                                 effect_strip=bool(arguments.get("effect_strip", False)),
                                 pre_release_hook=_drag_pre_release)
            if _drag_probe is None:
                _drag_probe = {"src_rect": None, "dst_rect": _drag_dst_rect, "src": None, "dst": None}
            else:
                _drag_probe.setdefault("dst_rect", _drag_dst_rect)
            _drag_probe["pre_release"] = _drag_c.get("snap")
            _drag_probe["final"] = (_after_from_final(_drag_probe.get("dst_rect"), _drag_c.get("snap"), 200)
                                    if _drag_probe.get("dst_rect") else None)
            _use_eff_arr = bool(arguments.get("verify_effect", True))
            _drag_probe["arrive"] = (_px_crop_snap(_PX_EFF_LAST.get("snap"), _drag_probe.get("src_rect"))
                                     if (_use_eff_arr and _drag_probe.get("src_rect")
                                         and _PX_EFF_LAST.get("snap")) else None)
            actual_sx = result.get("start_x", sx)
            actual_sy = result.get("start_y", sy)
            _hold_note = ("（**hold=true：拖完仍按住、未松开** —— 松开请调 `mouse_drag(release=true)`）"
                          if result.get("hold") else "（已自动松手）")
            _show_indicator(window=_get_focus_title(), action=f"拖拽 ({actual_sx},{actual_sy})→({ex},{ey})")
            # 读取窗口位置（按图内坐标轴写）
            pos = _window_rect_line()
            _prune_tracked_popups()
            _eff_txt = ("\n生效判定（纯像素）: " + _px_effect_line(result.get("effect"))) if result.get("effect") is not None else ""
            _sem_txt = "\n".join(_drag_probe_report(_drag_probe))
            if _sem_txt:
                _sem_txt = "\n局部判定（纯像素）：\n" + _sem_txt
            _pops_line = _popup_probe_line(_pops_before)   # 弹窗差集（Win32）
            if _pops_line:
                _sem_txt += "\n" + _pops_line
            _ds0, _dt0 = _AXIS0.get("start_x"), _AXIS0.get("start_y")
            _de0x, _de0y = _AXIS0.get("end_x"), _AXIS0.get("end_y")
            _drag_from = (("图内 (%s,%s)" % (_ds0, _dt0))
                          if (_ds0 is not None and _dt0 is not None) else "（默认起点）")
            _drag_to = (("图内 (%s,%s)" % (_de0x, _de0y))
                        if (_de0x is not None and _de0y is not None) else "(%s,%s)" % (ex, ey))
            return {"content": [{"type": "text", "text": f"拖拽 {_drag_from}→{_drag_to} [{result.get('method','?')}] {_hold_note}\n{_trace_note(result)}{_eff_txt}{_sem_txt}\n{pos}{_status_block()}"}]}

        # ★ P0 新增：通用搬移（drag / click-pick-place / auto）＋ 悬停（纯像素，不碰 OCR/UIA）
        elif name == "mouse_transfer":
            if (cancel := _check_cancel()): return cancel
            _mt_src = arguments.get("source")
            _mt_tgt = arguments.get("target")
            if _mt_src in (None, "") or _mt_tgt in (None, ""):
                return {"content": [{"type": "text", "text":
                        "mouse_transfer：必须给 source 与 target（'x,y'，**图内坐标**）"}], "isError": True}
            _show_indicator(window=_get_focus_title(), action=f"搬移 {_mt_src}→{_mt_tgt}")
            _mt_probe = None
            try:
                _mt_s = _parse_xy(_mt_src, "source")
                _mt_t = _parse_xy(_mt_tgt, "target")
                _mt_probe = _drag_probe_begin(_mt_s, _mt_t)
            except Exception:
                _mt_probe = None
            _mt_seg = {"pick_after": None, "pre_release": None}   # 分段帧（B/C）

            def _mt_pick_done():                     # click：拾取后、源邻域（B）
                try:
                    if _mt_probe and _mt_probe.get("src_rect"):
                        _mt_seg["pick_after"] = _px_snapshot(_mt_probe["src_rect"])
                except Exception:
                    pass

            def _mt_pre_release():                   # drag：松手前、落点邻域（C）
                try:
                    if _mt_probe and _mt_probe.get("dst_rect"):
                        _mt_seg["pre_release"] = _px_snapshot(_mt_probe["dst_rect"])
                except Exception:
                    pass

            _mt_pops_before = _popup_probe_begin()   # 动作前弹窗集合（Win32）
            result = mouse_transfer(_mt_src, _mt_tgt,
                                    mode=arguments.get("mode", "auto"),
                                    button=arguments.get("button", "left"),
                                    human=bool(arguments.get("human", False)),
                                    speed=(None if arguments.get("speed") is None else float(arguments.get("speed"))),
                                    precise=bool(arguments.get("precise", False)),
                                    verify_effect=bool(arguments.get("verify_effect", True)),
                                    settle_ms=int(arguments.get("settle_ms", _PX_SETTLE_MS) or 0),
                                    verify_region=arguments.get("verify_region", "") or "",
                                    change_threshold=(None if arguments.get("change_threshold") is None else float(arguments.get("change_threshold"))),
                                    effect_image=bool(arguments.get("effect_image", False)),
                                    effect_samples=int(arguments.get("effect_samples", 1) or 1),
                                     effect_span_ms=int(arguments.get("effect_span_ms", 0) or 0),
                                     effect_strip=bool(arguments.get("effect_strip", False)),
                                     safe_park=arguments.get("safe_park", "") or "",
                                    undo=arguments.get("undo", "auto"),
                                    on_pick_done=_mt_pick_done,
                                    pre_release_hook=_mt_pre_release)
            _ms0 = _AXIS0.get("source")
            _mt0 = _AXIS0.get("target")
            _ms_txt = (("图内 (%s,%s)" % _ms0) if _ms0 else result["source"])
            _mt_txt = (("图内 (%s,%s)" % _mt0) if _mt0 else result["target"])
            _mt_lines = ["鼠标搬移 %s→%s ｜ 请求模式=%s ｜ 实际=%s ｜ %s"
                         % (_ms_txt, _mt_txt, result["mode_requested"],
                            result["mode_used"],
                            "**生效（changed=true）**" if result["changed"] else "⚠ **未生效（changed=false）**")]
            for _st in (result.get("stages") or []):
                _mt_lines.append("  " + (_st.get("line") or ""))
            if result.get("undo"):
                _mt_lines.append("撤销记录：" + result["undo"])
            if result.get("safe_park"):
                _mt_lines.append(result["safe_park"])
            if _mt_probe is not None:
                _mt_verify = bool(arguments.get("verify_effect", True))
                _mt_used = result.get("mode_used")
                _mt_probe["mode_used"] = _mt_used
                _mt_probe["pick_after"] = _mt_seg.get("pick_after")
                _mt_probe["pre_release"] = _mt_seg.get("pre_release")
                _mt_eff_snap = _PX_EFF_LAST.get("snap") if _mt_verify else None
                # drag 模式：B 帧＝按下前；click 模式：C 帧＝放置前——都是「生效-before」那张，零额外抓取
                _mt_probe["arrive"] = (_px_crop_snap(_mt_eff_snap, _mt_probe.get("src_rect"))
                                       if (_mt_used == "drag" and _mt_probe.get("src_rect")) else None)
                _mt_probe["place_before"] = (_px_crop_snap(_mt_eff_snap, _mt_probe.get("dst_rect"))
                                             if (_mt_used == "click" and _mt_probe.get("dst_rect")) else None)
                _mt_probe["final"] = (_after_from_final(_mt_probe.get("dst_rect"),
                                                        _mt_seg.get("pre_release")
                                                        or _mt_probe.get("place_before")
                                                        or _mt_probe.get("dst"), 200)
                                      if _mt_probe.get("dst_rect") else None)
            _mt_sem = _transfer_probe_report(_mt_probe)
            if _mt_sem:
                _mt_lines.append("局部判定（纯像素）：")
                _mt_lines.extend("  " + _x for _x in _mt_sem)
            _mt_pops = _popup_probe_line(_mt_pops_before)   # 弹窗差集（Win32）
            if _mt_pops:
                _mt_lines.append(_mt_pops)
            return {"content": [{"type": "text", "text": "\n".join(_mt_lines) + _status_block()}]}

        elif name == "hover":
            if (cancel := _check_cancel()): return cancel
            hx = arguments.get("x")
            hy = arguments.get("y")
            if hx is None or hy is None:
                return {"content": [{"type": "text", "text": "请提供 x 和 y 坐标"}], "isError": True}
            _show_indicator(window=_get_focus_title(), action=f"悬停 ({hx},{hy})")
            # 局部像素变化测量 + 弹窗差集：悬停会不会让界面起反应（tooltip/高亮）、有没有弹出菜单
            _hv_rect, _hv_before = _local_snapshot(int(hx), int(hy), 160)
            _hv_b = {"snap": None}

            def _hv_arrive():
                try:
                    if _hv_rect:
                        _hv_b["snap"] = _px_snapshot(_hv_rect)
                except Exception:
                    pass

            _hv_pops = _popup_probe_begin()
            _px_phase_flush()      # 动作边界：清复用缓存（后帧绝不许裁到前帧）
            result = hover(hx, hy,
                           dwell_ms=arguments.get("dwell_ms", 600),
                           rect=arguments.get("rect", "") or "",
                           human=bool(arguments.get("human", False)),
                           speed=(None if arguments.get("speed") is None else float(arguments.get("speed"))),
                           precise=bool(arguments.get("precise", False)),
                           on_arrive=_hv_arrive)
            _hv_pt = _axis_fmt(int(result["x"]), int(result["y"]), "悬停 ")
            _hv = ("%s 停留 %dms ｜ 移动用时 %ss"
                   % (_hv_pt or "悬停（图外）", result["dwell_ms"], result["elapsed"]))
            if result.get("image_rect"):
                _ir = result["image_rect"]
                _h1 = _axis_from_screen(_ir[0], _ir[1])
                _h2 = _axis_from_screen(_ir[0] + _ir[2], _ir[1] + _ir[3])
                if _h1 and _h2:
                    _hv += ("\n局部图：图内 %d,%d %d×%d（见附图）"
                            % (int(round(_h1[0])), int(round(_h1[1])),
                               int(round(_h2[0] - _h1[0])), int(round(_h2[1] - _h1[1]))))
                else:
                    _hv += "\n局部图：见附图"
            if result.get("image_error"):
                _hv += "\n⚠ 局部图取失败：%s" % (result["image_error"],)
            _hv_sem = ""
            _hv_arr = _hv_b.get("snap")
            if _hv_rect:
                _hv_after = _after_from_final(_hv_rect, _hv_arr or _hv_before, 200)
                _seg_lines = []
                if _hv_arr is not None:
                    _s1 = _local_sem_line("移动前→到达后", _hv_before, _hv_arr, rect=_hv_rect)
                    if _s1:
                        _seg_lines.append("移动段（纯像素）：" + _s1)
                    _s2 = _local_sem_line("到达后→停留后", _hv_arr, _hv_after,
                                          "命中：" + _window_at_text(int(hx), int(hy)),
                                          rect=_hv_rect, motion=True)
                else:
                    _s2 = _local_sem_line("悬停点邻域", _hv_before, _hv_after,
                                          "命中：" + _window_at_text(int(hx), int(hy)),
                                          rect=_hv_rect, motion=True)
                if _s2:
                    _seg_lines.append("局部判定（纯像素）：" + _s2)
                if _seg_lines:
                    _hv += "\n" + "\n".join(_seg_lines)
            _hv_pl = _popup_probe_line(_hv_pops)
            if _hv_pl:
                _hv += "\n" + _hv_pl
            return {"content": [{"type": "text", "text": _hv + _status_block()}]}

        elif name == "set_window":
            x = arguments.get("x")
            y = arguments.get("y")
            w = arguments.get("width")
            h = arguments.get("height")
            _show_indicator(window=_get_focus_title(), action="调整窗口")
            result = set_window(x=x, y=y, width=w, height=h)
            return {"content": [{"type": "text", "text": f"窗口: left={result['left']}, top={result['top']}, {result['width']}x{result['height']}{_status_block()}"}]}

        elif name == "scroll":
            if (cancel := _check_cancel()): return cancel
            clicks = arguments["clicks"]
            x = arguments.get("x")
            y = arguments.get("y")
            mode = arguments.get("mode")
            # 局部像素变化测量：滚动前后各取一块邻域做差分（判断"滚了没有/内容变没变"）
            # ★前台滚轮按「光标位置」投递（x/y 不生效）⇒ 探针必须取真实光标位置，
            #   否则会拿 A 处的画面去解释 B 处的动作（修）。
            _use_bg_sc = (arguments.get("mode") == "background") or (
                arguments.get("mode") is None and _is_terminal_or_child())
            _sc_x, _sc_y = x, y
            try:
                _ptc = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(_ptc))
                _cur_sc = (_ptc.x, _ptc.y)
            except Exception:
                _cur_sc = None
            if not _use_bg_sc and _cur_sc:
                _sc_x, _sc_y = _cur_sc
            elif _sc_x is None or _sc_y is None:
                _sc_x, _sc_y = (_cur_sc if _cur_sc else (None, None))
            _sc_rect, _sc_before = (None, None)
            if _sc_x is not None and _sc_y is not None:
                _sc_rect, _sc_before = _local_snapshot(int(_sc_x), int(_sc_y), 160)
            _sc_pops_before = _popup_probe_begin()
            _px_phase_flush()      # 动作边界：清复用缓存（滚轮几毫秒就完，不赌 50ms）
            result = scroll(clicks, x, y, mode)
            _show_indicator(window=_get_focus_title(), action=f"滚动 {clicks} 格")
            method = result.get("method", "?")
            _sc0 = _AXIS0.get("x")
            _sc1 = _AXIS0.get("y")
            if _sc0 is not None and _sc1 is not None:
                pos = "图内 (%s,%s)" % (_sc0, _sc1)
            elif result.get('x') is not None:
                pos = "（坐标见附图）"
            else:
                pos = "光标位置"
            _at_sc = result.get("at_cursor")
            if _at_sc and result.get('x') is not None:
                try:
                    if (int(_at_sc[0]), int(_at_sc[1])) != (int(x or 0), int(y or 0)):
                        pos += "（实际作用在 %s）" % (_axis_fmt(_at_sc[0], _at_sc[1]) or "图外")
                except Exception:
                    pass
            extra = ""
            if _sc_rect:
                # 滚动是异步重绘：after 用「未测到变化就重抓」的 600ms 窗口（滚动前/后必须不同图）
                _sc_after = _after_from_final(_sc_rect, _sc_before, 600)
                _sc_line = _local_sem_line("滚动点邻域", _sc_before, _sc_after,
                                           "命中：" + _window_at_text(int(_sc_x), int(_sc_y)),
                                           rect=_sc_rect, motion=True)
                if _sc_line:
                    extra = "\n局部判定（纯像素）：" + _sc_line
            _sc_pl = _popup_probe_line(_sc_pops_before)
            if _sc_pl:
                extra += "\n" + _sc_pl
            _prune_tracked_popups()
            return {"content": [{"type": "text", "text": f"滚动 {clicks} 格 {pos} [{method}]{_status_block()}{extra}"}]}

        elif name == "get_mouse_position":
            pos = get_mouse_position()
            extra = f" [{pos['process']}] pid={pos.get('pid','?')} class={pos['class']} | {pos['title']}" if pos.get('title') else ""
            _mp_txt = (_axis_fmt(pos['x'], pos['y'], "鼠标") if _AXIS is not None
                       else "鼠标：（尚未建立图内坐标轴）")
            return {"content": [{"type": "text", "text": f"{_mp_txt or '鼠标：不在本图内'}{extra}"}]}

        elif name == "list_windows":
            keyword = arguments.get("keyword", "")
            # **默认列全部**（含隐藏），**在每一行上打标**（[可见] / [DWM隐藏] / [Win32不可见]）；
            #   要看过滤后的，显式传 include_hidden=false 即可（旧规则保留）。
            _only_visible = (arguments.get("include_hidden", None) is False)
            windows = enum_all_windows(include_hidden=not _only_visible)
            if keyword:
                windows = [w for w in windows if keyword.lower() in w["title"].lower()
                           or keyword.lower() in w.get("class","").lower()
                           or keyword.lower() in w.get("process","").lower()]
            if not windows:
                return {"content": [{"type": "text", "text": "未找到窗口（规则：全量·含隐藏；要只看显示着的传 include_hidden=false）"}]}
            all_count = len(windows)
            child_total = 0
            lines = [f"共 {all_count} 个窗口（每行均标了状态：可见 / DWM隐藏 / Win32不可见；要只看显示着的传 include_hidden=false）："]
            for w in windows:
                if _is_dwm_cloaked(w["hwnd"]):
                    hidden_mark = " [DWM隐藏·你看不到]"
                elif user32.IsWindowVisible(w["hwnd"]):
                    hidden_mark = " [可见]"
                else:
                    hidden_mark = " [Win32不可见]"
                lines.append(f"  {w['hwnd']} | [{w.get('process','?')}] pid={w.get('pid','?')} class={w.get('class','?')} ({w['left']},{w['top']} {w['width']}x{w['height']}){hidden_mark} | {w['title']}")
                children = list_child_windows(w["hwnd"])
                if children:
                    child_total += len(children)
                    for c in children:
                        vis = " [可见]" if user32.IsWindowVisible(c["hwnd"]) else ""
                        lines.append(f"    └ {c['hwnd']} | {c['class']} | ({c['left']},{c['top']} {c['width']}x{c['height']}){vis} | {c['title']}")
            if child_total:
                lines.append(f"（含 {child_total} 个子窗口）")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        elif name == "find_click":
            return _find_click_do(arguments)

        elif name == "wait":
            try:
                _conds = _wait_norm_conditions(arguments.get("conditions"))
            except ValueError as _ve:
                return {"content": [{"type": "text", "text": "wait 参数有误：%s" % _ve}], "isError": True}
            try:
                _secs = float(arguments.get("seconds", 1) or 1)
            except Exception:
                return {"content": [{"type": "text", "text": "seconds 必须是数字"}], "isError": True}
            _secs = max(0.05, min(_secs, 3600.0))
            _include = bool(arguments.get("include_image", True))
            _match = str(arguments.get("match") or "any").strip().lower()
            if _match not in ("any", "all"):
                return {"content": [{"type": "text",
                                     "text": "match 只认 any / all（收到 %r）" % (arguments.get("match"),)}], "isError": True}
            if not _conds:
                _show_indicator(window=_get_focus_title(), action=f"等待 {_secs:.1f}s")
                completed = wait_with_cancel(_secs)
                if completed:
                    return {"content": [{"type": "text", "text": f"等待完成 ({_secs:.1f}s){_status_block()}"}]}
                return {"content": [{"type": "text", "text": f"等待被取消{_status_block()}"}], "isError": True}
            _show_indicator(window=_get_focus_title(),
                            action="等条件 %d 个（%s，≤%.1fs）" % (len(_conds), _match, _secs))
            try:
                _ok, _winner, _info = _wait_race(_conds, _secs, match=_match)
            except ValueError as _ve:
                return {"content": [{"type": "text", "text": "wait 参数有误：%s" % _ve}], "isError": True}

            def _cond_detail(_inf):
                """一个条件的达成细节（回执用）。"""
                _k = (_inf or {}).get("kind")
                if _k == "window":
                    return "窗口：%s" % (str((_inf.get("window") or {}).get("title") or "")[:60],)
                if _k == "image":
                    _sc = float(_inf.get("score") or 0.0)
                    _h = _inf.get("hit")
                    _p = _axis_from_screen_raw(_h[0] + _h[2] / 2.0, _h[1] + _h[3] / 2.0) if _h else None
                    return ("相似度 %.3f%s" % (_sc, ("｜匹配中心：图内 (%d,%d)"
                                                    % (int(round(_p[0])), int(round(_p[1])))) if _p else ""))
                if _k == "region_changed":
                    return "变化：%.2f%%" % (float(_inf.get("ratio") or 0.0) * 100.0)
                if _k == "region_stable":
                    return "已静止：%dms" % int(_inf.get("stable_ms") or 0)
                return ""

            def _hits_of(_inf):
                """收集所有图片命中的屏幕矩形（附图里画框）。"""
                _out = []
                if not _inf:
                    return _out
                if _inf.get("kind") == "image" and _inf.get("hit"):
                    _out.append(_inf["hit"])
                for _r in (_inf.get("results") or []):
                    _i2 = _r.get("info") or {}
                    if _i2.get("kind") == "image" and _i2.get("hit"):
                        _out.append(_i2["hit"])
                return _out

            if _ok:
                if _info.get("kind") == "all":
                    _lines = ["等待达成：**全部条件同时满足**（实际 %.2fs｜采样 %d 次）"
                              % (float(_info.get("elapsed") or 0), int(_info.get("ticks") or 0))]
                    for _r in (_info.get("results") or []):
                        _lines.append("  · %s：%s" % (_r.get("mode"), _cond_detail(_r.get("info"))))
                else:
                    _lines = ["等待达成（实际 %.2fs｜采样 %d 次｜match=%s）"
                              % (float(_info.get("elapsed") or 0), int(_info.get("ticks") or 0), _match),
                              "命中条件：%s" % (_winner.get("mode") if _winner else "?")]
                    _det = _cond_detail(_info)
                    if _det:
                        _lines.append(_det)
                _content = [{"type": "text", "text": "\n".join(_lines) + _status_block()}]
                if _include:
                    _hits = _hits_of(_info)
                    _img = _wait_attach_image(_hits, red=1)
                    if _img:
                        _content.append(_img)
                return {"content": _content}
            if _info.get("aborted"):
                _lines = ["⚠ 等待被中止（%.2fs｜采样 %d 次）——序列总超时/ESC/被顶掉"
                          % (float(_info.get("elapsed") or 0), int(_info.get("ticks") or 0))]
            else:
                _lines = ["⚠ 等待超时（%.2fs｜采样 %d 次｜match=%s）：条件没有达成"
                          % (float(_info.get("elapsed") or 0), int(_info.get("ticks") or 0), _match)]
            for _m, _st in (_info.get("states") or []):
                _lines.append("  · %s：%s" % (_m, _st))
            return {"content": [{"type": "text", "text": "\n".join(_lines) + _status_block()}]}

        elif name == "sequence":
            # 新增：输入序列／一次性宏。聚焦只在开头一次；每步不再重复抢前台。
            # 六路：run（默认，**老路逐字保留**）｜append（排队）
            #   ｜overwrite（立刻顶掉旧的）｜list（列表＋内容）｜shot（随时取画面）｜stop。
            #   为什么非要有后台队列：`_HTTP_TOOL_LOCK` 把整个工具面锁住 ⇒ 同步跑宏时**连截图都调不了**；
            #   挪到不持那把锁的后台线程之后，「执行途中随时截图」才成立（见新增块头部注释）。
            _act = str(arguments.get("action") or "run").strip().lower()
            _from_saved = False
            # ── 宏槽三路（单槽持久化，刻意只存一套）：save / saved / run_saved ──
            if _act == "save":
                _msteps = arguments.get("steps")
                if not isinstance(_msteps, list) or not _msteps:
                    return {"content": [{"type": "text", "text": "action=save 需要 steps（非空数组）"}], "isError": True}
                if len(_msteps) > _MACRO_MAX_STEPS:
                    return {"content": [{"type": "text",
                                         "text": "steps 太多（%d 步，上限 %d）" % (len(_msteps), _MACRO_MAX_STEPS)}],
                            "isError": True}
                _mpayload = {"version": 1,
                             "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "note": str(arguments.get("note") or "")[:200],
                             "steps": _msteps,
                             "settings": {_k: arguments.get(_k) for _k in _MACRO_SETTING_KEYS}}
                _mok, _mwhere = _macro_save(_mpayload)
                if not _mok:
                    return {"content": [{"type": "text", "text": "宏槽写入失败：%s" % _mwhere}], "isError": True}
                _mbrief, _mln, _mcut = _seq_steps_brief(_msteps, 900)
                return {"content": [{"type": "text", "text":
                        "✓ 已存进**唯一宏槽**（覆盖旧的一套）\n  步数：%d｜文件：%s%s\n  内容：%s%s\n"
                        "  （刻意只存一套：避免反复套同一套固定动作、失去临场应变；"
                        "saved 看内容、run_saved 直接跑，也可以传 steps 改几步再跑）"
                        % (len(_msteps), _mwhere,
                           ("｜备注：" + _mpayload["note"]) if _mpayload["note"] else "",
                           _mbrief, ("（原文 %d 字已截断）" % _mln) if _mcut else "")}]}
            if _act == "saved":
                _md, _mwhy = _macro_load()
                if _md is None:
                    return {"content": [{"type": "text", "text": _mwhy}]}
                return {"content": [{"type": "text", "text": _macro_text(_md)}]}
            if _act == "run_saved":
                _md, _mwhy = _macro_load()
                if _md is None:
                    return {"content": [{"type": "text", "text": _mwhy}], "isError": True}
                _msteps = arguments.get("steps")
                if not isinstance(_msteps, list) or not _msteps:
                    _msteps = _md.get("steps") or []
                arguments["steps"] = _msteps
                _mcfg = _md.get("settings") or {}
                for _k in _MACRO_SETTING_KEYS:      # 槽里的设置当默认；本次显式传的优先
                    if arguments.get(_k) is None and _mcfg.get(_k) is not None:
                        arguments[_k] = _mcfg[_k]
                _from_saved = True
                _act = "run"
            # ── 只读/控制三路：**不注入任何输入** ⇒ 既不抢前台也不需要 steps（故 schema 的 required 已放宽）
            if _act == "list":
                return {"content": [{"type": "text",
                                     "text": _seq_jobs_text(bool(arguments.get("with_steps", True)))}]}
            if _act == "stop":
                return {"content": [{"type": "text",
                                     "text": _seq_job_stop(arguments.get("id") or "all")}]}
            if _act == "shot":
                import base64 as _b64
                _reg = str(arguments.get("shot_region") or "window").strip().lower()
                if _reg not in ("window", "fullscreen"):
                    return {"content": [{"type": "text",
                                         "text": "shot_region 只认 window / fullscreen"}], "isError": True}
                _as_watch = int(arguments.get("watch_ms", 0) or 0)
                _as_frames = int(arguments.get("frames", 1) or 1)
                if _as_watch > 0 or _as_frames > 1:
                    return {"content": [{"type": "text", "text":
                            "action=shot 的抽帧拼图已取消（会阻塞）；action=shot 只取当前一张；"
                            "回看请用持续会话 vision_action=start/pull。"}], "isError": True}
                if False:      # 旧一次性拼图块：保留代码但不可达（ 停用）
                    # ：途中观察也能抽帧拼图（纯像素；与截图工具同一套实现）
                    try:
                        _agrab = (lambda: _seq_grab_pil(_reg, None))
                        if _as_watch > 0:
                            _as_png, _as_meta = _watch_frames(
                                _agrab, watch_ms=_as_watch,
                                fps=float(arguments.get("fps", 6) or 6),
                                min_frames=int(arguments.get("min_frames", 3) or 3),
                                max_frames=int(arguments.get("max_frames", 6) or 6),
                                tile_max=int(arguments.get("tile_max", 0) or 0),
                                grid_cols=int(arguments.get("grid_cols", 0) or 0),
                                label_prefix="s",
                                sensitivity=float(arguments.get("sensitivity", 1.0) or 1.0),
                                cut_abs=arguments.get("cut_abs"),
                                integral_sec=arguments.get("integral_sec"),
                                dup_min=arguments.get("dup_min"))
                        else:
                            _as_png, _as_meta = _fixed_frames(
                                _agrab, frames=_as_frames,
                                interval_ms=int(arguments.get("interval_ms", 200) or 0),
                                tile_max=int(arguments.get("tile_max", 0) or 0),
                                grid_cols=int(arguments.get("grid_cols", 0) or 0),
                                label_prefix="s")
                    except Exception as _as_e:
                        return {"content": [{"type": "text",
                                             "text": "⚠ 抽帧拼图失败（未出图）：%s" % (str(_as_e)[:200],)}],
                                "isError": True}
                    _as_jc = _seq_jobs.get(_seq_cur[0]) if _seq_cur[0] else None
                    _as_jline = ("正在跑 %s（共 %s 步，已 %.1fs）"
                                 % (_as_jc.get("id"), len(_as_jc.get("steps") or []),
                                    time.time() - (_as_jc.get("t0") or time.time()))
                                 if _as_jc else "当前没有任务在跑")
                    _as_txt = ("当前画面抽帧拼图（%s）：保留 %d 帧 ｜ 单格 %d×%d ｜ 网格 %d×%d ｜ 画布 %d×%d ｜ %d 字节 ｜ %s"
                               % (_reg, _as_meta["frames"], _as_meta["tile"][0], _as_meta["tile"][1],
                                  _as_meta["grid"][0], _as_meta["grid"][1], _as_meta["canvas"][0],
                                  _as_meta["canvas"][1], _as_meta["bytes"], _as_jline))
                    return {"content": [{"type": "text", "text": _as_txt},
                                        {"type": "image",
                                         "data": _b64.b64encode(_as_png).decode("ascii"),
                                         "mimeType": "image/png"}]}
                _sd = _seq_shot_bytes(_reg)
                if not _sd:
                    return {"content": [{"type": "text", "text": "shot 取图失败：未聚焦窗口时按设计不抓"
                                         "前台/全屏（传 shot_region=fullscreen 可绕过）"}], "isError": True}
                _jc = _seq_jobs.get(_seq_cur[0]) if _seq_cur[0] else None
                _jline = ("正在跑 %s（共 %s 步，已 %.1fs）" % (_jc.get("id"), len(_jc.get("steps") or []),
                                                              time.time() - (_jc.get("t0") or time.time()))
                          if _jc else "当前没有任务在跑")
                _sd_marked = _sd
                if not arguments.get("clean"):
                    try:      # ★：这条也是"自己出图"的路径（同一条判据：默认带号／clean 给原图）
                        _sd_marked, _mb = _mark_attach_png(_sd, _focus_hwnd, region=_reg,
                                           style=arguments.get("mark_style"),
                                           grid_dash=arguments.get("grid_dash"),
                                           grid_color=arguments.get("grid_color"),
                                           ruler_inside=arguments.get("ruler_inside"))
                    except Exception as _me:
                        print(f"[标记] 跳过（不影响途中截图）：{_me}", file=sys.stderr)
                return {"content": [{"type": "text",
                                     "text": "当前画面（%s，%d 字节）｜%s" % (_reg, len(_sd_marked), _jline)},
                                    {"type": "image", "data": _b64.b64encode(_sd_marked).decode("ascii"),
                                     "mimeType": "image/png"}]}
            if _act not in ("run", "append", "overwrite"):
                return {"content": [{"type": "text", "text": "action 只认 run/append/overwrite/list/shot/stop"
                                     "（收到 %r）" % (arguments.get("action"),)}], "isError": True}
            if (cancel := _check_cancel()): return cancel
            # 修（全文件复扫查出）：原来这里只查"聚焦窗还活着"，**绕过了
            #   `_require_focus_target()` 那道护栏**——它多一条 **DWM 幽灵窗**判定（幽灵窗按坐标动作一律中止）。
            #   序列是纯坐标/输入动作 ⇒ 必须与 mouse_move/mouse_click/
            #   mouse_drag 走同一道闸（判据：同类动作的护栏必须一致，不能只挑一条路接）。
            try:
                _require_focus_target("输入序列")
            except Exception as _fe:
                return {"content": [{"type": "text", "text": f"输入序列已中止：{_fe}"}], "isError": True}
            steps = arguments.get("steps")
            tmo = arguments.get("timeout_ms", 60000)
            rel = bool(arguments.get("relative", True))
            _pc = bool(arguments.get("precise", False))   # 两档：快速档专用"精确(不走抖动)"
            # ★：`action` 放宽了 schema 的 required ⇒ 这里自己判——要跑就必须给 steps
            #   （`list`/`shot`/`stop` 在上面三路里已经返回了，走到这儿的只有 run/append/overwrite）
            if not isinstance(steps, list) or not steps:
                return {"content": [{"type": "text", "text": "action=%s 需要 steps（非空数组）" % _act}],
                        "isError": True}
            _ensure_foreground(_focus_hwnd)          # 沿用 mouse_move/mouse_drag 的做法：只做一次
            _show_indicator(window=_get_focus_title(),
                            action=f"输入序列 {len(steps) if isinstance(steps, list) else '?'} 步")
            if _act != "run":
                # 排队／顶掉：**立刻回执**（不等跑完），执行交给后台 worker。
                #   回执＝"做了什么 ＋ **列表与内容**"（能看清哪个在执行、内容是什么）。
                _jid, _killed, _cleaned = _seq_job_submit(
                    steps, timeout_ms=tmo, relative=rel, human=bool(arguments.get("human", False)),
                    speed=arguments.get("speed"), precise=_pc, hwnd=_focus_hwnd, overwrite=(_act == "overwrite"),
                    ime_ascii=arguments.get("ime_ascii", True),
                    ime_mode=arguments.get("ime_mode", "temp"),
                    unicode_text=bool(arguments.get("unicode_text", False)))
                _h = ("%s %s 已入队（action=%s）——立刻回执；跑完用 action=list 看结果、"
                      "action=shot 看现场\n" % ("覆盖" if _act == "overwrite" else "追加", _jid, _act))
                if _killed:
                    _h += "  · 被顶掉／清掉：%s\n" % ", ".join(_killed)
                if _cleaned:
                    _h += "  · 顺带清掉已结束的历史条：%s\n" % ", ".join(_cleaned)
                return {"content": [{"type": "text", "text": _h + _seq_jobs_text(True)}]}
            _pre_win = _win_snapshot_light()
            try:
                res = _run_input_sequence(steps, timeout_ms=tmo, relative=rel,
                                          human=bool(arguments.get("human", False)),
                                          speed=arguments.get("speed"),
                                          precise=_pc,
                                          ime_ascii=arguments.get("ime_ascii", True),
                                          ime_mode=arguments.get("ime_mode", "temp"),
                                          unicode_text=bool(arguments.get("unicode_text", False)))
            except ValueError as _ve:
                return {"content": [{"type": "text", "text": f"sequence 参数有误：{_ve}"}], "isError": True}
            # 动作/输入之后必须作废快照：
            #   本文件把这一时机**收在一处**：`_ocr_cache_invalidate()`——它同时做这些：
            #   ① **`_frame_stale()`：附图复用通道作废（防给一张过期画面）**
            #   ② 清编号批表 ③ 清该 hwnd 的旧快照。
            #   `mouse_click` 与 `send_keystrokes` 的分支都调它；**序列跑完同样必须调**——
            #   否则序列之后的附图会**复用宏之前登记的那一帧**（本线最忌讳的那类错）。
            try:
                if _popup_hwnd and _is_window_alive(_popup_hwnd):
                    _ocr_cache_invalidate(_popup_hwnd)
                else:
                    _ocr_cache_invalidate(_focus_hwnd)
            except Exception:
                pass
            _prune_tracked_popups()
            _ab = {"timeout": "⏱ 超时中止", "esc": "⛔ 被取消（ESC+`）"}.get(res.get("aborted") or "", "")
            # 新步型的明细（老步型不产生这些键 ⇒ 老回执逐字不变）
            _extra_lines = []
            for _m in (res.get("matches") or []):
                _first = ((_m.get("text") or "").split("\n") or [""])[0]
                _extra_lines.append("找图（%s）：%s" % (_m.get("action"), _first[:180]))
            for _w in (res.get("wait_conditions") or []):
                _extra_lines.append("等条件（match=%s）：%s｜用时 %ss"
                                    % (_w.get("match"),
                                       ("✓ 达成，命中 %s" % (_w.get("winner") or "-")) if _w.get("ok") else "⏱ 超时",
                                       _w.get("elapsed")))
            for _t in (res.get("transfers") or []):
                _extra_lines.append("搬移：%s→%s｜实际模式=%s｜changed=%s%s"
                                    % (_t.get("from"), _t.get("to"), _t.get("mode_used") or _t.get("mode"),
                                       str(bool(_t.get("changed"))).lower(),
                                       "" if _t.get("verify") else "（未做像素判定）"))
            for _h in (res.get("hovers") or []):
                _extra_lines.append("悬停：(%s,%s) 停留 %sms" % (_h.get("x"), _h.get("y"), _h.get("dwell_ms")))
            for _f in (res.get("focuses") or []):
                _extra_lines.append("聚焦：%s（hwnd %s）" % (_f.get("window"), _f.get("hwnd")))
            for _z in (res.get("sys") or []):
                _extra_lines.append("系统：%s" % json.dumps(_z, ensure_ascii=False)[:220])
            for _ws in (res.get("window_snaps") or []):
                _wtitles = "；".join(str(w.get("title") or "")[:30] for w in (_ws.get("windows") or [])[:10])
                _extra_lines.append("窗口清单：%d 个｜%s%s"
                                    % (int(_ws.get("count") or 0), _wtitles,
                                       "…" if int(_ws.get("count") or 0) > 10 else ""))
            _extra_txt = "".join("\n  " + _l for _l in _extra_lines)
            _txt = (f"输入序列 {res['steps_done']}/{res['steps_total']} 步 ｜ 用时 {res['elapsed']}s"
                    f"（超时 {res['timeout_ms']}ms）"
                    f"{(' ｜ ' + _ab) if _ab else ' ｜ ✓ 跑完'}"
                    f"{' ｜ 来自宏槽（run_saved）' if _from_saved else ''}\n"
                    f"  步型：{json.dumps(res['kinds'], ensure_ascii=False)}"
                    f" ｜ 请求位移 dx={res['requested_move']['dx']}, dy={res['requested_move']['dy']}"
                    f" ｜ 停等合计 {res['waited_s']}s"
                    + ("".join("\n  切块铺时间：%d 步 × %gpx（间隔 %gms，实速 %g px/s）%s"
                               % (_pm["steps"], _pm["step_px"], _pm["interval_ms"],
                                  _pm["speed_effective"], ("  " + _pm["pace_note"]) if _pm.get("pace_note") else "")
                               for _pm in (res.get("paces") or []))
                       if res.get("paces") else "")
                    + (f"\n  退出时抬起：{', '.join(res['keys_released_on_exit'])}"
                       if res.get("keys_released_on_exit") else "")
                     + ("".join("\n  打字：%s" % _n for _n in (res.get("kbd_notes") or []))
                        if res.get("kbd_notes") else "")
                    + _seq_shots_text(res)
                     + _extra_txt
                     + _win_delta_text(_pre_win) + _status_block())
            return {"content": [{"type": "text", "text": _txt}]}

        return {"content": [{"type": "text", "text": f"未知工具: {name}"}], "isError": True}

    except Exception as e:
        return {"content": [{"type": "text", "text": f"执行错误: {str(e)}\n{traceback.format_exc()}"}], "isError": True}


def _deep_clean(obj):
    """剥掉孤立代理字符（json.dumps 会炸）——stdio 与 HTTP 两条路共用。"""
    if isinstance(obj, str):
        return "".join(c for c in obj if not (0xD800 <= ord(c) <= 0xDFFF))
    if isinstance(obj, dict):
        return {k: _deep_clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_clean(i) for i in obj]
    return obj


def respond(rid, result):
    msg = {"jsonrpc": "2.0", "id": rid, "result": _deep_clean(result)}
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _tb_tail(limit=1200):
    """安全取 traceback 尾部：连"错误路径自己出错"都要兜住（实测踩过：
    兜底里一个 NameError 就会让客户端一声不吭干等超时——比报错更坏）。"""
    try:
        return traceback.format_exc()[-limit:]
    except Exception:
        return ""


def _rpc(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return _deep_clean(msg)


def _tool_call_result(params):
    """tools/call 执行 + 耗时贴首行（stdio 与 HTTP 共用；逻辑与改造前逐字一致）。"""
    t0 = time.time()
    _args = params.get("arguments", {}) or {}
    # 只在明确要带回截图时才让路：不整段工具调用暂停后台；
    # 由抓屏闸保证"前台截图 > 后台采样"（后台抢不到锁就跳过本帧，工具截图只等当前那一次抓取）。
    result = handle_tool_call(params.get("name"), _args)
    items = result.get("content", [])
    if items and items[0]["type"] == "text":
        text = items[0]["text"]
        lines = text.split("\n", 1)
        lines[0] += f" [{time.time()-t0:.1f}s]"
        sys_stats = _sys_stats_block()
        if _debug_timing and sys_stats:
            items[0]["text"] = "\n".join(lines) + sys_stats
        else:
            items[0]["text"] = "\n".join(lines)
    # 按住状态提醒：
    #   **每一次工具回执都检查**——入口自动松开的说明（`_HELD_AUTO_NOTE`）＋ 当前仍按住的（鼠标/键盘）。
    #   目的：模型从回执里就知道自己还处在"抓住"状态，不会忘了松开；鼠标残留下一次调用会被自动松开。
    try:
        if items and items[0].get("type") == "text":
            _hold_notes = []
            if _HELD_AUTO_NOTE:
                _hold_notes.append(_HELD_AUTO_NOTE)
            _hd_now = _held_desc()
            if _hd_now:
                _hold_notes.append(
                    "⚠ **当前处于按住状态**：%s——记得松开（键盘键请发成对的 `key_up`／`{\"key_up\":[...]}`；"
                    "鼠标若卡住，随便调一次工具会在入口自动松开并告警）" % _hd_now)
            if _hold_notes:
                items[0]["text"] += "\n" + "\n".join(_hold_notes)
    except Exception:
        pass
    # ★聚焦窗失联 ⇒ 同进程自动接管的说明（由 `_require_focus_target` 写；每轮入口清空）
    try:
        if _LAST_FOCUS_NOTE and items and items[0].get("type") == "text":
            items[0]["text"] += "\n🪟 " + _LAST_FOCUS_NOTE
    except Exception:
        pass
    # ★ P0：**动作回执固定带窗口矩形**（实战反馈：窗口尺寸会变、回执却不带 rect
    #   ⇒ 之前算好的坐标全作废）。纯 Win32 读一次 GetWindowRect，**不做窗口枚举、不碰识别**。
    try:
        if params.get("name") in _ACTION_RECT_TOOLS and items and items[0].get("type") == "text":
            _wr_line = _window_rect_line()
            if _wr_line:
                items[0]["text"] += "\n" + _wr_line
    except Exception:
        pass
    # ★0.1.88：坐标轴说明（本轮回执里的坐标按图内坐标解释；屏幕坐标不写回执）
    try:
        if _AXIS_NOTE and items and items[0].get("type") == "text":
            items[0]["text"] += "\n" + _AXIS_NOTE
    except Exception:
        pass
    # ★附画面：跑完把当前画面还给调用方。默认值按工具类别定（动作类默认开，见 _want_image）
    #   ★ 修（真机观察：一次 `sequence action=shot` 出**两张带号图、两套编号**）：
    #     文件里有**两处"自己出图"的路**（`capture_screenshot`、`sequence action=shot`）——它们各自把图
    #     塞进 content 之后，这里**又无条件附一张**：同一帧被重取/重标一次 ⇒ 两张图看着一样、编号却两套
    #     （差点把"两套号"当成两帧）。判据取最稳的那层：**content 里已有 image 就不再附**，
    #     并在回执文本里明写一句（**不静默跳过**——静默正是本线最忌的）。一次调用＝一张图。
    _has_img = any(isinstance(_c, dict) and _c.get("type") == "image"
                   for _c in (result.get("content") or []))
    if _has_img:
        if items and items[0].get("type") == "text":
            # ★：自带图的那两条路（capture_screenshot / sequence action=shot）也要把
            #   `_mark_attach_png` 写好的**标尺行**带出来（否则全屏图的"图原点/k/DPI"就丢了）。
            _img_extra = ""
            try:
                if _LAST_SHOT_NOTE:
                    _img_extra = "\n[附图] " + _LAST_SHOT_NOTE
            except Exception:
                pass
            items[0]["text"] += ("\n[附图] 本工具已自带一张图（同一帧），不再附第二张——免得同屏两套编号"
                                 + _img_extra)
    elif _want_image(params.get("name"), _args):
        # 本工具已生成「可选小图」（`effect_image` 的变化区域 / `hover` 的局部图）
        #   ⇒ **只回这张小图、不再附整图**（不返整图、省 token）。
        _extra = _take_extra_image()
        if _extra:
            _axis_reset("最近附图是变化区裁片/局部小图，不是坐标轴图——请重新截图")
            result.setdefault("content", []).append(_extra["item"])
            if items and items[0].get("type") == "text":
                items[0]["text"] += "\n[附图] " + _extra["note"] + "（本次只回这张小图，不另附整图）"
        else:
            _set_shot_note("")          # 每轮重置，别把上一枪的说明带过来
            _tp_at = time.time()
            # ★ 点击标记：只对确实由鼠标点下去的工具取坐标（relative 用当前光标）
            _mark_click = None
            try:
                _tn = params.get("name")
                if _tn == "mouse_click":
                    if _args.get("relative"):
                        _cp = get_mouse_position()
                        _mark_click = (_cp.get("x"), _cp.get("y"))
                    else:
                        _mark_click = (int(_args.get("x")), int(_args.get("y")))
                elif _tn == "mouse_drag":
                    _mark_click = (int(_args.get("end_x")), int(_args.get("end_y")))
            except Exception:
                _mark_click = None
            _reg = _args.get("include_image_region") or "window"
            # 只返回一张（原图/标注图），两种图都叠鼠标位置。
            _set_shot_note("")
            _it = _shot_item(_reg, mark_click=_mark_click,
                             mark_style=_args.get("mark_style"),
                             grid_dash=_args.get("grid_dash"),
                             grid_color=_args.get("grid_color"),
                             ruler_inside=_args.get("ruler_inside"))
            _timing_add("attach", time.time()-_tp_at)   # 第 19 步：跑完附图（单张，两种模式都带点击标记）
            if _it:
                result.setdefault("content", []).append(_it)
                if _LAST_SHOT_NOTE and items and items[0].get("type") == "text":
                    items[0]["text"] += f"\n[附图] {_LAST_SHOT_NOTE}"
    # ★回执末尾软提醒（批量/单步调用下模型不会主动规划取图方式）：
    #   动作类：有图 ⇒ 已附图可直接看；无图 ⇒ 需要画面时再调 capture_screenshot。
    #   截图类：连续观察可用后台三模式。
    try:
        _nm = params.get("name")
        _has_any = any(isinstance(_c, dict) and _c.get("type") == "image"
                       for _c in (result.get("content") or []))
        _guide = ""
        if _nm in _GUIDE_DESC_TOOLS:
            if _has_any:
                _guide = "（本回执已附图，可直接查看，不必再单独调 capture_screenshot）"
            else:
                _guide = ("（本回执未附图；需要看画面时再调 capture_screenshot。"
                          "若只是确认动作结果，可在动作末尾允许附图，include_image 默认就是开的，"
                          "省一次单独截图调用。）")
        elif _nm == "capture_screenshot" and _has_any:
            _guide = ("（连续观察可用后台三模式：vision_action=\"start\" 持续会话 / "
                      "watch_ms 概览拼图 / after_ms 延迟单拍，完成后自动注入）")
        if _guide and items and items[0].get("type") == "text":
            items[0]["text"] += "\n" + _guide
    except Exception:
        pass
    return result


def dispatch_message(msg):
    """JSON-RPC 分发：stdio 与 HTTP 共用同一条逻辑。返回响应 dict；通知类返回 None。"""
    rid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params", {}) or {}
    if method == "initialize":
        return _rpc(rid, {"protocolVersion": "2024-11-05",
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "desktop-control", "version": "4.0.0"}})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "tools/list":
        return _rpc(rid, {"tools": TOOLS})
    if method == "tools/call":
        return _rpc(rid, _tool_call_result(params))
    if method == "ping":
        return _rpc(rid, {})
    return _rpc(rid, None, {"code": -32601, "message": "未知方法: " + str(method)})


atexit.register(_hide_indicator)
atexit.register(_vision_shutdown)   # ★：进程退出时停掉持续观察会话，及时释放采集器


def _stdio_loop():
    """传统 stdio 模式（向后兼容：不带 --http 时就是老行为，一字不改）。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = dispatch_message(msg)
        if resp is None:
            continue
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


# ========== HTTP 模式 ==========
# 本实现自己就是 IP 端点（令牌校验）。
# 协议行为：POST /mcp + mcp-session-id 头；GET /mcp 回 405＝"不作 SSE"。
_HTTP_TOOL_LOCK = threading.Lock()   # 工具串行执行，语义与 stdio 完全一致


class _uia_disabled(object):
    """原 `_uia_thread_init`：UIA 已整层删除 ⇒ 只留空上下文管理器保持原调用形状。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _local_ips():
    ips = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.append(ip)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


class _McpHTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "desktop-control/4.0"
    token = ""
    port = 0

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, code, obj):
        body = json.dumps(_deep_clean(obj), ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("mcp-session-id", getattr(self, "_sid", "dsh-desktop-control"))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = (self.path.split("?", 1)[0]).rstrip("/") or "/"
        if path in ("/health", "/healthz"):
            self._send_json(200, {"ok": True, "server": "desktop-control", "tools": len(TOOLS),
                                  "pid": os.getpid(), "port": self.port})
        else:
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        try:
            self._do_post()
        except Exception as e:
            # 兜底：任何意外都必须回一个 JSON-RPC 错误，绝不断连接
            # （实测教训：工具抛错时"不回字节"比报错更坏——客户端只看到"无响应"；
            #  更狠的教训：兜底自己再炸一次，就连这句都没有了 ⇒ 绝不依赖可能出错的东西）
            try:
                self._send_json(500, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32603, "message": f"handler error: {e}",
                                                "data": _tb_tail()}})
            except Exception:
                try:   # 最后一道：哪怕只剩裸写，也要留个字节，绝不静默
                    self.wfile.write(b'{"jsonrpc":"2.0","id":null,"error":{"code":-32603,'
                                     b'"message":"handler error (response build failed)"}}')
                except Exception:
                    pass

    def _do_post(self):
        path = (self.path.split("?", 1)[0]).rstrip("/") or "/"
        if path != "/mcp":
            self._send_json(404, {"error": "not found"})
            return
        tok = self.headers.get("x-mcp-token", "")
        auth = self.headers.get("Authorization", "") or ""
        if not tok and auth.startswith("Bearer "):
            tok = auth[7:]
        if self.token and tok != self.token:
            self._send_json(401, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32001, "message": "bad token"}})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        raw = self.rfile.read(n) if n else b""
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": f"parse error: {e}"}})
            return
        self._sid = self.headers.get("mcp-session-id") or "dsh-desktop-control"
        try:
            with _uia_disabled(), _HTTP_TOOL_LOCK:
                resp = dispatch_message(msg)
        except Exception as e:
            resp = _rpc(msg.get("id"), None, {"code": -32603, "message": f"内部错误: {e}",
                                              "data": _tb_tail()})
        if resp is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(200, resp)


def _resolve_token(arg="", token_file=""):
    """令牌来源：参数 → 环境变量 → mcp-token.txt → 随机生成并落盘。
    重启令牌不变是硬要求（否则每次都要人工同步）。"""
    if arg:
        return arg.strip()
    env = os.environ.get("MCP_TOKEN")
    if env:
        return env.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [token_file] if token_file else []
    cands += [os.path.join(here, "mcp-token.txt")]
    for p in cands:
        if not p:
            continue
        try:
            t = open(p, encoding="utf-8").read().strip()
            if t:
                return t
        except Exception:
            pass
    t = "tok-" + secrets.token_hex(8)
    try:
        open(os.path.join(here, "mcp-token.txt"), "w", encoding="utf-8").write(t)
    except Exception:
        pass
    return t


def serve_http(port=8770, bind="0.0.0.0", token=""):
    _McpHTTPHandler.token = token
    _McpHTTPHandler.port = port
    httpd = ThreadingHTTPServer((bind, port), _McpHTTPHandler)
    httpd.daemon_threads = True
    print(f"[mcp] 监听 {bind}:{port}（POST /mcp；GET /health）；工具 {len(TOOLS)} 个；pid={os.getpid()}", flush=True)
    print(f"[mcp] 令牌: {token}", flush=True)
    for ip in _local_ips():
        print(f"[mcp] 可连地址: http://{ip}:{port}/mcp", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="桌面控制（默认 stdio；--http N 则自己开 IP 端点）")
    ap.add_argument("--http", type=int, default=0, help="以 HTTP 服务方式监听该端口（0=传统 stdio 模式）")
    ap.add_argument("--bind", default="0.0.0.0", help="HTTP 绑定地址，默认 0.0.0.0")
    ap.add_argument("--token", default="", help="令牌（客户端用 x-mcp-token 头带上）")
    ap.add_argument("--token-file", default="", help="从文件读令牌")
    args = ap.parse_args()
    if args.http:
        serve_http(args.http, args.bind, _resolve_token(args.token, args.token_file))
    else:
        _stdio_loop()


if __name__ == "__main__":
    main()


