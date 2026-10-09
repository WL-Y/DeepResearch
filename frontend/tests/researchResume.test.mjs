import assert from 'node:assert/strict'
import test, { beforeEach, afterEach } from 'node:test'
import { useResearch } from '../src/composables/useResearch.ts'

const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage, EventSource: globalThis.EventSource }
let streams, requests, record, resumeResponse
beforeEach(() => {
  streams = []; requests = []
  record = { id: 'run', topic: 'Topic', status: 'error', progress: 45, progress_text: '研究中断',
    tasks: [{ id: 1, title: 'Done', status: 'completed' }, { id: 2, title: 'Pending', status: 'failed' }],
    report: null, error: 'Model failed', last_event_id: 12 }
  resumeResponse = null
  const storage = new Map()
  globalThis.localStorage = { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  globalThis.EventSource = class {
    constructor(url) { this.url = url; this.closed = false; streams.push(this) }
    close() { this.closed = true }
  }
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options })
    if (url.includes('/history')) return Response.json([])
    if (url.endsWith('/resume')) {
      if (resumeResponse) return resumeResponse()
      record = { ...record, status: 'resuming', error: null, last_event_id: 14 }
      return Response.json({ research_id: 'run', status: 'resuming' }, { status: 202 })
    }
    return Response.json(record)
  }
})
afterEach(() => Object.assign(globalThis, originals))

test('history preserves legacy content without exposing follow-up actions', async () => {
  record.status = 'completed'
  record.report = '# Report\n\n## 追问与补充\n\n### Q: Old question\n\nSaved answer'
  const research = useResearch()
  await research.loadResearch('run')
  assert.equal(research.report.value, record.report)
  for (const key of ['askFollowUp', 'followUpAnswer', 'isFollowUpLoading']) {
    assert.equal(key in research, false)
  }
  assert.equal(requests.some(request => request.url.includes('/followup')), false)
})

test('resumes same record, preserves completed tasks and skips old SSE errors', async () => {
  const research = useResearch()
  await research.loadResearch('run')
  assert.equal(research.canResume.value, true)
  await research.continueResearch()
  assert.equal(research.error.value, null)
  assert.equal(research.isLoading.value, true)
  assert.equal(research.tasks.value[0].status, 'completed')
  assert.equal(streams[0].url, '/api/research/run/events?after=14')
  assert.equal(requests.filter(r => r.options?.method === 'POST')[0].url, '/api/research/run/resume')
  streams[0].onmessage({ data: JSON.stringify({ type: 'task_done', task_id: 2 }) })
  streams[0].onmessage({ data: JSON.stringify({ type: 'progress', stage: 'completed', percentage: 100, text: 'Done' }) })
  assert.equal(research.canResume.value, false)
  assert.equal(research.isLoading.value, false)
})

test('guards double clicks and keeps resume available after request failure', async () => {
  const research = useResearch()
  await research.loadResearch('run')
  let resolve
  resumeResponse = () => new Promise(done => { resolve = done })
  const pending = research.continueResearch()
  await research.continueResearch()
  assert.equal(requests.filter(r => r.url.endsWith('/resume')).length, 1)
  assert.equal(research.isResuming.value, true)
  resolve(Response.json({ detail: '上次请求仍在退出' }, { status: 409 }))
  await pending
  assert.equal(research.isResuming.value, false)
  assert.equal(research.canResume.value, true)
  assert.match(research.error.value, /仍在退出/)
  assert.equal(research.tasks.value[0].status, 'completed')
})

test('cancelled history has resume, completed report errors do not', async () => {
  const research = useResearch()
  record.status = 'cancelled'; record.error = null
  await research.loadResearch('run')
  assert.equal(research.canResume.value, true)
  record.status = 'completed'; record.report = '# Report'; record.error = 'History refresh failed'
  record.tasks = record.tasks.map(task => ({ ...task, status: 'completed' }))
  await research.loadResearch('run')
  assert.equal(research.canResume.value, false)
  await research.continueResearch()
  assert.equal(requests.filter(r => r.url.endsWith('/resume')).length, 0)
})

test('completed reports with failed tasks remain resumable', async () => {
  const research = useResearch()
  record.status = 'completed'; record.report = '# Partial report'
  await research.loadResearch('run')
  assert.equal(research.canResume.value, true)
})
