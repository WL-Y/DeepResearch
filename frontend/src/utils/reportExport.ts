export type ExportFormat = 'md' | 'txt' | 'html' | 'docx' | 'pdf'

export const exportFormats: { value: ExportFormat; label: string; description: string }[] = [
  { value: 'docx', label: 'Word 文档', description: '可编辑，适合继续整理' },
  { value: 'pdf', label: 'PDF 文档', description: '固定版式，适合分享与打印' },
  { value: 'md', label: 'Markdown', description: '保留标记，适合笔记工具' },
  { value: 'html', label: 'HTML 网页', description: '独立网页，可离线阅读' },
  { value: 'txt', label: '纯文本', description: '去除格式，保留来源链接' },
]

export function exportFilename(topic: string, format: ExportFormat): string {
  let name = topic.replace(/[\\/:*?"<>|\u0000-\u001f\u007f]/g, '-').replace(/^[ .]+|[ .]+$/g, '').slice(0, 80).replace(/[ .]+$/g, '')
  if (!name || /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(name)) name = 'research-report'
  return `${name}.${format}`
}

export async function createReportDownload(topic: string, content: string, format: ExportFormat, signal?: AbortSignal) {
  if (!content.trim()) throw new Error('报告内容为空，无法导出')
  const filename = exportFilename(topic, format)
  // Keep the existing offline Markdown export available when the API is down.
  if (format === 'md') return { blob: new Blob([content], { type: 'text/markdown;charset=utf-8' }), filename }
  const response = await fetch('/api/reports/export', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ topic, content, format }), signal,
  })
  if (!response.ok) {
    let message = `导出失败（${response.status}），请重试或选择 Markdown`
    try {
      const data = await response.json()
      if (typeof data.detail === 'string') message = data.detail
      else if (response.status === 422) message = '报告内容过长或格式无效，请选择 Markdown 导出'
    } catch { /* Non-JSON proxy errors must still be visible to the user. */ }
    throw new Error(message)
  }
  const blob = await response.blob()
  if (!blob.size) throw new Error('导出文件为空，请重试')
  const expectedTypes = { txt: 'text/plain', html: 'text/html', pdf: 'application/pdf', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }
  if (blob.type.split(';')[0] !== expectedTypes[format]) {
    throw new Error('服务返回的文件格式不正确，请确认后端已更新并重启')
  }
  return { blob, filename }
}
