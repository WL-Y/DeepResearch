import assert from 'node:assert/strict'
import test from 'node:test'
import { createReportDownload, exportFilename, exportFormats } from '../src/utils/reportExport.ts'

test('offers five actual formats and safe filenames', () => {
  assert.deepEqual(exportFormats.map(f => f.value).sort(), ['docx', 'html', 'md', 'pdf', 'txt'])
  assert.equal(exportFilename('研究/报告?', 'pdf'), '研究-报告-.pdf')
  assert.equal(exportFilename('CON', 'docx'), 'research-report.docx')
})

test('Markdown download works offline and preserves formatted report', async () => {
  const content = '# 主题\n\n首段内容。\n\n第二段。'
  const result = await createReportDownload('主题', content, 'md')
  assert.equal(await result.blob.text(), content)
  assert.equal(result.filename, '主题.md')
})

test('conversion preserves legacy report content including previously saved questions', async t => {
  const content = '# 研究\n\n正文\n\n### Q: 追问\n\n补充答案'
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/reports/export')
    assert.deepEqual(JSON.parse(options.body), { topic: '研究', content, format: 'pdf' })
    return new Response('%PDF-test', { headers: { 'Content-Type': 'application/pdf' } })
  })
  const result = await createReportDownload('研究', content, 'pdf')
  assert.equal(result.blob.type, 'application/pdf')
  assert.equal(result.filename, '研究.pdf')
})

test('HTTP failure does not download an error JSON as a document', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ detail: '导出失败' }), { status: 500 }))
  await assert.rejects(createReportDownload('主题', '# 内容', 'docx'), /导出失败/)
})

test('empty input and empty files are rejected', async t => {
  await assert.rejects(createReportDownload('主题', ' ', 'txt'), /为空/)
  t.mock.method(globalThis, 'fetch', async () => new Response(''))
  await assert.rejects(createReportDownload('主题', '# 内容', 'html'), /为空/)
})

test('a proxy HTML page cannot masquerade as a PDF', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('<html>SPA fallback</html>', { headers: { 'Content-Type': 'text/html' } }))
  await assert.rejects(createReportDownload('主题', '# 内容', 'pdf'), /文件格式不正确/)
})
