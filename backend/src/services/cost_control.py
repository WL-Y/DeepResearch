"""Persistent, fail-closed admission control, not a provider billing guarantee."""
import threading
import math
from contextvars import ContextVar
from uuid import uuid4

current_budget = ContextVar('research_budget', default=None)


class ResearchLimit(RuntimeError):
    pass


class OutputTruncated(ResearchLimit):
    """Paid but incomplete output, available for a caller's checkpoint."""
    def __init__(self, content):
        super().__init__('模型达到单次输出上限，内容尚未完整；已停止当前调用。')
        self.content = content


class ResearchBudget:
    def __init__(self, notes, research_id, token_limit=1500000, call_limit=200,
                 stage_token_limits=None, stage_call_limits=None):
        self.notes, self.research_id = notes, research_id
        self.token_limit, self.call_limit = token_limit, call_limit
        self.lock = threading.Lock()
        self.stopped = False
        self.data = notes.load_cost_state(research_id)
        self.stage_token_limits = stage_token_limits or {}
        self.stage_call_limits = stage_call_limits or {}
        self.data.setdefault('stage_used', {})
        self.data.setdefault('stage_calls', {})
        self.data.setdefault('reservation_stages', {})

    @staticmethod
    def estimate_tokens(prompt):
        """Conservative provider-independent estimate for mixed CJK/ASCII text."""
        non_ascii = sum(ord(character) > 127 for character in prompt)
        ascii_chars = len(prompt) - non_ascii
        return non_ascii + math.ceil(ascii_chars / 4)

    def reserve(self, prompt, output_limit, stage='other'):
        # Reconcile with provider-reported usage after the call.  The estimate
        # is only an admission guard and deliberately includes an envelope.
        reserved = self.estimate_tokens(prompt) + output_limit + 1024
        with self.lock:
            if self.stopped:
                raise ResearchLimit('研究已停止，不再发起新的模型请求。')
            if self.data['used'] + reserved > self.token_limit or self.data['calls'] >= self.call_limit:
                raise ResearchLimit('本项研究已达到累计模型预算或调用上限，已保存进度。继续研究不会重置额度；请检查预算后再继续。')
            stage_used = self.data['stage_used'].get(stage, 0)
            stage_calls = self.data['stage_calls'].get(stage, 0)
            if (stage in self.stage_token_limits
                    and stage_used + reserved > self.stage_token_limits[stage]):
                raise ResearchLimit(f'{stage} 阶段已达到模型预算上限。')
            if (stage in self.stage_call_limits
                    and stage_calls >= self.stage_call_limits[stage]):
                raise ResearchLimit(f'{stage} 阶段已达到模型调用上限。')
            key = uuid4().hex
            self.data['used'] += reserved
            self.data['calls'] += 1
            self.data['reservations'][key] = reserved
            self.data['reservation_stages'][key] = stage
            self.data['stage_used'][stage] = stage_used + reserved
            self.data['stage_calls'][stage] = stage_calls + 1
            self._save()
            return key

    def settle(self, key, actual):
        with self.lock:
            reserved = self.data['reservations'].pop(key)
            stage = self.data['reservation_stages'].pop(key, 'other')
            # A timeout, missing usage, or process crash retains the full
            # reservation: an unknown bill is not a free request.
            charged = actual if type(actual) is int and actual >= 0 else reserved
            self.data['used'] += charged - reserved
            self.data['stage_used'][stage] = self.data['stage_used'].get(stage, reserved) + charged - reserved
            self._save()

    def release(self, key):
        """Release a request proven not to have reached a model response."""
        with self.lock:
            reserved = self.data['reservations'].pop(key)
            stage = self.data['reservation_stages'].pop(key, 'other')
            self.data['used'] = max(0, self.data['used'] - reserved)
            self.data['calls'] = max(0, self.data['calls'] - 1)
            self.data['stage_used'][stage] = max(
                0, self.data['stage_used'].get(stage, reserved) - reserved
            )
            self.data['stage_calls'][stage] = max(
                0, self.data['stage_calls'].get(stage, 1) - 1
            )
            self._save()

    def _save(self):
        self.notes.save_cost_state(self.research_id, self.data)

    def stop(self):
        with self.lock:
            self.stopped = True
