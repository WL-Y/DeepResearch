<template>
  <details class="source-details" @toggle="onToggle">
    <summary>检索来源详情 <span>查看渠道与原文读取情况</span></summary>
    <div class="source-details-body">
      <p>以下包含检索候选资料，不等同于报告实际引用清单。同一论文经不同渠道发现只计一项；元数据核对不代表结论经过独立验证。</p>
      <p v-if="loading" role="status">正在加载来源…</p>
      <div v-else-if="error" role="alert">{{ error }} <button @click="load">重试</button></div>
      <template v-else-if="loaded">
        <p v-if="!sources.length">这份历史研究没有保存可展示的检索来源详情。</p>
        <template v-else>
          <p class="source-count">
            {{ sources.length }} 项候选资料 · {{ fullTextCount }} 项全文已获取 ·
            {{ partialTextCount }} 项部分获取 · {{ unconfirmedTextCount }} 项正文完整性未确认 · {{ serviceTextCount }} 项服务提取 ·
            {{ snippetOnlyCount }} 项仅搜索摘要 · {{ metadataOnlyCount }} 项论文介绍页
          </p>
          <p v-if="retrievalWarnings.length" class="source-warning">检索渠道提示：{{ retrievalWarnings.join('；') }}。这些提示属于检索渠道，不表示下面每篇资料都读取失败。</p>
          <p v-for="message in fallbackMessages" :key="message" class="source-recovery">{{ message }}</p>
          <ol>
            <li v-for="source in sources" :key="source.url">
              <a v-if="safeLink(source.url)" :href="safeLink(source.url)" target="_blank" rel="noopener noreferrer">{{ source.title || source.url }}</a>
              <strong v-else>{{ source.title }}</strong>
              <div class="source-tags">
                <span>{{ source.source_engines?.map(providerName).join(' / ') || '历史渠道未记录' }}</span>
                <span>{{ acquisitionName(source) }}</span>
                <span v-if="source.published_at">发布：{{ source.published_at.slice(0, 10) }}</span>
                <span v-if="source.publication_type === 'preprint'">预印本</span>
              </div>
              <small v-if="source.doi">DOI：{{ source.doi }}</small>
              <small v-if="source.fetched_at">读取时间：{{ source.fetched_at.slice(0, 10) }}</small>
              <small v-if="source.extracted_chars">
                已提取并缓存 {{ source.extracted_chars.toLocaleString() }} 字符。
              </small>
              <small v-for="decision in source.reading_decisions || []" :key="decision.task_id">
                任务 {{ decision.task_id }}：{{ decision.selected ? '已选入分析' : '未选入分析' }} · {{ decision.reason }}
              </small>
              <small v-if="source.analysis_reused">已复用本次研究中同一文档快照的全文笔记。</small>
              <small v-if="source.completeness_reason">完整性说明：{{ source.completeness_reason }}</small>
              <small v-if="source.analysis_version">
                {{ source.analysis_scope === 'extracted_full_text' ? '全文分块分析' : '已取得内容分析（不代表原文完整）' }}：
                {{ (source.analyzed_chars || 0).toLocaleString() }} / {{ (source.analysis_expected_chars || 0).toLocaleString() }} 字符，
                {{ source.analysis_completed_parts || 0 }} / {{ source.analysis_total_parts || 0 }} 块。
                {{ source.analysis_status === 'completed' ? '分析与汇总已完成。' : source.analysis_status === 'summarizing' ? '正文已处理，汇总待完成。' : '尚未全部完成。' }}
              </small>
              <small v-else>历史记录未保存模型实际处理量；检索预览 {{ (source.context_chars || 0).toLocaleString() }} 字符，不代表已分析全文。</small>
              <small v-if="source.analysis_coverage_verified">已核对全部提取文本的分块覆盖；不代表图片或扫描内容已识别。</small>
              <small v-if="source.analysis_missing_ranges?.length">待分析字符区间：{{ source.analysis_missing_ranges.map(([start, end]) => `${start + 1}–${end}`).join("，") }}</small>
              <small v-if="source.failure_category" class="source-warning">正文状态：{{ failureName(source.failure_category) }}</small>
              <a v-if="safeLink(source.fetched_url)" class="read-link" :href="safeLink(source.fetched_url)" target="_blank" rel="noopener noreferrer">实际读取地址 ↗</a>
              <small v-if="source.versions && source.versions.length > 1">已关联 {{ source.versions.length }} 个版本或入口，正文节选来自实际读取地址。</small>
            </li>
          </ol>
        </template>
      </template>
    </div>
  </details>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

interface Source {
  url: string
  title: string
  source_engines?: string[]
  content_status?: string
  has_content: boolean
  published_at?: string
  publication_type?: string
  doi?: string
  fetched_at?: string
  fetched_url?: string
  versions?: { url: string }[]
  warnings?: string[]
  fallbacks?: { provider: string; category: string; via: string; count: number }[]
  reading_decisions?: { task_id: number | string; selected: boolean; reason: string }[]
  analysis_reused?: boolean
  completeness_reason?: string
  analysis_coverage_verified?: boolean
  analysis_missing_ranges?: number[][]
  acquisition_status?: string
  context_status?: string
  extracted_chars?: number
  context_chars?: number
  evidence_chunk_count?: number
  failure_category?: string
  analysis_version?: number
  analysis_status?: string
  analysis_scope?: string
  analysis_expected_chars?: number
  analyzed_chars?: number
  analysis_total_parts?: number
  analysis_completed_parts?: number
}
const props = defineProps<{ researchId: string }>()
const sources = ref<Source[]>([])
const fullTextCount = ref(0)
const partialTextCount = ref(0)
const unconfirmedTextCount = ref(0)
const serviceTextCount = ref(0)
const snippetOnlyCount = ref(0)
const metadataOnlyCount = ref(0)
const loading = ref(false)
const loaded = ref(false)
const error = ref('')
let controller: AbortController | null = null
const safeLink = (value?: string) => {
  try {
    const url = new URL(value || '')
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined
  } catch { return undefined }
}
const providerName = (name: string) => ({ arxiv: 'arXiv', semantic_scholar: 'Semantic Scholar', openalex: 'OpenAlex', tavily: 'Tavily', duckduckgo: 'DuckDuckGo' }[name] || name)
const acquisitionName = (source: Source) => ({
  full_text_extracted: '正文已获取并缓存', partial_text_extracted: '正文仅部分获取',
  body_text_unconfirmed: '已取得正文，完整性未确认',
  service_extracted_text: '服务提取正文，原文完整性未确认', metadata_only: '仅论文介绍页或摘要页',
  read_failed: '正文获取失败，仅搜索摘要',
}[source.acquisition_status || ''] || (source.has_content ? '已获取来源内容' : '仅搜索摘要'))
const failureName = (name: string) => ({
  access_restricted: '需要登录、订阅或被站点拒绝', rate_limited: '来源限流',
  timeout: '读取超时', network_error: '网络连接失败', ocr_required: 'PDF需要OCR',
  unsupported_format: '正文格式暂不支持', read_budget_exceeded: '文件超过读取预算',
  not_found: '原始地址不存在', redirect_limit: '重定向次数过多',
}[name] || name)
const retrievalWarnings = computed(() => [...new Set(sources.value.flatMap(source => source.warnings || []))].map(warning => {
  const [provider, category] = warning.split(':').map(part => part.trim())
  return category ? `${providerName(provider)}：${failureName(category)}` : warning
}))
const fallbackMessages = computed(() => [...new Set(sources.value.flatMap(source => (source.fallbacks || []).map(item =>
  `${providerName(item.provider)} 接口${failureName(item.category)}；已通过 ${providerName(item.via)} 定向检索找到 ${item.count} 个论文入口，继续读取。原接口尚未恢复。`
)))])
const load = async () => {
  if (loading.value) return
  loading.value = true
  error.value = ''
  controller = new AbortController()
  try {
    const response = await fetch(`/api/research/${encodeURIComponent(props.researchId)}/sources`, { signal: controller.signal })
    if (!response.ok) throw new Error('来源详情暂时无法加载，请稍后重试。')
    const data = await response.json()
    if (!Array.isArray(data.sources)) throw new Error('来源详情格式无效。')
    sources.value = data.sources
    fullTextCount.value = data.full_text_count || 0
    partialTextCount.value = data.partial_text_count || 0
    unconfirmedTextCount.value = data.unconfirmed_text_count || 0
    serviceTextCount.value = data.service_text_count || 0
    snippetOnlyCount.value = data.snippet_only_count || 0
    metadataOnlyCount.value = data.metadata_only_count || 0
    loaded.value = true
  } catch (caught: any) {
    if (caught.name !== 'AbortError') error.value = caught.message || '加载失败。'
  } finally { loading.value = false }
}
const onToggle = (event: Event) => {
  if ((event.target as HTMLDetailsElement).open && !loaded.value) void load()
}
onBeforeUnmount(() => controller?.abort())
</script>

<style scoped>
.source-details { margin-top: 20px; border: 1px solid #e3ddd2; border-radius: 12px; color: #535d54; background: #faf7f0; font-size: 13px; }
summary { padding: 16px 20px; cursor: pointer; font-weight: 600; }
summary span { margin-left: 8px; color: #929087; font-size: 11px; font-weight: 400; }
.source-details-body { padding: 0 20px 18px; }
p { color: #7b8077; line-height: 1.7; }
.source-count { color: #397458; }
ol { padding-left: 20px; }
li { padding: 12px 0; border-top: 1px solid #e9e3d8; overflow-wrap: anywhere; }
a { color: #397458; text-underline-offset: 3px; }
.source-tags { display: flex; flex-wrap: wrap; gap: 6px 14px; margin: 7px 0; font-size: 11px; color: #7b8077; }
small { display: block; margin-top: 5px; }
.read-link { display: inline-block; margin-top: 6px; font-size: 11px; }
.source-warning { color: #8b713d; }
.source-recovery { color: #397458; }
button { margin-left: 8px; border: 1px solid #d8e3d7; border-radius: 6px; background: #f0f5ed; padding: 4px 10px; color: #397458; cursor: pointer; }
</style>
