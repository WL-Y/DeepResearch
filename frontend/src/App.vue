<template>
  <div class="app-shell">
    <div class="ambient ambient-one"></div>
    <div class="ambient ambient-two"></div>

    <header class="topbar">
      <button class="brand" @click="handleNewResearch">
        <span class="brand-mark">
          <svg viewBox="0 0 28 28" aria-hidden="true">
            <path d="M7 7.5h9.5a4.5 4.5 0 0 1 0 9H12l-5 4v-13Z" />
            <path d="m17.5 15.5 4 4M10 11h5M10 14h3" />
          </svg>
        </span>
        <span>
          <strong>DeepResearch</strong>
          <small>智能研究工作台</small>
        </span>
      </button>

      <div class="topbar-status">
        <span class="status-dot"></span>
        本地研究服务已连接
      </div>
    </header>

    <div v-if="!showModal" class="workspace-shell">
      <aside class="history-panel">
        <div class="history-header">
          <span>最近研究</span>
          <span class="history-count">{{ history.length }}</span>
        </div>

        <div v-if="history.length" class="history-list">
          <div
            v-for="item in history"
            :key="item.id"
            class="history-entry"
          >
            <button class="history-item" @click="handleOpenHistory(item.id)">
              <span class="history-icon" :class="`is-${item.status}`">
                <svg v-if="item.status === 'completed'" viewBox="0 0 20 20">
                  <path d="m5 10 3 3 7-7" />
                </svg>
                <svg v-else viewBox="0 0 20 20">
                  <circle cx="10" cy="10" r="5.5" />
                  <path d="M10 7v3l2 1.5" />
                </svg>
              </span>
              <span class="history-copy">
                <span class="history-topic">{{ item.topic }}</span>
                <span class="history-meta">
                  {{ historyStatus(item.status) }} · {{ formatDate(item.updated_at) }}
                </span>
              </span>
              <svg class="history-arrow" viewBox="0 0 20 20">
                <path d="m8 5 5 5-5 5" />
              </svg>
            </button>
            <button
              v-if="isHistoryDeletable(item.status)"
              class="history-delete"
              :aria-label="`删除研究：${item.topic}`"
              title="删除这条研究"
              @click="requestHistoryDelete(item)"
            >
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M4 6h12M8 3h4l1 3H7l1-3Zm-2 3 1 11h6l1-11M8.5 9v5M11.5 9v5" />
              </svg>
            </button>
          </div>
        </div>

        <div v-else class="history-empty">
          <span>暂无研究记录</span>
          <small>完成的研究会保存在这里</small>
        </div>

        <div class="history-summary">
          <div>
            <strong>{{ completedCount }}</strong>
            <span>已完成</span>
          </div>
          <div>
            <strong>{{ history.length }}</strong>
            <span>总任务</span>
          </div>
        </div>
      </aside>

      <main class="home-main">
        <section class="hero-section">
          <div class="eyebrow">
            <span class="eyebrow-spark">✦</span>
            面向复杂问题的自主研究
          </div>
          <h1>从一个问题开始，<br /><span>获得一份可靠的研究报告。</span></h1>
          <p>
            自动规划研究路径、搜索多源信息、交叉整理证据，最终生成结构清晰、来源可追溯的研究报告。
          </p>
        </section>

        <section class="composer-card">
          <div class="composer-head">
            <span class="composer-label">你想研究什么？</span>
            <span class="composer-mode"><i></i> 深度模式</span>
          </div>
          <textarea
            ref="topicInput"
            v-model="topic"
            rows="4"
            maxlength="500"
            placeholder="描述研究主题、关注角度和期望输出。例如：比较 YOLOv8 与 YOLO11 的性能、部署成本和适用场景……"
            autofocus
            @keydown.enter.exact.prevent="handleStart"
          ></textarea>
          <div class="composer-footer">
            <span>Enter 开始 · Shift + Enter 换行</span>
            <button
              class="start-btn"
              :disabled="!topic.trim() || isLoading || isResuming"
              @click="handleStart"
            >
              开始研究
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M4 10h12M12 6l4 4-4 4" />
              </svg>
            </button>
          </div>
        </section>

        <section class="examples-section">
          <div class="section-label">试试这些研究方向</div>
          <div class="example-grid">
            <button
              v-for="example in examples"
              :key="example.title"
              class="example-card"
              title="每次点击随机推荐一个研究主题"
              @click="selectExample(example)"
            >
              <span class="example-icon">{{ example.icon }}</span>
              <span>
                <strong>{{ example.title }}</strong>
                <small>{{ example.description }}</small>
              </span>
              <svg viewBox="0 0 20 20"><path d="m8 5 5 5-5 5" /></svg>
            </button>
          </div>
        </section>

        <section class="pipeline-section">
          <div class="pipeline-item">
            <span class="pipeline-number">01</span>
            <div><strong>智能规划</strong><small>拆解问题与研究维度</small></div>
          </div>
          <span class="pipeline-line"></span>
          <div class="pipeline-item">
            <span class="pipeline-number">02</span>
            <div><strong>多源检索</strong><small>搜索、筛选与交叉验证</small></div>
          </div>
          <span class="pipeline-line"></span>
          <div class="pipeline-item">
            <span class="pipeline-number">03</span>
            <div><strong>综合报告</strong><small>提炼结论与相关来源</small></div>
          </div>
        </section>
      </main>
    </div>

    <ResearchModal
      :research-id="researchId"
      :is-open="showModal"
      :topic="topic"
      :is-loading="isLoading"
      :progress="progress"
      :progress-text="progressText"
      :tasks="tasks"
      :report="report"
      :error="error"
      :can-resume="canResume"
      :is-resuming="isResuming"
      @close="showModal = false"
      @new-research="handleNewResearch"
      @cancel="cancelResearch"
      @resume="continueResearch"
    />

    <Transition name="dialog-fade">
      <div
        v-if="pendingDelete"
        class="confirm-backdrop"
        @click.self="closeDeleteDialog"
      >
        <section
          class="confirm-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="delete-dialog-title"
        >
          <span class="confirm-icon">
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="M4 6h12M8 3h4l1 3H7l1-3Zm-2 3 1 11h6l1-11M8.5 9v5M11.5 9v5" />
            </svg>
          </span>
          <h2 id="delete-dialog-title">删除这条历史研究？</h2>
          <p>“{{ pendingDelete.topic }}”的报告、研究步骤和相关记录都会永久删除。</p>
          <div v-if="deleteError" class="delete-error">{{ deleteError }}</div>
          <div class="confirm-actions">
            <button :disabled="isDeleting" @click="closeDeleteDialog">取消</button>
            <button class="danger-btn" :disabled="isDeleting" @click="confirmHistoryDelete">
              {{ isDeleting ? '正在删除…' : '确认删除' }}
            </button>
          </div>
        </section>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import ResearchModal from './components/ResearchModal.vue'
import { useResearch, type ResearchHistoryItem } from './composables/useResearch'

const topic = ref('')
const topicInput = ref<HTMLTextAreaElement | null>(null)
const showModal = ref(false)
const pendingDelete = ref<ResearchHistoryItem | null>(null)
const isDeleting = ref(false)
const deleteError = ref('')

interface ResearchExample {
  icon: string
  title: string
  description: string
  prompts: string[]
}

const examples: ResearchExample[] = [
  {
    icon: '⌁',
    title: '技术选型',
    description: '框架、模型与部署方案对比',
    prompts: [
      '比较当前主流大语言模型推理框架的性能、部署成本与适用场景',
      '对比 React、Vue 与 Svelte 在中大型前端项目中的开发效率和维护成本',
      '比较 PostgreSQL、MySQL 与 MongoDB 在高并发业务中的适用边界',
      '研究本地部署大语言模型时 Ollama、vLLM 与 llama.cpp 的选型差异',
      '对比 YOLO11、RT-DETR 与 Grounding DINO 的检测性能和部署要求',
      '分析 Kubernetes 与轻量级容器编排方案在中小团队中的成本收益',
    ],
  },
  {
    icon: '◫',
    title: '行业洞察',
    description: '市场现状、玩家与未来趋势',
    prompts: [
      '研究生成式 AI 在教育行业的应用现状、主要参与者与未来三年趋势',
      '分析中国新能源汽车产业链的竞争格局、关键企业与增长机会',
      '研究具身智能行业的商业化进展、主要玩家和未来五年趋势',
      '分析 AI 编程工具市场的产品差异、商业模式和竞争壁垒',
      '研究低空经济产业的政策环境、核心环节与潜在风险',
      '分析跨境电商 SaaS 市场的客户需求、竞争格局与发展空间',
    ],
  },
  {
    icon: '⌬',
    title: '学术前沿',
    description: '关键论文、方法与研究空白',
    prompts: [
      '梳理多模态大模型在视觉推理方向的关键论文、技术路线与研究空白',
      '研究检索增强生成在降低大模型幻觉方面的最新方法与评测结论',
      '梳理世界模型在机器人决策中的主要技术路线和开放问题',
      '研究小样本目标检测近五年的代表性方法、数据集与性能瓶颈',
      '分析大语言模型智能体记忆机制的分类、代表论文与研究空白',
      '梳理扩散模型在视频生成方向的关键进展和仍待解决的问题',
    ],
  },
]

const lastExampleIndex = new Map<string, number>()

const selectExample = (example: ResearchExample) => {
  const previousIndex = lastExampleIndex.get(example.title)
  let nextIndex = Math.floor(Math.random() * example.prompts.length)
  if (example.prompts.length > 1 && nextIndex === previousIndex) {
    nextIndex = (nextIndex + 1) % example.prompts.length
  }
  lastExampleIndex.set(example.title, nextIndex)
  topic.value = example.prompts[nextIndex]
}

const {
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
} = useResearch()

const completedCount = computed(
  () => history.value.filter((item) => item.status === 'completed').length
)

const handleStart = async () => {
  if (!topic.value.trim() || isLoading.value || isResuming.value) return
  showModal.value = true
  await startResearch(topic.value)
}

const handleNewResearch = async () => {
  showModal.value = false
  topic.value = ''
  await nextTick()
  topicInput.value?.focus()
}

const handleOpenHistory = async (id: string) => {
  const restoredTopic = await loadResearch(id)
  if (restoredTopic) {
    topic.value = restoredTopic
    showModal.value = true
  }
}

const isHistoryDeletable = (status: string) =>
  ['completed', 'error', 'cancelled'].includes(status)

const requestHistoryDelete = (item: ResearchHistoryItem) => {
  pendingDelete.value = item
  deleteError.value = ''
}

const closeDeleteDialog = () => {
  if (isDeleting.value) return
  pendingDelete.value = null
  deleteError.value = ''
}

const confirmHistoryDelete = async () => {
  if (!pendingDelete.value || isDeleting.value) return
  isDeleting.value = true
  deleteError.value = ''
  try {
    await deleteResearch(pendingDelete.value.id)
    pendingDelete.value = null
  } catch (caught: any) {
    deleteError.value = caught.message || '删除失败，请稍后重试'
  } finally {
    isDeleting.value = false
  }
}

const historyStatus = (status: string) => ({
  resuming: '恢复中',
  planning: '正在规划',
  executing: '研究进行中',
  reporting: '正在撰写',
  completed: '研究完成',
  cancelled: '已取消',
  error: '研究中断',
}[status] || status)

const formatDate = (value: string) => {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
  }).format(date)
}

onMounted(async () => {
  await loadHistory()
  const restoredTopic = await resumeLastResearch()
  if (restoredTopic) {
    topic.value = restoredTopic
    showModal.value = true
  }
})
</script>

<style scoped>
.app-shell {
  --bg: #f7f3eb;
  --panel: rgba(255, 253, 248, 0.88);
  --line: rgba(92, 80, 60, 0.13);
  --muted: #7d8177;
  --text: #2c342f;
  --accent: #3f8766;
  min-height: 100vh;
  position: relative;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
  -webkit-font-smoothing: antialiased;
}

.ambient {
  position: fixed;
  border-radius: 999px;
  filter: blur(110px);
  pointer-events: none;
  opacity: 0.5;
}

.ambient-one { width: 420px; height: 420px; top: -180px; right: 8%; background: #efd4b9; }
.ambient-two { width: 360px; height: 360px; bottom: -220px; left: 30%; background: #c8ddce; }

.topbar {
  height: 72px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 32px;
  border-bottom: 1px solid var(--line);
  background: rgba(250, 247, 240, 0.86);
  backdrop-filter: blur(20px);
  position: relative;
  z-index: 2;
}

.brand { display: flex; align-items: center; gap: 12px; border: 0; background: none; color: inherit; cursor: pointer; text-align: left; }
.brand-mark { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 12px; color: #fffdf8; background: linear-gradient(145deg, #5da37d, #34795b); box-shadow: 0 8px 26px rgba(63, 135, 102, .18); }
.brand-mark svg { width: 23px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.brand strong, .brand small { display: block; }
.brand strong { font-size: 15px; letter-spacing: .01em; }
.brand small { margin-top: 2px; color: var(--muted); font-size: 11px; }
.topbar-status { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
.status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 5px rgba(63, 135, 102, .09); }

.workspace-shell { min-height: calc(100vh - 72px); display: grid; grid-template-columns: 292px 1fr; position: relative; z-index: 1; }
.history-panel { border-right: 1px solid var(--line); padding: 22px 16px; background: rgba(242, 236, 225, .7); display: flex; flex-direction: column; min-height: calc(100vh - 72px); }
.history-header { display: flex; align-items: center; justify-content: space-between; margin: 8px 10px 10px; color: #666c65; font-size: 12px; font-weight: 650; }
.history-count { min-width: 21px; padding: 2px 7px; border-radius: 20px; background: rgba(78,68,52,.06); color: var(--muted); text-align: center; }
.history-list { display: grid; gap: 5px; }
.history-entry { position: relative; min-width: 0; }
.history-item { width: 100%; padding: 11px 9px; display: grid; grid-template-columns: 30px 1fr 16px; align-items: center; gap: 9px; border: 1px solid transparent; border-radius: 11px; color: inherit; background: transparent; cursor: pointer; text-align: left; }
.history-item:hover { border-color: var(--line); background: rgba(255,253,248,.72); }
.history-icon { width: 28px; height: 28px; display: grid; place-items: center; border-radius: 8px; color: #7c887f; background: rgba(78,68,52,.05); }
.history-icon.is-completed { color: var(--accent); background: rgba(63, 135, 102, .09); }
.history-icon.is-error { color: #f08e8e; }
.history-icon svg, .history-arrow { width: 15px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.history-copy, .history-topic, .history-meta { display: block; min-width: 0; }
.history-topic { overflow: hidden; color: #464d47; font-size: 12.5px; font-weight: 560; text-overflow: ellipsis; white-space: nowrap; }
.history-meta { margin-top: 4px; color: #929087; font-size: 10.5px; }
.history-arrow { color: #aaa398; }
.history-delete { width: 28px; height: 28px; position: absolute; top: 50%; right: 6px; display: grid; place-items: center; border: 1px solid transparent; border-radius: 8px; color: #a0998f; background: #f6f1e8; cursor: pointer; opacity: 0; transform: translateY(-50%); transition: .18s ease; }
.history-delete svg { width: 14px; fill: none; stroke: currentColor; stroke-width: 1.45; stroke-linecap: round; stroke-linejoin: round; }
.history-entry:hover .history-delete, .history-delete:focus-visible { opacity: 1; }
.history-entry:hover .history-arrow { opacity: 0; }
.history-delete:hover { border-color: rgba(177,73,73,.16); color: #b04f4f; background: #fff5f1; }
.history-empty { margin: 32px 10px; display: grid; gap: 5px; color: #85877f; font-size: 12px; }
.history-empty small { color: #aaa59b; }
.history-summary { margin-top: auto; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding-top: 18px; border-top: 1px solid var(--line); }
.history-summary div { padding: 12px; border: 1px solid rgba(92,80,60,.06); border-radius: 10px; background: rgba(255,253,248,.56); }
.history-summary strong, .history-summary span { display: block; }
.history-summary strong { color: #3f5147; font-size: 17px; }
.history-summary span { margin-top: 3px; color: #929087; font-size: 10px; }

.home-main { width: min(960px, calc(100% - 64px)); margin: 0 auto; padding: 70px 0 48px; }
.hero-section { max-width: 790px; }
.eyebrow { display: inline-flex; align-items: center; gap: 8px; margin-bottom: 20px; padding: 7px 11px; border: 1px solid rgba(63,135,102,.16); border-radius: 999px; color: #65766c; background: rgba(63,135,102,.055); font-size: 11px; letter-spacing: .05em; }
.eyebrow-spark { color: var(--accent); }
.hero-section h1 { margin: 0; font-family: Georgia, 'Times New Roman', 'Noto Serif SC', serif; font-size: clamp(40px, 5vw, 68px); font-weight: 500; line-height: 1.08; letter-spacing: -.035em; }
.hero-section h1 span { color: #8e9188; }
.hero-section p { max-width: 680px; margin: 22px 0 0; color: #737a73; font-size: 15px; line-height: 1.8; }

.composer-card { margin-top: 38px; padding: 18px; border: 1px solid rgba(102,86,62,.14); border-radius: 18px; background: rgba(255,253,248,.94); box-shadow: 0 24px 64px rgba(84,67,43,.10); }
.composer-head, .composer-footer { display: flex; align-items: center; justify-content: space-between; }
.composer-head { padding: 0 3px 12px; }
.composer-label { color: #4b534e; font-size: 12px; font-weight: 650; }
.composer-mode { display: flex; align-items: center; gap: 6px; color: #858a82; font-size: 10.5px; }
.composer-mode i { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
.composer-card textarea { width: 100%; min-height: 116px; resize: none; border: 0; outline: 0; padding: 12px 4px; color: #303832; background: transparent; font: inherit; font-size: 17px; line-height: 1.65; }
.composer-card textarea::placeholder { color: #aaa79e; }
.composer-footer { padding-top: 14px; border-top: 1px solid rgba(92,80,60,.09); }
.composer-footer > span { color: #aaa69d; font-size: 10.5px; }
.start-btn { padding: 11px 16px; display: flex; align-items: center; gap: 9px; border: 0; border-radius: 10px; color: #fffdf8; background: linear-gradient(135deg, #4b9270, #337758); cursor: pointer; font-size: 12px; font-weight: 750; box-shadow: 0 10px 24px rgba(63,135,102,.18); }
.start-btn:hover:not(:disabled) { transform: translateY(-1px); filter: brightness(1.06); }
.start-btn:disabled { opacity: .35; cursor: not-allowed; }
.start-btn svg { width: 16px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; }

.examples-section { margin-top: 30px; }
.section-label { margin-bottom: 11px; color: #8a887f; font-size: 10.5px; font-weight: 650; letter-spacing: .08em; text-transform: uppercase; }
.example-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.example-card { min-width: 0; padding: 14px; display: grid; grid-template-columns: 34px 1fr 14px; align-items: center; gap: 10px; border: 1px solid rgba(92,80,60,.1); border-radius: 12px; color: inherit; background: rgba(255,253,248,.66); cursor: pointer; text-align: left; }
.example-card:hover { border-color: rgba(63,135,102,.22); background: rgba(255,253,248,.96); transform: translateY(-2px); box-shadow: 0 10px 24px rgba(84,67,43,.07); }
.example-icon { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 9px; color: #3f8766; background: rgba(63,135,102,.08); font-size: 16px; }
.example-card strong, .example-card small { display: block; }
.example-card strong { color: #4a504b; font-size: 12px; }
.example-card small { margin-top: 4px; overflow: hidden; color: #929087; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.example-card svg { width: 14px; fill: none; stroke: #aaa398; stroke-width: 1.7; }

.pipeline-section { margin-top: 40px; padding-top: 20px; display: flex; align-items: center; border-top: 1px solid var(--line); }
.pipeline-item { display: flex; align-items: center; gap: 10px; }
.pipeline-number { color: #a29d92; font: 10px/1 monospace; }
.pipeline-item strong, .pipeline-item small { display: block; }
.pipeline-item strong { color: #616861; font-size: 11px; }
.pipeline-item small { margin-top: 3px; color: #96938a; font-size: 9.5px; }
.pipeline-line { flex: 1; height: 1px; margin: 0 22px; background: linear-gradient(90deg, rgba(63,135,102,.2), rgba(92,80,60,.07)); }

button, .example-card, .history-item { transition: .2s ease; }

.confirm-backdrop { position: fixed; inset: 0; z-index: 1200; display: grid; place-items: center; padding: 20px; background: rgba(54,48,39,.24); backdrop-filter: blur(5px); }
.confirm-dialog { width: min(420px, 100%); padding: 28px; border: 1px solid rgba(92,80,60,.13); border-radius: 18px; color: #303832; background: #fffdf8; box-shadow: 0 30px 90px rgba(82,65,42,.22); }
.confirm-icon { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 12px; color: #b04f4f; background: #fff0eb; }
.confirm-icon svg { width: 20px; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.confirm-dialog h2 { margin: 18px 0 0; font-family: Georgia, 'Noto Serif SC', serif; font-size: 22px; font-weight: 560; }
.confirm-dialog p { margin: 10px 0 0; color: #777970; font-size: 13px; line-height: 1.7; }
.delete-error { margin-top: 14px; padding: 9px 11px; border-radius: 8px; color: #a64242; background: #fff0eb; font-size: 11px; }
.confirm-actions { margin-top: 24px; display: flex; justify-content: flex-end; gap: 9px; }
.confirm-actions button { padding: 10px 15px; border: 1px solid rgba(92,80,60,.13); border-radius: 9px; color: #666b65; background: #f8f4ec; cursor: pointer; font-size: 12px; font-weight: 650; }
.confirm-actions button:hover:not(:disabled) { background: #f1ebe1; }
.confirm-actions .danger-btn { border-color: #a84b43; color: #fff; background: #a84b43; }
.confirm-actions .danger-btn:hover:not(:disabled) { background: #963e38; }
.confirm-actions button:disabled { opacity: .55; cursor: wait; }
.dialog-fade-enter-active, .dialog-fade-leave-active { transition: opacity .18s ease; }
.dialog-fade-enter-active .confirm-dialog, .dialog-fade-leave-active .confirm-dialog { transition: transform .18s ease, opacity .18s ease; }
.dialog-fade-enter-from, .dialog-fade-leave-to { opacity: 0; }
.dialog-fade-enter-from .confirm-dialog, .dialog-fade-leave-to .confirm-dialog { opacity: 0; transform: translateY(8px) scale(.98); }

@media (max-width: 900px) {
  .workspace-shell { grid-template-columns: 230px 1fr; }
  .home-main { width: calc(100% - 40px); padding-top: 50px; }
  .example-grid { grid-template-columns: 1fr; }
  .pipeline-section { display: none; }
}

@media (max-width: 680px) {
  .topbar { height: 62px; padding: 0 18px; }
  .topbar-status { display: none; }
  .workspace-shell { display: block; }
  .history-panel { display: none; }
  .home-main { width: calc(100% - 30px); padding: 42px 0 30px; }
  .hero-section h1 { font-size: 40px; }
  .hero-section p { font-size: 13px; }
  .composer-footer > span { display: none; }
  .composer-footer { justify-content: flex-end; }
}
</style>
