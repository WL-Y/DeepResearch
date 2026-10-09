import { marked, Renderer } from 'marked'

/** Legacy display fallback: no claim-level evidence is available in the UI.
 * New reports already contain evidence-ranked clusters from the backend.
 * Keep the original priority order, never invent or retarget a citation.
 */
export function limitCitationGroups(markdown: string): string {
  const citation = String.raw`\[\d+\]\(https?:\/\/[^\s)]+ "citation"\)`
  const pattern = new RegExp(
    String.raw`(?<code>\x60+)[\s\S]*?\k<code>|~~~[^\n]*\n[\s\S]*?~~~`
    + `|(?<group>${citation}(?:(?:[ \\t,，、]|\\n(?![ \\t]*\\n))*${citation})*)`
    + String.raw`|!?\[[^\]\n]*\]\([^\n]*?\)`, 'g',
  )
  return markdown.replace(pattern, (...args) => {
    const groups = args[args.length - 1] as { group?: string }
    if (!groups.group) return args[0]
    const seen = new Set<string>()
    const retained: string[] = []
    for (const match of groups.group.matchAll(new RegExp(citation, 'g'))) {
      const url = match[0].match(/\]\((\S+) "citation"\)$/)![1]
      if (seen.has(url)) continue
      seen.add(url)
      retained.push(match[0])
      if (retained.length === 2) break
    }
    return retained.join('')
  })
}

// Only split plain prose at complete Chinese sentences. Structured Markdown,
// numbers, links, equations and sentences inside quotations stay untouched.
function splitLongParagraph(text: string): string {
  if (text.length <= 360 || !/[\u3400-\u9fff]/u.test(text)) return text
  const pairs: Record<string, string> = { '“': '”', '‘': '’', '「': '」', '『': '』', '（': '）', '(': ')', '[': ']', '{': '}', '【': '】' }
  const stack: string[] = []
  const parts: string[] = []
  let start = 0
  for (let i = 0; i < text.length; i++) {
    const char = text[i]
    if (pairs[char]) stack.push(pairs[char])
    else if (char === stack[stack.length - 1]) stack.pop()
    const sentenceEnd = /[。！？]/u.test(char)
      || (/[”’」』]/u.test(char) && /[。！？]/u.test(text[i - 1] || ''))
    if (sentenceEnd && !stack.length && i + 1 - start >= 180 && text.length - i - 1 >= 100) {
      parts.push(text.slice(start, i + 1).trim())
      start = i + 1
    }
  }
  parts.push(text.slice(start).trim())
  return parts.join('\n\n')
}

/** Presentation-only normalization; never overwrite the stored research. */
export function normalizeReportMarkdown(markdown: string): string {
  const source = limitCitationGroups(markdown.replace(/\r\n?/g, '\n'))
  let tokens = marked.lexer(source, { gfm: true })
  const meaningful = tokens.filter(token => token.type !== 'space')
  // Some models incorrectly wrap the entire report in a Markdown code fence.
  if (meaningful.length === 1 && meaningful[0].type === 'code'
      && /^(?:markdown|md)$/i.test(meaningful[0].lang || '')
      && /^#\s+/.test(meaningful[0].text)) {
    tokens = marked.lexer(limitCitationGroups(meaningful[0].text), { gfm: true })
  }
  // Reference definitions live in lexer metadata rather than block tokens.
  // Preserve such documents verbatim instead of losing their link targets.
  if (Object.keys(tokens.links).length) return source
  let sourceSection = false
  return tokens.filter(token => token.type !== 'space').map(token => {
    if (token.type === 'heading') {
      const title = token.text.replace(/^\*\*([^*]+)\*\*$/, '$1')
      sourceSection = /^(?:核心来源|参考文献|参考资料|引用来源|references|sources)$/i.test(title.trim())
      return `${'#'.repeat(token.depth)} ${title}`
    }
    if (sourceSection && token.type === 'list'
        && !token.items.some((item: { tokens: { type: string }[] }) => item.tokens.some(part => part.type === 'code'))) {
      return token.raw.replace(/(^[ \t]*\d+\.\s+\[[^\n]*\]\(https?:\/\/[^\n]+\))[ \t]+—[ \t]*(?:一手论文|综述|官方资料|会议资料|第三方整理|网页资料)(?:·预印本)?[ \t]*$/gm, '$1').replace(/\n+$/, '')
    }
    if (token.type === 'paragraph' && token.tokens?.every(part => part.type === 'text')
        && !/[`$\\]/.test(token.raw) && !/ {2}\n/.test(token.raw)) {
      return splitLongParagraph(token.raw.replace(/\n+$/, ''))
    }
    return token.raw.replace(/\n+$/, '')
  }).join('\n\n')
}

const renderer = new Renderer()
const renderLink = renderer.link.bind(renderer)
renderer.link = (href: string, title: string | null | undefined, text: string) => {
  if (title === 'citation' && /^\d+$/.test(text) && /^https?:\/\//i.test(href)) {
    const link = renderLink(href, `来源 ${text}：点击查看原文（新标签页）`, text)
      .replace('<a ', `<a target="_blank" rel="noopener noreferrer" aria-label="引用来源 ${text}，在新标签页查看原文" `)
    return `<sup class="report-citation">${link}</sup>`
  }
  return renderLink(href, title, text)
}
const renderTable = renderer.table.bind(renderer)
renderer.table = (header: string, body: string) =>
  `<div class="report-table-scroll" role="region" aria-label="报告表格，可横向滚动" tabindex="0">${renderTable(header, body)}</div>\n`

// Callers must sanitize this HTML before inserting it into the document.
export function renderReportMarkdown(markdown: string, citationsAlreadyLimited = false): string {
  const source = citationsAlreadyLimited ? markdown : limitCitationGroups(markdown)
  return marked.parse(source, { async: false, gfm: true, breaks: false, renderer }) as string
}
