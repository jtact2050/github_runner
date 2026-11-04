"""Read persisted Codex JSONL incrementally without losing partial events."""
import json
import time
import uuid

from common import JobError


class SessionError(JobError):
    """Continuing could lose or cross issue conversation history."""


class SessionEvents:
    def __init__(self, stream, on_session, on_event=None):
        self.stream, self.on_session = stream, on_session
        self.on_event = on_event
        self.session_id = None
        self.last_message = None
        self.completed = False
        self.failure = None

    def consume(self, final=False, deadline=None):
        while True:
            # A continuously growing log must not starve the process watchdog.
            if deadline is not None and time.monotonic() >= deadline:
                return
            offset = self.stream.tell()
            line = self.stream.readline()
            if not line:
                return
            if not line.endswith(b'\n') and not final:
                self.stream.seek(offset)
                return
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                # Retain the raw line in the log; never mistake malformed output for success.
                self.failure = 'Malformed Codex JSONL event'
                continue
            if not isinstance(event, dict):
                self.failure = 'Invalid Codex JSONL event'
                continue
            if self.on_event:
                self.on_event(event, offset)
            kind = event.get('type')
            if kind == 'thread.started':
                session_id = event.get('thread_id')
                try:
                    if str(uuid.UUID(session_id)) != session_id:
                        raise ValueError()
                except (ValueError, TypeError, AttributeError):
                    raise SessionError('Codex returned an invalid session ID') from None
                if self.session_id and self.session_id != session_id:
                    raise SessionError('Codex changed session IDs during the same invocation')
                self.on_session(session_id)
                self.session_id = session_id
            elif kind == 'item.completed':
                item = event.get('item') or {}
                if isinstance(item, dict) and item.get('type') == 'agent_message':
                    message = item.get('text')
                    if not isinstance(message, str):
                        self.failure = 'Invalid Codex agent message'
                    else:
                        self.last_message = message
            elif kind == 'turn.completed':
                self.completed = True
            elif kind == 'turn.failed':
                self.failure = 'Codex turn failed: ' + json.dumps(event.get('error'))
