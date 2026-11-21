import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { normalize, Poller, sleepSignal } from '../poller.js'

function fixture(overrides = {}, disk, initial) {
  let at = Date.parse('2026-11-20T04:00:00Z')
  const calls = [], deliveries = []
  let saved = initial
  const store = disk ?? { load: () => saved ?? new Poller({}, { now: () => at }).state,
    save: (next) => { saved = structuredClone(next) } }
  const config = { key: 'fwr_unit_test', sessionId: 'fish', ...overrides }
  const f = { calls, deliveries, store, state: { fish: '鱼#1', figure: { asleep: false }, where: { kind: 'place' } },
    notify: { cursor: 'base', items: [] }, ready: true, alarmReady: true, step(n) { at += n * 1000 }, time: () => at }
  f.target = { id: 'fish', status: 'idle', followup: (p) => deliveries.push(['wake', p]),
    inject: (p) => deliveries.push(['notice', p]) }
  f.roots = [f.target, { id: 'another', followup() { assert.fail('delivered to another conversation') } }]
  f.open = () => new Poller(config, {
    store, roots: () => f.roots, now: f.time, mcpReady: () => f.ready, alarmReady: () => f.alarmReady,
    socialReady: () => f.socialReady !== false,
    request(path, key, { cursor }) {
      calls.push([path, cursor]); assert.equal(key, config.key)
      const result = f[path]
      if (result instanceof Error) throw result
      return result
    },
  })
  f.poller = f.open()
  return f
}

test('sleep suspends only the notify path, keeps the cursor across restart and resumes on an explicit wake', async () => {
  const f = fixture()
  await f.poller.tick()
  f.step(30)
  f.state.figure.asleep = true
  await f.poller.tick()
  assert.equal(f.poller.status().sleeping, true)
  assert.deepEqual(f.calls, [['state', ''], ['notify', ''], ['state', '']])
  f.poller.stop()
  f.poller = f.open()
  f.state = new Error('network')
  await f.poller.tick()
  assert.equal(f.poller.status().sleeping, true)
  f.step(60); await f.poller.tick()
  assert.equal(f.calls.length, 4)
  f.step(180)
  f.state = { fish: '鱼#1', figure: { pose: 'doze' } }
  await f.poller.tick()
  assert.equal(f.poller.status().sleeping, true)
  f.step(120)
  f.state.figure.asleep = false
  f.notify = { cursor: 'new', items: [{ kind: 'message', t: f.time(), text: '不应进入提示' }], skipped: 7 }
  await f.poller.tick()
  assert.deepEqual(f.calls.at(-1), ['notify', 'base'])
  assert.equal(f.poller.state.cursor, 'new')
  assert.equal(f.poller.status().lastSkipped, 7)
  assert.equal(f.deliveries[0][0], 'wake')
  assert.doesNotMatch(JSON.stringify(f.deliveries), /不应进入提示/)
})

test('self-directed idle replaces guided outreach and an uncertain turn is not replayed', async () => {
  const f = fixture({ idleEnabled: true, idleMinutes: 15, idleStyle: 'self', socialMode: 'friends', maxIdlePerDay: 1 })
  const attempts = []
  f.target.followup = (p) => {
    attempts.push({ prompt: p.content[0].text, reserved: f.store.load().idle })
    throw new Error('delivery may already have happened')
  }
  await f.poller.tick()
  f.step(900); await f.poller.tick()
  assert.equal(attempts.length, 1)
  assert.match(attempts[0].prompt, /由鱼做主.*#1/)
  assert.match(attempts[0].prompt, /什么都不做/)
  assert.doesNotMatch(attempts[0].prompt, /主人启用的闲时社交/)
  assert.equal(attempts[0].reserved, 1)
  assert.match(f.poller.status().lastDelivery, /不重试/)
  assert.equal(f.store.load().social, 0)
  f.poller.stop(); f.poller = f.open()
  f.step(900); await f.poller.tick()
  assert.equal(attempts.length, 1)
})

test('social uses idle and daily quotas and does not replay a failed or uncertain attempt after restart', async () => {
  const f = fixture({ idleEnabled: true, idleMinutes: 15, maxIdlePerDay: 12,
    socialMode: 'friends', socialAction: 'greet', socialPreferences: '逛公园',
    socialIntervalMinutes: 30, maxSocialPerDay: 1 })
  await f.poller.tick()
  f.step(900)
  let submitted = 0
  f.target.followup = (payload) => {
    submitted++
    const msg = payload.content[0].text
    assert.match(msg, /确认的已有好友/)
    assert.match(msg, /逛公园/)
    assert.doesNotMatch(msg, /免费的结伴邀请/)
    assert.equal(f.store.load().social, 1)
    throw new Error('unknown result')
  }
  await f.poller.tick()
  assert.equal(submitted, 1)
  assert.equal(f.poller.status().social, 1)
  assert.match(f.poller.status().lastDelivery, /投递结果未知/)
  f.poller.stop()
  f.poller = f.open()
  f.target.followup = (payload) => f.deliveries.push(payload.content[0].text)
  f.step(900); await f.poller.tick()
  f.step(1800); await f.poller.tick()
  assert.equal(f.deliveries.length, 2)
  assert.ok(f.deliveries.every((m) => m.includes('不主动发起新聊天')))
  assert.equal(f.poller.status().social, 1)
})

test('social is suppressed while asleep, with missing tools, or during quiet hours', async () => {
  const f = fixture({ idleEnabled: true, idleMinutes: 15, socialMode: 'nearby', quietStart: '12:30', quietEnd: '13:00' })
  await f.poller.tick()
  f.step(900); f.socialReady = false; await f.poller.tick()
  assert.match(f.deliveries.at(-1)[1].content[0].text, /不主动发起新聊天/)
  f.socialReady = true
  f.step(900); await f.poller.tick()
  assert.equal(f.deliveries.length, 1)
  f.step(1800); f.state.figure.asleep = true; await f.poller.tick()
  assert.equal(f.deliveries.length, 1)
  assert.equal(f.poller.status().social, 0)
  f.step(120); f.state.figure.asleep = false; await f.poller.tick()
  f.step(900); await f.poller.tick()
  assert.match(f.deliveries.at(-1)[1].content[0].text, /当前周围的鱼/)
  assert.equal(f.poller.status().social, 1)
})

test('durable queue survives delivery failure, retries once and deduplicates a repeated event', async (t) => {
  const path = mkdtempSync(join(tmpdir(), 'fishworld-harness-test-'))
  t.after(() => rmSync(path, { recursive: true, force: true }))
  let state = fixture()
  const original = state
  const key = 'fwr_unit_test'
  state.poller = new Poller({ key, stateDirectory: path, sessionId: 'fish' }, {
    now: state.time, roots: () => state.roots, mcpReady: () => true, request: (part) => state[part],
  })
  await state.poller.tick()
  state.step(30)
  state.notify = { cursor: '1', items: [{ kind: 'message', t: state.time(), from: 'private sender', text: 'private body' }] }
  state.target.followup = () => { throw new Error('host rejected') }
  await state.poller.tick()
  const file = readFileSync(join(path, readdirSync(path)[0]), 'utf8')
  assert.doesNotMatch(file, /private sender|private body|fwr_unit_test/)
  assert.equal(state.poller.status().pending, 1)
  state.poller.stop()
  state.target.followup = (p) => original.deliveries.push(['wake', p])
  state.poller = new Poller({ key, stateDirectory: path, sessionId: 'fish' }, {
    now: state.time, roots: () => state.roots, mcpReady: () => true, request: (part) => state[part],
  })
  await state.poller.tick()
  state.step(30); await state.poller.tick()
  assert.equal(state.deliveries.length, 1)
  assert.equal(state.poller.status().pending, 0)
})

test('scheduled alarm rechecks sleep on time, survives restart, then restores the original notification cursor', async () => {
  const f = fixture({ wakeTime: '12:01' })
  await f.poller.tick()
  f.step(30)
  f.state.figure.asleep = true
  await f.poller.tick()
  f.step(30)
  await f.poller.tick()
  assert.deepEqual(f.calls, [['state', ''], ['notify', ''], ['state', ''], ['state', '']])
  assert.equal(f.poller.status().alarmDay, '2026-11-20')
  assert.equal(f.poller.status().wakes, 1)
  assert.match(f.deliveries[0][1].content[0].text, /定时起床.*鱼 #1.*world_do/)
  f.poller.stop()
  f.poller = f.open()
  await f.poller.tick()
  assert.equal(f.deliveries.length, 1)
  f.step(120)
  f.state.figure.asleep = false
  f.notify = { cursor: 'after', items: [] }
  await f.poller.tick()
  assert.deepEqual(f.calls.at(-1), ['notify', 'base'])
  assert.equal(f.poller.state.cursor, 'after')
})

test('alarm waits for quiet hours, healthy state, mounted tools and idle session; failure is not replayed', async () => {
  const f = fixture({ wakeTime: '12:00', quietStart: '23:00', quietEnd: '12:01' })
  f.state.figure.asleep = true
  await f.poller.tick()
  assert.match(f.poller.status().alarmStatus, /免打扰/)
  f.step(60)
  f.alarmReady = false
  await f.poller.tick()
  assert.match(f.poller.status().alarmStatus, /未挂载/)
  f.alarmReady = true
  f.target.status = 'running'
  await f.poller.tick()
  assert.match(f.poller.status().alarmStatus, /等待会话/)
  f.target.status = 'idle'
  f.step(60)
  f.state = new Error('network')
  await f.poller.tick()
  assert.match(f.poller.status().alarmStatus, /有效状态/)
  assert.equal(f.deliveries.length, 0)
  f.state = { fish: '鱼#1', figure: { asleep: true } }
  f.target.followup = () => { throw Error('uncertain') }
  f.step(240)
  await f.poller.tick()
  assert.match(f.poller.status().alarmStatus, /结果未知/)
  f.poller = f.open()
  await f.poller.tick()
  assert.equal(f.poller.status().wakes, 1)
  assert.equal(f.poller.status().alarmDay, '2026-11-20')
})

test('already-awake fish does not get woken during a later nap; expired or disabled alarm does nothing', async () => {
  const f = fixture({ wakeTime: '12:00', maxWakesPerDay: 1 })
  await f.poller.tick()
  assert.equal(f.poller.status().alarmDay, '2026-11-20')
  f.state.figure.asleep = true
  f.step(30)
  await f.poller.tick()
  assert.equal(f.deliveries.length, 0)
  for (const config of [{ wakeTime: '09:59' }, { wakeTime: '' }, { wakeTime: '12:00', maxWakesPerDay: 0 }]) {
    const other = fixture(config)
    other.state.figure.asleep = true
    await other.poller.tick()
    assert.equal(other.deliveries.length, 0)
  }
})

test('quiet hours, missing MCP, stale doorbells and likes inject without waking', async () => {
  const f = fixture({ quietStart: '23:00', quietEnd: '13:00' })
  await f.poller.tick()
  f.step(30)
  f.notify = { cursor: '1', items: [{ kind: 'message', t: f.time(), text: 'a' }] }
  await f.poller.tick()
  f.step(3600)
  f.ready = false
  f.notify = { cursor: '2', items: [{ kind: 'message', t: f.time(), text: 'b' }] }
  await f.poller.tick()
  f.step(30); f.ready = true
  f.notify = { cursor: '3', items: [{ kind: 'doorbell', t: f.time() - 200_000 }, { kind: 'like', t: f.time() }] }
  await f.poller.tick()
  assert.deepEqual(f.deliveries.map(([kind]) => kind), ['notice', 'notice', 'notice'])
})

test('idle is opt-in, waits for an idle session and a fresh empty activity, and respects the cap', async () => {
  const f = fixture({ idleEnabled: true, idleMinutes: 15, maxIdlePerDay: 1 })
  await f.poller.tick()
  f.step(900)
  f.target.status = 'running'
  await f.poller.tick()
  f.target.status = 'idle'
  f.state.doing = { name: '行程还没结束', until: f.time() + 30_000 }
  f.step(30); await f.poller.tick()
  assert.equal(f.deliveries.length, 0)
  delete f.state.doing
  f.step(30); await f.poller.tick()
  f.step(900); await f.poller.tick()
  assert.equal(f.poller.status().idle, 1)
  assert.equal(f.deliveries.length, 1)
})

test('a quiet-hours event collected while the session was busy never wakes after the window', async () => {
  const f = fixture({ quietStart: '23:00', quietEnd: '12:01' })
  await f.poller.tick()
  f.step(30)
  f.target.status = 'running'
  f.notify = { cursor: '1', items: [{ kind: 'message', t: f.time() }] }
  await f.poller.tick()
  assert.equal(f.poller.status().pending, 1)
  f.step(30)
  f.target.status = 'idle'
  await f.poller.tick()
  assert.deepEqual(f.deliveries.map(([kind]) => kind), ['notice'])
})

test('unselected sessions do not poll; invalid response never advances cursor; tool test is explicit', async () => {
  const f = fixture({ sessionId: '' })
  await f.poller.tick()
  assert.equal(f.calls.length, 0)
  assert.equal((await f.poller.test()).ok, false)
  f.poller.update(normalize({ sessionId: 'fish' }))
  await f.poller.tick()
  f.step(30)
  f.notify = { cursor: 'invalid', items: [{ text: 'missing kind' }] }
  await f.poller.tick()
  assert.equal(f.poller.state.cursor, 'base')
  assert.match(f.poller.status().notifyError, /保留原游标/)
  assert.equal((await f.poller.test()).ok, true)
  assert.match(f.deliveries[0][1].content[0].text, /只调用.*world_now/)
  assert.equal(f.poller.status().wakes, 1)
  f.poller.update(normalize({ sessionId: 'fish', maxWakesPerDay: 1 }))
  assert.equal((await f.poller.test()).ok, false)
  assert.equal(sleepSignal({ figure: { asleep: false, pose: 'sleep' } }), false)
  assert.throws(() => normalize({ eventModes: { like: 'wake' } }))
  assert.throws(() => normalize({ wakeTime: '12:70' }))
  assert.equal(sleepSignal({ figure: { pose: 'unknown-new-pose' } }), null)
})
