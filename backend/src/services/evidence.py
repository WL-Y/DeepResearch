"""Shared evidence identity and presentation helpers."""

import re
from urllib.parse import urlsplit, urlunsplit, quote


SOURCES_SECTION_PATTERN = re.compile(
    r"\n#{2,3}\s*(?:参考文献|核心来源|相关来源|Sources|References)\s*\n.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def limit_citation_groups(text: str, sources: dict[int, dict]) -> str:
    """Keep at most two distinct sources per citation cluster.

    The writer selects citations. This deterministic fallback
    ranks overflow using local evidence overlap, then source relevance/quality.
    Never adds a source the draft did not cite, and leaves code/links untouched.
    """
    pattern = re.compile(
        r"(?P<code>`+)[\s\S]*?(?P=code)|~~~[^\n]*\n[\s\S]*?~~~"
        r"|!?\[[^\]\n]*\]\([^\n]*?\)"
        r"|(?P<group>\[\d+\](?!\()(?:(?:[ \t,，、]|\r?\n(?![ \t]*\r?\n))*\[\d+\](?!\())*)"
    )

    def terms(value):
        english = re.findall(r'[a-z0-9][a-z0-9_.%-]*', value.lower())
        chinese = [part[i:i + 2] for part in re.findall(r'[\u3400-\u9fff]+', value)
                   for i in range(len(part) - 1)]
        return set(english + chinese)

    def replace(match):
        if not match.group('group'):
            return match.group(0)
        original = [int(n) for n in re.findall(r'\[(\d+)\]', match.group(0))]
        numbers = list(dict.fromkeys(original))
        available = [n for n in numbers if n in sources]
        identities = {source_key(sources[n]['url']) if sources[n].get('url') else str(n) for n in available}
        if len(original) <= 2 and len(identities) == len(original):
            return match.group(0)
        before = text[:match.start()].rstrip(' \t。.!！？?')
        context = re.split(r'[\n。！？!?；;]', before)[-1][-350:]
        query = terms(context)

        def score(number):
            source = sources[number]
            evidence = re.sub(r'\[\d+\]', '', source.get('text', '') + ' ' + source.get('title', ''))
            overlap = len(query & terms(evidence))
            details = source.get('details', [])
            direct = any(d.get('relevance') == 'direct' for d in details)
            primary = any(d.get('kind') == 'primary' for d in details)
            read = any(d.get('has_content') for d in details)
            return (overlap, direct, primary, read, -numbers.index(number))

        chosen, identities = [], set()
        for number in sorted(available, key=score, reverse=True):
            identity = source_key(sources[number]['url']) if sources[number].get('url') else str(number)
            if identity in identities:
                continue
            identities.add(identity)
            chosen.append(number)
            if len(chosen) == 2:
                break
        # Retain the original order after relevance selection.
        return ''.join(f'[{n}]' for n in numbers if n in chosen)

    return pattern.sub(replace, text)


def source_key(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org", "alphaxiv.org", "www.alphaxiv.org"}:
        match = re.search(r"/(?:abs|html|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", parsed.path)
        if match:
            return "arxiv:" + match.group(1)
    if host in {'doi.org', 'dx.doi.org'}:
        match = re.fullmatch(r'/10\.48550/arxiv\.(\d{4}\.\d{4,5})(?:v\d+)?', parsed.path, re.I)
        if match:
            return 'arxiv:' + match.group(1)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))


def source_link(title: str, url: str) -> str:
    label = re.sub(r"[\r\n]+", " ", title).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({quote(url, safe=':/?=&%#@+;,~!$*')})"


def hide_citations(text: str, titles: dict[int, str], *,
                   citation_links: dict[int, tuple[int, str]] | None = None) -> str:
    """Hide unselected markers; link selected ones for superscript presentation.

    Keep the Markdown portable: the UI and exporters style links titled
    'citation' as superscripts. Source names remain sentence constituents.
    """
    pattern = re.compile(
        r"(?P<code>`+)[\s\S]*?(?P=code)|~~~[^\n]*\n[\s\S]*?~~~"
        r"|!?\[[^\]\n]*\]\([^\n]*?\)"
        r"|\[(?P<number>\d+)\](?!\()"
    )

    def replace(match):
        if match.group('number') is None:
            return match.group(0)
        number = int(match.group('number'))
        link = ''
        if citation_links and number in citation_links:
            display_number, url = citation_links[number]
            if url.startswith(('http://', 'https://')):
                link = source_link(str(display_number), url)[:-1] + ' "citation")'
        before = text[:match.start()].rstrip()
        after = text[match.end():].lstrip()
        # Absence statements take a trailing citation, not a source-name object.
        if re.search(r"(?:未见|未取得|未发现|未核实|未核验|未涉及|未提供|缺失|缺少|尚无|没有)$", before):
            return link
        grammatical = (
            re.search(r"(?:在|根据|来自|来源|与|和|(?<!涉)及|对比|相比|参见|详见|由)$", before)
            or re.match(r"(?:与|和|及|是|为|属于|属|指出|显示|表明|认为|报告|提出|提供|采用|中的|的)", after)
            or re.search(r"(?:^|\n|[；;：:])\s*$", text[:match.start()])
        )
        if grammatical:
            title = titles.get(number, "未核实来源")
            clean_title = re.sub(r"[\[\]<>\r\n]", "", title)
            clean_title = re.sub(r"^(?:GitHub\s*-\s*|PDF\s*)", "", clean_title, flags=re.I)
            return (clean_title if len(clean_title) <= 80 else clean_title[:77] + "…") + link
        return link

    cleaned = pattern.sub(replace, text).replace("[引用不可用]", "（来源待核实）")
    return re.sub(r"[ \t]+([，。；：！？])", r"\1", cleaned).strip()
