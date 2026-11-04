import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import load_json, save_json
from session_events import SessionError, SessionEvents
from worker import Worker
from test_worker import FakeAPI
from result_fixture import SESSION_ID, event_stream, model_result


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.api = FakeAPI()
        self.worker = Worker(self.api, 1, root=self.directory / 'repo', state_dir=self.directory / 'state')
        self.worker.root.mkdir()

    def run_cli(self, worker, session_id=SESSION_ID):
        popen = subprocess.Popen
        commands = []
        output = event_stream(json.dumps(model_result()), session_id=session_id)
        def start(command, **kwargs):
            commands.append(command)
            script = ('import sys; from pathlib import Path; '
                      'assert not sys.stdin.isatty(); '
                      'Path("received-prompt.txt").write_text(sys.stdin.read()); '
                      f'print({output!r}, end="")')
            return popen([sys.executable, '-c', script], **kwargs)
        with patch('worker.subprocess.Popen', side_effect=start):
            worker.run_codex(self.api.issue, 'Implement and test', [])
        return commands[0]

    def test_new_and_resumed_jobs_receive_unattended_policy_and_stdin_eof(self):
        self.worker.cli_timeout = 5
        for resumed in (False, True):
            with self.subTest(resumed=resumed):
                command = self.run_cli(self.worker)
                self.assertEqual('resume' in command, resumed)
                self.assertEqual(command[command.index('-a') + 1], 'never')
                self.assertEqual(command[command.index('--sandbox') + 1], 'workspace-write')
                for setting in ('CI=1', 'GIT_TERMINAL_PROMPT=0', 'GCM_INTERACTIVE=never',
                                'PAGER=cat', 'GIT_PAGER=cat'):
                    self.assertIn(setting, command)
                # The subprocess can only return a result after reading EOF from
                # the actual prompt pipe; no interactive stdin remains open.
                prompt = (self.worker.root / 'received-prompt.txt').read_text()
                self.assertIn('No user is available to answer.', prompt)
                self.assertIn('Do not ask the user questions', prompt)
                self.assertIn('choose the best supported approach', prompt)
                self.assertIn('A preference or design choice is not a blocker.', prompt)
                self.assertIn('Never invent credentials,', prompt)
                self.assertIn('structured blocker rules below', prompt)
                self.assertIn('The trusted controller handles commits', prompt)
                self.assertIn("automatic pull-request/push CI workflows", prompt)
                self.assertIn('Local validation is limited to exactly these two categories:', prompt)
                self.assertIn('1. pnpm test:all as the baseline.', prompt)
                self.assertIn('2. Required automatic CI checks that pnpm test:all does not cover', prompt)
                self.assertIn('Do not run individual test files, focused tests, extra suites, benchmarks, or', prompt)
                self.assertIn('even while debugging.', prompt)
                self.assertNotIn('Run focused checks while fixing code', prompt)
                self.assertNotIn('workflow suites are opt-in', prompt)
                self.assertIn('Do not repeat a complete passing gate just because a session resumed.', prompt)
                self.assertIn('reassess historical next_steps using this policy', prompt)
                self.assertIn('or ignore known product failures.', prompt)

    def test_same_session_resumes_across_passes_and_controller_restarts(self):
        first = self.run_cli(self.worker)
        self.assertNotIn('resume', first)
        self.assertNotIn('--ephemeral', first)
        self.assertIn('--json', first)
        session = load_json(self.worker.path)['codex_session']
        self.assertEqual(session, {'id': SESSION_ID, 'repo': self.api.repo, 'issue': 1, 'branch': 'codex/issue-1'})
        second = self.run_cli(self.worker)
        restarted = Worker(self.api, 1, root=self.worker.root, state_dir=self.worker.state_dir)
        third = self.run_cli(restarted)
        for command in (second, third):
            self.assertIn('resume', command)
            self.assertEqual(command[-2:], [SESSION_ID, '-'])
            self.assertNotIn('--last', command)
            self.assertIn('CODEX_HOME=' + str(self.directory / '.codex'), command)
            self.assertIn('CODEX_SQLITE_HOME=' + str(self.directory / '.codex-runtime'), command)
            self.assertLess(command.index('--sandbox'), command.index('resume'))
            self.assertGreater(command.index('--output-schema'), command.index('resume'))

    def test_crash_before_session_save_recovers_id_from_pending_event_log(self):
        path = self.worker.state_dir / 'runs/attempt/stdout.log'
        path.parent.mkdir(parents=True)
        path.write_text(event_stream('partial answer', completed=False))
        self.worker.save(codex_pending_events=str(path.relative_to(self.worker.state_dir)))
        command = self.run_cli(self.worker)
        self.assertIn('resume', command)
        self.assertEqual(command[-2], SESSION_ID)

    def test_foreign_session_and_escaping_event_path_stop_before_cli(self):
        for fields in (
            {'codex_session': {'id': SESSION_ID, 'repo': self.api.repo, 'issue': 2, 'branch': self.worker.branch}},
            {'codex_session': {'id': SESSION_ID, 'repo': self.api.repo, 'issue': 1, 'branch': 'codex/other'}},
            {'codex_session': {'id': '--last', 'repo': self.api.repo, 'issue': 1, 'branch': self.worker.branch}},
            {'codex_session': None, 'codex_pending_events': '../other-issue/stdout.log'},
        ):
            with self.subTest(fields=fields):
                self.worker.state.pop('codex_pending_events', None)
                self.worker.save(**fields)
                with patch('worker.subprocess.Popen') as popen, self.assertRaises(SessionError):
                    self.worker.run_codex(self.api.issue, 'Implement', [])
                popen.assert_not_called()

    def test_unexpected_new_session_never_silently_replaces_saved_session(self):
        self.run_cli(self.worker)
        with self.assertRaises(SessionError):
            self.run_cli(self.worker, session_id='0199a213-81c0-7800-8aa1-bbab2a035a54')
        self.assertEqual(load_json(self.worker.path)['codex_session']['id'], SESSION_ID)

    def test_session_failure_preserves_state_for_retry_without_starting_another_conversation(self):
        with patch.object(self.worker, 'integrate_base'), \
                patch.object(self.worker, 'run_codex', side_effect=SessionError('Session data missing')) as run, \
                patch.object(self.worker, 'publish') as publish, self.assertRaisesRegex(Exception, 'Session data missing'):
            self.worker.work(self.api.issue, 'Continue', [])
        run.assert_called_once()
        publish.assert_not_called()
        state = load_json(self.worker.path)
        self.assertNotIn('manual_retry_required', state)
        self.assertEqual(state['phase'], 'blocked')

    def test_crash_after_result_written_restores_newest_handoff(self):
        self.worker.save(rounds=2, last_result_round=1, last_result=model_result(summary='Old result'))
        newer = model_result('in_progress', summary='Latest progress', next_steps=['Run integration tests'])
        save_json(self.worker.state_dir / 'result-2.json', newer)
        def coding(*args):
            self.assertEqual(self.worker.state['last_result'], newer)
            return model_result()
        with patch.object(self.worker, 'integrate_base'), patch.object(self.worker, 'run_codex', side_effect=coding), \
                patch.object(self.worker, 'publish'):
            self.worker.work(self.api.issue, 'Continue', [])

    def test_completed_issue_new_attempt_does_not_reuse_old_session(self):
        self.run_cli(self.worker)
        fresh = {'repo': self.api.repo, 'issue': 1, 'rounds': 0, 'handled': [], 'branch': 'codex/issue-1-retry-new'}
        save_json(self.worker.path, fresh)
        restarted = Worker(self.api, 1, root=self.worker.root, state_dir=self.worker.state_dir)
        command = self.run_cli(restarted, session_id='0199a213-81c0-7800-8aa1-bbab2a035a55')
        self.assertNotIn('resume', command)

    def test_session_id_is_saved_while_model_is_still_running(self):
        popen = subprocess.Popen
        prefix = json.dumps({'type': 'thread.started', 'thread_id': SESSION_ID}) + '\n'
        script = ('import sys,time; sys.stdin.read(); '
                  f'sys.stdout.write({prefix!r}); sys.stdout.flush(); time.sleep(5)')
        def start(command, **kwargs):
            return popen([sys.executable, '-c', script], **kwargs)
        with patch('worker.subprocess.Popen', side_effect=start), patch.object(self.worker, 'cli_timeout', 1.2), \
                self.assertRaises(subprocess.TimeoutExpired):
            self.worker.run_codex(self.api.issue, 'Implement', [])
        self.assertEqual(load_json(self.worker.path)['codex_session']['id'], SESSION_ID)

    def test_partial_json_event_is_read_once_when_completed(self):
        stream = io.BytesIO()
        sessions = []
        reader = SessionEvents(stream, sessions.append)
        payload = event_stream(json.dumps(model_result())).encode()
        stream.write(payload[:30])
        stream.seek(0)
        reader.consume()
        self.assertEqual(sessions, [])
        stream.seek(0, 2)
        stream.write(payload[30:])
        stream.seek(0)
        reader.consume()
        reader.consume()
        self.assertEqual(sessions, [SESSION_ID])
        self.assertTrue(reader.completed)
        self.assertEqual(json.loads(reader.last_message), model_result())

    def test_malformed_and_failed_turns_cannot_look_successful(self):
        for tail in ('not-json\n', json.dumps({'type': 'turn.failed', 'error': {'message': 'disconnected'}})):
            stream = io.BytesIO((event_stream('answer', completed=False) + tail).encode())
            reader = SessionEvents(stream, lambda session_id: None)
            reader.consume(final=True)
            self.assertFalse(reader.completed)
            self.assertIsNotNone(reader.failure)

    def test_busy_event_stream_yields_to_timeout_without_losing_events(self):
        stream = io.BytesIO(event_stream('answer').encode())
        observed = []
        reader = SessionEvents(stream, lambda session: None, lambda event, offset: observed.append(event))
        with patch('session_events.time.monotonic', side_effect=[0, 2]):
            reader.consume(deadline=1)
        self.assertEqual(len(observed), 1)
        self.assertFalse(reader.completed)
        reader.consume(final=True)
        self.assertEqual(len(observed), 3)
        self.assertTrue(reader.completed)
