from pathlib import Path
import re
import shutil
import json
import os
import tempfile


class NoteTool:
    """将研究笔记保存为 Markdown 文件"""

    _RESEARCH_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

    def __init__(self, workspace: str = "./workspace"):
        self.workspace = Path(workspace)
        self.runs_dir = self.workspace / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _run_dir(self, research_id: str) -> Path:
        """返回隔离的研究目录，并拒绝可能导致路径穿越的 ID。"""
        if not self._RESEARCH_ID_PATTERN.fullmatch(research_id):
            raise ValueError("无效的 research_id")
        return self.runs_dir / research_id

    def save_note(self, research_id: str, task_id: int, title: str, content: str):
        """保存任务笔记"""
        notes_dir = self._run_dir(research_id) / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        filepath = notes_dir / f"{task_id}.md"
        filepath.write_text(f"# {title}\n\n{content}", encoding="utf-8")
        return str(filepath)

    def save_report(self, research_id: str, content: str):
        """保存最终报告"""
        run_dir = self._run_dir(research_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        filepath = run_dir / "report.md"
        filepath.write_text(content, encoding="utf-8")
        return str(filepath)

    def load_note(self, research_id: str, task_id: int) -> str:
        """读取任务笔记"""
        filepath = self._run_dir(research_id) / "notes" / f"{task_id}.md"
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return ""

    def save_performance(self, research_id: str, attempt_id: str, data: dict):
        if not self._RESEARCH_ID_PATTERN.fullmatch(attempt_id):
            raise ValueError('无效的 attempt_id')
        return self._save_json(research_id, f'performance-{attempt_id}.json', data)

    def load_json(self, research_id, filename):
        # Only internal, fixed filenames may be passed by services.
        if filename not in {'cost.json', 'report-state.json', 'report-materials.json', 'report-input.json'}:
            raise ValueError('无效的状态文件')
        path = self._run_dir(research_id) / filename
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def load_cost_state(self, research_id):
        saved = self.load_json(research_id, 'cost.json')
        if saved is not None:
            if (not isinstance(saved, dict) or saved.get('version') != 1
                    or any(type(saved.get(k)) is not int or saved[k] < 0 for k in ('used', 'calls'))
                    or not isinstance(saved.get('reservations'), dict)
                    or any(type(v) is not int or v < 0 for v in saved.get('reservations', {}).values())
                    or not isinstance(saved.get('stage_used', {}), dict)
                    or any(not isinstance(k, str) or type(v) is not int or v < 0
                           for k, v in saved.get('stage_used', {}).items())
                    or not isinstance(saved.get('stage_calls', {}), dict)
                    or any(not isinstance(k, str) or type(v) is not int or v < 0
                           for k, v in saved.get('stage_calls', {}).items())
                    or not isinstance(saved.get('reservation_stages', {}), dict)
                    or any(k not in saved.get('reservations', {}) or not isinstance(v, str)
                           for k, v in saved.get('reservation_stages', {}).items())
                    or saved['used'] < sum(saved.get('reservations', {}).values())):
                raise ValueError('预算记录损坏，请检查后再继续，不能自动清零')
            return saved
        # Import previously observed usage instead of granting expensive old
        # research a fresh budget on every resume. Missing calls stay charged.
        used = calls = 0
        for path in self._run_dir(research_id).glob('performance-*.json'):
            record = json.loads(path.read_text(encoding='utf-8'))
            spans = [s for s in record.get('spans', []) if s.get('stage', '').startswith('llm.')]
            observed_calls = max(record.get('counters', {}).get('llm_logical_calls', 0), len(spans))
            calls += observed_calls
            for entry in spans:
                actual = entry.get('total_tokens')
                used += actual if type(actual) is int and actual >= 0 else entry.get('input_chars', 0) * 4 + 8000
            used += max(0, observed_calls - len(spans)) * 50000
        return {'version': 1, 'used': used, 'calls': calls, 'reservations': {},
                'stage_used': {}, 'stage_calls': {}, 'reservation_stages': {}}

    def save_cost_state(self, research_id, data):
        return self._save_json(research_id, 'cost.json', data)

    def save_report_state(self, research_id, data):
        return self._save_json(research_id, 'report-state.json', data)

    def save_report_materials(self, research_id, data):
        return self._save_json(research_id, 'report-materials.json', data)

    def save_report_input(self, research_id, data):
        return self._save_json(research_id, 'report-input.json', data)

    def task_progress(self, research_id, task_id, data=None):
        if type(task_id) is not int or task_id < 1:
            raise ValueError('无效任务编号')
        filename = f'task-state-{task_id}.json'
        if data is not None:
            return self._save_json(research_id, filename, data)
        path = self._run_dir(research_id) / filename
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def summary_batches(self, research_id, task_id, data=None):
        """Independent from gap state; workers checkpoint before returning."""
        if type(task_id) is not int or task_id < 1:
            raise ValueError('无效任务编号')
        filename = f'summary-batches-{task_id}.json'
        if data is not None:
            return self._save_json(research_id, filename, data)
        path = self._run_dir(research_id) / filename
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def reading_selection(self, research_id, task_id, data=None):
        if type(task_id) is not int or task_id < 1:
            raise ValueError('无效任务编号')
        filename = f'reading-selection-{task_id}.json'
        if data is not None:
            return self._save_json(research_id, filename, data)
        path = self._run_dir(research_id) / filename
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def source_note(self, research_id, key, data=None):
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ValueError('无效全文笔记键')
        filename = f'source-note-{key}.json'
        if data is not None:
            return self._save_json(research_id, filename, data)
        path = self._run_dir(research_id) / filename
        try:
            return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        except (OSError, ValueError):
            return None

    def _save_json(self, research_id: str, filename: str, data: dict):
        directory = self._run_dir(research_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as handle:
            temp_path = handle.name
            json.dump(data, handle, ensure_ascii=False, indent=2)
        try:
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        return str(path)

    def load_report(self, research_id: str) -> str:
        """读取最终报告"""
        filepath = self._run_dir(research_id) / "report.md"
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return ""

    def delete_run(self, research_id: str) -> bool:
        """删除指定研究的报告和中间笔记。"""
        run_dir = self._run_dir(research_id)
        if not run_dir.exists():
            return False
        shutil.rmtree(run_dir)
        return True
