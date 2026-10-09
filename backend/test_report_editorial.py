"""Offline regressions for reusable report composition and bibliography handling."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.models import TaskSummary, TodoItem
from src.services.evidence import source_key
from src.services.references import citation_catalog, display_reference_type
from src.services.report_materials import compact_materials
from src.services.reporting import ReportingService
from src.services.search import SearchService
from src.services.summarization import SummarizationService
from src.tools.note import NoteTool


def summary(text, urls, *, title='研究问题', details=None, titles=None):
    return TaskSummary(task=TodoItem(id=1, title=title, intent='', query='q'), summary=text,
                       sources=urls, source_titles=titles or ['资料'] * len(urls),
                       source_details=details or [{} for _ in urls])


def pack(items, **kwargs):
    normalized = ReportingService.__new__(ReportingService)._renumber_citations(items)
    return json.loads(compact_materials(normalized, **kwargs))


class EditorialMaterialsTests(unittest.TestCase):
    def test_generic_topics_keep_findings_and_separate_material_limits(self):
        for topic, finding in [
            ('历史研究', '档案记载该工程于当年启动'),
            ('政策研究', '该条款仅适用于文件指定地区'),
            ('技术比较', '该算法在相同数据集上采用固定训练步数'),
            ('企业研究', '该公司年报按地区披露收入'),
        ]:
            with self.subTest(topic=topic):
                item = summary(f'## 任务 1 新增资料\n\n{finding}[1]。\n\n'
                               '本轮资料未取得更早版本，不能判断变化发生时间[1]。\n\n'
                               'metadata_status=verification_failed\n\n无效引用[任务 1 新增资料]。',
                               ['https://example.org/document'], title=topic)
                payload = pack([item])
                section = payload['sections'][0]
                self.assertIn(finding + '[1]', section['summary'])
                self.assertNotIn('未取得', section['summary'])
                self.assertEqual(section['limitations'], ['现有资料未取得更早版本，不能判断变化发生时间[1]。'])
                self.assertEqual(section['heading'], topic)
                encoded = json.dumps(payload, ensure_ascii=False)
                for internal in ('任务 1', '新增资料', '本轮资料', 'verification_failed', 'task_id'):
                    self.assertNotIn(internal, encoded)

    def test_reference_cap_discards_whole_assertion_instead_of_only_marker(self):
        item = summary('保留的发现[1]。\n\n不能失去来源的结论[16]。\n\n'
                       '这项判断需要两个来源[1][16]。',
                       [f'https://example.org/{i}' for i in range(16)])
        # All references must be cited to enter the globally numbered catalog.
        item.summary += '\n\n' + '\n\n'.join(f'材料 {i}[{i}]。' for i in range(2, 16))
        payload = pack([item])
        self.assertEqual(len(payload['sources']), 15)
        text = payload['sections'][0]['summary']
        self.assertIn('保留的发现[1]', text)
        self.assertNotIn('不能失去来源的结论', text)
        self.assertNotIn('这项判断需要两个来源', text)

    def test_missing_reference_passage_is_not_silently_uncited(self):
        payload = pack([summary('有效结论[1]。\n\n无效数字 98%[99]。', ['https://example.org/1'])])
        text = payload['sections'][0]['summary']
        self.assertIn('有效结论[1]', text)
        self.assertNotIn('98%', text)
        self.assertNotIn('引用不可用', text)

    def test_document_type_does_not_imply_fulltext_was_read(self):
        payload = pack([summary('发现[1]。', ['https://arxiv.org/abs/2412.11936'],
                                title='任务 1 新增资料')])
        self.assertEqual(payload['sources'][0]['type'], '一手论文·预印本')
        self.assertEqual(payload['sources'][0]['basis'], '搜索摘要或来源摘要')
        self.assertEqual(payload['sections'][0]['heading'], '研究发现')

    def test_capacity_preserves_complete_sentence_with_its_condition(self):
        long = '完整判断' + '测试条件' * 3000 + '[1]。'
        payload = pack([summary(long + '\n\n短结论仅适用于指定条件[1]。', ['https://example.org/1'])], max_chars=5000)
        self.assertEqual(payload['sections'][0]['summary'], '短结论仅适用于指定条件[1]。')
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False)), 5000)

    def test_complete_table_and_citations_survive_together(self):
        table = '| 对象 | 条件 | 数值 |\n|---|---|---|\n| 方法 A | 同一数据集 | 42%[1] |'
        payload = pack([summary(table, ['https://example.org/1'])])
        self.assertEqual(payload['sections'][0]['summary'], table)

    def test_incremental_summary_adds_no_process_heading_and_keeps_offsets(self):
        previous = summary('旧结果[1]。', ['https://example.org/1'])
        added = summary('新结果[1]。', ['https://example.org/2'])
        merged = SummarizationService.merge_summaries(previous.task, [], previous, added)
        self.assertEqual(merged.summary, '旧结果[1]。\n\n新结果[2]。')
        self.assertEqual(len(merged.sources), 2)

    def test_machine_status_is_not_in_summary_writer_source_text(self):
        row = {'url': 'https://example.org/1', 'title': '资料', 'snippet': '内容',
               'content': '', 'metadata_status': 'verification_failed',
               'acquisition_status': 'full_text_extracted', 'context_status': 'selected_passages_used'}
        formatted = SummarizationService(Mock())._format_results([row])
        for status in ('verification_failed', 'full_text_extracted', 'selected_passages_used'):
            self.assertNotIn(status, formatted)


class BibliographyTests(unittest.TestCase):
    TITLE = 'A systematic survey of methods and evaluation in a research field'

    def test_preprint_versions_mirror_and_repository_doi_have_one_identity(self):
        urls = ['https://arxiv.org/abs/2412.11936v1', 'https://arxiv.org/pdf/2412.11936v3.pdf',
                'https://www.alphaxiv.org/abs/2412.11936', 'https://doi.org/10.48550/arXiv.2412.11936']
        self.assertEqual(len({source_key(u) for u in urls}), 1)
        catalog = citation_catalog([{'url': u, 'title': self.TITLE} for u in urls])
        reference = catalog[source_key(urls[0])]
        self.assertEqual(reference['url'], 'https://arxiv.org/abs/2412.11936')
        self.assertEqual(display_reference_type(reference), '综述·预印本')
        self.assertEqual(set(reference['consulted_urls']), set(urls))

    def test_preprint_and_publication_share_one_primary_reference_in_any_order(self):
        preprint = {'url': 'https://arxiv.org/abs/2412.11936', 'title': self.TITLE,
                    'doi': '10.48550/arxiv.2412.11936', 'authors': ['Alice Li', 'Bob Wu']}
        publication = {'url': 'https://doi.org/10.1234/published', 'title': self.TITLE,
                       'doi': '10.1234/published', 'authors': ['Alice Li', 'Bob Wu'],
                       'metadata_status': 'doi_verified', 'publication_type': 'journal-article'}
        for records in ([preprint, publication], [publication, preprint]):
            catalog = citation_catalog(records)
            self.assertEqual(catalog[source_key(preprint['url'])]['url'], publication['url'])
            self.assertEqual(catalog[source_key(publication['url'])]['url'], publication['url'])

    def test_two_publications_with_different_dois_are_not_merged_by_title(self):
        rows = [{'url': f'https://doi.org/10.1234/{n}', 'doi': f'10.1234/{n}',
                 'title': self.TITLE, 'authors': ['Alice Li', 'Bob Wu'],
                 'publication_type': 'journal-article'} for n in (1, 2)]
        catalog = citation_catalog(rows)
        self.assertEqual(len({r['url'] for r in catalog.values()}), 2)

    def test_full_title_conference_wrapper_joins_paper_but_similar_title_does_not(self):
        rows = [{'url': 'https://arxiv.org/abs/2412.11936', 'title': self.TITLE},
                {'url': 'https://iclr.cc/virtual/2026/poster/1', 'title': 'ICLR Poster ' + self.TITLE},
                {'url': 'https://iclr.cc/virtual/2026/poster/2', 'title': self.TITLE + ' part two'}]
        catalog = citation_catalog(rows)
        self.assertEqual(catalog[source_key(rows[1]['url'])]['url'], rows[0]['url'])
        self.assertEqual(catalog[source_key(rows[2]['url'])]['url'], rows[2]['url'])

    def test_actual_original_read_replaces_mirror_without_losing_origin(self):
        mirror = 'https://alphaxiv.org/abs/2412.11936'
        actual = 'https://arxiv.org/html/2412.11936v2'
        reference = citation_catalog([{'url': mirror, 'title': self.TITLE,
                                      'fetched_url': actual, 'document_scope': 'paper_fulltext'}])[source_key(mirror)]
        self.assertEqual(reference['url'], 'https://arxiv.org/abs/2412.11936')
        self.assertEqual(set(reference['consulted_urls']), {mirror, actual})

    def test_mirror_only_is_not_relabelled_as_a_read_original(self):
        url = 'https://alphaxiv.org/abs/2412.11936'
        reference = citation_catalog([{'url': url, 'title': self.TITLE}])[source_key(url)]
        self.assertEqual(reference['url'], url)
        self.assertEqual(display_reference_type(reference), '第三方整理')

    def test_final_report_uses_canonical_link_without_type_suffix_or_task_labels(self):
        items = [summary('发现[1]。', [url], title=f'任务 {i} 新增资料', titles=[self.TITLE])
                 for i, url in enumerate(('https://arxiv.org/abs/2412.11936v1',
                                          'https://alphaxiv.org/abs/2412.11936'), 1)]
        with tempfile.TemporaryDirectory() as directory:
            writer = Mock(run=Mock(return_value='# 报告\n\n发现[1]。'))
            service = ReportingService(writer, NoteTool(directory))
            report = service.generate('任意研究主题', items, 'test')
            self.assertIn('https://arxiv.org/abs/2412.11936', report)
            self.assertNotIn('综述·预印本', report)
            self.assertNotIn('alphaxiv', report)
            self.assertNotIn('涉及：', report)
            self.assertNotIn('任务 1', report)
            self.assertEqual(writer.run.call_count, 1)


class OriginalReadingTests(unittest.TestCase):
    def test_original_repository_read_precedes_mirror_with_existing_budget(self):
        service = SearchService.__new__(SearchService)
        service.source_reader = Mock()
        service.source_reader.read_document.return_value = {'content': 'Original text', 'document_scope': 'paper_fulltext'}
        result = service._read_candidate({'url': 'https://alphaxiv.org/abs/2412.11936'}, 'research')
        service.source_reader.read_document.assert_called_once_with('https://arxiv.org/abs/2412.11936', 'research')
        self.assertEqual(result['fetched_url'], 'https://arxiv.org/abs/2412.11936')

    def test_arxiv_repository_doi_is_not_sent_to_crossref(self):
        service = SearchService.__new__(SearchService)
        service.crossref = Mock()
        with tempfile.TemporaryDirectory() as directory:
            service.cache_dir = Path(directory)
            rows = [{'doi': '10.48550/arxiv.2412.11936'}]
            service._verify_metadata(rows)
        service.crossref.lookup.assert_not_called()
        self.assertNotIn('metadata_status', rows[0])

    def test_read_slots_reserve_relevant_originals_not_unrelated_papers(self):
        rows = [
            {'url': 'https://example.org/blog/robot', 'title': 'robot planning survey'},
            {'url': 'https://arxiv.org/abs/2412.00001', 'title': 'robot planning experiments'},
            {'url': 'https://arxiv.org/abs/2412.00002', 'title': 'medieval ocean archives'},
            {'url': 'https://arxiv.org/abs/2412.00003', 'title': 'robot planning survey'},
        ]
        selected = SearchService._original_candidates(rows, 'robot planning')
        self.assertEqual(selected, [rows[1]])


if __name__ == '__main__':
    unittest.main()
