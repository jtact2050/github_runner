import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import JobError, load_json
from dispatch import Dispatcher, arguments as dispatch_arguments
from worker import Worker, arguments as worker_arguments
from test_worker import FakeAPI
from result_fixture import SESSION_ID, model_result


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.api = FakeAPI()
        self.api.token = 'test-private-token'
        self.worker = Worker(self.api, 1, root=self.directory / 'repo', state_dir=self.directory / 'state',
                             cli_timeout=5, progress_interval=0.2)
        self.worker.root.mkdir()

    def test_real_process_repairs_test_failure_without_cli_restart_and_reports_live_progress(self):
        initial = [
            {'type': 'thread.started', 'thread_id': SESSION_ID},
            {'type': 'item.updated', 'item': {'id': 'plan', 'type': 'todo_list',
             'items': [{'text': 'Reproduce, repair and validate', 'completed': False}]}},
            {'type': 'item.started', 'item': {'id': 'test1', 'type': 'command_execution', 'command': 'python3 -m unittest', 'status': 'in_progress'}},
        ]
        tail = [
            {'type': 'item.completed', 'item': {'id': 'test1', 'type': 'command_execution', 'command': 'python3 -m unittest',
             'status': 'failed', 'exit_code': 1, 'aggregated_output': 'AssertionError: expected 2; test-private-token'}},
            {'type': 'item.completed', 'item': {'id': 'edit', 'type': 'file_change', 'changes': [{'path': 'logic.py', 'kind': 'update'}]}},
            {'type': 'item.started', 'item': {'id': 'test2', 'type': 'command_execution', 'command': 'python3 -m unittest', 'status': 'in_progress'}},
            {'type': 'item.completed', 'item': {'id': 'test2', 'type': 'command_execution', 'command': 'python3 -m unittest',
             'status': 'completed', 'exit_code': 0, 'aggregated_output': 'All regression tests passed'}},
            {'type': 'item.completed', 'item': {'id': 'plan', 'type': 'todo_list', 'items': [{'text': 'Reproduce, repair and validate', 'completed': True}]}},
            {'type': 'item.completed', 'item': {'id': 'answer', 'type': 'agent_message', 'text': json.dumps(model_result())}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 50}},
        ]
        # The child reads a snapshot while still running, proving that progress is
        # persisted before completion. It models several commands in ONE CLI PID.
        script = ('import sys,time,json; from pathlib import Path; sys.stdin.read(); '
                  f'print({chr(10).join(json.dumps(e) for e in initial)!r}, flush=True); '
                  'sys.stderr.write("test process is running\\n"); sys.stderr.flush(); time.sleep(2.3); '
                  f'p=Path({str(self.worker.state_dir / "progress.json")!r}); '
                  f'Path({str(self.directory / "during.json")!r}).write_text(p.read_text()); '
                  f'print({chr(10).join(json.dumps(e) for e in tail)!r}, flush=True)')
        popen = subprocess.Popen
        def start(command, **kwargs):
            return popen([sys.executable, '-c', script], **kwargs)
        with patch('worker.subprocess.Popen', side_effect=start) as cli, \
                patch.object(self.worker, 'integrate_base'), patch.object(self.worker, 'publish') as publish:
            self.worker.work(self.api.issue, 'Implement and test in one run', [])
        cli.assert_called_once()
        publish.assert_called_once()
        live = load_json(self.directory / 'during.json')
        self.assertEqual(live['status'], 'running')
        self.assertEqual(live['session_id'], SESSION_ID)
        self.assertTrue(any(item['id'] == 'test1' for item in live['active_items']))
        self.assertGreater(live['seconds_since_output'], 0)
        self.assertLess(live['remaining_seconds'], 5)
        final = load_json(self.worker.state_dir / 'progress.json')
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(final['commands_completed'], 2)
        self.assertEqual(final['commands_failed'], 1)
        self.assertEqual(final['active_items'], [])
        self.assertEqual(final['usage']['output_tokens'], 50)
        self.assertEqual(final['last_command']['exit_code'], 0)
        directory = self.worker.state_dir / final['artifacts']
        records = [json.loads(line) for line in (directory / 'cli-events.jsonl').read_text().splitlines()]
        self.assertEqual(len(records), len(initial) + len(tail))
        self.assertEqual([row['stdout_offset'] for row in records], sorted(set(row['stdout_offset'] for row in records)))
        readable = (self.worker.state_dir / 'progress.log').read_text()
        self.assertIn('AssertionError: expected 2', readable)
        self.assertIn('All regression tests passed', readable)
        self.assertIn('stderr activity', readable)
        self.assertNotIn(self.api.token, readable)
        self.assertNotIn(self.api.token, (directory / 'cli-events.jsonl').read_text())
        self.assertTrue(final['plan']['items'][0]['completed'])
        for path in [directory / 'progress.json', directory / 'progress.log', directory / 'cli-events.jsonl',
                     self.worker.state_dir / 'progress.json', self.worker.state_dir / 'progress.log']:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_configured_timeout_stops_one_process_and_preserves_output_for_automatic_retry(self):
        self.worker.cli_timeout = 0.3
        prefix = json.dumps({'type': 'thread.started', 'thread_id': SESSION_ID})
        script = f'import sys,time; sys.stdin.read(); print({prefix!r}, flush=True); sys.stderr.write("still testing\\n"); sys.stderr.flush(); time.sleep(30)'
        popen = subprocess.Popen
        children = []
        def start(command, **kwargs):
            process = popen([sys.executable, '-c', script], **kwargs)
            children.append(process)
            return process
        with patch('worker.subprocess.Popen', side_effect=start) as cli, \
                patch.object(self.worker, 'integrate_base'), patch.object(self.worker, 'publish') as publish, \
                self.assertRaisesRegex(JobError, 'CLI run stopped'):
            self.worker.work(self.api.issue, 'Implement', [])
        cli.assert_called_once()
        publish.assert_not_called()
        self.assertIsNotNone(children[0].poll())
        self.assertNotIn('manual_retry_required', load_json(self.worker.path))
        final = load_json(self.worker.state_dir / 'progress.json')
        self.assertEqual(final['status'], 'timed_out')
        self.assertEqual(final['timeout_seconds'], 0.3)
        self.assertEqual(final['session_id'], SESSION_ID)
        directory = self.worker.state_dir / final['artifacts']
        self.assertIn(prefix, (directory / 'stdout.log').read_text())
        self.assertIn('still testing', (directory / 'stderr.log').read_text())

    def test_cli_parameters_defaults_validation_and_container_forwarding(self):
        for parse, required in [(dispatch_arguments, ['--repo', 'owner/repo']),
                                (worker_arguments, ['--repo', 'owner/repo', '--issue', '1'])]:
            self.assertEqual(parse(required).cli_timeout, 7200)
            for option in ('--cli-timeout', '--progress-interval'):
                for bad in ('0', '-1', 'not-a-number'):
                    with self.subTest(option=option, value=bad), self.assertRaises(SystemExit):
                        parse(required + [option, bad])
        args = dispatch_arguments(['--repo', self.api.repo, '--cli-timeout', '10800', '--progress-interval', '10'])
        self.api.issue['milestone'] = {'number': 1, 'state': 'open'}
        with patch('dispatch.ROOT', self.directory):
            queue = Dispatcher(self.api, args)
            with patch('dispatch.inspect_container', return_value=None), \
                    patch.object(self.api, 'pages', return_value=[], create=True), \
                    patch('dispatch.docker') as docker:
                docker.return_value.stdout = 'id'
                queue.launch(1, 1)
            command = docker.call_args.args
            self.assertEqual(command[command.index('--cli-timeout') + 1], '10800')
            self.assertEqual(command[command.index('--progress-interval') + 1], '10')


if __name__ == '__main__':
    unittest.main()
