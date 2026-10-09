"""Regression cases derived from the world-model report audit; no network or LLM."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.models import TodoItem, TaskSummary
from src.repository import ResearchRepository
from src.services.evidence import hide_citations, limit_citation_groups, source_key
from src.services.source_reader import SourceReader, _ReadableTextParser
from src.services.summarization import SummarizationService
from src.services.reporting import ReportingService
from src.services.research import ResearchManager
from src.services.search import SearchService
from src.services.planning import PlanningService
from src.tools.note import NoteTool
from test_core import SequenceAgent


class ReadingTests(unittest.TestCase):
    def test_experiments_beyond_prefix_survive_selection(self):
        document = ("Introduction and generic background. " * 1100
                    + "\nAppendix: DreamZero experiments hardware GB200 latency 150ms, action chunks 7Hz. " * 10
                    + "\nReferences unrelated citations " * 600)
        excerpt = SourceReader().select_passages(document, "DreamZero hardware latency")
        self.assertIn("GB200", excerpt)
        self.assertIn("7Hz", excerpt)
        self.assertLessEqual(len(excerpt), 14000)

    def test_html_preserves_word_and_table_boundaries(self):
        parser = _ReadableTextParser()
        parser.feed('<script>ignore()</script><p>Policy <b>control</b> works.</p><table><tr><td>GPU</td><td>GB200</td></tr></table>')
        self.assertNotIn("ignore", parser.text())
        self.assertIn("Policy control works.", parser.text())
        self.assertIn("GPU | GB200", parser.text())

    def test_arxiv_html_failure_falls_back_to_pdf_with_read_status(self):
        reader = SourceReader()
        with patch.object(reader, "_fetch", side_effect=[ValueError("no HTML"), ("Experimental details " * 40, "PDF", "https://arxiv.org/pdf/2602.15922", False)]):
            result = reader.read_document("https://arxiv.org/abs/2602.15922")
        self.assertIn("PDF", result["content_status"])
        self.assertIn("正文已获取", result["content_status"])
        self.assertEqual(result["acquisition_status"], "full_text_extracted")
        self.assertEqual(result["context_status"], "full_text_used")
        self.assertTrue(result["content"])

    def test_long_complete_document_is_not_mislabeled_as_unavailable(self):
        reader = SourceReader(max_chars=3000)
        with patch.object(reader, "_fetch", return_value=("Methods and results 42%. " * 1000, "PDF", "https://x.org/p.pdf", False)):
            result = reader.read_document("https://x.org/p.pdf", "methods results")
        self.assertEqual(result["acquisition_status"], "full_text_extracted")
        self.assertEqual(result["context_status"], "selected_passages_used")
        self.assertTrue(result["full_text_available"])
        self.assertTrue(result["evidence_chunks"])
        self.assertIn("正文已获取并缓存", result["content_status"])

    def test_pdf_extraction_is_used_and_response_closed(self):
        response = Mock(headers={"content-type": "application/pdf"}, is_redirect=False, is_permanent_redirect=False, encoding=None)
        response.iter_content.return_value = [b"%PDF-fixture"]
        page = Mock()
        page.extract_text.return_value = "Experiments hardware and measured latency " * 20
        with patch.object(SourceReader, "_validate_public_url"), patch("src.services.source_reader.requests.get", return_value=response), patch("src.services.source_reader.PdfReader", return_value=Mock(pages=[page])) as pdf:
            result = SourceReader().read_document("https://example.org/paper.pdf")
        pdf.assert_called_once()
        response.close.assert_called_once()
        self.assertIn("PDF 第 1 页", result["content"])

    def test_private_redirect_is_blocked(self):
        response = Mock(headers={"location": "http://127.0.0.1/private"}, is_redirect=True)
        with patch("src.services.source_reader.socket.getaddrinfo", side_effect=[[(2, 1, 6, "", ("8.8.8.8", 443))], [(2, 1, 6, "", ("127.0.0.1", 80))]]), patch("src.services.source_reader.requests.get", return_value=response) as get:
            result = SourceReader().read_document("https://example.org/paper")
        self.assertFalse(result["content"])
        get.assert_called_once()


class EvidenceTests(unittest.TestCase):
    def test_absence_citations_never_insert_page_titles(self):
        titles = {1: "GitHub - leofan90/Awesome-World-Models: A comprehensive list of papers", 2: "WorldModelGym: a decision-based fidelity benchmark"}
        for text in ("安全认证链在本次材料中未见 [1]。", "指标阈值未见[2]。", "未取得[1]。", "节选未涉及[2]。"):
            cleaned = hide_citations(text, titles)
            self.assertNotIn("GitHub", cleaned)
            self.assertNotIn("WorldModelGym", cleaned)
            self.assertNotRegex(cleaned, r"\[\d+\]")



    def test_planner_keeps_explicit_method_checklist(self):
        payload = [{"title": str(i), "intent": "method comparison", "query": f"query{i}", "verification_query": f"verify{i}", "required_methods": ["Dreamer", "TD-MPC2"] if i == 1 else []} for i in range(1, 4)]
        tasks = PlanningService(SequenceAgent([]))._parse_tasks(json.dumps(payload))
        self.assertEqual(tasks[0].required_methods, ["Dreamer", "TD-MPC2"])

    def test_numeric_blog_cannot_displace_direct_original_work(self):
        summaries = [TaskSummary(task=TodoItem(id=i, title=str(i), intent="", query=""),
                    summary="Finding[1]。", sources=[f"https://example.org/{i}"], source_titles=[f"Paper{i}"],
                    source_details=[{"relevance": "direct", "kind": "commentary" if i == 1 else "primary", "has_content": True}]) for i in range(1, 17)]
        draft = "# Report\n" + "\n".join(f"性能{i}%[1]。" for i in range(10)) + "\n" + '\n'.join(f'原始实验[{i}]。' for i in range(2, 17))
        with tempfile.TemporaryDirectory() as directory:
            report = ReportingService(SequenceAgent([draft, draft]), NoteTool(directory)).generate("Robot", summaries, "run")
        self.assertNotIn("[Paper1](", report)
        self.assertEqual(report.split('## 核心来源')[1].count("](https://"), 15)

    def test_required_method_has_a_reserved_read_slot(self):
        service = SearchService.__new__(SearchService)
        broad = [{"title": f"Robotics survey {i}", "url": f"https://example.org/{i}", "snippet": "robotics decision world models"} for i in range(8)]
        original = {"title": "TD-MPC2", "url": "https://arxiv.org/abs/2310.16828", "snippet": "control"}
        service.search = Mock(return_value=broad + [original])
        service.source_reader = Mock()
        service.source_reader.read_document.return_value = {"content": "experiment details"}
        results = service.search_many(["robotics decision world models"], priority_terms=["TD-MPC2"])
        self.assertEqual(results[0]["title"], "TD-MPC2")
        self.assertIn("content", results[0])

    def test_reference_names_survive_grammatical_use(self):
        draft = "[1]与[2]为同一论文不同版本。不可与[3]的数字合并。\n资料覆盖；[1]是论文。事实为 42%[3]。"
        result = hide_citations(draft, {1: "DayDreamer预印本", 2: "DayDreamer会议版", 3: "DreamZero"})
        self.assertIn("DayDreamer预印本与DayDreamer会议版为同一论文", result)
        self.assertIn("不可与DreamZero的数字合并", result)
        self.assertIn("事实为 42%。", result)
        self.assertNotRegex(result, r"\[\d+\]")
        self.assertEqual(hide_citations("节选未涉及 [1]。", {1: "Paper"}), "节选未涉及。")

    def test_arxiv_versions_have_one_identity(self):
        self.assertEqual(source_key("https://arxiv.org/html/2602.15922v1"), source_key("https://arxiv.org/pdf/2602.15922v2"))

    def test_cluster_limit_selects_local_evidence_not_lowest_numbers(self):
        sources = {
            1: {'url': 'https://example.org/1', 'text': '行业发展历史背景'},
            2: {'url': 'https://example.org/2', 'text': '市场宣传'},
            3: {'url': 'https://example.org/3', 'text': '模型延迟降低到150ms'},
            4: {'url': 'https://example.org/4', 'text': '模型延迟实测为150ms'},
        }
        result = limit_citation_groups('模型延迟降低到150ms。[1][2], [3]、[4]', sources)
        self.assertEqual(result, '模型延迟降低到150ms。[3][4]')

    def test_cluster_limit_deduplicates_and_preserves_code_and_separate_claims(self):
        sources = {i: {'url': f'https://example.org/{i}'} for i in range(1, 5)}
        sources[4]['url'] = sources[1]['url']
        self.assertEqual(limit_citation_groups('结论[1][1][4][2][3]', sources), '结论[1][2]')
        self.assertEqual(limit_citation_groups('结论[1]。另一结论[2][3]。', sources), '结论[1]。另一结论[2][3]。')
        for text in ['`x[1][2][3]`', '```python\nx[1][2][3]\n```', '[1](https://example.org)']:
            self.assertEqual(limit_citation_groups(text, sources), text)
        self.assertEqual(limit_citation_groups('结论[1]\n[2][3]', sources), '结论[1][2]')
        self.assertEqual(limit_citation_groups('[1][2]\n\n[3]', sources), '[1][2]\n\n[3]')

    def test_report_source_list_only_includes_surviving_cluster_sources(self):
        summaries = [TaskSummary(task=TodoItem(id=i, title=str(i), intent='', query=''),
                    summary=f'{text}[1]。', sources=[f'https://example.org/{i}'], source_titles=[text])
                    for i, text in enumerate(['历史背景', '宣传', '模型延迟150ms', '模型延迟实测150ms'], 1)]
        draft = '# Report\n\n模型延迟实测150ms[1][2][3][4]。'
        with tempfile.TemporaryDirectory() as directory:
            result = ReportingService(SequenceAgent([draft, draft]), NoteTool(directory)).generate('延迟', summaries, 'run')
        self.assertEqual(result.count('"citation"'), 2)
        self.assertIn('https://example.org/3', result)
        self.assertIn('https://example.org/4', result)
        self.assertNotIn('https://example.org/1', result)
        self.assertNotIn('https://example.org/2', result)

    def test_citation_links_keep_code_and_names_and_remove_invalid_markers(self):
        text = '[2]指出结果为42%[1][2]。无效[99]。`array[1]`\n\n```python\nvalue[2]\n```\n\n[1](https://ordinary.test)'
        result = hide_citations(text, {1: '论文甲', 2: '论文乙'}, citation_links={2: (1, 'https://b.test/x(a)')})
        self.assertIn('论文乙[1](https://b.test/x%28a%29 "citation")指出', result)
        self.assertIn('42%[1](https://b.test/x%28a%29 "citation")', result)
        self.assertNotIn('[99]', result)
        self.assertIn('`array[1]`', result)
        self.assertIn('```python\nvalue[2]\n```', result)
        self.assertIn('[1](https://ordinary.test)', result)

    def test_visible_citations_follow_first_appearance_without_padding(self):
        summaries = [TaskSummary(task=TodoItem(id=i, title=str(i), intent='', query=''),
                    summary='事实[1]。', sources=[f'https://example.org/{i}'], source_titles=[f'Paper{i}'])
                    for i in range(1, 4)]
        draft = '# Report\n\n先出现[3]。后出现[1]。再次出现[3]。无效[99]。'
        with tempfile.TemporaryDirectory() as directory:
            report = ReportingService(SequenceAgent([draft, draft]), NoteTool(directory)).generate('Topic', summaries, 'run')
        self.assertEqual(report.count('[1](https://example.org/3 "citation")'), 2)
        self.assertIn('[2](https://example.org/1 "citation")', report)
        self.assertIn('1. [Paper3](https://example.org/3)', report)
        self.assertIn('2. [Paper1](https://example.org/1)', report)
        self.assertNotIn('https://example.org/2', report)
        self.assertNotIn('[99]', report)


    def test_late_primary_numeric_source_displaces_early_surveys(self):
        summaries = [TaskSummary(
            task=TodoItem(id=i, title="Robot", intent="", query=""), summary="证据[1]。",
            sources=[f"https://example.org/{i}"], source_titles=[f"Paper{i}"],
            source_details=[{"kind": "primary" if i == 16 else "survey", "relevance": "direct", "has_content": True}],
        ) for i in range(1, 17)]
        draft = "# Report\n" + '\n'.join(f'背景[{i}]。' for i in range(1, 16)) + "\n延迟150ms、频率7Hz[16]。"
        with tempfile.TemporaryDirectory() as directory:
            agent = SequenceAgent([draft, draft])
            report = ReportingService(agent, NoteTool(directory)).generate("Robot", summaries, "run")
        self.assertIn("[Paper16](https://example.org/16)", report)
        self.assertEqual(report.split('## 核心来源')[1].count("](https://"), 15)
        self.assertEqual(len(agent.prompts), 1)





if __name__ == "__main__":
    unittest.main()
