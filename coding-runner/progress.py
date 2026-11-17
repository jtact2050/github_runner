"""Observable CLI activity, without treating silence or process liveness as success."""
import json
import os
import time
import uuid

from common import save_json, stamp


def link_progress_log(state_dir, directory):
    """Point at the latest run without losing the old combined issue log."""
    target = (directory / 'progress.log').relative_to(state_dir)
    if '..' in target.parts or not (directory / 'progress.log').is_file():
        raise ValueError('Progress target must exist inside the issue directory')
    link = state_dir / 'progress.log'
    if link.exists() and not link.is_symlink():
        history = state_dir / 'progress-history.log'
        if history.exists():
            history = state_dir / f'progress-history-{uuid.uuid4().hex}.log'
        link.rename(history)
    temporary = state_dir / f'.progress-link-{uuid.uuid4().hex}'
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


CLAUDE_EVENT_TYPES = {'system', 'assistant', 'user', 'result', 'rate_limit_event', 'stream_event'}
CLAUDE_FILE_TOOLS = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}


def tool_detail(name, tool_input):
    if not isinstance(tool_input, dict):
        return name
    for key in ('command', 'file_path', 'pattern', 'path', 'description'):
        if isinstance(tool_input.get(key), str):
            return f'{name}: {tool_input[key]}'
    return name


class CLIProgress:
    def __init__(self, worker, directory):
        self.worker, self.directory = worker, directory
        self.diagnostics = worker.diagnostics
        self.started = self.last_output = time.monotonic()
        self.last_heartbeat = self.started
        self.active = {}
        self.completed_commands = set()
        self.byte_counts = {'stdout': 0, 'stderr': 0}
        self.data = {
            'repo': worker.api.repo, 'issue': worker.number, 'run_id': worker.run_id,
            'round': worker.state['rounds'], 'status': 'preparing', 'pid': None,
            'started_at': stamp(), 'timeout_seconds': worker.cli_timeout,
            'heartbeat_interval_seconds': worker.progress_interval,
            'artifacts': str(directory.relative_to(worker.state_dir)),
            'event_count': 0, 'commands_completed': 0, 'commands_failed': 0,
            'last_activity': 'Preparing CLI invocation', 'last_output_at': None,
        }
        self.diagnostics.append(self.directory / 'progress.log', '')
        link_progress_log(worker.state_dir, directory)
        self.write('Preparing one uninterrupted CLI run')
        self.snapshot()

    def write(self, message, console=False):
        if not message:
            return
        text = f'[{stamp()}] run={self.worker.run_id} round={self.data["round"]} {message}\n'
        self.diagnostics.append(self.directory / 'progress.log', text)
        self.diagnostics.append(self.worker.state_dir / 'progress-history.log', text)
        if console:
            print(self.diagnostics.redact(f'issue #{self.worker.number}: {message}'), flush=True)

    def process_started(self, pid):
        self.started = self.last_output = self.last_heartbeat = time.monotonic()
        self.data.update(status='running', pid=pid, started_at=stamp(),
                         last_activity='CLI process started; waiting for first event')
        self.write(f'CLI started: pid={pid}, timeout={self.worker.cli_timeout}s', console=True)
        self.snapshot()

    def on_event(self, event, offset):
        self.last_output = time.monotonic()
        self.data.update(last_output_at=stamp(), event_count=self.data['event_count'] + 1)
        kind = event.get('type', 'unknown')
        self.data['last_event_type'] = kind
        self.diagnostics.append(self.directory / 'cli-events.jsonl', json.dumps(
            {'timestamp': stamp(), 'elapsed_seconds': round(self.last_output - self.started, 3),
             'stdout_offset': offset, 'event': event}, ensure_ascii=False) + '\n')
        if kind in CLAUDE_EVENT_TYPES:
            self.write(self.claude_event(event, kind))
            return
        if kind == 'thread.started':
            self.data['session_id'] = event.get('thread_id')
        if 'usage' in event:
            self.data['usage'] = event['usage']
        item = event.get('item')
        message = kind
        if isinstance(item, dict):
            identity, item_type = item.get('id'), item.get('type', 'unknown')
            detail = item.get('command') or item.get('text') or item.get('tool') or item_type
            if not isinstance(detail, str):
                detail = json.dumps(detail, ensure_ascii=False)
            self.data['last_activity'] = detail[:4000]
            if identity and (kind == 'item.started' or kind == 'item.updated' and identity in self.active):
                active = self.active.setdefault(identity, {
                    'id': identity, 'type': item_type,
                    'started_elapsed_seconds': round(self.last_output - self.started, 3)})
                active['description'] = detail[:4000]
            if kind == 'item.completed':
                active = self.active.pop(identity, None)
                if item_type == 'command_execution':
                    if identity is None or identity not in self.completed_commands:
                        if identity is not None:
                            self.completed_commands.add(identity)
                        self.data['commands_completed'] += 1
                        if type(item.get('exit_code')) is int and item['exit_code'] != 0:
                            self.data['commands_failed'] += 1
                    self.data['last_command'] = {
                        'command': detail, 'exit_code': item.get('exit_code'),
                        'status': item.get('status'),
                        'duration_seconds': round(self.last_output - self.started - active['started_elapsed_seconds'], 3)
                        if active else None}
            if item_type in ('todo_list', 'plan'):
                self.data['plan'] = item
            if item_type == 'file_change':
                self.data['latest_file_changes'] = item.get('changes', [])
            message = f'{kind} {item_type}: {detail}'
            if item_type == 'command_execution':
                message += f' [status={item.get("status")}, exit={item.get("exit_code")}]'
                if item.get('aggregated_output'):
                    message += '\n' + item['aggregated_output']
            elif item_type not in ('agent_message', 'reasoning'):
                message += '\n' + json.dumps(item, ensure_ascii=False)
        else:
            message += ' ' + json.dumps(event, ensure_ascii=False)
            self.data['last_activity'] = message[:4000]
        self.write(message)

    def claude_event(self, event, kind):
        """Track Claude stream-json tool calls like Codex items."""
        elapsed = round(self.last_output - self.started, 3)
        message = event.get('message') if isinstance(event.get('message'), dict) else {}
        content = message.get('content') if isinstance(message.get('content'), list) else []
        lines = []
        if kind == 'system':
            if event.get('subtype') == 'init':
                self.data['session_id'] = event.get('session_id')
                self.data['model'] = event.get('model')
                lines.append(f"session started: model={event.get('model')}")
            elif event.get('subtype') == 'thinking_tokens':
                return ''
            else:
                lines.append('system ' + json.dumps(event, ensure_ascii=False)[:4000])
        elif kind == 'assistant':
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'text' and (block.get('text') or '').strip():
                    self.data['last_activity'] = block['text'][:4000]
                    lines.append('assistant: ' + block['text'])
                elif block.get('type') == 'tool_use':
                    name, tool_input = block.get('name', 'tool'), block.get('input')
                    detail = tool_detail(name, tool_input)
                    self.data['last_activity'] = detail[:4000]
                    if block.get('id'):
                        self.active[block['id']] = {'id': block['id'], 'type': name, 'description': detail[:4000],
                                                    'started_elapsed_seconds': elapsed}
                    if name == 'TodoWrite':
                        self.data['plan'] = tool_input
                    elif name in CLAUDE_FILE_TOOLS and isinstance(tool_input, dict):
                        self.data['latest_file_changes'] = [{'path': tool_input.get('file_path'), 'tool': name}]
                    lines.append('tool_use ' + detail)
            if message.get('usage'):
                self.data['usage'] = message['usage']
        elif kind == 'user':
            for block in content:
                if not isinstance(block, dict) or block.get('type') != 'tool_result':
                    continue
                active = self.active.pop(block.get('tool_use_id'), None)
                output = block.get('content')
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                failed = bool(block.get('is_error'))
                if active and active['type'] == 'Bash':
                    self.data['commands_completed'] += 1
                    self.data['commands_failed'] += failed
                    # Claude reports failures, not numeric exit codes.
                    self.data['last_command'] = {
                        'command': active['description'], 'exit_code': None, 'status': 'failed' if failed else 'completed',
                        'duration_seconds': round(elapsed - active['started_elapsed_seconds'], 3)}
                label = active['description'] if active else block.get('tool_use_id')
                lines.append(f"tool_result {label} [error={failed}]\n{output[:8000]}")
        elif kind == 'result':
            if event.get('usage'):
                self.data['usage'] = event['usage']
            self.data['last_activity'] = f"result: {event.get('subtype')}"
            lines.append(f"result {event.get('subtype')}: is_error={event.get('is_error')}, "
                         f"turns={event.get('num_turns')}, terminal_reason={event.get('terminal_reason')}")
        elif kind == 'rate_limit_event':
            info = event.get('rate_limit_info') or {}
            if info.get('status') == 'allowed':
                return ''
            lines.append('rate limit ' + json.dumps(info, ensure_ascii=False))
        return '\n'.join(lines)

    def poll(self, exit_code=None, force=False):
        now = time.monotonic()
        for name in self.byte_counts:
            path = self.directory / (name + '.log')
            size = path.stat().st_size if path.exists() else 0
            if size != self.byte_counts[name]:
                self.last_output = now
                self.data['last_output_at'] = stamp()
                if name == 'stderr':
                    with path.open('rb') as stream:
                        stream.seek(max(self.byte_counts[name], size - 8192))
                        tail = stream.read().decode('utf-8', errors='replace')
                    self.data['stderr_tail'] = tail
                    self.write('stderr activity (latest up to 8 KiB; full output in stderr.log):\n' + tail)
            self.byte_counts[name] = size
        self.data['exit_code'] = exit_code
        if force or now - self.last_heartbeat >= self.worker.progress_interval:
            self.last_heartbeat = now
            self.snapshot()
            self.worker.event('codex_heartbeat', progress=self.data)
            self.write(f'{self.data["status"]}: elapsed={self.data["elapsed_seconds"]}s, '
                       f'remaining={self.data["remaining_seconds"]}s, '
                       f'no new output for={self.data["seconds_since_output"]}s, '
                       f'events={self.data["event_count"]}, active_items={len(self.active)}, '
                       f'commands_finished={self.data["commands_completed"]}, '
                       f'failed_command_runs={self.data["commands_failed"]}; '
                       f'last activity: {self.data["last_activity"][:240]}', console=True)
        else:
            self.snapshot()

    def snapshot(self):
        now = time.monotonic()
        self.data.update(updated_at=stamp(), elapsed_seconds=round(now - self.started, 1),
                         remaining_seconds=round(max(0, self.worker.cli_timeout - (now - self.started)), 1),
                         seconds_since_output=round(now - self.last_output, 1),
                         active_items=list(self.active.values()), output_bytes=dict(self.byte_counts))
        data = self.diagnostics.sanitize(self.data)
        save_json(self.directory / 'progress.json', data)
        save_json(self.worker.state_dir / 'progress.json', data)

    def finish(self, status, **details):
        self.data.update(status=status, finished_at=stamp(), **details)
        self.poll(exit_code=self.data.get('exit_code'), force=True)
