"""Deterministic tests for storage isolation and citation normalization."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.models import TaskSummary, TodoItem
from src.repository import ResearchRepository
from src.services.planning import PlanningService
from src.services.reporting import ReportingService
from src.services.report_materials import compact_materials
from src.services.research import ResearchManager
from src.services.search import SearchService
from src.services.source_reader import SourceReader
from src.tools.note import NoteTool


class FakeAgent:
    def run(self, prompt: str) -> str:
        return "# Generated report"


class SequenceAgent:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []

    def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self.responses)


class PlanningServiceTests(unittest.TestCase):
    def test_invalid_duplicate_plan_is_repaired_once(self):
        duplicate_plan = json.dumps([
            {"title": "Same", "intent": "One", "query": "same query", "verification_query": "verify one"},
            {"title": "Same", "intent": "Two", "query": "same query", "verification_query": "verify two"},
            {"title": "Same", "intent": "Three", "query": "same query", "verification_query": "verify three"},
        ])
        valid_plan = json.dumps([
            {"title": "A", "intent": "Goal A", "query": "query a", "verification_query": "verify a"},
            {"title": "B", "intent": "Goal B", "query": "query b", "verification_query": "verify b"},
            {"title": "C", "intent": "Goal C", "query": "query c", "verification_query": "verify c"},
        ])
        agent = SequenceAgent([duplicate_plan, valid_plan])

        tasks = PlanningService(agent).plan("Topic")

        self.assertEqual([task.id for task in tasks], [1, 2, 3])
        self.assertEqual(len(agent.prompts), 2)
        self.assertIn("未通过格式或质量校验", agent.prompts[1])

    def test_json_extraction_ignores_non_json_brackets(self):
        service = PlanningService(SequenceAgent([]))
        response = "说明 [不是 JSON]\n```json\n" + json.dumps([
            {"title": "A", "intent": "Goal A", "query": "query a", "verification_query": "verify a"},
            {"title": "B", "intent": "Goal B", "query": "query b", "verification_query": "verify b"},
            {"title": "C", "intent": "Goal C", "query": "query c", "verification_query": "verify c"},
        ]) + "\n```"

        tasks = service._parse_tasks(response)

        self.assertEqual([task.title for task in tasks], ["A", "B", "C"])


class NoteToolTests(unittest.TestCase):
    def test_research_runs_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            notes = NoteTool(temp_dir)
            first = notes.save_report("run-one", "first")
            second = notes.save_report("run-two", "second")

            self.assertNotEqual(first, second)
            self.assertEqual(notes.load_report("run-one"), "first")
            self.assertEqual(notes.load_report("run-two"), "second")

            notes.save_report("run-one", "updated")
            self.assertEqual(notes.load_report("run-one"), "updated")
            self.assertEqual(notes.load_report("run-two"), "second")

    def test_research_id_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            notes = NoteTool(temp_dir)
            with self.assertRaises(ValueError):
                notes.save_report("../outside", "content")

    def test_delete_run_removes_report_and_notes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            notes = NoteTool(temp_dir)
            notes.save_note("run-one", 1, "Task", "content")
            notes.save_report("run-one", "report")

            self.assertTrue(notes.delete_run("run-one"))
            self.assertFalse(Path(temp_dir, "runs", "run-one").exists())
            self.assertFalse(notes.delete_run("run-one"))


class ReportingServiceTests(unittest.TestCase):
    def test_citations_are_global_deduplicated_and_only_include_used_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ReportingService(FakeAgent(), NoteTool(temp_dir))
            summaries = [
                TaskSummary(
                    task=TodoItem(id=1, title="One", intent="", query=""),
                    summary="First [1], shared [3].",
                    sources=["https://a.test", "https://unused.test", "https://shared.test"],
                    source_titles=["Source A", "Unused", "Shared source"],
                ),
                TaskSummary(
                    task=TodoItem(id=2, title="Two", intent="", query=""),
                    summary="Shared again [1], then new [2].",
                    sources=["https://shared.test", "https://b.test"],
                    source_titles=["Shared source", "Source B"],
                ),
            ]

            normalized = service._renumber_citations(summaries)
            formatted = compact_materials(normalized)

            self.assertIn("First [1], shared [2].", formatted)
            self.assertIn("Shared again [2], then new [3].", formatted)
            self.assertNotIn("https://unused.test", formatted)
            self.assertEqual(formatted.count("https://shared.test"), 1)

            report = service.generate("Topic", summaries, "test-run")
            self.assertTrue(report.startswith("# Generated report"))
            self.assertNotIn("## 核心来源", report)  # Do not append uncited fallbacks.
            self.assertNotIn("https://unused.test", report)
            self.assertTrue(Path(temp_dir, "runs", "test-run", "report.md").exists())

    def test_out_of_range_citation_is_not_forwarded_as_valid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ReportingService(FakeAgent(), NoteTool(temp_dir))
            summaries = [TaskSummary(
                task=TodoItem(id=1, title="One", intent="", query=""),
                summary="Supported [1], hallucinated [9].",
                sources=["https://a.test"],
            )]

            normalized = service._renumber_citations(summaries)

            self.assertIn("Supported [1]", normalized[0][0].summary)
            self.assertIn("hallucinated [引用不可用]", normalized[0][0].summary)

    def test_final_report_links_markers_and_caps_verified_sources_at_fifteen(self):
        draft = (
            "# Report\n\n" + '\n'.join(f'Fact {i}[{i}].' for i in range(1, 17))
            + "\n\n## 参考文献\n\n[1] https://invented.test"
        )
        summaries = [
            TaskSummary(
                task=TodoItem(id=index, title=str(index), intent="", query=""),
                summary="Fact [1].",
                sources=[f"https://source-{index}.test"],
                source_titles=[f"Source {index}"],
            )
            for index in range(1, 17)
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            service = ReportingService(
                SequenceAgent([draft, draft]), NoteTool(temp_dir)
            )
            report = service.generate("Topic", summaries, "test-run")

        self.assertNotRegex(report, r"\[\d+\](?!\()")
        self.assertNotIn("invented.test", report)
        self.assertEqual(report.count('"citation"'), 15)
        self.assertEqual(report.split('## 核心来源')[1].count("](https://source-"), 15)
        self.assertNotIn("https://source-16.test", report)


class SearchServiceTests(unittest.TestCase):
    def test_empty_results_are_not_cached(self):
        class EmptyEngine:
            name = "empty"

            def search(self, query, max_results):
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            service = SearchService.__new__(SearchService)
            service.engines = [EmptyEngine()]
            service.cache_dir = Path(temp_dir)

            with self.assertRaisesRegex(RuntimeError, "no_results"):
                service.search("query", 5)

            self.assertEqual(list(Path(temp_dir).glob("*.json")), [])

    def test_cache_key_includes_engine_and_result_limit(self):
        class CountingEngine:
            def __init__(self, name):
                self.name = name
                self.calls = 0

            def search(self, query, max_results):
                self.calls += 1
                return [{
                    "title": self.name,
                    "url": f"https://{self.name}.test/{max_results}",
                    "snippet": "result",
                }]

        with tempfile.TemporaryDirectory() as temp_dir:
            first_engine = CountingEngine("first")
            service = SearchService.__new__(SearchService)
            service.engines = [first_engine]
            service.cache_dir = Path(temp_dir)

            service.search("query", 3)
            service.search("query", 5)
            self.assertEqual(first_engine.calls, 2)

            second_engine = CountingEngine("second")
            service.engines = [second_engine]
            service.search("query", 3)
            self.assertEqual(second_engine.calls, 1)

    def test_search_many_deduplicates_ranks_and_enriches_sources(self):
        class Reader:
            def read_document(self, url, query=""):
                return {"content": "full source text", "content_status": "网页正文节选"}

        service = SearchService.__new__(SearchService)
        service.source_reader = Reader()

        def fake_search(query, max_results):
            common = {
                "title": "Shared result",
                "url": "https://example.com/shared#section",
                "snippet": "benchmark result",
            }
            if query == "discover":
                return [common, {
                    "title": "Community post",
                    "url": "https://blog.test/post/",
                    "snippet": "comparison",
                }]
            return [common, {
                "title": "Official benchmark",
                "url": "https://dataset.edu/benchmark",
                "snippet": "official benchmark metric",
            }]

        service.search = fake_search
        results = service.search_many(["discover", "verify"], max_total=3)

        self.assertEqual(len(results), 3)
        # With no query-term overlap, cross-query coverage outranks a domain suffix.
        self.assertEqual(results[0]["url"], "https://example.com/shared")
        shared = next(item for item in results if "shared" in item["url"])
        self.assertEqual(shared["matched_queries"], ["discover", "verify"])
        self.assertNotIn("#section", shared["url"])


class SourceReaderTests(unittest.TestCase):
    def test_private_and_non_http_urls_are_rejected(self):
        for url in ("http://127.0.0.1/private", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                SourceReader._validate_public_url(url)


class RepositoryTests(unittest.TestCase):
    def test_events_and_state_survive_repository_recreation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir, "research.db")
            repository = ResearchRepository(str(database))
            repository.create("run-one", "Topic")
            event_id = repository.record_event(
                "run-one",
                "progress",
                {"stage": "executing", "percentage": 25, "text": "Working"},
                {"status": "executing", "progress": 25, "progress_text": "Working"},
            )

            reopened = ResearchRepository(str(database))
            record = reopened.get("run-one")
            events = reopened.events_after("run-one")

            self.assertEqual(record["progress"], 25)
            self.assertEqual(record["status"], "executing")
            self.assertEqual(events[0]["id"], event_id)

    def test_interrupted_jobs_are_marked_as_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ResearchRepository(str(Path(temp_dir, "research.db")))
            repository.create("run-one", "Topic")

            self.assertEqual(repository.mark_interrupted_runs(), 1)
            self.assertEqual(repository.get("run-one")["status"], "error")
            self.assertEqual(repository.events_after("run-one")[-1]["type"], "error")

    def test_delete_removes_run_and_cascades_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ResearchRepository(str(Path(temp_dir, "research.db")))
            repository.create("run-one", "Topic")
            repository.record_event("run-one", "progress", {"percentage": 5})

            self.assertTrue(repository.delete("run-one"))
            self.assertIsNone(repository.get("run-one"))
            self.assertEqual(repository.events_after("run-one"), [])
            self.assertFalse(repository.delete("run-one"))


class ResearchManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_job_persists_and_replays_events(self):
        calling_thread = threading.get_ident()
        worker_threads = []
        saved_research_ids = []

        class Planning:
            def plan(self, topic):
                worker_threads.append(threading.get_ident())
                return [TodoItem(id=1, title="Task", intent="Intent", query="Query")]

        class Search:
            def search(self, query):
                worker_threads.append(threading.get_ident())
                return [{"title": "Source", "url": "https://a.test", "snippet": "Fact"}]

        class Summarization:
            def summarize(self, task, results):
                worker_threads.append(threading.get_ident())
                return TaskSummary(task=task, summary="Fact [1]", sources=["https://a.test"])

        class Reporting:
            def generate(self, topic, summaries, research_id):
                worker_threads.append(threading.get_ident())
                saved_research_ids.append(research_id)
                return "# Report"

        class Notes:
            def save_note(self, research_id, task_id, title, content):
                worker_threads.append(threading.get_ident())
                saved_research_ids.append(research_id)

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ResearchRepository(str(Path(temp_dir, "research.db")))
            manager = ResearchManager(
                repository=repository,
                planning=Planning(),
                search=Search(),
                summarization=Summarization(),
                reporting=Reporting(),
                notes=Notes(),
            )
            research_id = await manager.start("Topic")
            events = []
            event_ids = []
            async for message in manager.stream(research_id):
                lines = message.decode("utf-8").splitlines()
                event_ids.append(int(next(line[4:] for line in lines if line.startswith("id: "))))
                data_line = next(line[6:] for line in lines if line.startswith("data: "))
                events.append(json.loads(data_line))

            record = repository.get(research_id)
            replayed = []
            async for message in manager.stream(research_id, event_ids[-2]):
                replayed.append(message)

        executing = next(event for event in events if event.get("stage") == "executing")
        report = next(event for event in events if event["type"] == "report")

        self.assertEqual(executing["task_id"], 1)
        self.assertRegex(report["research_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(saved_research_ids, [report["research_id"], report["research_id"]])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["report"], "# Report")
        self.assertEqual(len(replayed), 1)
        self.assertIn(b'"stage": "completed"', replayed[0])
        self.assertTrue(worker_threads)
        self.assertTrue(all(thread_id != calling_thread for thread_id in worker_threads))

    async def test_one_failed_task_does_not_abort_remaining_tasks(self):
        class Planning:
            def plan(self, topic):
                return [
                    TodoItem(id=1, title="Fails", intent="", query="bad"),
                    TodoItem(id=2, title="Works", intent="", query="good"),
                ]

        class Search:
            def search(self, query):
                if query == "bad":
                    raise RuntimeError("temporary search failure")
                return [{"title": "Source", "url": "https://a.test", "snippet": "Fact"}]

        class Summarization:
            def summarize(self, task, results):
                return TaskSummary(task=task, summary="Fact [1]", sources=["https://a.test"])

        class Reporting:
            def generate(self, topic, summaries, research_id):
                return "# Partial report"

        class Notes:
            def save_note(self, research_id, task_id, title, content):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ResearchRepository(str(Path(temp_dir, "research.db")))
            manager = ResearchManager(
                repository, Planning(), Search(), Summarization(), Reporting(), Notes()
            )
            research_id = await manager.start("Topic")
            event_types = []
            async for message in manager.stream(research_id):
                data_line = next(
                    line[6:]
                    for line in message.decode("utf-8").splitlines()
                    if line.startswith("data: ")
                )
                event_types.append(json.loads(data_line)["type"])

            record = repository.get(research_id)

        self.assertIn("task_failed", event_types)
        self.assertIn("report", event_types)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["tasks"][0]["status"], "failed")
        self.assertEqual(record["tasks"][1]["status"], "completed")

    async def test_cancel_marks_running_task_and_persists_terminal_state(self):
        search_started = threading.Event()
        release_search = threading.Event()

        class Planning:
            def plan(self, topic):
                return [TodoItem(id=1, title="Task", intent="", query="query")]

        class Search:
            def search(self, query):
                search_started.set()
                release_search.wait(timeout=2)
                return []

        class Unused:
            def summarize(self, task, results):
                raise AssertionError("cancelled search must not be summarized")

            def generate(self, topic, summaries, research_id):
                raise AssertionError("cancelled research must not generate a report")

            def save_note(self, research_id, task_id, title, content):
                raise AssertionError("cancelled research must not save a note")

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ResearchRepository(str(Path(temp_dir, "research.db")))
            manager = ResearchManager(
                repository, Planning(), Search(), Unused(), Unused(), Unused()
            )
            research_id = await manager.start("Topic")
            self.assertTrue(await asyncio.to_thread(search_started.wait, 1))

            self.assertTrue(await manager.cancel(research_id))
            release_search.set()
            record = repository.get(research_id)

        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["tasks"][0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
