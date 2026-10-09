import asyncio
import json
import threading
from collections import defaultdict
from typing import AsyncIterator, Dict, Optional, Set, Tuple
from uuid import uuid4
from src.models import TodoItem, TaskSummary

from src.repository import ResearchRepository, TERMINAL_STATUSES
from src.services.planning import PlanningService, DependencyQueryError
from src.services.reporting import ReportingService
from src.services.search import SearchService
from src.tools.search.http import SearchUnavailable
from src.services.summarization import SummarizationService
from src.tools.note import NoteTool
from src.services.performance import PerformanceRun, current_run, current_task, span
from src.services.cost_control import ResearchBudget, current_budget, ResearchLimit, OutputTruncated
from src.services.summary_batches import summarize_batches
from src.services.source_reader import SourceReader
from src.services.fulltext_analysis import analyze_fulltext
from src.services.source_notes import analyze_selected_sources


Event = Tuple[int, str, dict]


def sse_event(event_id: int, event_type: str, data: dict) -> bytes:
    payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
    return f"id: {event_id}\ndata: {payload}\n\n".encode("utf-8")


class ResearchManager:
    """Runs research independently from SSE clients and persists every event."""

    def __init__(
        self,
        repository: ResearchRepository,
        planning: PlanningService,
        search: SearchService,
        summarization: SummarizationService,
        reporting: ReportingService,
        notes: NoteTool,
        *, task_concurrency: int = 2,
        token_budget: int = 1500000, max_model_calls: int = 200,
        report_token_budget: int = 100000, report_max_model_calls: int = 2,
    ):
        self.repository = repository
        self.planning = planning
        self.search = search
        self.summarization = summarization
        self.reporting = reporting
        self.notes = notes
        self.task_concurrency = max(1, min(2, task_concurrency))
        self.token_budget, self.max_model_calls = token_budget, max_model_calls
        self.report_token_budget = report_token_budget
        self.report_max_model_calls = report_max_model_calls
        self._publish_locks = defaultdict(asyncio.Lock)
        self._percentages = {}
        self._jobs: Dict[str, asyncio.Task] = {}
        self._subscribers: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
        self.control_lock = asyncio.Lock()
        self._inflight: Dict[str, Set[asyncio.Task]] = defaultdict(set)
        self._budgets = {}

    def _launch(self, research_id: str, topic: str, *, resume=False):
        job = asyncio.create_task(self._run(research_id, topic, resume=resume), name=f'research-{research_id}')
        self._jobs[research_id] = job
        def finished(done):
            if self._jobs.get(research_id) is done:
                self._jobs.pop(research_id, None)
        job.add_done_callback(finished)

    async def _call(self, research_id, function, *args, **kwargs):
        # Cancelling an asyncio task cannot stop a synchronous LLM/search call.
        # Retain its handle to prevent overlapping writes when resuming.
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._inflight[research_id].add(worker)
        def finished(done):
            workers = self._inflight.get(research_id)
            if workers is not None:
                workers.discard(done)
                if not workers:
                    self._inflight.pop(research_id, None)
            if not done.cancelled():
                done.exception()  # Observe errors even after the parent was cancelled.
        worker.add_done_callback(finished)
        return await asyncio.shield(worker)

    async def resume(self, research_id: str) -> dict:
        async with self.control_lock:
            record = await asyncio.to_thread(self.repository.get, research_id)
            if not record:
                raise KeyError(research_id)
            job = self._jobs.get(research_id)
            if job and not job.done():
                if record['status'] in {'error', 'cancelled'}:
                    raise ValueError('上次研究正在退出，请稍后继续')
                return {'research_id': research_id, 'status': record['status']}
            if any(not worker.done() for worker in self._inflight.get(research_id, set())):
                raise ValueError('上次模型或检索请求仍在退出，请稍后再点继续研究')
            await asyncio.to_thread(self.repository.begin_resume, research_id)
            self._launch(research_id, record['topic'], resume=True)
            return {'research_id': research_id, 'status': 'resuming'}

    def _refresh_records(self, records):
        reader = getattr(self.search, 'source_reader', None)
        if isinstance(reader, SourceReader):
            for record in records:
                record.update(reader.refresh_metadata(record))

    def _summarize(self, research_id, task, results, previous_results=None, previous=None, on_progress=None):
        service = self.summarization
        batched = (isinstance(service, SummarizationService) and hasattr(service, 'agent')
                   and isinstance(self.notes, NoteTool))
        reader = getattr(self.search, 'source_reader', None)
        if batched and isinstance(reader, SourceReader):
            def record_progress(message):
                self.repository.save_checkpoint(research_id, task.id, results=results)
                if on_progress:
                    on_progress(message)
            analyze = analyze_selected_sources if service.optimize_reading else analyze_fulltext
            return analyze(service, self.notes, research_id, task, results, reader, record_progress)
        def summarize(current_task, batch, **kwargs):
            return summarize_batches(service, self.notes, research_id, current_task, batch, **kwargs)
        if previous is not None:
            return service.update_summary(task, previous_results, previous, results,
                                          **({'summarize': summarize} if batched else {}))
        return summarize(task, results) if batched else service.summarize(task, results)

    def _refresh_summary(self, summary):
        reader = getattr(self.search, 'source_reader', None)
        if not isinstance(reader, SourceReader):
            return summary
        for index, detail in enumerate(summary.source_details):
            if index < len(summary.sources):
                record = {**detail, 'url': summary.sources[index]}
                detail.update(reader.refresh_metadata(record))
        return summary

    def _restore_summary(self, research_id, task, checkpoint):
        saved = checkpoint.get('summary')
        if saved:
            try:
                summary = TaskSummary.model_validate(saved)
                if summary.task.id == task.id and summary.task.query == task.query and summary.summary.strip():
                    if (isinstance(self.summarization, SummarizationService)
                            and isinstance(getattr(self.search, 'source_reader', None), SourceReader)
                            and summary.retrieval.get('analysis_version') != 1):
                        return None  # Old excerpt summaries cannot stand in for full-text analysis.
                    summary.task = task
                    self._refresh_summary(summary)
                    return summary
            except (ValueError, TypeError):
                pass
        # Old runs have Markdown notes with a JSON provenance appendix.
        if (task.status == 'completed' and hasattr(self.notes, 'load_note')
                and not (isinstance(self.summarization, SummarizationService)
                         and isinstance(getattr(self.search, 'source_reader', None), SourceReader))):
            try:
                note = self.notes.load_note(research_id, task.id)
                content, marker, metadata = note.rpartition('\n\n## 来源记录与读取状态\n\n')
                if marker:
                    data = json.loads(metadata)
                    sources = data['sources']
                    if isinstance(sources, list) and sources and all(isinstance(s, dict) and s.get('url') for s in sources):
                        body = content.split('\n\n', 1)[-1].strip()
                        if body:
                            summary = TaskSummary(task=task, summary=body,
                                sources=[s['url'] for s in sources], source_titles=[s.get('title', s['url']) for s in sources],
                                source_details=sources)
                            self._refresh_summary(summary)
                            return summary
            except (OSError, ValueError, TypeError, KeyError):
                pass
        return None

    async def start(self, topic: str) -> str:
        research_id = uuid4().hex
        await asyncio.to_thread(self.repository.create, research_id, topic)
        self._launch(research_id, topic)
        return research_id

    async def cancel(self, research_id: str) -> bool:
        job = self._jobs.get(research_id)
        if not job or job.done():
            return False
        if research_id in self._budgets:
            self._budgets[research_id].stop()
        job.cancel()
        try:
            await job
        except asyncio.CancelledError:
            pass
        record = await asyncio.to_thread(self.repository.get, research_id)
        if record and record['status'] not in TERMINAL_STATUSES:
            # The job may have been cancelled before its coroutine first ran.
            await self._publish(research_id, 'progress',
                {'stage': 'cancelled', 'percentage': record['progress'], 'text': '研究已停止，可继续研究。'},
                {'status': 'cancelled', 'progress_text': '研究已停止，可继续研究。'})
        return True

    async def stream(
        self, research_id: str, after_event_id: int = 0
    ) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers[research_id].add(queue)
        last_sent = max(after_event_id, 0)

        try:
            boundary = await asyncio.to_thread(self.repository.resume_boundary, research_id)
            last_sent = max(last_sent, boundary - 1)
            events = await asyncio.to_thread(
                self.repository.events_after, research_id, last_sent
            )
            for event in events:
                last_sent = event["id"]
                yield sse_event(event["id"], event["type"], event["data"])
                if self._is_terminal_event(event["type"], event["data"]):
                    return

            record = await asyncio.to_thread(self.repository.get, research_id)
            if record and record["status"] in TERMINAL_STATUSES:
                return

            while True:
                try:
                    event_id, event_type, data = await asyncio.wait_for(
                        queue.get(), timeout=15
                    )
                except asyncio.TimeoutError:
                    yield b": keep-alive\n\n"
                    continue

                if event_id <= last_sent:
                    continue
                last_sent = event_id
                yield sse_event(event_id, event_type, data)
                if self._is_terminal_event(event_type, data):
                    return
        finally:
            subscribers = self._subscribers.get(research_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(research_id, None)

    async def _run(self, research_id: str, topic: str, *, resume=False) -> None:
        todos = []
        attempt_id = uuid4().hex
        recorder = PerformanceRun(
            lambda data: self.notes.save_performance(research_id, attempt_id, data)
                if hasattr(self.notes, 'save_performance') else None,
            resumed=resume, concurrency=self.task_concurrency)
        token = current_run.set(recorder)
        outcome = 'error'
        self._percentages[research_id] = 0
        budget, budget_token = None, None
        try:
            if hasattr(self.notes, 'load_cost_state'):
                budget = ResearchBudget(
                    self.notes, research_id, self.token_budget, self.max_model_calls,
                    # Legacy compression remains charged to `report` and to
                    # the global ledger. New writing has its own bounded reserve.
                    stage_token_limits={'report': self.report_token_budget,
                                        'report_write': self.report_token_budget},
                    stage_call_limits={'report': self.report_max_model_calls,
                                       'report_write': min(2, self.report_max_model_calls)},
                )
                self._budgets[research_id] = budget
                budget_token = current_budget.set(budget)
            checkpoints = await asyncio.to_thread(self.repository.checkpoints, research_id) if resume else {}
            if resume:
                record = await asyncio.to_thread(self.repository.get, research_id)
                todos = [TodoItem.model_validate(item) for item in record['tasks']]
            if not todos:
                await self._publish(
                    research_id,
                    "progress",
                    {"stage": "planning", "percentage": 5, "text": "正在规划研究任务..."},
                    {"status": "planning", "progress": 5, "progress_text": "正在规划研究任务..."},
                )

                with span('planning'):
                    todos = await self._call(research_id, self.planning.plan, topic)
            restored = {}
            for task in todos:
                saved = await self._call(research_id, self._restore_summary, research_id, task, checkpoints.get(task.id, {})) if resume else None
                if saved:
                    task.status = 'completed'
                    restored[task.id] = saved
                    results = checkpoints.get(task.id, {}).get('results')
                    if isinstance(results, list):
                        await self._call(research_id, self._refresh_records, results)
                    await self._call(research_id, self.repository.save_checkpoint,
                        research_id, task.id, summary=saved.model_dump(), results=results)
                else:
                    task.status = 'pending'
                    task.failure_reason = None
            await self._publish(
                research_id,
                "plan",
                {"tasks": [task.model_dump() for task in todos]},
                {"tasks_json": [task.model_dump() for task in todos], "error": None},
            )

            summaries_by_id = dict(restored)
            task_errors = {}

            async def execute(task):
                if task.id in restored:
                    return
                finished_count = sum(item.status in {'completed', 'failed'} for item in todos)
                percentage = 10 + int((finished_count / len(todos)) * 70)
                task.status = "running"
                progress_text = f"正在研究：{task.title}"
                await self._publish(
                    research_id,
                    "progress",
                    {
                        "stage": "executing",
                        "percentage": percentage,
                        "text": progress_text,
                        "task_id": task.id,
                    },
                    {
                        "status": "executing",
                        "progress": percentage,
                        "progress_text": progress_text,
                        "tasks_json": [item.model_dump() for item in todos],
                    },
                )

                try:
                    results = checkpoints.get(task.id, {}).get('results')
                    if not isinstance(results, list) or not results or not all(isinstance(r, dict) for r in results):
                        results = None
                    if results is not None:
                        pass  # Completed retrieval survives a failed summarization call.
                    else:
                        if task.depends_on:
                            try:
                                with span('dependency_query'):
                                    refined = await self._call(research_id, self.planning.refine_dependent,
                                        task, [summaries_by_id[d] for d in task.depends_on])
                            except DependencyQueryError as error:
                                # The already-planned queries remain valid input.
                                # Do not catch cancellation, quota errors or other
                                # failures, and do not pay for a formatting retry.
                                fallback = PlanningService.normalize_queries({
                                    'query': task.query, 'verification_query': task.verification_query})
                                refined = task.model_copy(update=fallback)
                                await self._publish(research_id, 'query_refinement_fallback', {
                                    'task_id': task.id, 'reason': str(error),
                                    'message': '搜索词优化未通过校验，已使用原计划查询继续研究。',
                                    'query': fallback['query'], 'verification_query': fallback['verification_query'],
                                })
                            task.query, task.verification_query = refined.query, refined.verification_query
                            await self._publish(research_id, 'plan',
                                {'tasks': [item.model_dump() for item in todos]},
                                {'tasks_json': [item.model_dump() for item in todos]})
                    if results is None and hasattr(self.search, "search_many"):
                        queries = [task.query]
                        if task.verification_query:
                            queries.append(task.verification_query)
                        kwargs = {'priority_terms': task.required_methods}
                        if isinstance(self.search, SearchService):
                            kwargs['fallback_context'] = f'{topic} {task.title}'
                        results = await self._call(research_id, self.search.search_many, queries, **kwargs)
                    elif results is None:
                        results = await self._call(research_id, self.search.search, task.query)
                    await self._call(research_id, self._refresh_records, results)
                    await self._call(research_id, self.repository.save_checkpoint, research_id, task.id, results=results)
                    partial = self.notes.task_progress(research_id, task.id) if hasattr(self.notes, 'task_progress') else None
                    loop, owner = asyncio.get_running_loop(), asyncio.current_task()
                    analysis_active = threading.Event()
                    analysis_active.set()
                    def analysis_progress(message):
                        async def deliver():
                            if owner.done() or not analysis_active.is_set():
                                return
                            await self._publish(research_id, 'progress',
                                {'stage': 'executing', 'percentage': percentage, 'text': message, 'task_id': task.id},
                                {'progress_text': message})
                        if not loop.is_closed():
                            asyncio.run_coroutine_threadsafe(deliver(), loop).result()
                    try:
                        if (partial and partial.get('query') == task.query
                                and isinstance(self.summarization, SummarizationService)):
                            summary = await self._call(research_id, self._summarize, research_id, task, results,
                                partial['results'], TaskSummary.model_validate(partial['summary']), on_progress=analysis_progress)
                        else:
                            summary = await self._call(research_id, self._summarize, research_id, task, results,
                                on_progress=analysis_progress)
                    finally:
                        analysis_active.clear()
                    await self._call(research_id, self._refresh_summary, summary)
                    if hasattr(self.notes, 'task_progress'):
                        await self._call(research_id, self.notes.task_progress, research_id, task.id,
                            {'query': task.query, 'results': results, 'summary': summary.model_dump()})
                    task.status = "completed"
                    await self._call(research_id, self.repository.save_checkpoint, research_id, task.id, summary=summary.model_dump())
                    await self._call(
                        research_id, self.notes.save_note,
                        research_id,
                        task.id,
                        task.title,
                        summary.summary + "\n\n## 来源记录与读取状态\n\n" + json.dumps(
                            {"sources": [{"url": url, "title": summary.source_titles[i] if i < len(summary.source_titles) else url,
                              **(summary.source_details[i] if i < len(summary.source_details) else {})}
                             for i, url in enumerate(summary.sources)]}, ensure_ascii=False, indent=2),
                    )
                    await self._publish(
                        research_id,
                        "task_done",
                        {"task_id": task.id, "summary": summary.summary},
                        {"tasks_json": [item.model_dump() for item in todos]},
                    )
                    summaries_by_id[task.id] = summary
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if isinstance(error, ResearchLimit) and not isinstance(error, OutputTruncated):
                        raise  # Real budget exhaustion still stops admission.
                    task.status = "failed"
                    task_errors[task.id] = (f'任务“{task.title}”输出被截断；已保存检索材料，其他独立任务继续。'
                                           if isinstance(error, OutputTruncated) else self._safe_error(error))
                    task.failure_reason = self._task_failure_reason(error)
                    await self._publish(
                        research_id,
                        "task_failed",
                        {"task_id": task.id, "message": task.failure_reason},
                        {"tasks_json": [item.model_dump() for item in todos]},
                    )

            async def bounded(task):
                task_token = current_task.set(task.id)
                try:
                    with span('research_task') as metrics:
                        await execute(task)
                        metrics['task_status'] = task.status
                finally:
                    current_task.reset(task_token)

            # Waiting dependencies do not occupy slots needed by ready work.
            pending = [task for task in todos if task.id not in restored]
            children = set()
            dependency_blocked = False
            try:
                while pending or children:
                    ready = [task for task in pending if all(d in summaries_by_id for d in task.depends_on)]
                    for task in ready[:max(0, self.task_concurrency - len(children))]:
                        pending.remove(task)
                        children.add(asyncio.create_task(bounded(task)))
                    if not children:
                        dependency_blocked = bool(pending)
                        for task in pending:
                            task.status = 'failed'
                            task.failure_reason = '前置任务未完成，因此本任务没有执行；已保留其余研究进度。完成前置任务后可继续研究重试。'
                            await self._publish(research_id, 'task_failed',
                                {'task_id': task.id, 'message': task.failure_reason},
                                {'tasks_json': [item.model_dump() for item in todos]})
                        break
                    done, children = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
                    for child in done:
                        child.result()
            finally:
                # Stop queued/running coroutines before publishing cancellation.
                # Shielded synchronous calls remain tracked in _inflight for resume.
                for child in children:
                    if not child.done():
                        child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
            summaries = [summaries_by_id[task.id] for task in todos if task.id in summaries_by_id]
            failure_reason = '；'.join(task_errors[k] for k in sorted(task_errors)[:2])
            if dependency_blocked:
                raise RuntimeError('前置任务失败，部分依赖任务尚未执行。已保存完成的研究。' + failure_reason)
            if not summaries:
                raise RuntimeError("所有子任务均执行失败，无法生成报告。" + failure_reason)
            await self._publish(
                research_id,
                "progress",
                {"stage": "reporting", "percentage": 85, "text": "正在撰写研究报告..."},
                {"status": "reporting", "progress": 85, "progress_text": "正在撰写研究报告..."},
            )

            loop, owner = asyncio.get_running_loop(), asyncio.current_task()
            report_active = threading.Event()
            report_active.set()

            def report_progress(text):
                async def deliver():
                    # A cancelled synchronous report worker may still finish;
                    # it must not revive the UI or append late progress events.
                    if owner.done() or not report_active.is_set():
                        return
                    await self._publish(research_id, 'progress',
                        {'stage': 'reporting', 'percentage': 85, 'text': text},
                        {'progress_text': text})
                if not loop.is_closed():
                    asyncio.run_coroutine_threadsafe(deliver(), loop).result()

            progress_kwargs = {'on_progress': report_progress} if isinstance(self.reporting, ReportingService) else {}
            try:
                report = await self._call(
                    research_id, self.reporting.generate, topic, summaries, research_id, **progress_kwargs
                )
            finally:
                report_active.clear()
            completion_text = '研究完成！'
            await self._publish(
                research_id,
                "report",
                {"content": report, "research_id": research_id},
                {"report": report},
            )
            await self._publish(
                research_id,
                "progress",
                {"stage": "completed", "percentage": 100, "text": completion_text},
                {"status": "completed", "progress": 100, "progress_text": completion_text, "error": None},
            )
            outcome = 'completed'
        except asyncio.CancelledError:
            outcome = 'cancelled'
            for task in todos:
                if task.status == "running":
                    task.status = "cancelled"
            record = await asyncio.to_thread(self.repository.get, research_id)
            percentage = record["progress"] if record else 0
            await asyncio.shield(
                self._publish(
                    research_id,
                    "progress",
                    {
                        "stage": "cancelled",
                        "percentage": percentage,
                        "text": "研究已取消。",
                    },
                    {
                        "status": "cancelled",
                        "progress_text": "研究已取消。",
                        "tasks_json": [task.model_dump() for task in todos] if todos else (record['tasks'] if record else []),
                    },
                )
            )
            raise
        except Exception as error:
            message = self._safe_error(error)
            for task in todos:
                if task.status == 'running':
                    task.status = 'cancelled'
            await self._publish(
                research_id,
                "error",
                {"message": message},
                {"status": "error", "error": message, "progress_text": "研究中断",
                 **({'tasks_json': [task.model_dump() for task in todos]} if todos else {})},
            )
        finally:
            if budget:
                budget.stop()
                self._budgets.pop(research_id, None)
            if budget_token is not None:
                current_budget.reset(budget_token)
            recorder.finish(outcome)
            current_run.reset(token)
            self._percentages.pop(research_id, None)
            self._publish_locks.pop(research_id, None)

    async def _publish(
        self,
        research_id: str,
        event_type: str,
        data: dict,
        updates: Optional[dict] = None,
    ) -> int:
        async with self._publish_locks[research_id]:
            data, updates = dict(data), dict(updates or {})
            if event_type == 'progress' and data.get('stage') == 'executing':
                percentage = max(data['percentage'], self._percentages.get(research_id, 0))
                self._percentages[research_id] = percentage
                data['percentage'] = percentage
                updates['progress'] = percentage

            async def commit():
                event_id = await asyncio.to_thread(
                    self.repository.record_event, research_id, event_type, data, updates)
                for queue in list(self._subscribers.get(research_id, set())):
                    queue.put_nowait((event_id, event_type, data))
                return event_id

            writer = asyncio.create_task(commit())
            try:
                return await asyncio.shield(writer)
            except asyncio.CancelledError:
                # A SQLite write cannot be cancelled; finish it before releasing
                # the lock so no executing event arrives after the terminal event.
                await writer
                raise

    @staticmethod
    def _is_terminal_event(event_type: str, data: dict) -> bool:
        return event_type == "error" or (
            event_type == "progress"
            and data.get("stage") in {"completed", "cancelled"}
        )

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error).strip()
        return message[:500] if message else error.__class__.__name__

    @staticmethod
    def _task_failure_reason(error: Exception) -> str:
        if isinstance(error, SearchUnavailable):
            categories = {warning.get('category') for warning in error.warnings}
            providers = {warning.get('provider') for warning in error.warnings if warning.get('provider')}
            if categories and categories <= {'no_results'}:
                return (f'搜索渠道均未返回相关资料（已尝试 {len(providers)} 个渠道），因此该任务未进入分析。'
                        '其他独立任务已继续完成，可点击“继续研究”重试。')
            reasons = []
            if 'timeout' in categories:
                reasons.append('部分搜索渠道超时')
            if 'rate_limited' in categories:
                reasons.append('部分搜索渠道受到限流')
            if categories & {'network_error', 'provider_error', 'server_error'}:
                reasons.append('部分搜索渠道暂不可用')
            if 'authentication_error' in categories:
                reasons.append('搜索服务认证配置异常')
            if categories & {'access_denied', 'blocked'}:
                reasons.append('搜索服务拒绝访问')
            if not reasons:
                reasons.append('搜索渠道未返回可用资料')
            return '；'.join(reasons) + '。已保留其他研究进度，可点击“继续研究”重试。'
        if isinstance(error, OutputTruncated):
            return '分析结果超出模型单次输出上限；已保存检索材料，其他独立任务继续完成，可点击“继续研究”重试。'
        return '该子任务在检索或分析阶段未能完成；已保留其他研究进度，可点击“继续研究”重试。'
