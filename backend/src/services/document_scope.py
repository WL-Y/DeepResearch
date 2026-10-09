"""Conservative document classification based on extracted body, not URL alone."""
import re
from urllib.parse import urlparse

VERSION = 4
SECTIONS = re.compile(
    r'(?im)^\s*(?:\d+(?:\.\d+)*[. ]\s*)?'
    r'(introduction|methods|materials and methods|methodology|results|discussion|'
    r'results and discussion|conclusions?|experiments|experimental results|background|implementation|evaluation|limitations|'
    r'摘要|引言|方法|实验|结果|讨论|结论)(?:\s*[:：.、—-]\s*[^\n]+)?\s*$')
END = re.compile(r'(?im)^\s*(?:references|bibliography|参考文献|data availability|code availability)\s*$')


def classify_document(raw):
    parsed = urlparse(raw['fetched_url'])
    host, path, text = (parsed.hostname or '').lower(), parsed.path.lower(), raw['text']
    headings = list(SECTIONS.finditer(text))
    end = END.search(text)
    body_end = end.start() if end else len(text)
    substantive = [m for i, m in enumerate(headings) if m.start() < body_end and
                   len(text[m.end():min(body_end, headings[i + 1].start() if i + 1 < len(headings) else body_end)].strip()) >= 300]
    publisher = any(host == d or host.endswith('.' + d) for d in (
        'nature.com', 'springer.com', 'springeropen.com', 'wiley.com', 'plos.org',
        'science.org', 'sciencedirect.com', 'pmc.ncbi.nlm.nih.gov', 'frontiersin.org',
        'mdpi.com', 'acm.org', 'ieee.org', 'pnas.org', 'elifesciences.org'))
    article = bool(re.search(r'/(?:articles?|doi|science/article|document|full)/', path))
    structured = len({m[1].lower() for m in substantive}) >= 2 and body_end >= 2500 and end is not None
    if not structured and re.search(
        r'radware bot manager captcha|verify (?:that )?you are human|checking your browser|'
        r'just a moment|access denied|enable javascript and cookies', text[:2000], re.I):
        return 'access_barrier', '页面为验证码或访问拦截内容'
    if raw['kind'] == 'PDF':
        if re.search(r'(?:poster|slides)\.pdf$', path):
            return 'supporting_material', '海报或幻灯片，不作为论文正文'
        return 'paper_fulltext', 'PDF 正文；完整性另由提取状态判断'
    if host in {'arxiv.org', 'www.arxiv.org'} and '/html/' in path:
        return 'paper_fulltext', 'arXiv HTML 正文；完整性另由提取状态判断'
    if host == 'proceedings.neurips.cc' and path.endswith('-abstract.html'):
        return 'metadata_page', '会议论文摘要入口，尚未获取论文正文'
    if publisher and article:
        if structured and '/abstract/' not in path and '/article-abstract/' not in path:
            return 'paper_fulltext', '出版社论文页含实质性正文段落和参考文献：' + ', '.join(m[1] for m in substantive)
        if len(substantive) >= 2 and body_end >= 2500:
            return 'body_unconfirmed', '已取得实质性章节正文，但结束结构不足，完整性尚未确认'
        return 'metadata_page', '论文页面尚未确认包含完整正文结构，不能只凭域名或篇幅认定全文'
    if ('/article-abstract/' in path
            or host in {'huggingface.co', 'www.emergentmind.com', 'emergentmind.com', 'liner.com'}
            or ((host == 'microsoft.com' or host.endswith('.microsoft.com')) and '/publication/' in path)
            or (host.startswith('research.') and '/publications/' in path)):
        return 'metadata_page', '论文介绍或摘要页面'
    return 'webpage_body', '普通网页正文，不自动等同于论文正文'


def completeness(raw, scope):
    """Separate extraction coverage from evidence that the original is complete."""
    if scope == 'access_barrier':
        return 'failed', '访问拦截页，未取得正文'
    if scope == 'metadata_page':
        return 'unconfirmed', '仅确认介绍或摘要内容'
    if raw['partial']:
        return 'partial', raw.get('partial_reason') or '提取达到页数、时间或字符上限'
    # Only explicit access/truncation notices, not a generic “read more” link.
    if re.search(r'(?:subscribe|sign in|log in|purchase) to (?:continue reading|read (?:the )?(?:full|rest))|'
                 r'订阅后(?:继续阅读|查看全文)|登录后(?:继续阅读|查看全文)|购买后(?:阅读|查看全文)',
                 raw['text'][-2500:], re.I):
        return 'partial', '正文含登录、订阅或购买后继续阅读提示'
    if raw.get('pdf_empty_pages'):
        return 'unconfirmed', '部分 PDF 页面无可提取文字，可能为空白页、图片或扫描页；未确认文字覆盖'
    if raw.get('acquisition_status') == 'service_extracted_text':
        return 'unconfirmed', '第三方返回了正文，但原文完整性未经确认'
    if scope == 'body_unconfirmed':
        return 'unconfirmed', '已取得章节正文，尚未确认文档结束结构'
    return 'confirmed', '已处理全部可解析内容，未检测到截断；不包含图片内容识别'
