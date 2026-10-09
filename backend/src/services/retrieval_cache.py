"""Versioned, expiring JSON cache; corruption or disk failure is a cache miss."""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


class RetrievalCache:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + '.json')

    def get(self, key, ttl):
        try:
            entry = json.loads(self.path(key).read_text(encoding='utf-8'))
            if entry['version'] == 1 and 0 <= time.time() - entry['created_at'] < ttl:
                return entry['data']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def put(self, key, data):
        temp_path = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory, delete=False) as handle:
                temp_path = handle.name
                json.dump({'version': 1, 'created_at': time.time(), 'data': data}, handle, ensure_ascii=False)
            os.replace(temp_path, self.path(key))
        except OSError:
            pass
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
