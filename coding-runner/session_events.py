"""Read persisted Codex/Claude JSONL incrementally without losing partial events."""
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


def valid_session_id(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


class ClaudeSessionEvents:
    """Claude Code `--output-format stream-json` with the same reader interface.

    The controller assigns the session ID before launch (`--session-id`), so the
    init event only confirms it. The final `result` event carries the
    schema-validated `structured_output`, exposed as `last_message` JSON.
    """

    def __init__(self, stream, on_session, on_event=None):
        self.stream, self.on_session = stream, on_session
        self.on_event = on_event
        self.session_id = None
        self.last_message = None
        self.completed = False
        self.failure = None
        self.result = None

    def consume(self, final=False, deadline=None):
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return
            offset = self.stream.tell()
            line = self.stream.readline()
            if not line:
                return
            if not line.endswith(b'\n') and not final:
                self.stream.seek(offset)
                return
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                self.failure = 'Malformed Claude stream-json event'
                continue
            if not isinstance(event, dict):
                self.failure = 'Invalid Claude stream-json event'
                continue
            if self.on_event:
                self.on_event(event, offset)
            session_id = event.get('session_id')
            if session_id is not None:
                if not valid_session_id(session_id):
                    raise SessionError('Claude returned an invalid session ID')
                if self.session_id and self.session_id != session_id:
                    raise SessionError('Claude changed session IDs during the same invocation')
                if not self.session_id:
                    self.on_session(session_id)
                    self.session_id = session_id
            if event.get('type') == 'result':
                self.result = event
                if event.get('is_error') or event.get('subtype') != 'success':
                    status = event.get('api_error_status')
                    detail = event.get('result') if isinstance(event.get('result'), str) else ''
                    self.failure = (f"Claude run failed ({event.get('subtype')}"
                                    + (f', HTTP {status}' if status else '') + f'): {detail[:2000]}')
                    continue
                output = event.get('structured_output')
                if output is None:
                    self.failure = 'Claude returned no structured output'
                    continue
                self.last_message = json.dumps(output, ensure_ascii=False)
                self.completed = True
