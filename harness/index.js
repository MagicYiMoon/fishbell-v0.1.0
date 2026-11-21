import Schema from '@deepseek-ai/schemastery'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { DEFAULTS, EVENTS, Poller, defaultMode } from './poller.js'
import { panelRoute } from './panel-api.js'

export const name = 'fishworld-harness'
export const inject = ['agents', 'tools']
const number = (key, min, max) => Schema.number().min(min).max(max).default(DEFAULTS[key])
export const Config = Schema.object({
  key: Schema.string().default('').description('fwr_ 观看密钥；不要用 MCP 操作密钥。'),
  stateDirectory: Schema.string().default('').description('可选缓存目录；留空时使用当前 DSH_HOME 下的 fishworld-harness 目录。'),
  sessionId: Schema.string().default('').description('唯一目标会话 ID；也可在插件设置页选择。'),
  enabled: Schema.boolean().default(true),
  pauseWhileSleeping: Schema.boolean().default(true),
  intervalSeconds: number('intervalSeconds', 15, 900),
  sleepCheckSeconds: number('sleepCheckSeconds', 60, 3600),
  wakeTime: Schema.string().default('').description('定时起床，北京时间 HH:MM；留空关闭。'),
  maxWakesPerDay: number('maxWakesPerDay', 0, 100),
  talkCooldownSeconds: number('talkCooldownSeconds', 0, 3600),
  quietStart: Schema.string().default(''),
  quietEnd: Schema.string().default(''),
  idleEnabled: Schema.boolean().default(false),
  idleMinutes: number('idleMinutes', 15, 1440),
  maxIdlePerDay: number('maxIdlePerDay', 0, 24),
  idleStyle: Schema.string().default(DEFAULTS.idleStyle),
  socialMode: Schema.string().default(DEFAULTS.socialMode),
  socialAction: Schema.string().default(DEFAULTS.socialAction),
  socialIntervalMinutes: number('socialIntervalMinutes', 30, 1440),
  maxSocialPerDay: number('maxSocialPerDay', 0, 12),
  socialPreferences: Schema.string().default(''),
  eventModes: Schema.object(Object.fromEntries(Object.keys(EVENTS).map((k) =>
    [k, Schema.string().default(defaultMode(k))]))).default({}),
})

export function mcpDiagnostic(tools, agent) {
  if (!agent) return { state: 'no_session', message: '请先打开并选择养鱼会话' }
  if (typeof tools.schemas !== 'function') return { state: 'unknown', message: '当前宿主未提供工具清单；无法确认已挂载' }
  try {
    const names = tools.schemas(agent).map((x) => x.name).filter((n) =>
      typeof n === 'string' && (n === 'world_now' || /^mcp__[^\s]+__world_now$/.test(n)))
    return names.length === 1
      ? { state: 'visible', message: '会话能看到 world_now；实际连通及是不是同一条鱼请做只读测试', tools: names }
      : names.length > 1
        ? { state: 'multiple', message: '发现多个 world_now，请在这个会话仅保留对应鱼的连接', tools: names }
        : { state: 'missing', message: '目标会话看不到 world_now，请检查 MCP 是否启用、连接是否正常' }
  } catch { return { state: 'unknown', message: '读取目标会话的可见工具失败' } }
}

function matchingToolsReady(tools, agent, required) {
  try {
    const names = tools.schemas(agent).map(({ name }) => name).filter((name) => typeof name === 'string')
    const now = names.filter((name) => name === 'world_now' || /^mcp__[^\s]+__world_now$/.test(name))
    return now.length === 1 && required.every((name) => {
      const group = names.filter((tool) => tool === name || tool.startsWith('mcp__') && tool.endsWith('__' + name))
      return group.length === 1 && group[0] === now[0].replace(/world_now$/, name)
    })
  } catch { return false }
}

export function apply(ctx, config) {
  const roots = () => {
    try { return ctx.agents.roots().filter((a) => typeof a?.id === 'string' && a.id) }
    catch { return [] }
  }
  const fallback = join(process.env.DSH_HOME?.trim() || join(homedir(), '.dsh'), 'fishworld-harness')
  const poller = new Poller({ ...config, stateDirectory: config.stateDirectory || fallback }, {
    roots, mcpReady: (agent) => mcpDiagnostic(ctx.tools, agent).state === 'visible',
    alarmReady: (agent) => matchingToolsReady(ctx.tools, agent, ['world_do']),
    socialReady: (agent, mode) => matchingToolsReady(ctx.tools, agent,
      mode === 'friends' ? ['world_friends', 'world_say'] : ['world_say']),
  })
  const diagnostic = () => mcpDiagnostic(ctx.tools, poller.target())
  function tool(key, description, execute) {
    ctx.effect(() => ctx.tools.register({
      name: `fishworld_harness_${key}`,
      description,
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      output: { schema: { type: 'object' }, render: (_args, value) =>
        [{ type: 'text', text: JSON.stringify(value, null, 2) }] },
      execute(_args, exec) {
        if (!poller.target() || exec?.agent !== poller.target()) {
          return { available: false, message: '请在该插件选中的养鱼会话中使用' }
        }
        return execute()
      },
    }))
  }
  tool('status', '查看本会话的鱼世界轮询、睡眠暂停、通知队列和错误。不发请求。', () => poller.status())
  tool('fish', '读本会话最近一次观看状态缓存（位置、活动和睡眠）。不发请求。', () => poller.currentFish())
  tool('mcp', '检查本会话能否看到鱼世界 world_now，不调用 MCP，不修改配置。', diagnostic)
  const tick = () => void poller.tick().catch(() => { poller.timing.delivery = '后台检查异常，请查看配置和目标会话' })
  ctx.effect(() => {
    const id = setInterval(tick, 2000)
    id.unref?.()
    return () => { clearInterval(id); poller.stop() }
  })
  ctx.inject(['webServer'], (scope) => {
    const handler = panelRoute({
      poller, roots, diagnostic,
      rejection(req) {
        const connection = scope.get('connection')
        return typeof connection?.requestRejection === 'function' ? connection.requestRejection(req) : 403
      },
      async persist(cfg) {
        const editor = scope.get('configEditor')
        const entry = ctx.fiber?.entry
        if (!entry || typeof editor?.edit !== 'function') throw new Error('配置不可保存')
        await editor.edit(entry, (current) => ({ ...current, ...cfg }))
      },
    })
    scope.effect(() => scope.webServer.register(handler))
  })
}
