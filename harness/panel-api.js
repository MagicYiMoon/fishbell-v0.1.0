import { DEFAULTS, normalize } from './poller.js'

export const ROUTE = '/fishworld-harness/settings'
const loopback = (address) => ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address)
function allowed(req) {
  if (!loopback(req.socket.remoteAddress) || !loopback(req.socket.localAddress) ||
      req.headers['x-fishworld-panel'] !== '1' ||
      ['forwarded', 'x-forwarded-for', 'x-forwarded-host'].some((h) => h in req.headers)) return false
  const host = req.headers.host ?? ''
  if (!/^(?:127\.0\.0\.1|localhost|\[::1\])(?::\d{1,5})?$/i.test(host)) return false
  const origin = `${req.socket.encrypted ? 'https' : 'http'}://${host}`
  if (req.headers['sec-fetch-site'] && req.headers['sec-fetch-site'] !== 'same-origin') return false
  if (req.headers.origin && req.headers.origin !== origin) return false
  return req.method !== 'POST' || req.headers.origin === origin
}
function json(res, code, data) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff', 'Cross-Origin-Resource-Policy': 'same-origin' })
  res.end(JSON.stringify(data))
}
async function parse(req) {
  if (!/^application\/json(?:;|$)/i.test(req.headers['content-type'] ?? '')) throw new Error('需要 JSON 请求')
  let len = 0
  const data = []
  for await (const chunk of req.iterator({ destroyOnReturn: false })) {
    len += chunk.length
    if (len > 12_288) { req.resume(); throw new Error('请求过大') }
    data.push(chunk)
  }
  return JSON.parse(Buffer.concat(data).toString('utf8'))
}

export function panelRoute({ poller, roots, diagnostic, persist, rejection }) {
  let saving = false
  const view = () => ({
    config: poller.config, status: poller.status(), mcp: diagnostic(),
    sessions: roots().map((a) => ({ id: a.id })),
  })
  async function serve(req, res) {
    if (!allowed(req)) return json(res, 403, { ok: false, message: '请在本机 Harness 界面内打开设置' })
    const denied = rejection(req)
    if (denied) return json(res, denied, { ok: false, message: '请从已登录的 Harness 界面打开设置' })
    if (req.method === 'GET') return json(res, 200, { ok: true, ...view() })
    if (req.method !== 'POST') return json(res, 405, { ok: false, message: '不支持此请求' })
    let body
    try { body = await parse(req) } catch { return json(res, 400, { ok: false, message: '设置请求格式无效' }) }
    if (body?.action === 'test') {
      const result = await poller.test()
      return json(res, result.ok ? 200 : 409, result)
    }
    if (body?.action !== 'save' || !body.config || typeof body.config !== 'object' || Array.isArray(body.config)) {
      return json(res, 400, { ok: false, message: '设置格式无效' })
    }
    if (saving) return json(res, 409, { ok: false, message: '另一个保存正在进行，请稍后再试' })
    let cfg
    try {
      if (Object.keys(body.config).some((k) => !Object.hasOwn(DEFAULTS, k) && k !== 'eventModes')) {
        return json(res, 400, { ok: false, message: '设置包含不支持的字段' })
      }
      cfg = normalize({ ...poller.config, ...body.config })
    } catch (err) { return json(res, 400, { ok: false, message: err.message }) }
    saving = true
    try {
      let persisted = false
      try { await persist(cfg); persisted = true } catch { /* still apply to this runtime */ }
      poller.update(cfg)
      return json(res, 200, { ok: true, persisted,
        message: persisted ? '已保存到当前配置。' : '已在本次运行生效；未能写入配置，重启后可能恢复原设置。', ...view() })
    } finally { saving = false }
  }
  return { kind: 'exact', path: ROUTE, handler(req, res) {
    return serve(req, res).catch(() => {
      if (!res.headersSent) json(res, 500, { ok: false, message: '设置暂时不可用' })
      else if (!res.writableEnded) res.end()
    })
  } }
}
