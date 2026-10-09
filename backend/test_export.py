"""Offline regression tests for downloadable, safe report formats."""
from io import BytesIO
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader

from src.api_exports import router
from src.services.exporting import export_report, parse_blocks, plain, readable_blocks, safe_filename

SAMPLE = '''# 机器人决策研究报告

## 执行摘要

本报告比较 **方法 A** 与方法 B。成功率由 35% 提高到 65%，绝对差为 30 个百分点，不能解释为相对提升 30%。

## 方法对照

| 方法 | 成功率 | 条件 |
| --- | --- | --- |
| 方法 A | 65% | 20 次试验 |
| 方法 B | 35% | 相同协议 |

## 验证步骤

3. 固定实验协议。
   - 保留样本数和失败判据。
4. 重复评估，并报告不确定性。

```python
rate = 13 / 20
print(rate)
```

参见 [原始论文](https://example.org/paper?q=a&version=2)，不代表已经独立复现。
'''


class ExportTests(unittest.TestCase):
    def test_citation_links_are_superscript_in_html_word_and_pdf(self):
        content = '# 报告\n\n结果为65%[12](https://example.org/paper "citation")。\n\n12. [论文](https://example.org/paper)'
        runs = parse_blocks(content)[1]['runs']
        self.assertEqual([r['text'] for r in runs if r.get('superscript')], ['12'])
        html = export_report('主题', content, 'html').decode()
        self.assertEqual(html.count('<sup class="report-citation">'), 1)
        self.assertIn('>12</a></sup>', html)
        with ZipFile(BytesIO(export_report('主题', content, 'docx'))) as archive:
            self.assertEqual(archive.read('word/document.xml').decode().count('w:val="superscript"'), 1)
        pdf = PdfReader(BytesIO(export_report('主题', content, 'pdf')))
        self.assertIn('65%', ''.join(page.extract_text() for page in pdf.pages))
        self.assertTrue(any(page.get('/Annots') for page in pdf.pages))
        self.assertEqual(export_report('主题', content, 'md').decode(), content)
        self.assertIn('https://example.org/paper', export_report('主题', content, 'txt').decode('utf-8-sig'))

    def test_only_numeric_citation_links_are_superscript(self):
        content = '[论文](https://example.org "citation") [12](https://example.org) `x[1]`'
        self.assertNotIn('<sup', export_report('主题', content, 'html').decode())

    def test_markdown_preserves_source(self):
        self.assertEqual(export_report('主题', SAMPLE, 'md').decode(), SAMPLE)

    def test_plain_text_keeps_links_and_numbers_without_markers(self):
        text = export_report('主题', SAMPLE, 'txt').decode('utf-8-sig')
        self.assertIn('35%', text)
        self.assertIn('65%', text)
        self.assertIn('https://example.org/paper?q=a&version=2', text)
        self.assertNotIn('**', text)
        self.assertNotIn('##', text)
        self.assertIn('3. 固定实验协议', text)
        self.assertIn('4. 重复评估', text)

    def test_html_is_standalone_and_never_loads_active_content(self):
        content = SAMPLE + '\n\n<script>alert(1)</script>\n\n![追踪](https://example.org/tracker.png)\n\n[恶意](javascript:alert(1))'
        html = export_report('</title><script>bad</script>', content, 'html').decode()
        self.assertIn('<meta charset="utf-8">', html)
        self.assertIn('Content-Security-Policy', html)
        self.assertIn('<table>', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)
        self.assertNotIn('href="javascript:', html)

    def test_docx_is_a_real_archive_with_headings_table_and_hyperlink(self):
        payload = export_report('主题', SAMPLE, 'docx')
        self.assertTrue(payload.startswith(b'PK'))
        with ZipFile(BytesIO(payload)) as archive:
            document = archive.read('word/document.xml').decode()
            relationships = archive.read('word/_rels/document.xml.rels').decode()
            self.assertIn('机器人决策研究报告', document)
            self.assertIn('w:val="Title"', document)
            self.assertIn('w:tblHeader', document)
            self.assertIn('w:hyperlink', document)
            self.assertIn('https://example.org/paper', relationships)
            self.assertIn('D9D9D9', document)

    def test_pdf_contains_extractable_chinese_numbers_and_links(self):
        payload = export_report('主题', SAMPLE, 'pdf')
        self.assertTrue(payload.startswith(b'%PDF-'))
        pdf = PdfReader(BytesIO(payload))
        text = ''.join(page.extract_text() for page in pdf.pages)
        for value in ['机器人决策', '35%', '65%', '30', '原始论文']:
            self.assertIn(value, text)
        self.assertTrue(any(page.get('/Annots') for page in pdf.pages))

    def test_long_wide_tables_are_transformed_without_data_loss(self):
        content = '| A | B | C | D | E |\n|---|---|---|---|---|\n| 方法 | ' + '详细条件。' * 100 + ' | 65% | 20 次 | 原论文 |'
        original = parse_blocks(content)[0]['rows']
        blocks = list(readable_blocks(parse_blocks(content)))
        rendered = ''.join(plain(block['runs']) for block in blocks)
        for row in original:
            for cell in row: self.assertIn(plain(cell), rendered)
        self.assertNotIn('table', [block['kind'] for block in blocks])
        self.assertTrue(export_report('宽表', content, 'pdf').startswith(b'%PDF-'))

    def test_safe_filenames(self):
        self.assertEqual(safe_filename('CON', 'pdf'), 'research-report.pdf')
        name = safe_filename('../../研究\r\n主题:*?', 'docx')
        self.assertNotIn('/', name)
        self.assertNotIn('\n', name)
        self.assertTrue(name.endswith('.docx'))

    def test_header_only_table_is_not_dropped(self):
        source = '| A | B | C | D | E |\n|---|---|---|---|---|'
        blocks = list(readable_blocks(parse_blocks(source)))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]['rows'][0]), 5)


class ExportApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)

    def test_all_formats_have_correct_download_headers(self):
        for format in ('md', 'html', 'txt', 'pdf', 'docx'):
            response = self.client.post('/api/reports/export', json={'topic': '机器人研究', 'content': SAMPLE, 'format': format})
            self.assertEqual(response.status_code, 200, response.text[:100] if response.status_code != 200 else '')
            self.assertIn('attachment;', response.headers['content-disposition'])
            self.assertIn("filename*=UTF-8''", response.headers['content-disposition'])
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')

    def test_invalid_empty_oversized_and_control_character_requests(self):
        cases = [{'format': 'exe'}, {'content': ' '}, {'content': 'x' * 200001}, {'content': '\x00'}, {'topic': ''}]
        for case in cases:
            response = self.client.post('/api/reports/export', json={'topic': '主题', 'content': SAMPLE, 'format': 'pdf', **case})
            self.assertEqual(response.status_code, 422)

    def test_conversion_errors_are_separate_and_readable(self):
        with self.assertLogs('src.api_exports', level='ERROR'), patch('src.api_exports.export_report', side_effect=RuntimeError('internal path')):
            response = self.client.post('/api/reports/export', json={'topic': '主题', 'content': SAMPLE, 'format': 'pdf'})
        self.assertEqual(response.status_code, 500)
        self.assertIn('原报告不受影响', response.json()['detail'])
        self.assertNotIn('internal path', response.text)


if __name__ == '__main__': unittest.main()
