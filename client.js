/**
 * dsh-desktop-control · 客户端设置页
 * 手写 ModuleLoader 格式：注册为「设置」里的一个 section。
 * 页面只做两件事：显示当前状态；切换桌面控制的开启/关闭。
 */
window.__ModuleLoader__.load({
  id: 'dsh-desktop-control',
  factory: (require) => {
    const React = require('react')
    const API = '/desktop-control/api'

    const styles = [
      '.dc-page{font-family:ui-monospace,monospace;font-size:12px;line-height:1.6;padding:14px 16px;max-width:860px;color:#222}',
      '.dc-page h3{margin:0 0 6px;font-size:13px}',
      '.dc-hint{color:#666;font-size:11px;margin:0 0 12px}',
      '.dc-card{background:#fff;border:1px solid #ccc;border-radius:8px;padding:10px 12px;margin-bottom:10px}',
      '.dc-row{display:flex;align-items:center;gap:12px}',
      '.dc-label-col{flex:1;min-width:0}',
      '.dc-label{font-weight:600;color:#222}',
      '.dc-sub{font-size:11px;color:#666}',
      '.dc-switch{position:relative;display:inline-block;width:44px;height:24px;flex:none;cursor:pointer}',
      '.dc-input{opacity:0;width:0;height:0;position:absolute}',
      '.dc-slider{position:absolute;inset:0;background:#bbb;border-radius:12px;transition:background .15s}',
      '.dc-slider:before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:transform .15s}',
      '.dc-input:checked + .dc-slider{background:var(--theme-accent,#4a9eff)}',
      '.dc-input:checked + .dc-slider:before{transform:translateX(20px)}',
      '.dc-info{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 10px}',
      '.dc-status{font-weight:600;padding:1px 8px;border-radius:10px;font-size:11px}',
      '.dc-status.on{background:rgba(46,204,113,.15);color:#1a8a4a}',
      '.dc-status.off{background:rgba(255,193,7,.12);color:#9a6b00}',
      '.dc-status.err{background:rgba(221,51,51,.1);color:#a11}',
      '.dc-meta{font-size:11px;color:#666}',
      '.dc-note{font-size:11px;color:#8a5a00;background:rgba(255,193,7,.08);border-left:3px solid #d9a300;padding:6px 10px;border-radius:6px;margin:0}',
      '.dc-error{display:none;white-space:pre-wrap;word-break:break-word;background:rgba(221,51,51,.06);border:1px solid rgba(221,51,51,.35);border-left:3px solid #d33;border-radius:6px;padding:8px 10px;margin:0 0 10px;color:#a11;font-size:11px;line-height:1.5}',
    ].join('')

    function el(tag, cls, text) {
      const e = document.createElement(tag)
      if (cls) e.className = cls
      if (text !== void 0) e.textContent = text
      return e
    }

    function fetchJson(path, init) {
      return fetch(API + path, {
        headers: { 'content-type': 'application/json' },
        ...(init || {}),
      }).then((r) => r.json())
    }

    /** 原生 DOM 构建页面；返回卸载清理函数。 */
    function buildPage(container) {
      const style = document.createElement('style')
      style.textContent = styles

      const page = el('div', 'dc-page')
      const h = el('h3', null, '桌面控制')
      const hint = el('p', 'dc-hint',
        '默认关闭。打开＝按磁盘上最新的安装加载工具，每次打开都会重新加载一次；'
        + '关闭＝注销工具并停掉后台 Python 本体。开着开关时装了新包，下一次动作前自动换上，不用重启应用。'
        + '首次开启前请先安装 Python 依赖：python -m pip install -r requirements.txt（mss、Pillow、pywin32、psutil、numpy）。')

      const card = el('div', 'dc-card')
      const row = el('div', 'dc-row')
      const labelCol = el('div', 'dc-label-col')
      const label = el('div', 'dc-label', '启用桌面控制')
      const sub = el('div', 'dc-sub', '关闭＝注销工具并停掉 Python 本体；打开＝按最新安装重新加载')
      labelCol.append(label, sub)

      const wrap = el('label', 'dc-switch')
      const input = document.createElement('input')
      input.type = 'checkbox'
      input.className = 'dc-input'
      const slider = el('span', 'dc-slider')
      wrap.append(input, slider)
      row.append(labelCol, wrap)
      card.append(row)

      const card2 = el('div', 'dc-card')
      const row2 = el('div', 'dc-row')
      const labelCol2 = el('div', 'dc-label-col')
      const label2 = el('div', 'dc-label', '附图显示真实光标')
      const sub2 = el('div', 'dc-sub', '关闭＝附图不画系统光标、也不报鼠标位置；应用自带光标的时候推荐关闭')
      labelCol2.append(label2, sub2)
      const wrap2 = el('label', 'dc-switch')
      const input2 = document.createElement('input')
      input2.type = 'checkbox'
      input2.className = 'dc-input'
      const slider2 = el('span', 'dc-slider')
      wrap2.append(input2, slider2)
      row2.append(labelCol2, wrap2)
      card2.append(row2)

      const info = el('div', 'dc-info')
      const status = el('div', 'dc-status off', '已关闭')
      const meta = el('div', 'dc-meta', '')
      info.append(status, meta)

      const errBox = el('pre', 'dc-error', '')

      const note = el('p', 'dc-note',
        '提示：开启后 AI 可以通过截图与模拟键鼠操作本机桌面。仅在你信任的会话里使用，用完建议关闭。')

      page.append(style, h, hint, card, card2, info, errBox, note)

      let busy = false
      let busy2 = false
      function render(s) {
        errBox.textContent = ''
        errBox.style.display = 'none'
        input.checked = s.enabled === true
        input2.checked = s.cursorShow !== false
        status.textContent = s.enabled ? '已开启' : '已关闭'
        status.className = 'dc-status ' + (s.enabled ? 'on' : 'off')
        meta.textContent = '插件 ' + (s.version || '?')
          + ' ｜ 解释器 ' + (s.python || '?')
          + ' ｜ 工具 ' + (s.toolCount || 0) + ' 个'
      }
      function refresh() {
        return fetchJson('/state').then(render).catch((e) => {
          status.textContent = '读取失败'
          status.className = 'dc-status err'
          errBox.textContent = String(e.message || e)
          errBox.style.display = 'block'
        })
      }

      input.addEventListener('change', () => {
        if (busy) return
        busy = true
        const want = input.checked
        fetchJson('/enabled', { method: 'POST', body: JSON.stringify({ enabled: want }) })
          .then((s) => {
            if (s.ok === false) {
              status.textContent = '切换失败'
              status.className = 'dc-status err'
              errBox.textContent = String(s.error || '未知错误')
              errBox.style.display = 'block'
              input.checked = !want
              return
            }
            render(s)
          })
          .catch((e) => {
            status.textContent = '切换失败'
            status.className = 'dc-status err'
            errBox.textContent = String(e.message || e)
            errBox.style.display = 'block'
            input.checked = !want
          })
          .finally(() => { busy = false })
      })

      input2.addEventListener('change', () => {
        if (busy2) return
        busy2 = true
        const want = input2.checked
        fetchJson('/cursor-show', { method: 'POST', body: JSON.stringify({ on: want }) })
          .then((s) => {
            if (s.ok === false) {
              errBox.textContent = String(s.error || '未知错误')
              errBox.style.display = 'block'
              input2.checked = !want
              return
            }
            render(s)
          })
          .catch((e) => {
            errBox.textContent = String(e.message || e)
            errBox.style.display = 'block'
            input2.checked = !want
          })
          .finally(() => { busy2 = false })
      })

      refresh()
      const timer = window.setInterval(refresh, 10000)
      container.appendChild(page)
      return function () {
        window.clearInterval(timer)
        container.textContent = ''
      }
    }

    function DesktopControlView() {
      const ref = React.useRef(null)
      React.useEffect(() => {
        if (!ref.current) return
        return buildPage(ref.current)
      }, [])
      return React.createElement('div', { ref, style: { width: '100%' } })
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.effect(() => {
          ctx.slots.inject('settings.section', () => ctx.slots.register({
            name: 'settings.section',
            id: 'desktop-control',
            order: 66,
            label: () => '桌面控制',
          }, DesktopControlView))
        }, 'desktop-control: settings page')
      },
    }
  },
})
