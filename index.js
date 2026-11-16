// @ts-nocheck
/**
 * dsh-desktop-control · Host 半边（Node 壳）
 *
 * 形态：**Node 只做壳**——
 *   ① 启动时用 Python 本体（worker.py --dump-tools）取回工具面（名字/描述/参数 schema，
 *      单一出处＝Python 的 TOOLS，不在 JS 里抄一份）；
 *   ② 每个工具注册成 Harness 工具：execute 时把 {name, args} 用**私有 stdio 行协议**发给
 *      Python 常驻进程（私有行协议，不走 HTTP），把回来的 content 转成文本 + 图片附件；
 *   ③ 图片回执：base64 → ctx.get('attachments').saveImage() → {type:'image', attachment}。
 *
 * 红线：**识别层（OCR/UIA/图标 YOLO）已整层删除**，视觉只靠截图；动作/窗口/序列全部由 Python 执行。
 */
import { spawn, execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { createUserMessage } from '@deepseek-ai/dsh-llm'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN_ID = 'dsh-desktop-control'
const WORKER = path.join(__dirname, 'worker.py')
const PY_DEFAULT = 'python'
const STATE_FILE = path.join(os.homedir(), '.dsh', 'desktop-control', 'state.json')
const API_PATH = '/desktop-control/api'

/** 安装根目录：pnpm 布局下 __dirname = <根>/node_modules/.pnpm/<包>@<版本>/node_modules/<包>。
 *  目录安装（非 pnpm）时为空。 */
const INSTALL_BASE = (() => {
    const i = __dirname.indexOf(path.sep + 'node_modules' + path.sep + '.pnpm' + path.sep)
    return i > 0 ? __dirname.slice(0, i) : ''
})()
/** profile 里指向「当前已安装版本」的软链（升级后自动指向新版本；目录安装时为空）。 */
const LINKED_ROOT = INSTALL_BASE
    ? path.join(INSTALL_BASE, 'node_modules', 'dsh-desktop-control')
    : ''

function readVersion(root) {
    try { return JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version || '' } catch { return '' }
}

/** 只按**版本号**判断是否需要重载：开发期改脚本/改 Python 而不升版本，不会触发重载
 *  （避免反复重启重载破坏工具面）；发布时升版本才会自动换。 */
let installedRoot = __dirname       // 当前生效的安装目录（热重载后指向新版本）
let workerPath = WORKER             // 当前生效的 worker.py
let PLUGIN_VERSION = readVersion(__dirname)

/** Python 依赖缺失时，给一段可直接照着做的安装提示。 */
function pythonHint(python, err) {
    const raw = String((err && (err.stderr || err.message)) || err || '')
    const m = raw.match(/ModuleNotFoundError: No module named '([^']+)'/)
    const req = path.join(installedRoot, 'requirements.txt')
    if (m) {
        const mod = m[1]
        const pkgs = {
            mss: 'mss', PIL: 'Pillow', psutil: 'psutil', numpy: 'numpy',
            win32api: 'pywin32', win32com: 'pywin32', pythoncom: 'pywin32', pywintypes: 'pywin32',
        }
        return '缺少 Python 依赖：' + mod + '（pip 包：' + (pkgs[mod] || mod) + '）\n'
            + '安装依赖：\n'
            + '  ' + python + ' -m pip install -r "' + req + '"'
    }
    const tail = raw.trim()
    return 'Python 本体启动失败。' + (tail ? '\n原始错误：' + (tail.length > 800 ? tail.slice(-800) : tail) : '')
}
const DUMP_TIMEOUT_MS = 180000
const CALL_TIMEOUT_MS = 180000
const TOOL_TIMEOUTS = { sequence: 900000, wait: 90000 }
const PNG_MEDIA = 'image/png'
/** ★UTF-8 强制：Windows 上 Python 默认按 GBK 编解码 stdio，协议流是 UTF-8（中文/箭头会炸）。 */
const PY_ENV = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }

/** ★附图显示真实光标（设置页开关）：默认开。开＝附图叠加系统光标图标并报鼠标位置；
 *  关＝两样都不出（应用自带光标时避免两个光标打架）。
 *  只写进 Python 的环境变量，工具面一个字不动 ⇒ 不破提示词缓存。 */
let CURSOR_SHOW = true
function syncCursorEnv() {
    PY_ENV.DSH_DESKTOP_CURSOR_SHOW = CURSOR_SHOW ? '1' : '0'
}

// ══════════════════════════════════════════════════════════════════════════════
// 【统一注入通道 · unified push channel】——**集中在文件开头，便于查阅**。
//
//   语义：**运行中 ⇒ 上下文注入**（agent/pre-step 尾部注入，不新开回合）
//        ｜**空闲 ⇒ `followup` 唤醒**｜**不在册 ⇒ 冷启动/排队兜底**。
//   本插件用途：**后台截图完成后自动注入上下文**——不再要求调用方 `vision_action="pull"` 取图。
//   两处必要适配：
//     ① 依赖件（sessionId/liveAgent/notice/queueContext/trackDriveMsg/wakeNonResident）用本插件最小实现；
//     ② `opts.blocks`（图片内容块）——只拼 text 时注入截图无法带 image 块。
// ══════════════════════════════════════════════════════════════════════════════
function createUnifiedPush(ctx) {
    const PLUGIN_SOURCE = { kind: 'plugin:dsh-desktop-control' }
    const PLUGIN_NOTICE_SOURCE = { kind: 'plugin:dsh-desktop-control#notice' }
    const pendingContext = new Map()
    const pendingNotice = new Map()
    const lastPushTime = new Map()
    const pendingWake = new Map()
    const teamWakePending = new Map()
    const teamWakeStats = { queued: 0, last: null }

    function sessionId(agent) {
        return agent?.session?.id || agent?.session?.header?.id || null
    }

    function liveAgent(sid) {
        try {
            return sid ? ctx.get('agents')?.get?.(sid) : null
        } catch {
            return null
        }
    }

    /** 面板条原语（有面板的实现走 SSE；本插件没有面板，空实现——`pushTo` 内部只在 notice 时调它）。 */
    function notice() {}

    /** 「驱动轮次」消息 id 记录（用于 pre-step 预约校验；本插件不需要，空实现）。 */
    function trackDriveMsg() {}

    /** 没有活体时的唤醒通道（完整实现会尝试 subagents/agentTeams；本插件只做上下文注入 ⇒ 恒 false）。 */
    async function wakeNonResident() {
        return false
    }

    function mkMsg(src, text, blocks) {
        const content = Array.isArray(blocks) && blocks.length
            ? blocks
            : [{ type: 'text', text: String(text ?? '') }]
        return createUserMessage({ content, source: { ...src } })
    }

    /** 上下文队列（多带一个 `blocks` 供图片注入用）。
     *  ★同 kind 的**图片类**注入只留最新一张（队列深度＝1：宁可丢老画面，
     *  不给模型看十几秒前的图）。 */
    function queueContext(sid, text, isNotice, kind, noDowngrade, blocks) {
        const map = isNotice ? pendingNotice : pendingContext
        const K = String(kind || '')
        const item = { t: text, blocks, kind: K, nd: noDowngrade === true }
        let arr = map.get(sid)
        if (arr && (K === 'frame' || K === 'watch' || K === 'peek')) {
            const kept = arr.filter((it) => it.kind !== K)
            arr.length = 0
            arr.push(...kept)
        }
        if (arr) {
            if (arr.length < 4) arr.push(item)
        } else {
            map.set(sid, [item])
        }
    }

    function drainContextFor(sid) {
        const arr = pendingContext.get(sid)
        if (!arr?.length) return null
        pendingContext.delete(sid)
        return arr
    }

    function drainNoticesFor(sid) {
        const arr = pendingNotice.get(sid)
        if (!arr?.length) return []
        pendingNotice.delete(sid)
        return arr
    }

    // —— 注入通道：`agent/pre-step` 尾部挂消息（{prepend:true} 最外层变换）——
    ctx.on('agent/pre-step', async (payload, next) => {
        const decision = await next()
        if (!decision || decision.kind !== 'enter') return decision
        const sid = sessionId(payload?.agent)
        if (!sid) return decision
        const out = []
        for (const it of (drainNoticesFor(sid) || [])) out.push(mkMsg(PLUGIN_NOTICE_SOURCE, it.t, it.blocks))
        for (const it of (drainContextFor(sid) || [])) out.push(mkMsg(PLUGIN_SOURCE, it.t, it.blocks))
        if (!out.length) return decision
        return { ...decision, messages: [...(decision.messages || []), ...out] }
    }, { prepend: true })

    // ────────────────────────── 以下为 pushTo 本体 ──────────────────────────
    function pushTo(agent, text, opts) {
        try {
            if (!text) return false
            const sid = sessionId(agent)
            if (!sid) return false
            lastPushTime.set(sid, Date.now()) // A1：任何推送都刷新限频时间
            const a = agent || liveAgent(sid)
            const msgSrc = opts?.notice === true ? PLUGIN_NOTICE_SOURCE : PLUGIN_SOURCE
            if (opts?.notice === true) {
                try { notice(sid, String(text).split('\n')[0].slice(0, 120), opts?.variant || 'info') } catch { }
            }
            if (opts?.hard === true) Promise.resolve().then(() => hardStopSession(sid)).catch(() => { })
            // ①【通道分工】：运行中 ⇒ 上下文注入；空闲/完全结束＝收件箱 followup
            if (opts?.forceContext === true || (a && a.status === 'running')) {
                if ((opts?.notice === true || opts?.immediate === true) && opts?.hard !== true) {
                    const ag = (a && typeof a.inject === 'function') ? a : liveAgent(sid)
                    if (ag && typeof ag.inject === 'function') {
                        try {
                            ag.inject(mkMsg(msgSrc, text, opts?.blocks))
                            teamWakeStats.last = { sid: String(sid), via: opts?.notice === true ? 'notice-inject' : 'immediate-inject' }
                            return true
                        } catch { /* 取不到就退回队列 */ }
                    }
                }
                queueContext(sid, text, opts?.notice === true, opts?.kind, opts?.noDowngrade, opts?.blocks)
                teamWakeStats.last = { sid: String(sid), via: opts?.forceContext === true ? 'context-forced' : 'context' }
                return true
            }
            // ② 例外：回合已关闭 ⇒ 官方唤醒 API（唯一会落成 user 消息的一条）
            if (a && typeof a.followup === 'function') {
                const msg = mkMsg(msgSrc, text, opts?.blocks)
                if (opts?.notice !== true) trackDriveMsg(sid, msg?.id)
                pendingWake.set(sid, Date.now())
                a.followup(msg)
                teamWakeStats.last = { sid: String(sid), via: 'followup' } // 空闲 ⇒ 唤醒
                return true
            }
            // ③ 没有活体：按 opts 意图补真正的唤醒通道；通知类/forceContext 绝不唤人，照旧走上下文队列
            if (opts?.notice !== true && opts?.inbox === true) {
                teamWakeStats.last = { sid: String(sid), via: 'waking' }
                const p = wakeNonResident(sid, text).then((ok) => {
                    if (!ok) {
                        queueContext(sid, text, false, undefined, opts?.noDowngrade, opts?.blocks)
                        teamWakeStats.queued += 1
                        teamWakeStats.last = { sid: String(sid), via: 'queued' }
                    }
                    return ok
                }).catch(() => {
                    try { queueContext(sid, text, false, undefined, opts?.noDowngrade, opts?.blocks) } catch { }
                    return false
                })
                teamWakePending.set(String(sid), p)
                return true
            }
            queueContext(sid, text, opts?.notice === true, opts?.kind, opts?.noDowngrade, opts?.blocks)
            teamWakeStats.queued += 1
            teamWakeStats.last = { sid: String(sid), via: 'queued' }
            return true
        } catch {
            return false
        }
    }

    /** 掐回合（按 `opts.hard` 调用；本插件不会传 hard，保留符号以免 pushTo 抛 ReferenceError）。 */
    function hardStopSession() { }

    return { pushTo, sessionId, liveAgent, stats: teamWakeStats }
}

/** 每个工具回给模型的规范值：{ text, images: [ImageAttachmentRef] } */
function renderResult(_args, value) {
    const blocks = []
    if (value && value.text) blocks.push({ type: 'text', text: String(value.text) })
    if (value && Array.isArray(value.images)) {
        for (const ref of value.images) blocks.push({ type: 'image', attachment: ref })
    }
    return blocks
}

/**
 * Python 的 inputSchema（标准 JSON Schema）→ defineTool 的 ParameterSchemaSpec。
 * 只做**保守映射**：认得出的标量/数组保留类型；认不出的降级成 `json`（Python 侧本来就会校验）。
 */
function propSpec(p) {
    const base = {}
    if (p && typeof p.description === 'string') base.description = p.description
    if (p && p.default !== undefined) base.default = p.default
    const t = p && p.type
    if (t === 'string' || t === 'number' || t === 'integer' || t === 'boolean') {
        return { ...base, type: t }
    }
    if (t === 'array') {
        const it = p.items
        if (it && (it.type === 'string' || it.type === 'number' || it.type === 'integer' || it.type === 'boolean')) {
            return { ...base, type: 'array', items: { type: it.type, ...(it.description ? { description: it.description } : {}) } }
        }
        return { ...base, type: 'array', items: { type: 'json' } }
    }
    return { ...base, type: 'json' }
}

function parametersOf(inputSchema) {
    const props = (inputSchema && inputSchema.properties) || {}
    const out = {}
    for (const [k, v] of Object.entries(props)) out[k] = propSpec(v)
    return out
}

/** 一行 JSON 进程（常驻；崩溃后下次调用自动重启）。 */
class PyWorker {
    constructor(python, workerFile) {
        this.python = python
        this.workerFile = workerFile || WORKER
        this.proc = null
        this.buf = ''
        this.seq = 0
        this.pending = new Map()
        this.onPush = null          // ★后台推送回调（本体 push 行 → 统一注入通道）
        this.debug = !!process.env.DSH_DESKTOP_DEBUG
    }

    _start() {
        if (this.proc) return
        syncCursorEnv()
        const p = spawn(this.python, [this.workerFile], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, env: PY_ENV })
        this.proc = p
        p.stdout.setEncoding('utf8')
        p.stdout.on('data', (chunk) => this._onData(chunk))
        p.stderr.on('data', (d) => {
            if (this.debug) process.stderr.write('[desktop-py] ' + String(d))
        })
        p.on('error', (e) => this._fail(new Error('python spawn 失败：' + e.message)))
        p.on('exit', (code) => this._fail(new Error('python worker 退出（code=' + code + '）')))
    }

    _fail(err) {
        this.proc = null
        this.buf = ''
        for (const [, p] of this.pending) p.reject(err)
        this.pending.clear()
    }

    _onData(chunk) {
        this.buf += chunk
        for (;;) {
            const i = this.buf.indexOf('\n')
            if (i < 0) break
            const line = this.buf.slice(0, i).trim()
            this.buf = this.buf.slice(i + 1)
            if (!line) continue
            let msg
            try { msg = JSON.parse(line) } catch { continue }
            // **后台推送行**（无 id）：本体后台截图完成后推过来，交给统一注入通道。
            if (msg && msg.push) {
                try { if (this.onPush) this.onPush(msg.push) } catch { /* 推送失败绝不影响协议 */ }
                continue
            }
            const slot = this.pending.get(msg.id)
            if (!slot) continue
            this.pending.delete(msg.id)
            clearTimeout(slot.timer)
            slot.resolve(msg)
        }
    }

    call(name, args, timeoutMs) {
        this._start()
        const proc = this.proc
        const id = ++this.seq
        const payload = JSON.stringify({ id, name, args: args || {} }) + '\n'
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                if (this.pending.delete(id)) reject(new Error(`${name} 超时（${timeoutMs}ms）`))
            }, timeoutMs)
            this.pending.set(id, { resolve, reject, timer })
            try {
                proc.stdin.write(payload)
            } catch (e) {
                this.pending.delete(id); clearTimeout(timer)
                reject(new Error('写 python stdin 失败：' + e.message))
            }
        })
    }

    ping() {
        // 轻量探活：dump 一次工具面（也顺便保证解释器/本体可导入）
        syncCursorEnv()
        execFileSync(this.python, [this.workerFile, '--dump-tools'], {
            encoding: 'utf8', timeout: DUMP_TIMEOUT_MS, windowsHide: true, env: PY_ENV,
        })
        return true
    }

    stop() {
        if (!this.proc) return
        try { this.proc.kill() } catch { /* ignore */ }
        this.proc = null
    }
}

function dumpTools(python, workerFile) {
    syncCursorEnv()
    const out = execFileSync(python, [workerFile || WORKER, '--dump-tools'], {
        encoding: 'utf8', timeout: DUMP_TIMEOUT_MS, windowsHide: true, env: PY_ENV,
    })
    const lines = String(out || '').split(/\r?\n/).filter((l) => l.trim())
    if (!lines.length) throw new Error('python --dump-tools 没有输出')
    const parsed = JSON.parse(lines[lines.length - 1])
    return Array.isArray(parsed.tools) ? parsed.tools : []
}

export const name = PLUGIN_ID
export const inject = ['tools', 'webServer']

export function apply(ctx, config) {
    const cfg = (config && typeof config === 'object') ? config : {}
    const python = cfg.pythonPath || process.env.DSH_DESKTOP_PYTHON || PY_DEFAULT
    const stateFile = cfg.stateFile || STATE_FILE
    const apiPath = cfg.apiPath || API_PATH
    let worker = new PyWorker(python, workerPath)

    // 状态文件只记录非默认值：没有文件、或 enabled !== true，一律按关闭处理。
    function readState() {
        try {
            const s = JSON.parse(fs.readFileSync(stateFile, 'utf8'))
            return (s && typeof s === 'object') ? s : {}
        } catch { return {} }
    }
    function writeState(s) {
        fs.mkdirSync(path.dirname(stateFile), { recursive: true })
        const tmp = stateFile + '.tmp'
        fs.writeFileSync(tmp, JSON.stringify(s, null, 2), 'utf8')
        fs.renameSync(tmp, stateFile)
    }
    function isEnabled() { return readState().enabled === true }
    // 附图显示真实光标：状态文件里只有显式 false 才算关（默认开）。
    function isCursorShow() { return readState().cursorShow !== false }
    CURSOR_SHOW = isCursorShow()     // 任何 Python 进程起来之前先同步进环境

    // 后台截图完成后自动注入上下文（不要求调用方 pull）：
    //   本体后台线程完成时推一行 `{"push":{kind,png_b64,meta}}`，这里走**统一注入通道** `pushTo`。
    //   选 `forceContext: true` ⇒ 无论会话在不在跑都**只注入上下文、绝不唤醒**（后台截图不该开新回合）。
    const push = createUnifiedPush(ctx)
    let pushTarget = null          // 最近一次发起「后台截图」的会话（peek / start）
    async function onPush(ev) {
        try {
            const agent = pushTarget
            if (!agent) return
            const png = String(ev?.png_b64 || '')
            const meta = ev?.meta || {}
            const head = String(ev?.text || (ev?.kind === 'watch'
                ? '【后台概览 · 自动注入】拼图 ' + (meta.canvas ? meta.canvas.join('×') : '') + '（' + (meta.frames || '?') + ' 帧）'
                : (ev?.kind === 'frame'
                    ? '【持续观察 · 自动注入】' + (meta.tile ? meta.tile.join('×') : '')
                    : (ev?.kind === 'expired'
                        ? '【持续观察 · 已到期】会话 ' + (ev?.id || '') + '（采集已停，要看新的请重新 start）'
                        : '【后台截图 · 自动注入】单拍 ' + (meta.tile ? meta.tile.join('×') : '')))))
            const blocks = [{ type: 'text', text: head }]
            const attachments = ctx.get('attachments')
            if (png && attachments) {
                try {
                    const ref = await attachments.saveImage({
                        data: Buffer.from(png, 'base64'), mediaType: PNG_MEDIA, name: 'desktop-bg-shot',
                    })
                    if (ref) blocks.push({ type: 'image', attachment: ref })
                } catch (e) {
                    blocks.push({ type: 'text', text: '[图片保存失败：' + e.message + ']' })
                }
            }
            push.pushTo(agent, head, { forceContext: true, blocks, kind: String(ev?.kind || '') })
        } catch { /* 注入失败不影响工具本身 */ }
    }
    /** ★推送回调必须挂在**每一个** PyWorker 实例上：`reloadFromDisk()` 会重建 worker
     *  （每次插件升级都会触发）——只在启动时挂一次的话，升级后后台推送会被静默丢掉，
     *  表现为「自动注入从此不再出现、pull 却一直正常」（实测定位）。 */
    function wireWorker(w) { w.onPush = onPush; return w }
    worker = wireWorker(worker)

    // 诊断工具（本体起不来时至少有一条可读的回执，而不是"插件静默消失"）
    const registerDiag = (err) => {
        try {
            ctx.effect(() => ctx.tools.register(defineTool({
                name: 'desktop_control_diag',
                description: '仅诊断：桌面控制插件未能加载 Python 本体（工具未注册）。调用它只回原因与修法。',
                parameters: {},
                output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: String(v && v.text || '') }] },
                isConcurrencySafe: () => true,
                timeoutMs: 10000,
                async execute() {
                    return { text: err }
                },
            })))
        } catch (e) {
            process.stderr.write('[desktop] 诊断工具也没注册上：' + e.message + '\n')
        }
    }

    // 工具面：启用后才注册；关闭时全部注销。工具定义第一次启用时向 Python 本体取。
    let toolDefs = null
    const disposers = []
    function toolNames() {
        return toolDefs ? toolDefs.map((t) => t.name) : []
    }
    function buildTool(t) {
        const toolName = t.name
        const params = parametersOf(t.inputSchema)
        const timeoutMs = TOOL_TIMEOUTS[toolName] || CALL_TIMEOUT_MS
        return defineTool({
                name: toolName,
                description: t.description || toolName,
                parameters: params,
                output: { schema: { type: 'json' }, render: renderResult },
                isConcurrencySafe: () => false,
                timeoutMs,
                async execute(args, exec) {
                    // ★记下"发起后台截图"的会话：推送回来时注入到它。
                    //   capture_screenshot 的 peek/start、以及 sequence 里可能用到的后台单拍（shot 步）都算。
                    if (exec && exec.agent) {
                        if (toolName === 'capture_screenshot') {
                            const va = String((args && args.vision_action) || '')
                            if (va === 'peek' || va === 'start') pushTarget = exec.agent
                        } else if (toolName === 'sequence') {
                            pushTarget = exec.agent   // 宏里的 shot 步/action=shot 也是后台单拍
                        }
                    }
                    // 开关开着时，只在两次工具调用之间核对一次「是不是装了新包」
                    checkForUpdate()
                    const res = await worker.call(toolName, args || {}, timeoutMs)
                    if (res.error) return { text: '⚠ ' + toolName + ' 失败：' + res.error, images: [] }
                    const content = Array.isArray(res.content) ? res.content : []
                    const texts = []
                    const images = []
                    const attachments = ctx.get('attachments')
                    for (const item of content) {
                        if (!item || typeof item !== 'object') continue
                        if (item.type === 'text') {
                            texts.push(String(item.text || ''))
                        } else if (item.type === 'image' && item.data) {
                            if (!attachments) { texts.push('[图片已省略：宿主没有 attachments 服务]'); continue }
                            try {
                                const data = Buffer.from(String(item.data), 'base64')
                                const ref = await attachments.saveImage({
                                    data, mediaType: item.mimeType || PNG_MEDIA, name: toolName,
                                })
                                if (ref) images.push(ref)
                            } catch (e) {
                                texts.push('[图片保存失败：' + e.message + ']')
                            }
                        }
                    }
                    const text = texts.join('\n')
                    if (res.isError) return { text: text || ('⚠ ' + toolName + ' 返回错误'), images }
                    return { text, images }
                },
        })
    }
    function registerTools() {
        if (disposers.length) return
        if (!toolDefs) toolDefs = dumpTools(python, workerPath)
        for (const t of toolDefs) {
            try {
                disposers.push(ctx.tools.register(buildTool(t)))
            } catch (e) {
                process.stderr.write('[desktop] 工具注册失败 ' + t.name + '：' + e.message + '\n')
            }
        }
    }
    function unregisterTools() {
        for (const d of disposers.splice(0)) {
            try { d() } catch { /* 已释放 */ }
        }
    }

    /** 读「当前磁盘上安装的版本」：pnpm 布局看 profile 软链；就地覆盖安装看自身目录。 */
    function installedInfo() {
        const tries = LINKED_ROOT ? [LINKED_ROOT, __dirname] : [__dirname]
        for (const p of tries) {
            try {
                const real = fs.realpathSync(p)
                const version = readVersion(real)
                if (version) return { version, root: real }
            } catch { /* 试下一个 */ }
        }
        return null
    }

    /** 清掉 Python 字节码缓存（__pycache__）：换版本后强制用新源码重新编译。 */
    function clearPyCache(root) {
        try {
            for (const name of fs.readdirSync(root)) {
                if (name !== '__pycache__') continue
                try { fs.rmSync(path.join(root, name), { recursive: true, force: true }) } catch { /* 占用就跳过 */ }
            }
        } catch { /* 目录不可读就跳过 */ }
    }

    /** 按磁盘上最新的安装重新加载：停旧本体 → 清 Python 字节码缓存 → 重新 dump 工具面 → 重新注册。
     *  触发点只有两个：① 拨开关「打开」（每次都重载）；② 开关开着时检测到**版本号变了**（工具调用前查一次）。
     *  关闭开关不做任何更新检查。失败时回退旧版本，并对同一版本做退避。 */
    let failedVersion = ''
    function reloadFromDisk() {
        const oldDefs = toolDefs
        const oldWorkerPath = workerPath
        const oldRoot = installedRoot
        const oldVersion = PLUGIN_VERSION
        unregisterTools()
        worker.stop()
        toolDefs = null
        let target = ''
        try {
            const info = installedInfo()
            if (info) {
                target = info.version
                workerPath = path.join(info.root, 'worker.py')
                installedRoot = info.root
                PLUGIN_VERSION = info.version
            } else {
                workerPath = WORKER
                installedRoot = __dirname
                PLUGIN_VERSION = readVersion(__dirname)
            }
            worker = wireWorker(new PyWorker(python, workerPath))
            clearPyCache(installedRoot)
            registerTools()
            failedVersion = ''
        } catch (e) {
            failedVersion = target
            toolDefs = oldDefs
            workerPath = oldWorkerPath
            installedRoot = oldRoot
            PLUGIN_VERSION = oldVersion
            worker = wireWorker(new PyWorker(python, workerPath))
            try { if (oldDefs && isEnabled()) registerTools() } catch { /* 保底 */ }
            throw e
        }
    }

    /** 开关开着时：只在**工具调用前**核对一次版本号，变了就在两次调用之间换掉。
     *  ★开发期改脚本/改 Python 但不升版本 ⇒ 不触发（不会反复重启重载破坏工具面）。
     *  不监听文件、不轮询、不在调用进行中换进程。 */
    function checkForUpdate() {
        if (!isEnabled()) return                 // 关闭状态不检查、不更新
        const info = installedInfo()
        if (!info || !info.version) return
        if (info.version === PLUGIN_VERSION) return
        if (info.version === failedVersion) return   // 这个版本装不起来，别反复重试
        try {
            reloadFromDisk()
        } catch (e) {
            process.stderr.write('[desktop] 自动重载失败：' + e.message + '\n')
        }
    }

    // 不做任何后台检查：没有文件监听、没有定时器。只有两个触发点——
    //   ① 拨开关「打开」：reloadFromDisk()；② 工具调用前：checkForUpdate() 核一次版本号。

    // 默认关闭：状态里没有 enabled=true 时，不启动 Python 本体，也不注册任何工具。
    try {
        if (isEnabled()) registerTools()
    } catch (e) {
        registerDiag(pythonHint(python, e))
    }

    ctx.effect(() => () => {
        try { unregisterTools() } catch { /* 忽略 */ }
        worker.stop()
    })

    // host API（设置页消费）：状态查询与启用/关闭。关闭即注销工具并按关闭落盘。
    ctx.effect(() => ctx.webServer.register({
        kind: 'prefix',
        path: apiPath,
        handler: async (req, res) => {
            const u = new URL(req.url, 'http://dsh')
            const send = (code, obj) => {
                res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' })
                res.end(JSON.stringify(obj))
            }
            const snapshot = () => {
                const disk = installedInfo()
                return {
                    ok: true,
                    enabled: isEnabled(),
                    cursorShow: isCursorShow(),
                    python,
                    version: PLUGIN_VERSION,
                    installedVersion: disk ? disk.version : PLUGIN_VERSION,
                    toolCount: isEnabled() ? toolNames().length : 0,
                    tools: isEnabled() ? toolNames() : [],
                }
            }
            try {
                if (u.pathname === apiPath + '/state' && (!req.method || req.method === 'GET')) {
                    send(200, snapshot())
                } else if (u.pathname === apiPath + '/enabled' && req.method === 'POST') {
                    let body = ''
                    for await (const chunk of req) body += chunk
                    const want = JSON.parse(body || '{}').enabled === true
                    const st = readState()
                    if (want) st.enabled = true
                    else delete st.enabled
                    writeState(st)                     // 先落盘：重载里 isEnabled() 才是新状态
                    if (want) {
                        // 「打开」＝每次都按磁盘上最新的安装重新加载
                        try {
                            reloadFromDisk()
                        } catch (e) {
                            unregisterTools(); worker.stop(); toolDefs = null
                            const back = readState(); delete back.enabled; writeState(back)
                            throw e
                        }
                    } else {
                        // 真关闭：只注销工具 + 停掉 Python 本体；不做任何更新检查
                        unregisterTools()
                        worker.stop()
                        toolDefs = null
                    }
                    send(200, snapshot())
                } else if (u.pathname === apiPath + '/cursor-show' && req.method === 'POST') {
                    // 只切一个环境开关：落盘 → 停掉 Python 本体（下次动作按新状态重起），工具面不动。
                    let body = ''
                    for await (const chunk of req) body += chunk
                    const on = JSON.parse(body || '{}').on !== false
                    const st = readState()
                    if (on) delete st.cursorShow
                    else st.cursorShow = false
                    writeState(st)
                    CURSOR_SHOW = isCursorShow()
                    syncCursorEnv()
                    worker.stop()
                    send(200, snapshot())
                } else {
                    send(404, { ok: false, error: 'not found: ' + u.pathname })
                }
            } catch (e) {
                send(400, { ok: false, error: pythonHint(python, e) })
            }
        },
    }), 'dsh-desktop-control: settings api')
}
