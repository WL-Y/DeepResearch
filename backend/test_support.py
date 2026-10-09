"""Small shared report fixtures; no model or network calls."""
from src.models import TaskSummary, TodoItem

GOOD = '# Report\n\nThe study reports a result of 42% under its test conditions[1].'


def summary():
    return TaskSummary(task=TodoItem(id=1, title='Task', query='query', intent='research'),
        summary='The study reports 42% under its test conditions[1].',
        sources=['https://example.org/paper'], source_titles=['Original paper'])
