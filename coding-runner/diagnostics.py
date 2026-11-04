"""Private, append-only issue history and durable diagnostic artifacts."""
import base64
import fcntl
import json
import os
from pathlib import Path

from common import stamp


class Diagnostics:
    def __init__(self, directory, repo, issue, *, run_id=None, token=None):
        self.directory = Path(directory)
        self.context = {'repo': repo, 'issue': int(issue), 'run_id': run_id}
        self.secrets = []
        if token:
            self.secrets = [token, base64.b64encode(f'x-access-token:{token}'.encode()).decode()]

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, '[redacted]')
        return text

    def event(self, event, **fields):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = dict(self.context, timestamp=stamp(), event=event, **fields)
        # Redact string values before encoding, including tokens containing JSON escapes.
        record = self.sanitize(record)
        path = self.directory / 'issue-log.jsonl'
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a') as stream:
            os.fchmod(stream.fileno(), 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def sanitize(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, bytes):
            return self.redact(value.decode('utf-8', errors='replace'))
        if isinstance(value, dict):
            return {self.redact(str(key)): self.sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.sanitize(item) for item in value]
        return value

    def write(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(self.sanitize(text))
            stream.flush()
            os.fsync(stream.fileno())

    def json(self, path, value):
        self.write(path, json.dumps(self.sanitize(value), indent=2, ensure_ascii=False) + '\n')

    def append(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a') as stream:
            os.fchmod(stream.fileno(), 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(self.sanitize(text))
            stream.flush()
            os.fsync(stream.fileno())
