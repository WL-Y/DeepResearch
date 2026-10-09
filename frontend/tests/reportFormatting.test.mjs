import assert from 'node:assert/strict'
import test from 'node:test'
import { marked } from 'marked'
import { limitCitationGroups, normalizeReportMarkdown, renderReportMarkdown } from '../src/utils/reportFormatting.ts'

test('removes generated source-type suffixes from legacy references while preserving prose and links', () => {
  const reference = '1. [FewMMBench](https://arxiv.org/abs/2602.21854)'
  const report = '# Report\n\n正文讨论一手论文与预印本。\n\n## 核心来源\n\n'
    + reference + ' — 一手论文·预印本\n2. [Survey](https://example.org/survey) — 综述'
  const formatted = normalizeReportMarkdown(report)
  assert.ok(formatted.includes(reference + '\n2. [Survey](https://example.org/survey)'))
  assert.ok(formatted.includes('正文讨论一手论文与预印本。'))
  assert.ok(!formatted.includes(' — 一手论文·预印本'))
  const code = '## 核心来源\n\n```text\n' + reference + ' — 一手论文·预印本\n```'
  assert.ok(normalizeReportMarkdown(code).includes(reference + ' — 一手论文·预印本'))
})

test('caps each citation location at two without changing targets or separate claims', () => {
  const c = n => `[${n}](https://example.org/${n} "citation")`
  const source = `第一句${c(1)}${c(2)}${c(3)}。第二句${c(4)}，${c(5)}、${c(6)}。`
  const limited = `第一句${c(1)}${c(2)}。第二句${c(4)}${c(5)}。`
  assert.equal(limitCitationGroups(source), limited)
  assert.equal(normalizeReportMarkdown(source), limited)
  assert.equal((renderReportMarkdown(source).match(/<sup /g) || []).length, 4)
  assert.equal(limitCitationGroups(`${c(1)}\n${c(2)} ${c(3)}`), `${c(1)}${c(2)}`)
  assert.equal(limitCitationGroups(`${c(1)}${c(2)}\n\n${c(3)}`), `${c(1)}${c(2)}\n\n${c(3)}`)
})

test('citation cap deduplicates and leaves code and source lists untouched', () => {
  const c = n => `[${n}](https://example.org/${n} "citation")`
  assert.equal(limitCitationGroups(`${c(1)}${c(1)}${c(2)}${c(3)}`), `${c(1)}${c(2)}`)
  for (const source of ['`' + c(1) + c(2) + c(3) + '`', '```text\n' + c(1) + c(2) + c(3) + '\n```',
    '1. [第一篇](https://example.org/1)\n2. [第二篇](https://example.org/2)\n3. [第三篇](https://example.org/3)']) {
    assert.equal(limitCitationGroups(source), source)
  }
})

test('renders portable citation links as accessible superscripts', () => {
  const source = '结果为65%[1](https://example.org/one "citation")[15](https://example.org/two "citation")。\n\n1. [论文](https://example.org/one)'
  const html = renderReportMarkdown(normalizeReportMarkdown(source))
  assert.equal((html.match(/<sup class="report-citation">/g) || []).length, 2)
  assert.match(html, /target="_blank" rel="noopener noreferrer"/)
  assert.match(html, /aria-label="引用来源 15/)
  assert.match(html, />15<\/a><\/sup>/)
  assert.match(html, /href="https:\/\/example.org\/one"/)
})

test('does not turn ordinary links, code or unknown markers into citations', () => {
  const source = '[15](https://example.org) [论文](https://example.org "citation") `x[1]`\n\n旧报告没有引用位置。未核实[99]。'
  const html = renderReportMarkdown(source)
  assert.doesNotMatch(html, /<sup/)
  assert.match(html, /<code>x\[1\]<\/code>/)
  assert.match(html, /未核实\[99\]/)
})

test('normalizes heading spacing and a whole-report Markdown fence', () => {
  const result = normalizeReportMarkdown('```markdown\r\n# 标题\r\n## **执行摘要**\r\n研究内容。\r\n```')
  assert.equal(result, '# 标题\n\n## 执行摘要\n\n研究内容。')
})

test('splits long Chinese prose without altering words, numbers or negation', () => {
  const paragraph = '方法 A 在 20 次实验中达到 65%，基线为 35%，相差 30 个百分点，但不代表已经独立复现。'.repeat(12)
  const result = normalizeReportMarkdown(paragraph)
  assert.ok(result.includes('\n\n'))
  assert.equal(result.replace(/\s/g, ''), paragraph.replace(/\s/g, ''))
  assert.ok(result.split('\n\n').every(part => part.endsWith('。')))
  assert.equal(normalizeReportMarkdown(result), result)
})

test('does not break quotes or parentheses across paragraphs', () => {
  const quoted = '“' + '这是原文中的完整引述，不能拆散。'.repeat(30) + '”'
  const paragraph = '背景说明。'.repeat(30) + quoted + '后续分析。'.repeat(30)
  const result = normalizeReportMarkdown(paragraph)
  assert.ok(result.includes(quoted))
  const parenthesized = '（' + '括号中的完整说明。'.repeat(40) + '）'
  assert.ok(normalizeReportMarkdown(paragraph + parenthesized).includes(parenthesized))
})

test('preserves code, nested lists, equations, tables and inline links', () => {
  const blocks = [
    '```python\n# Not a heading\nvalue = 3.14\n\nprint(value)\n```',
    '1. 第一步\n   - 子项一\n   - 子项二\n2. 第二步',
    '| 方法 | 结果 |\n| --- | --- |\n| A | 65% |',
    '$$\nP(z_{t+1}|a_t,z_t)\n$$',
    '参见 [原文](https://example.org/a?q=3.14)。'.repeat(30),
    '**加粗文字与结论。'.repeat(25) + '**',
    '> 引用文字。\n> 第二行。',
  ]
  for (const block of blocks) {
    assert.equal(normalizeReportMarkdown(block), block)
  }
})

test('preserves reference-style links, hard breaks and indented code', () => {
  for (const source of ['正文 [论文][paper]\n\n[paper]: https://example.org/paper', '第一行  \n第二行', '    # Code heading\n    value = 42']) {
    assert.equal(normalizeReportMarkdown(source), source)
  }
})

test('does not invent sentence boundaries in English prose or giant sentences', () => {
  for (const paragraph of ['Use v3.14 and example.org for this experiment. '.repeat(20), '不可随意拆开这个句子'.repeat(60) + '。']) {
    assert.equal(normalizeReportMarkdown(paragraph), paragraph)
  }
})

test('wraps tables in a keyboard-accessible scrolling region', () => {
  const html = renderReportMarkdown('| 方法 | 结果 |\n| --- | --- |\n| A | 65% |')
  assert.match(html, /class="report-table-scroll"/)
  assert.match(html, /tabindex="0"/)
  assert.match(html, /<table>/)
  assert.match(html, /65%/)
})

test('empty input stays empty and soft line breaks stay in one paragraph', () => {
  assert.equal(normalizeReportMarkdown(''), '')
  const html = renderReportMarkdown(normalizeReportMarkdown('# 标题\n正文第一行\n正文第二行'))
  assert.equal((html.match(/<p>/g) || []).length, 1)
  assert.doesNotMatch(html, /<br/)
  assert.equal(marked.lexer(normalizeReportMarkdown('# 标题\n\n- A\n- B')).filter(t => t.type === 'list').length, 1)
})
