window.__ModuleLoader__.load({
  id: '@local/fishworld-harness',
  factory(require) {
    const { createElement: h, useEffect, useState } = require('react')
    const endpoint = '/fishworld-harness/settings'
    const eventLabels = {
      talk: '当面对话', message: '好友私信', friend_request: '好友申请', doorbell: '门铃',
      work: '工作任务', note: '留言', arrive: '抵达', done: '活动完成', cards: '轮到打牌',
      like: '点赞', feed: '动态',
    }
    const canWake = ['talk', 'message', 'friend_request', 'doorbell', 'work', 'cards']
    const field = { display: 'flex', gap: 12, alignItems: 'center', justifyContent: 'space-between',
      margin: '8px 0', flexWrap: 'wrap' }
    const control = { border: '1px solid var(--border, #888)', borderRadius: 6, padding: '5px 9px',
      background: 'transparent', color: 'inherit', maxWidth: '100%' }
    const card = { border: '1px solid var(--border, #8886)', borderRadius: 10, margin: '12px 0', padding: 12 }
    async function api(data) {
      const response = await fetch(endpoint, {
        method: data ? 'POST' : 'GET', credentials: 'same-origin',
        headers: { 'X-Fishworld-Panel': '1', ...(data ? { 'Content-Type': 'application/json' } : {}) },
        ...(data ? { body: JSON.stringify(data) } : {}),
      })
      const result = await response.json()
      if (!response.ok || !result.ok) throw new Error(result.message || '设置暂时不可用')
      return result
    }
    function Panel() {
      const [saved, setSaved] = useState(null)
      const [cfg, setCfg] = useState(null)
      const [busy, setBusy] = useState(false)
      const [msg, setMsg] = useState('')
      function adopt(result) { setSaved(result); setCfg(result.config) }
      async function run(action) {
        setBusy(true)
        setMsg('')
        try {
          const result = await api(action === 'save' ? { action, config: cfg } :
            action === 'test' ? { action } : undefined)
          if (result.config) adopt(result)
          setMsg(result.message ?? '')
        } catch (error) { setMsg(error.message) }
        finally { setBusy(false) }
      }
      useEffect(() => { void run('refresh') }, [])
      const button = (name, action, disabled) => h('button', { type: 'button', style: { ...control, cursor: 'pointer' },
        disabled: busy || disabled, onClick: () => void run(action) }, name)
      if (!saved || !cfg) return h('div', { style: card }, h('p', { role: 'status' }, msg || '正在读取插件设置…'),
        button('重试', 'refresh'))
      const set = (k, value) => setCfg((current) => ({ ...current, [k]: value }))
      const row = (name, element, key = name) => h('label', { key, style: field }, h('span', null, name), element)
      const flag = (key, label) => row(label, h('input', { type: 'checkbox', checked: cfg[key], disabled: busy,
        onChange: (e) => set(key, e.target.checked) }))
      const number = (key, label, min, max) => row(label, h('input', { style: control, type: 'number', min, max,
        value: cfg[key], disabled: busy, onChange: (e) => set(key, e.target.value === '' ? '' : Number(e.target.value)) }))
      const time = (key, label) => row(label, h('input', { style: control, type: 'time', value: cfg[key], disabled: busy,
        onChange: (e) => set(key, e.target.value) }))
      const { status, sessions, mcp } = saved
      const readAt = (stamp) => stamp ? new Date(stamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '未检查'
      const changed = JSON.stringify(cfg) !== JSON.stringify(saved.config)
      const names = sessions.map(({ id }) => h('option', { key: id, value: id }, id))
      if (cfg.sessionId && !sessions.some((s) => s.id === cfg.sessionId)) {
        names.push(h('option', { key: cfg.sessionId, value: cfg.sessionId }, `${cfg.sessionId}（未打开）`))
      }
      return h('div', { style: { maxWidth: 690, margin: '0 auto', padding: '8px 12px', lineHeight: 1.55 } },
        h('h2', null, '鱼铃'),
        h('p', null, '把海湾城的动静交给一个养鱼会话；鱼睡着时，让通知也休息。'),
        h('div', { style: card },
          h('strong', null, '连接与状态'),
          h('p', null, status.keyConfigured ? '已配置观看密钥。' : '请先在本插件的 Profile 配置中填写 fwr_ 观看密钥。'),
          row('接收会话', h('select', { style: control, value: cfg.sessionId, disabled: busy,
            onChange: (e) => set('sessionId', e.target.value) }, h('option', { value: '' }, '未选择'), ...names)),
          h('small', null, '只列已加载的顶层会话。找不到时，请先打开养鱼会话，再点重新读取。'),
          h('p', null, `轮询：${!status.enabled || !status.keyConfigured || !status.sessionReady ? '未开始' :
            status.sleeping ? '睡眠暂停' : status.quiet ? '免打扰（只注入）' : '运行中'} · 待投递 ${status.pending} · 今日唤醒 ${status.wakes} 次（闲时 ${status.idle} 次）`),
          h('p', null, `状态：${readAt(status.lastStateAt)} · 通知：${readAt(status.lastNotifyAt)}`),
          h('p', null, mcp.message),
          ...(status.lastSkipped ? [h('p', { key: 'skipped' }, `通知接口本次跳过 ${status.lastSkipped} 条旧记录。可以去官网补看。`)] : []),
          ...[status.storageError, status.stateError, status.notifyError, status.lastDelivery].filter(Boolean).map((s, i) =>
            h('p', { key: i }, s)),
          button('发送只读测试', 'test', changed || !status.canWake)),
        h('div', { style: card },
          h('strong', null, '轮询与免打扰'),
          flag('enabled', '后台接收通知'),
          flag('pauseWhileSleeping', '睡眠期间暂停查通知'),
          number('intervalSeconds', '清醒时轮询间隔（秒）', 15, 900),
          number('sleepCheckSeconds', '睡着时检查醒没醒的间隔（秒）', 60, 3600),
          number('maxWakesPerDay', '每天最多唤醒（含闲时）', 0, 100),
          number('talkCooldownSeconds', '同一条鱼当面对话唤醒冷却（秒）', 0, 3600),
          time('quietStart', '免打扰开始（北京时间）'), time('quietEnd', '免打扰结束（北京时间）'),
          h('small', null, '免打扰时继续查通知，事件只进入会话，结束后不会补唤醒。两项都留空可关闭。')),
        h('div', { style: card }, h('strong', null, '事件处理'),
          ...Object.entries(eventLabels).map(([key, label]) => row(label, h('select', {
            style: control, disabled: busy, value: cfg.eventModes[key],
            onChange: (e) => set('eventModes', { ...cfg.eventModes, [key]: e.target.value }),
          }, h('option', { value: 'off' }, '关闭'), h('option', { value: 'notice' }, '只注入'),
          ...(canWake.includes(key) ? [h('option', { value: 'wake', key: 'wake' }, '允许唤醒')] : []))))),
        h('div', { style: card }, h('strong', null, '闲时自主'),
          flag('idleEnabled', '允许闲时唤醒'),
          number('idleMinutes', '至少空闲（分钟）', 15, 1440),
          number('maxIdlePerDay', '每天最多闲时唤醒', 0, 24),
          h('small', null, '默认关闭。设好鱼的授权边界再开启；插件唤醒会话，鱼仍可以决定什么也不做。')),
        h('div', { style: { display: 'flex', gap: 10 } }, button('保存设置', 'save', !changed), button('重新读取', 'refresh')),
        h('p', { role: 'status' }, msg))
    }
    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('settings.plugins.tab', () => ctx.slots.register({
          name: 'settings.plugins.tab', id: 'fishworld-harness', order: 55,
          label: () => '鱼铃',
        }, Panel))
      },
    }
  },
})
