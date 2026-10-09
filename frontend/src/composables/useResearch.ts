import { computed, ref } from 'vue'

export interface Task {
  id: number
  title: string
  intent: string
  query: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  failure_reason?: string | null
}

interface ResearchRecord {
  id: string
  topic: string
  status: string
  progress: number
  progress_text: string
  tasks: Task[]
  report: string | null
  error: string | null
  last_event_id?: number
}

export interface ResearchHistoryItem {
  id: string
  topic: string
  status: string
  progress: number
  progress_text: string
  created_at: string
  updated_at: string
  has_report: boolean
  task_count: number
}

const ACTIVE_RESEARCH_KEY = 'deepresearch.activeResearchId'

export function useResearch() {
  const isLoading = ref(false)
  const progress = ref(0)
  const progressText = ref('')
  const tasks = ref<Task[]>([])
  const report = ref('')
  const researchId = ref('')
  const error = ref<string | null>(null)
  const history = ref<ResearchHistoryItem[]>([])
  const researchStatus = ref('')
  const isResuming = ref(false)
  const canResume = computed(() => Boolean(researchId.value)
    && (['error', 'cancelled'].includes(researchStatus.value)
      || (researchStatus.value === 'completed' && tasks.value.some((task) => task.status === 'failed')))
    && !isLoading.value)
  let eventSource: EventSource | null = null
  let loadVersion = 0

  const closeEventSource = () => {
    eventSource?.close()
    eventSource = null
  }

  const resetState = () => {
    loadVersion++
    closeEventSource()
    isLoading.value = true
    progress.value = 0
    progressText.value = '正在创建研究任务...'
    tasks.value = []
    report.value = ''
    researchId.value = ''
    error.value = null
    researchStatus.value = 'planning'
  }

  const handleEvent = (data: any) => {
    switch (data.type) {
      case 'progress':
        researchStatus.value = data.stage
        progress.value = data.percentage
        progressText.value = data.text
        if (data.stage === 'executing' && data.task_id) {
          tasks.value = tasks.value.map((task) =>
            task.id === data.task_id
              ? { ...task, status: 'running' as const }
              : task
          )
        }
        if (data.stage === 'completed' || data.stage === 'cancelled') {
          if (data.stage === 'cancelled') {
            tasks.value = tasks.value.map((task) =>
              task.status === 'running'
                ? { ...task, status: 'cancelled' as const }
                : task
            )
          }
          closeEventSource()
          isLoading.value = false
          localStorage.removeItem(ACTIVE_RESEARCH_KEY)
          void loadHistory()
        }
        break

      case 'plan':
        tasks.value = data.tasks.map((task: Task) => ({
          ...task,
          status: task.status || 'pending',
        }))
        break

      case 'task_done':
        tasks.value = tasks.value.map((task) =>
          task.id === data.task_id
            ? { ...task, status: 'completed' as const }
            : task
        )
        break

      case 'task_failed':
        tasks.value = tasks.value.map((task) =>
          task.id === data.task_id
            ? { ...task, status: 'failed' as const, failure_reason: data.message || '暂无失败详情。' }
            : task
        )
        break

      case 'report':
        report.value = data.content
        researchId.value = data.research_id || researchId.value
        break

      case 'error':
        researchStatus.value = 'error'
        error.value = data.message
        closeEventSource()
        isLoading.value = false
        localStorage.removeItem(ACTIVE_RESEARCH_KEY)
        void loadHistory()
        break
    }
  }

  const connectToEvents = (id: string, after = 0) => {
    closeEventSource()
    eventSource = new EventSource(`/api/research/${id}/events?after=${after}`)
    const connectedSource = eventSource
    eventSource.onmessage = (event) => {
      if (eventSource !== connectedSource) return
      try {
        handleEvent(JSON.parse(event.data))
      } catch {
        error.value = '收到无法解析的研究进度数据'
      }
    }
    eventSource.onerror = () => {
      if (eventSource !== connectedSource) return
      if (isLoading.value) {
        progressText.value = '连接暂时中断，正在自动重连...'
      }
    }
  }

  const startResearch = async (topic: string) => {
    resetState()
    try {
      const response = await fetch('/api/research', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic: topic.trim() }),
      })
      if (!response.ok) {
        throw new Error(`创建研究任务失败（${response.status}）`)
      }
      const created = await response.json()
      researchId.value = created.research_id
      localStorage.setItem(ACTIVE_RESEARCH_KEY, created.research_id)
      connectToEvents(created.research_id)
    } catch (caught: any) {
      error.value = caught.message || '创建研究任务失败'
      isLoading.value = false
    }
  }

  const loadResearch = async (id: string): Promise<string | null> => {
    const version = ++loadVersion
    try {
      const response = await fetch(`/api/research/${id}`)
      if (version !== loadVersion) return null
      if (!response.ok) {
        localStorage.removeItem(ACTIVE_RESEARCH_KEY)
        return null
      }
      const record: ResearchRecord = await response.json()
      if (version !== loadVersion) return null
      closeEventSource()
      researchId.value = record.id
      researchStatus.value = record.status
      progress.value = record.progress
      progressText.value = record.progress_text
      tasks.value = record.tasks.map((task) => {
        if (task.status !== 'running') return task
        if (record.status === 'cancelled') {
          return { ...task, status: 'cancelled' as const }
        }
        if (record.status === 'error') {
          return { ...task, status: 'failed' as const }
        }
        return task
      })
      report.value = record.report || ''
      error.value = record.error
      isLoading.value = !['completed', 'error', 'cancelled'].includes(record.status)
      if (isLoading.value) {
        localStorage.setItem(ACTIVE_RESEARCH_KEY, record.id)
        connectToEvents(record.id, record.last_event_id || 0)
      } else {
        localStorage.removeItem(ACTIVE_RESEARCH_KEY)
      }
      return record.topic
    } catch {
      return null
    }
  }

  const resumeLastResearch = async (): Promise<string | null> => {
    const id = localStorage.getItem(ACTIVE_RESEARCH_KEY)
    return id ? loadResearch(id) : null
  }

  const continueResearch = async () => {
    if (!canResume.value || isResuming.value) return
    const id = researchId.value
    isResuming.value = true
    try {
      const response = await fetch(`/api/research/${id}/resume`, { method: 'POST' })
      if (!response.ok) {
        let message = `继续研究失败（${response.status}）`
        try { message = (await response.json()).detail || message } catch { /* use fallback */ }
        throw new Error(message)
      }
      // Load the same record and subscribe only after its current event cursor.
      // Never reset completed tasks, reports or the research identifier.
      if (researchId.value === id && await loadResearch(id) === null) {
        throw new Error('恢复请求已提交，但进度读取失败，请从历史研究重新打开')
      }
      void loadHistory()
    } catch (caught: any) {
      if (researchId.value === id) error.value = caught.message || '继续研究失败'
    } finally {
      isResuming.value = false
    }
  }

  const loadHistory = async () => {
    try {
      const response = await fetch('/api/research/history?limit=8')
      history.value = response.ok ? await response.json() : []
    } catch {
      history.value = []
    }
  }

  const deleteResearch = async (id: string) => {
    const response = await fetch(`/api/research/${id}`, { method: 'DELETE' })
    if (!response.ok) {
      let message = `删除失败（${response.status}）`
      try {
        const body = await response.json()
        if (body.detail) message = body.detail
      } catch {
        // Keep the status-based fallback when the response has no JSON body.
      }
      throw new Error(message)
    }
    history.value = history.value.filter((item) => item.id !== id)
    if (localStorage.getItem(ACTIVE_RESEARCH_KEY) === id) {
      localStorage.removeItem(ACTIVE_RESEARCH_KEY)
    }
  }

  const cancelResearch = async () => {
    if (!researchId.value || !isLoading.value) return
    try {
      const response = await fetch(
        `/api/research/${researchId.value}/cancel`,
        { method: 'POST' }
      )
      if (!response.ok && response.status !== 409) {
        throw new Error(`取消失败（${response.status}）`)
      }
    } catch (caught: any) {
      error.value = caught.message || '取消研究失败'
    }
  }

  return {
    researchId,
    isLoading,
    progress,
    progressText,
    tasks,
    report,
    error,
    history,
    canResume,
    isResuming,
    continueResearch,
    startResearch,
    loadResearch,
    loadHistory,
    deleteResearch,
    resumeLastResearch,
    cancelResearch,
  }
}
