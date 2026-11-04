"""Observable CLI activity, without treating silence or process liveness as success."""
import json
import time

from common import save_json, stamp


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
        self.write('Preparing one uninterrupted CLI run')
        self.snapshot()

    def write(self, message, console=False):
        text = f'[{stamp()}] run={self.worker.run_id} round={self.data["round"]} {message}\n'
        self.diagnostics.append(self.directory / 'progress.log', text)
        self.diagnostics.append(self.worker.state_dir / 'progress.log', text)
        if console:
            print(self.diagnostics.redact(f'issue #{self.worker.number}: {message}'), flush=True)

    def process_started(self, pid):
        self.started = self.last_output = self.last_heartbeat = time.monotonic()
        self.data.update(status='running', pid=pid, started_at=stamp())
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
