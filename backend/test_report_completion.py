"""Truncation checkpoints and bounded completion without external model calls."""
import tempfile
import unittest
from unittest.mock import Mock

from src.config import Settings
from src.services.cost_control import OutputTruncated, ResearchLimit
from src.services.reporting import ReportingService
from src.tools.note import NoteTool
from test_support import GOOD, summary
from test_core import SequenceAgent


class ReportCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.notes = NoteTool(self.temp.name)

    def service(self, writer):
        return ReportingService(writer, self.notes)

    def test_complete_report_uses_one_writing_call(self):
        writer = Mock(run=Mock(return_value=GOOD))
        self.assertIn('42%', self.service(writer).generate('topic', [summary()], 'run'))
        self.assertEqual(writer.run.call_count, 1)

    def test_truncation_continues_once_and_preserves_prefix(self):
        prefix, tail = GOOD[:23], GOOD[23:]
        writer = Mock(run=Mock(side_effect=[OutputTruncated(prefix), tail]))
        snapshots = []
        save = self.notes.save_report_state
        def persist(run, state):
            snapshots.append(dict(state))
            return save(run, state)
        self.notes.save_report_state = persist
        service = self.service(writer)
        self.assertIn('42%', service.generate('topic', [summary()], 'run'))
        self.assertTrue(any(s.get('partial_draft') == prefix for s in snapshots))
        saved = self.notes.load_json('run', 'report-state.json')
        self.assertEqual(saved['draft'], GOOD)
        self.assertEqual(saved['continuation_attempts'], 1)
        self.assertNotIn('partial_draft', saved)
        self.assertEqual(writer.run.call_count, 2)

    def test_repeated_truncation_is_saved_and_resume_cannot_rewrite(self):
        writer = Mock(run=Mock(side_effect=[OutputTruncated('partial'), OutputTruncated(' tail')]))
        service = self.service(writer)
        with self.assertRaisesRegex(ResearchLimit, '续写仍达到'):
            service.generate('topic', [summary()], 'run')
        self.assertEqual(self.notes.load_json('run', 'report-state.json')['partial_draft'], 'partial tail')
        self.assertEqual(self.notes.load_report('run'), '')
        with self.assertRaisesRegex(ResearchLimit, '续写额度已用完'):
            self.service(writer).generate('topic', [summary()], 'run')
        self.assertEqual(writer.run.call_count, 2)

    def test_restart_uses_partial_checkpoint_without_first_draft_call(self):
        writer = Mock(run=Mock(side_effect=[OutputTruncated(GOOD[:23])]))
        service = self.service(writer)
        save = self.notes.save_report_state
        def crash_after_save(run, state):
            save(run, state)
            if state.get('partial_draft') and not state.get('continuation_attempts'):
                raise RuntimeError('process interrupted')
        self.notes.save_report_state = crash_after_save
        with self.assertRaisesRegex(RuntimeError, 'process interrupted'):
            service.generate('topic', [summary()], 'run')
        self.notes.save_report_state = save
        continuation_writer = Mock(run=Mock(return_value=GOOD[23:]))
        self.assertIn('42%', self.service(continuation_writer).generate('topic', [summary()], 'run'))
        self.assertEqual(continuation_writer.run.call_count, 1)

    def test_report_output_cap_is_separate_from_summary_cap(self):
        settings = Settings(_env_file=None)
        self.assertEqual(settings.llm_max_output_tokens, 5000)
        self.assertEqual(settings.report_max_output_tokens, 12000)


if __name__ == '__main__':
    unittest.main()
