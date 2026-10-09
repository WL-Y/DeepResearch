<template>
  <div v-if="isOpen" class="research-workspace">
    <header class="workspace-header">
      <button class="back-btn" title="返回研究首页" @click="$emit('close')">
        <svg viewBox="0 0 20 20"><path d="m12 5-5 5 5 5" /></svg>
      </button>
      <div class="workspace-brand">
        <span class="brand-glyph">D</span>
        <span>DeepResearch</span>
      </div>
      <span class="header-divider"></span>
      <div class="header-topic">
        <span>当前研究</span>
        <strong>{{ topic }}</strong>
      </div>
      <div class="header-actions">
        <button
          v-if="report && !isLoading && !isResuming"
          class="header-action"
          title="返回首页，输入新的研究主题"
          @click="$emit('newResearch')"
        >
          开始新研究
        </button>
        <button v-if="canResume || isResuming" class="resume-btn" :disabled="isResuming" @click="$emit('resume')">
          {{ isResuming ? '正在恢复…' : '继续研究' }}
        </button>
        <span class="run-state" :class="stateClass">
          <i></i>{{ stateLabel }}
        </span>
        <details v-if="report" ref="exportMenu" class="export-menu" @keydown.esc.prevent="closeExportMenu">
          <summary class="header-action" :aria-disabled="isExporting" @click="isExporting && $event.preventDefault()">
            <svg viewBox="0 0 20 20"><path d="M10 3v9m0 0 3-3m-3 3L7 9M4 14v2h12v-2" /></svg>
            {{ isExporting ? '正在导出…' : '导出报告' }}
            <span aria-hidden="true">⌄</span>
          </summary>
          <div class="export-options" role="group" aria-label="选择报告导出格式">
            <button v-for="format in exportFormats" :key="format.value" :disabled="isExporting" @click="exportReport(format.value)">
              <span><strong>{{ format.label }}</strong><small>{{ format.description }}</small></span>
              <span class="export-extension">.{{ format.value }}</span>
            </button>
          </div>
        </details>
      </div>
    </header>

    <div class="workspace-body">
      <aside class="trace-panel">
        <div class="trace-overview">
          <div class="trace-kicker">研究进度</div>
          <div class="progress-row">
            <strong>{{ progress }}%</strong>
            <span>{{ completedTasks }}/{{ tasks.length || '—' }} 个任务</span>
          </div>
          <div class="linear-progress">
            <span :style="{ width: `${progress}%` }"></span>
          </div>
          <p>{{ progressText || '等待研究开始' }}</p>
        </div>

        <div class="trace-heading">
          <span>研究路径</span>
          <span v-if="isLoading" class="live-label"><i></i> LIVE</span>
        </div>

        <div v-if="tasks.length" class="task-list">
          <div
            v-for="(task, index) in tasks"
            :key="task.id"
            class="task-card"
            :class="task.status"
          >
            <span class="task-index">{{ String(index + 1).padStart(2, '0') }}</span>
            <span class="task-status-icon">
              <svg v-if="task.status === 'completed'" viewBox="0 0 18 18"><path d="m5 9 2.5 2.5L13 6" /></svg>
              <svg v-else-if="task.status === 'failed'" viewBox="0 0 18 18"><path d="m6 6 6 6m0-6-6 6" /></svg>
              <svg v-else-if="task.status === 'cancelled'" viewBox="0 0 18 18"><path d="M5 9h8" /></svg>
              <i v-else></i>
            </span>
            <span class="task-copy">
              <strong>{{ task.title }}</strong>
              <small>{{ taskStatus(task.status) }}</small>
            </span>
            <p v-if="task.status === 'failed'" class="task-failure-reason">
              {{ task.failure_reason || '暂无失败详情（该任务失败原因未被旧版本保存）。' }}
            </p>
          </div>
        </div>

        <div v-else class="task-skeletons">
          <div v-for="number in 4" :key="number" class="task-skeleton">
            <span></span><i></i>
          </div>
        </div>

        <div class="trace-footer">
          <div class="trace-note">
            <svg viewBox="0 0 20 20"><path d="M10 2.5 16 5v4.5c0 3.8-2.6 6.5-6 8-3.4-1.5-6-4.2-6-8V5l6-2.5Z" /><path d="m7.5 10 1.7 1.7 3.4-4" /></svg>
            <span>
              <strong>{{ isResuming ? '正在恢复研究' : isLoading ? '后台持续运行' : canResume ? (tasks.some((task) => task.status === 'failed') ? '可补全失败任务' : '可从保存进度继续') : report ? '研究已完成' : '研究已停止' }}</strong>
              <small>{{ isLoading ? '离开页面不会中断研究' : canResume || isResuming ? '复用已保存结果，不必从头开始' : report ? '报告可从历史研究再次查看' : '可返回首页发起研究' }}</small>
            </span>
          </div>
          <button v-if="isLoading" class="cancel-btn" @click="$emit('cancel')">停止研究</button>
        </div>
      </aside>

      <main class="content-panel">
        <div v-if="isLoading && !report" class="researching-state">
          <div class="radar-wrap">
            <div class="radar-circle one"></div>
            <div class="radar-circle two"></div>
            <div class="radar-core">
              <svg viewBox="0 0 28 28"><circle cx="11" cy="11" r="6" /><path d="m15.5 15.5 5 5M11 8v6M8 11h6" /></svg>
            </div>
          </div>
          <span class="stage-pill">{{ stageLabel }}</span>
          <h2>正在构建研究结论</h2>
          <p>{{ progressText }}。研究完成后，报告会自动出现在这里。</p>
          <div class="activity-card">
            <span class="activity-pulse"></span>
            <span>
              <strong>{{ currentTaskTitle }}</strong>
              <small>AI 正在检索、阅读并整理多源信息</small>
            </span>
          </div>
        </div>

        <div v-if="error && !report" class="message-state error-state">
          <span class="message-icon">!</span>
          <h2>研究中断</h2>
          <p>{{ error }}</p>
          <p v-if="canResume" class="resume-hint">点击右上角“继续研究”，复用已保存的结果，只处理尚未完成的部分。旧记录缺少完整恢复数据时会重做对应子任务。</p>
          <button @click="$emit('close')">返回研究首页</button>
        </div>

        <div v-if="!isLoading && !report && !error" class="message-state">
          <span class="message-icon muted">—</span>
          <h2>{{ progressText || '研究已停止' }}</h2>
          <p>{{ canResume ? '任务状态已保存，可点击右上角“继续研究”从保存的进度恢复。' : '你可以从历史研究查看任务，或返回首页发起新的研究。' }}</p>
          <button @click="$emit('close')">返回研究首页</button>
        </div>

        <div v-if="report" class="report-view">
          <div v-if="exportError" class="export-feedback export-failed" role="alert">{{ exportError }}</div>
          <div v-else-if="exportStatus" class="export-feedback" role="status">{{ exportStatus }}</div>
          <div v-if="error" class="inline-alert">{{ error }}</div>
          <div class="report-meta">
            <div>
              <span class="report-label">RESEARCH REPORT</span>
              <span class="report-ready"><i></i> 报告已生成</span>
            </div>
            <button class="copy-btn" @click="copyReport">
              <svg viewBox="0 0 20 20"><rect x="6" y="6" width="10" height="10" rx="2" /><path d="M13 6V4H4v9h2" /></svg>
              {{ copied ? '已复制' : '复制全文' }}
            </button>
          </div>

          <article class="report-paper" :lang="reportLanguage">
            <div class="markdown-body" v-html="renderedReport"></div>
          </article>
          <SourceDetails v-if="researchId" :key="researchId" :research-id="researchId" />
        </div>
      </main>
    </div>

  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import DOMPurify from 'dompurify'
import SourceDetails from './SourceDetails.vue'
import { normalizeReportMarkdown, renderReportMarkdown } from '../utils/reportFormatting'
import { createReportDownload, exportFormats } from '../utils/reportExport'
import type { ExportFormat } from '../utils/reportExport'
import type { Task } from '../composables/useResearch'

const props = defineProps<{
  researchId?: string
  isOpen: boolean
  topic: string
  isLoading: boolean
  progress: number
  progressText: string
  tasks: Task[]
  report: string
  error: string | null
  canResume?: boolean
  isResuming?: boolean
}>()

const emit = defineEmits<{ close: []; newResearch: []; cancel: []; resume: [] }>()
const copied = ref(false)
const exportMenu = ref<HTMLDetailsElement | null>(null)
const isExporting = ref(false)
const exportError = ref('')
const exportStatus = ref('')
let exportController: AbortController | null = null
const closeExportMenu = () => { if (exportMenu.value) exportMenu.value.open = false }
const closeExportOutside = (event: PointerEvent) => {
  if (event.target instanceof Node && !exportMenu.value?.contains(event.target)) closeExportMenu()
}
onMounted(() => document.addEventListener('pointerdown', closeExportOutside))
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', closeExportOutside)
  exportController?.abort()
})

// Older reports without citation positions are left intact, not guessed at.
const displayReport = computed(() => props.report ? normalizeReportMarkdown(props.report) : '')
const reportLanguage = computed(() => /[\u3400-\u9fff]/u.test(props.topic) ? 'zh-CN' : 'en')
const renderedReport = computed(() => DOMPurify.sanitize(
  renderReportMarkdown(displayReport.value, true),
  { USE_PROFILES: { html: true }, ADD_ATTR: ['target'], FORBID_TAGS: ['style', 'iframe', 'form', 'object', 'embed'], FORBID_ATTR: ['style'] }
))

const completedTasks = computed(() => props.tasks.filter((task) => task.status === 'completed').length)
const currentTaskTitle = computed(() => props.tasks.find((task) => task.status === 'running')?.title || props.progressText || '正在规划研究路径')
const stageLabel = computed(() => props.progress < 10 ? '研究规划' : props.progress < 85 ? '多源检索' : '综合撰写')
const stateLabel = computed(() => props.isResuming ? '恢复中' : props.error ? '研究中断' : props.isLoading ? '研究进行中' : props.report ? '报告已生成' : '研究已停止')
const stateClass = computed(() => props.isResuming ? 'is-live' : props.error ? 'is-error' : props.isLoading ? 'is-live' : props.report ? 'is-complete' : 'is-muted')

const taskStatus = (status: string) => ({ completed: '证据整理完成', failed: '任务未完成', cancelled: '任务已取消', running: '正在执行', pending: '等待执行' }[status] || status)

const copyReport = async () => {
  await navigator.clipboard.writeText(displayReport.value)
  copied.value = true
  window.setTimeout(() => { copied.value = false }, 1600)
}

const exportReport = async (format: ExportFormat) => {
  if (isExporting.value) return
  closeExportMenu()
  isExporting.value = true
  exportError.value = ''
  exportStatus.value = ''
  exportController = new AbortController()
  const timeout = window.setTimeout(() => exportController?.abort(), 60000)
  try {
    const { blob, filename } = await createReportDownload(props.topic, displayReport.value, format, exportController.signal)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 60000)
    exportStatus.value = `${filename} 已生成，请查看浏览器下载。`
  } catch (error) {
    exportError.value = error instanceof Error && error.name !== 'AbortError'
      ? error.message : '导出已取消或超时，请重试或选择 Markdown 格式'
  } finally {
    window.clearTimeout(timeout)
    isExporting.value = false
    exportController = null
  }
}
</script>

<style scoped>
.research-workspace { --ink: #10221c; --green: #3dbb83; position: fixed; inset: 0; z-index: 1000; display: flex; flex-direction: column; color: #dcebe4; background: #07100f; }
.workspace-header { height: 68px; flex: 0 0 68px; display: flex; align-items: center; gap: 14px; padding: 0 22px; border-bottom: 1px solid rgba(182,220,202,.12); background: rgba(8,20,18,.95); }
.back-btn, .header-action, .copy-btn { display: flex; align-items: center; justify-content: center; gap: 7px; border: 1px solid rgba(185,220,204,.13); color: #9fb5ac; background: rgba(255,255,255,.025); cursor: pointer; }
.back-btn { width: 36px; height: 36px; flex-shrink: 0; border-radius: 10px; }
.back-btn:hover, .header-action:hover, .copy-btn:hover { color: #dcece5; border-color: rgba(114,227,173,.3); background: rgba(114,227,173,.07); }
.back-btn svg, .header-action svg, .copy-btn svg { width: 17px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.workspace-brand { display: flex; align-items: center; gap: 8px; color: #cfe0d8; font-size: 13px; font-weight: 700; }
.brand-glyph { width: 26px; height: 26px; display: grid; place-items: center; border-radius: 8px; color: #092118; background: #73dda9; font-family: Georgia, serif; }
.header-divider { width: 1px; height: 26px; background: rgba(255,255,255,.09); }
.header-topic { min-width: 0; display: grid; gap: 3px; }
.header-topic span { color: #60776e; font-size: 9px; letter-spacing: .08em; text-transform: uppercase; }
.header-topic strong { max-width: min(48vw, 620px); overflow: hidden; color: #b9ccc4; font-size: 12px; font-weight: 550; text-overflow: ellipsis; white-space: nowrap; }
.header-actions { margin-left: auto; flex-shrink: 0; display: flex; align-items: center; gap: 9px; white-space: nowrap; }
.run-state { display: flex; align-items: center; gap: 7px; padding: 7px 10px; border-radius: 20px; color: #81978e; background: rgba(255,255,255,.035); font-size: 10px; }
.run-state i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.run-state.is-live { color: #6edda7; }
.run-state.is-live i { box-shadow: 0 0 0 4px rgba(110,221,167,.1); animation: blink 1.8s infinite; }
.run-state.is-complete { color: #78cda6; }
.run-state.is-error { color: #ef8e8e; }
.header-action { height: 34px; padding: 0 12px; border-radius: 9px; font-size: 10.5px; }
.header-action:disabled { opacity: .5; cursor: not-allowed; }
.export-menu { position: relative; flex-shrink: 0; }
.export-menu summary { list-style: none; user-select: none; }
.export-menu summary::-webkit-details-marker { display: none; }
.export-menu summary[aria-disabled="true"] { cursor: wait; opacity: .7; }
.export-options { position: absolute; top: calc(100% + 8px); right: 0; z-index: 20; width: 270px; max-width: calc(100vw - 32px); padding: 6px; border: 1px solid #e3ddd2; border-radius: 12px; background: #fffdf9; box-shadow: 0 12px 36px rgba(82,65,42,.16); }
.export-options button { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; padding: 12px; border: 0; border-radius: 8px; color: #35443b; background: transparent; text-align: left; cursor: pointer; }
.export-options button:hover, .export-options button:focus-visible { background: #f1f4ee; outline: 2px solid #669477; outline-offset: -2px; }
.export-options strong, .export-options small { display: block; }
.export-options strong { font-size: 13px; }
.export-options small { margin-top: 4px; color: #87877f; font-size: 11px; }
.export-extension { color: #778c7f; font: 11px monospace; }
.export-feedback { margin-bottom: 14px; padding: 10px 12px; border-radius: 8px; color: #397458; background: #edf3ec; font-size: 12px; overflow-wrap: anywhere; }
.export-failed { color: #a4453c; background: #fbede9; }

.workspace-body { flex: 1; min-height: 0; display: grid; grid-template-columns: 310px 1fr; }
.trace-panel { min-height: 0; display: flex; flex-direction: column; padding: 24px 18px 18px; border-right: 1px solid rgba(182,220,202,.11); background: #0a1715; overflow-y: auto; }
.trace-overview { padding: 16px; border: 1px solid rgba(174,213,194,.11); border-radius: 13px; background: linear-gradient(145deg, rgba(26,48,42,.7), rgba(12,29,25,.8)); }
.trace-kicker { color: #748b82; font-size: 10px; letter-spacing: .07em; text-transform: uppercase; }
.progress-row { margin-top: 11px; display: flex; align-items: baseline; justify-content: space-between; }
.progress-row strong { color: #e2f2ea; font-size: 28px; font-weight: 520; }
.progress-row span { color: #71877e; font-size: 10px; }
.linear-progress { height: 3px; margin-top: 11px; overflow: hidden; border-radius: 10px; background: rgba(255,255,255,.07); }
.linear-progress span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #48c58b, #91ebbf); transition: width .5s ease; }
.trace-overview p { margin: 10px 0 0; color: #849b92; font-size: 10.5px; line-height: 1.5; }
.trace-heading { margin: 26px 4px 10px; display: flex; justify-content: space-between; color: #9bb0a7; font-size: 10.5px; font-weight: 650; }
.live-label { display: flex; align-items: center; gap: 5px; color: #54c88f; font-size: 8px; letter-spacing: .1em; }
.live-label i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.task-list { display: grid; gap: 6px; }
.task-card { display: grid; grid-template-columns: 22px 22px 1fr; align-items: center; gap: 8px; padding: 11px 9px; border: 1px solid transparent; border-radius: 10px; }
.task-card.running { border-color: rgba(90,211,153,.18); background: rgba(90,211,153,.055); }
.task-card.completed { background: rgba(255,255,255,.018); }
.task-card.failed { background: rgba(223,119,119,.055); }
.task-index { color: #465d54; font: 9px/1 monospace; }
.task-status-icon { width: 20px; height: 20px; display: grid; place-items: center; border-radius: 50%; color: #526a61; background: rgba(255,255,255,.04); }
.task-status-icon svg { width: 13px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; }
.task-status-icon i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.task-card.running .task-status-icon { color: #65d79d; background: rgba(101,215,157,.1); }
.task-card.running .task-status-icon i { animation: blink 1.5s infinite; }
.task-card.completed .task-status-icon { color: #69c897; }
.task-card.failed .task-status-icon { color: #df7777; }
.task-copy { min-width: 0; }
.task-copy strong, .task-copy small { display: block; }
.task-copy strong { overflow: hidden; color: #93a9a0; font-size: 10.5px; font-weight: 550; text-overflow: ellipsis; white-space: nowrap; }
.task-card.running .task-copy strong { color: #c5ddd3; }
.task-copy small { margin-top: 4px; color: #50665d; font-size: 9px; }
.task-failure-reason { grid-column: 3 / -1; margin: 0; color: #ba7772; font-size: 9px; line-height: 1.5; }
.task-card.running .task-copy small { color: #65a98a; }
.task-skeletons { display: grid; gap: 8px; }
.task-skeleton { padding: 12px; border-radius: 10px; background: rgba(255,255,255,.018); }
.task-skeleton span, .task-skeleton i { display: block; border-radius: 5px; background: rgba(255,255,255,.05); }
.task-skeleton span { width: 72%; height: 8px; }
.task-skeleton i { width: 42%; height: 6px; margin-top: 7px; }
.trace-footer { margin-top: auto; padding-top: 20px; }
.trace-note { display: flex; align-items: center; gap: 9px; padding: 11px; border-top: 1px solid rgba(255,255,255,.055); }
.trace-note svg { width: 18px; flex: 0 0 auto; fill: none; stroke: #5d9c7e; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.trace-note strong, .trace-note small { display: block; }
.trace-note strong { color: #82998f; font-size: 9.5px; }
.trace-note small { margin-top: 3px; color: #4c6259; font-size: 8.5px; }
.cancel-btn { width: 100%; margin-top: 10px; padding: 9px; border: 1px solid rgba(221,109,109,.18); border-radius: 9px; color: #c87d7d; background: rgba(221,109,109,.045); cursor: pointer; font-size: 10px; }
.resume-btn { padding: 9px 15px; border: 1px solid #4d785b; border-radius: 8px; color: #fffdf8; background: #4d785b; font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap; }
.resume-btn:hover { background: #3d654a; }
.resume-btn:disabled { opacity: .6; cursor: wait; }
.resume-btn:focus-visible { outline: 2px solid #4d785b; outline-offset: 3px; }
.message-state .resume-hint { max-width: 510px; font-size: 13px; color: #69796d; line-height: 1.8; }

.content-panel { min-width: 0; min-height: 0; overflow-y: auto; background: radial-gradient(circle at 50% 0, rgba(38,89,70,.13), transparent 38%), #0b1312; }
.researching-state, .message-state { min-height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 50px; text-align: center; }
.radar-wrap { width: 112px; height: 112px; position: relative; display: grid; place-items: center; }
.radar-circle { position: absolute; border: 1px solid rgba(103,219,163,.16); border-radius: 50%; animation: radar 2.6s ease-out infinite; }
.radar-circle.one { inset: 14px; }
.radar-circle.two { inset: 0; animation-delay: .8s; }
.radar-core { width: 52px; height: 52px; display: grid; place-items: center; border: 1px solid rgba(103,219,163,.22); border-radius: 17px; color: #78dfac; background: rgba(103,219,163,.08); }
.radar-core svg { width: 27px; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; }
.stage-pill { margin-top: 24px; padding: 6px 10px; border-radius: 20px; color: #67c998; background: rgba(103,219,163,.08); font-size: 9px; letter-spacing: .08em; }
.researching-state h2, .message-state h2 { margin: 15px 0 0; color: #dbeae3; font-family: Georgia, 'Noto Serif SC', serif; font-size: 29px; font-weight: 500; }
.researching-state > p, .message-state p { max-width: 470px; margin: 12px 0 0; color: #71867e; font-size: 12px; line-height: 1.8; }
.activity-card { min-width: 390px; margin-top: 30px; display: flex; align-items: center; gap: 12px; padding: 14px 16px; border: 1px solid rgba(255,255,255,.07); border-radius: 12px; background: rgba(255,255,255,.025); text-align: left; }
.activity-pulse { width: 7px; height: 7px; border-radius: 50%; background: #65d79d; box-shadow: 0 0 0 6px rgba(101,215,157,.08); animation: blink 1.5s infinite; }
.activity-card strong, .activity-card small { display: block; }
.activity-card strong { max-width: 330px; overflow: hidden; color: #9fb6ac; font-size: 10.5px; text-overflow: ellipsis; white-space: nowrap; }
.activity-card small { margin-top: 4px; color: #52675f; font-size: 9px; }
.message-icon { width: 48px; height: 48px; display: grid; place-items: center; border: 1px solid rgba(235,120,120,.24); border-radius: 15px; color: #ef9191; background: rgba(235,120,120,.07); font-size: 22px; }
.message-icon.muted { color: #789087; border-color: rgba(255,255,255,.1); background: rgba(255,255,255,.03); }
.message-state button { margin-top: 22px; padding: 10px 14px; border: 1px solid rgba(114,227,173,.2); border-radius: 9px; color: #92dcb9; background: rgba(114,227,173,.06); cursor: pointer; }

.report-view { width: 100%; max-width: 1080px; margin: 0 auto; padding: 28px clamp(16px, 3vw, 36px) 64px; }
.report-meta { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.report-meta > div { display: flex; align-items: center; gap: 10px; }
.report-label { color: #61756d; font: 9px/1 monospace; letter-spacing: .11em; }
.report-ready { display: flex; align-items: center; gap: 5px; color: #64b88f; font-size: 9px; }
.report-ready i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.copy-btn { padding: 8px 10px; border-radius: 8px; font-size: 9.5px; }
.report-paper { min-width: 0; padding: 42px clamp(24px, 4vw, 52px) 56px; border: 1px solid #dfe8e3; border-radius: 7px; color: var(--ink); background: #f5f8f6; box-shadow: 0 24px 80px rgba(0,0,0,.28); }
.inline-alert { margin-bottom: 14px; padding: 10px 12px; border-radius: 8px; color: #e9a1a1; background: rgba(201,74,74,.1); font-size: 11px; }
.markdown-body { min-width: 0; color: #25362f; font-family: 'Segoe UI', 'Microsoft YaHei', 'Noto Sans SC', sans-serif; font-size: 15px; line-height: 1.85; overflow-wrap: anywhere; }
.markdown-body :deep(h1) { margin: 0 0 28px; color: #10251d; font-family: Georgia, 'Noto Serif SC', 'SimSun', serif; font-size: clamp(25px, 2.5vw, 32px); font-weight: 700; line-height: 1.5; letter-spacing: 0; text-align: center; }
.markdown-body :deep(h2) { margin: 32px 0 14px; padding-top: 16px; border-top: 1px solid #dce6e1; color: #153128; font-size: 21px; font-weight: 650; line-height: 1.5; }
.markdown-body :deep(h3) { margin: 24px 0 10px; color: #1b4938; font-size: 17px; font-weight: 650; line-height: 1.6; }
.markdown-body :deep(h4), .markdown-body :deep(h5), .markdown-body :deep(h6) { margin: 20px 0 8px; font-size: 15px; line-height: 1.6; }
.markdown-body :deep(p) { margin: 0 0 1em; }
.markdown-body:lang(zh) :deep(> p) { text-indent: 2em; text-align: start; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { margin: 14px 0 20px; padding-left: 1.6em; }
.markdown-body :deep(li) { margin: 8px 0; padding-left: .25em; text-indent: 0; }
.markdown-body :deep(li > p) { margin: 6px 0; }
.markdown-body :deep(li > ul), .markdown-body :deep(li > ol) { margin: 6px 0; }
.markdown-body :deep(li::marker) { color: #66816f; }
.markdown-body :deep(strong) { color: #122f24; }
.markdown-body :deep(blockquote) { margin: 18px 0; padding: 13px 18px; border-left: 3px solid #56b887; border-radius: 0 7px 7px 0; color: #40584e; background: #e9f2ed; }
.markdown-body :deep(code) { padding: 2px 5px; border-radius: 4px; color: #195c43; background: #e5eee9; }
.markdown-body :deep(pre) { margin: 18px 0; padding: 17px; overflow-x: auto; border-radius: 8px; color: #d7e9e0; background: #10211c; }
.markdown-body :deep(pre code) { padding: 0; color: inherit; background: transparent; }
.markdown-body :deep(.report-table-scroll) { max-width: 100%; margin: 20px 0; overflow-x: auto; border: 1px solid #e2dbcf; border-radius: 6px; }
.markdown-body :deep(.report-table-scroll:focus-visible) { outline: 2px solid #669477; outline-offset: 3px; }
.markdown-body :deep(table) { width: 100%; margin: 0; border-collapse: separate; border-spacing: 0; font-size: 13px; line-height: 1.7; }
.markdown-body :deep(th), .markdown-body :deep(td) { min-width: 120px; max-width: 320px; padding: 11px 14px; border-right: 1px solid #dce5e1; border-bottom: 1px solid #dce5e1; text-align: left; vertical-align: top; text-indent: 0; }
.markdown-body :deep(th:last-child), .markdown-body :deep(td:last-child) { border-right: 0; }
.markdown-body :deep(th) { color: #16382b; background: #e7efeb; }
.markdown-body :deep(tr:last-child td) { border-bottom: 0; }
.markdown-body :deep(a) { color: #167451; text-decoration-color: rgba(22,116,81,.3); text-underline-offset: 3px; }
.markdown-body :deep(hr) { margin: 28px 0; border: 0; border-top: 1px solid #dce5e1; }


/* Warm paper theme */
.research-workspace {
  --ink: #302f2a;
  --green: #3f8766;
  color: #3b433d;
  background: #f7f3eb;
}
.workspace-header {
  border-color: rgba(92, 80, 60, .13);
  background: rgba(252, 249, 243, .97);
  box-shadow: 0 1px 14px rgba(82, 65, 42, .04);
}
.back-btn, .header-action, .copy-btn {
  border-color: rgba(92, 80, 60, .14);
  color: #70766f;
  background: rgba(255, 253, 248, .84);
}
.back-btn:hover, .header-action:hover, .copy-btn:hover {
  color: #315f49;
  border-color: rgba(63, 135, 102, .3);
  background: rgba(63, 135, 102, .07);
}
.workspace-brand { color: #39443d; }
.brand-glyph { color: #fffdf8; background: #3f8766; }
.header-divider { background: rgba(92, 80, 60, .13); }
.header-topic span { color: #9a968c; }
.header-topic strong { color: #535a54; }
.run-state { color: #777d76; background: rgba(92, 80, 60, .055); }
.run-state.is-live { color: #397e5e; }
.run-state.is-complete { color: #397e5e; }

.trace-panel {
  border-color: rgba(92, 80, 60, .12);
  background: #f1ece2;
}
.trace-overview {
  border-color: rgba(92, 80, 60, .11);
  background: linear-gradient(145deg, rgba(255,253,248,.96), rgba(247,242,233,.92));
  box-shadow: 0 10px 28px rgba(82, 65, 42, .045);
}
.trace-kicker { color: #929087; }
.progress-row strong { color: #35443b; }
.progress-row span { color: #898b83; }
.linear-progress { background: rgba(92, 80, 60, .09); }
.linear-progress span { background: linear-gradient(90deg, #397e5e, #78aa8d); }
.trace-overview p { color: #777e77; }
.trace-heading { color: #666d67; }
.live-label { color: #397e5e; }
.task-card.running { border-color: rgba(63,135,102,.18); background: rgba(63,135,102,.07); }
.task-card.completed { background: rgba(255,253,248,.58); }
.task-card.failed { background: rgba(179,78,67,.045); }
.task-index { color: #aaa398; }
.task-status-icon { color: #8b918a; background: rgba(92,80,60,.055); }
.task-card.running .task-status-icon { color: #397e5e; background: rgba(63,135,102,.1); }
.task-card.completed .task-status-icon { color: #4c8e6c; }
.task-copy strong { color: #656c66; }
.task-card.running .task-copy strong { color: #345543; }
.task-copy small { color: #9b988f; }
.task-failure-reason { color: #a65b52; }
.task-card.running .task-copy small { color: #54816a; }
.task-skeleton { background: rgba(255,253,248,.55); }
.task-skeleton span, .task-skeleton i { background: rgba(92,80,60,.08); }
.trace-note { border-color: rgba(92,80,60,.09); }
.trace-note svg { stroke: #53836a; }
.trace-note strong { color: #717871; }
.trace-note small { color: #9e9b92; }

.content-panel {
  background: radial-gradient(circle at 50% 0, rgba(230,205,172,.2), transparent 38%), #f8f5ef;
}
.radar-circle { border-color: rgba(63,135,102,.18); }
.radar-core { border-color: rgba(63,135,102,.24); color: #3f8766; background: rgba(63,135,102,.07); }
.stage-pill { color: #397e5e; background: rgba(63,135,102,.08); }
.researching-state h2, .message-state h2 { color: #313b35; }
.researching-state > p, .message-state p { color: #777e77; }
.activity-card { border-color: rgba(92,80,60,.11); background: rgba(255,253,248,.84); box-shadow: 0 12px 34px rgba(82,65,42,.06); }
.activity-pulse { background: #3f8766; box-shadow: 0 0 0 6px rgba(63,135,102,.09); }
.activity-card strong { color: #56615a; }
.activity-card small { color: #99968e; }
.message-icon.muted { color: #858a83; border-color: rgba(92,80,60,.12); background: rgba(92,80,60,.04); }
.message-state button { border-color: rgba(63,135,102,.22); color: #397458; background: rgba(63,135,102,.07); }

.report-label { color: #8e8b82; }
.report-ready { color: #397e5e; }
.report-paper {
  border-color: #e3ddd2;
  color: var(--ink);
  background: #fffdf9;
  box-shadow: 0 22px 70px rgba(82,65,42,.11);
}
.markdown-body { color: #3d403b; }
.markdown-body :deep(h1) { color: #2e332f; }
.markdown-body :deep(h2) { border-color: #e8e1d6; color: #35443b; }
.markdown-body :deep(h3) { color: #3e604e; }
.markdown-body :deep(strong) { color: #303a34; }
.markdown-body :deep(blockquote) { border-color: #669477; color: #536158; background: #f1f4ee; }
.markdown-body :deep(code) { color: #356148; background: #eef1eb; }
.markdown-body :deep(pre) { color: #f3f0e9; background: #36423b; }
.markdown-body :deep(table) { border-color: #e2dbcf; }
.markdown-body :deep(th), .markdown-body :deep(td) { border-color: #e7e0d5; }
.markdown-body :deep(th) { color: #35443b; background: #f1eee7; }
.markdown-body :deep(a) { color: #337556; text-decoration-color: rgba(51,117,86,.3); }
.markdown-body :deep(sup.report-citation) { position: relative; top: -.45em; vertical-align: baseline; font-size: .72em; line-height: 0; white-space: nowrap; text-indent: 0; }
.markdown-body :deep(.report-citation a) { padding: 2px 3px; border-radius: 3px; font-weight: 600; text-decoration: none; }
.markdown-body :deep(.report-citation a:hover) { color: #214e39; background: #e8efe4; text-decoration: underline; }
.markdown-body :deep(.report-citation a:focus-visible) { outline: 2px solid #669477; outline-offset: 2px; }
.markdown-body :deep(hr) { border-color: #e7e0d5; }


@keyframes blink { 50% { opacity: .35; } }
@keyframes radar { from { transform: scale(.72); opacity: .8; } to { transform: scale(1.08); opacity: 0; } }

@media (min-width: 761px) and (max-width: 1100px) {
  .workspace-body { grid-template-columns: 220px minmax(0, 1fr); }
  .trace-panel { padding: 20px 12px; }
  .report-view { padding-inline: 18px; }
  .report-paper { padding-inline: 28px; }
}

@media (max-width: 760px) {
  .workspace-header { gap: 8px; padding-inline: 12px; }
  .workspace-brand, .header-divider { display: none; }
  .header-topic strong { max-width: 46vw; }
  .run-state { display: none; }
  .workspace-body { grid-template-columns: 1fr; overflow-y: auto; }
  .trace-panel { min-height: auto; max-height: 260px; border-right: 0; border-bottom: 1px solid rgba(182,220,202,.11); }
  .trace-footer { display: none; }
  .content-panel { overflow: visible; }
  .report-view { padding: 20px 12px 70px; }
  .report-paper { padding: 34px 20px 50px; }
  .markdown-body { font-size: 14px; }
  .markdown-body :deep(h1) { font-size: 24px; }
  .markdown-body :deep(h2) { font-size: 19px; }
  .activity-card { min-width: 0; width: 100%; }
}
</style>
