import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import load_json
from session_events import ClaudeSessionEvents, SessionError
from worker import Worker, ModelCapacityError, ModelRunError, arguments
from test_worker import FakeAPI
from result_fixture import SESSION_ID, model_result

WRAPPER = Path(__file__).resolve().parents[1] / 'claude-coding.sh'


def claude_stream(session_id, result=None, error=None, tool=True):
    events = [{'type': 'system', 'subtype': 'init', 'session_id': session_id, 'model': 'claude-opus-5-5',
               'tools': ['Bash', 'Read', 'Edit', 'Write', 'Glob', 'Grep', 'StructuredOutput']}]
    if tool:
        events += [
            {'type': 'assistant', 'session_id': session_id, 'message': {'content': [
                {'type': 'text', 'text': 'Running the full test gate'},
                {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Bash', 'input': {'command': 'pnpm test:all'}}]}},
            {'type': 'user', 'session_id': session_id, 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': '1 failed', 'is_error': True}]}},
            {'type': 'assistant', 'session_id': session_id, 'message': {'content': [
                {'type': 'tool_use', 'id': 'toolu_2', 'name': 'Edit', 'input': {'file_path': '/workspace/repo/app.py'}}]}},
            {'type': 'user', 'session_id': session_id, 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_2', 'content': 'ok'}]}},
        ]
    final = {'type': 'result', 'session_id': session_id, 'subtype': 'success', 'is_error': False,
             'num_turns': 3, 'usage': {'output_tokens': 10}}
    if error:
        final.update(error)
    else:
        final.update(result=json.dumps(result), structured_output=result)
    events.append(final)
    return ''.join(json.dumps(event) + '\n' for event in events)


class ClaudeEventTests(unittest.TestCase):
    def read(self, text, final=True):
        seen = []
        reader = ClaudeSessionEvents(io.BytesIO(text.encode()), seen.append)
        reader.consume(final=final)
        return reader, seen

    def test_success_exposes_structured_output_and_session(self):
        reader, seen = self.read(claude_stream(SESSION_ID, model_result()))
        self.assertEqual(seen, [SESSION_ID])
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.failure)
        self.assertEqual(json.loads(reader.last_message), model_result())

    def test_error_and_missing_output_are_failures(self):
        reader, _ = self.read(claude_stream(SESSION_ID, error={
            'subtype': 'error_during_execution', 'is_error': True, 'api_error_status': 529,
            'result': 'API Error: overloaded'}))
        self.assertFalse(reader.completed)
        self.assertIn('HTTP 529', reader.failure)
        reader, _ = self.read(claude_stream(SESSION_ID, error={'result': 'done'}))
        self.assertFalse(reader.completed)
        self.assertIn('no structured output', reader.failure)

    def test_partial_line_waits_and_malformed_line_fails(self):
        text = claude_stream(SESSION_ID, model_result())
        reader, _ = self.read(text[:-5], final=False)
        self.assertFalse(reader.completed)
        reader, _ = self.read('not json\n' + text)
        self.assertTrue(reader.completed)
        self.assertEqual(reader.failure, 'Malformed Claude stream-json event')

    def test_invalid_or_changing_session_ids_stop(self):
        with self.assertRaises(SessionError):
            self.read(claude_stream('--last', model_result()))
        other = str(uuid.uuid4())
        mixed = claude_stream(SESSION_ID, tool=False, result=model_result()).replace(
            '"type": "result", "session_id": "' + SESSION_ID, '"type": "result", "session_id": "' + other)
        with self.assertRaises(SessionError):
            self.read(mixed)


class ClaudeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.api = FakeAPI()
        self.api.token = 'test-private-token'
        self.worker = Worker(self.api, 1, root=self.directory / 'repo', state_dir=self.directory / 'state',
                             cli_timeout=10)
        self.worker.root.mkdir()

    def run_cli(self, stream=None, worker=None):
        worker = worker or self.worker
        commands = []
        popen = subprocess.Popen

        def start(command, **kwargs):
            commands.append(command)
            session = command[command.index('--claude-session') + 1]
            output = stream(session) if callable(stream) else stream or claude_stream(session, model_result())
            script = ('import sys; from pathlib import Path; '
                      'Path("received-prompt.txt").write_text(sys.stdin.read()); '
                      f'print({output!r}, end="")')
            return popen([sys.executable, '-c', script], **kwargs)

        with patch('worker.subprocess.Popen', side_effect=start):
            result = worker.run_codex(self.api.issue, 'Implement and test', [])
        return result, commands[0]

    def test_defaults_are_claude_opus_high(self):
        self.assertEqual((self.worker.provider, self.worker.model, self.worker.effort),
                         ('claude', 'claude-opus-5-5', 'high'))
        args = arguments(['--repo', 'o/r', '--issue', '1'])
        self.assertEqual((args.provider, args.model, args.effort), ('claude', None, 'high'))

    def test_command_is_unattended_credential_free_and_session_preassigned(self):
        result, command = self.run_cli()
        self.assertEqual(result, model_result())
        session = self.worker.state['claude_session']
        self.assertEqual(session, {'repo': self.api.repo, 'issue': 1, 'branch': 'codex/issue-1',
                                   'id': command[command.index('--claude-session') + 1]})
        self.assertEqual(command[:6], ['sudo', '-H', '-u', 'codex', 'env', '-i'])
        self.assertIn('/opt/codex-coding/claude-coding.sh', command)
        self.assertIn('CLAUDE_CONFIG_DIR=' + str(self.directory / '.claude'), command)
        for setting in ('CI=1', 'GIT_TERMINAL_PROMPT=0', 'PAGER=cat', 'HOME=/home/codex'):
            self.assertIn(setting, command)
        self.assertEqual(command[command.index('--model') + 1], 'claude-opus-5-5')
        self.assertEqual(command[command.index('--effort') + 1], 'high')
        self.assertEqual(command[command.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(command[command.index('--setting-sources') + 1], 'user')
        self.assertEqual(command[command.index('--output-format') + 1], 'stream-json')
        for flag in ('-p', '--verbose', '--strict-mcp-config', '--disable-slash-commands'):
            self.assertIn(flag, command)
        settings = json.loads(command[command.index('--settings') + 1])
        self.assertTrue(settings['disableAllHooks'])
        for rule in ('WebFetch', 'WebSearch', 'Bash(gh *)', 'Read(//home/codex/.claude-auth/**)'):
            self.assertIn(rule, settings['permissions']['deny'])
        schema = json.loads(command[command.index('--json-schema') + 1])
        self.assertEqual(schema['required'], ['status', 'summary', 'tests', 'test_results', 'next_steps', 'blocker'])
        self.assertFalse(any('test-private-token' in part or 'TOKEN=' in part or 'GH_' in part
                             for part in command))
        prompt = (self.worker.root / 'received-prompt.txt').read_text()
        self.assertIn('Claude Code specifics', prompt)
        self.assertIn('No user is available to answer.', prompt)

    def test_same_session_is_reused_across_rounds_and_restarts(self):
        _, first = self.run_cli()
        restarted = Worker(self.api, 1, root=self.worker.root, state_dir=self.worker.state_dir, cli_timeout=10)
        _, second = self.run_cli(worker=restarted)
        identity = lambda command: command[command.index('--claude-session') + 1]
        self.assertEqual(identity(first), identity(second))
        self.assertIsNone(restarted.state.get('codex_pending_events'))

    def test_session_is_saved_before_launch_and_mismatch_is_rejected(self):
        other = str(uuid.uuid4())
        with self.assertRaisesRegex(SessionError, 'different session ID'):
            self.run_cli(stream=lambda session: claude_stream(other, model_result()))
        self.assertNotEqual(self.worker.state['claude_session']['id'], other)

    def test_foreign_saved_session_stops_before_cli(self):
        self.worker.save(claude_session={'repo': self.api.repo, 'issue': 1, 'branch': 'codex/other',
                                         'id': SESSION_ID})
        with patch('worker.subprocess.Popen') as popen, self.assertRaises(SessionError):
            self.worker.run_codex(self.api.issue, 'Continue', [])
        popen.assert_not_called()

    def test_error_result_and_capacity_classification(self):
        error = {'subtype': 'error_max_turns', 'is_error': True, 'result': 'Reached max turns'}
        with self.assertRaisesRegex(ModelRunError, 'error_max_turns'):
            self.run_cli(stream=lambda session: claude_stream(session, error=error))
        overloaded = {'subtype': 'error_during_execution', 'is_error': True, 'api_error_status': 529,
                      'result': 'API Error: Overloaded'}
        with self.assertRaises(ModelCapacityError):
            self.run_cli(stream=lambda session: claude_stream(session, error=overloaded))
        self.assertIn('claude_session', self.worker.state)

    def test_invalid_structured_result_is_rejected(self):
        with self.assertRaisesRegex(ModelRunError, 'Invalid model result'):
            self.run_cli(stream=lambda session: claude_stream(session, {'status': 'completed'}))

    def test_progress_tracks_claude_tool_calls(self):
        self.run_cli()
        progress = load_json(self.worker.state_dir / 'progress.json')
        self.assertEqual(progress['commands_completed'], 1)
        self.assertEqual(progress['commands_failed'], 1)
        self.assertEqual(progress['last_command']['command'], 'Bash: pnpm test:all')
        self.assertEqual(progress['latest_file_changes'], [{'path': '/workspace/repo/app.py', 'tool': 'Edit'}])
        self.assertEqual(progress['session_id'], self.worker.state['claude_session']['id'])
        log = (self.worker.state_dir / 'progress.log').read_text()
        self.assertIn('tool_use Bash: pnpm test:all', log)
        self.assertIn('[error=True]', log)
        self.assertNotIn('test-private-token', log)

    def test_switching_from_codex_keeps_codex_session_and_starts_claude(self):
        codex = {'repo': self.api.repo, 'issue': 1, 'branch': 'codex/issue-1', 'id': SESSION_ID}
        self.worker.save(provider='codex', codex_session=codex)
        switched = Worker(self.api, 1, root=self.worker.root, state_dir=self.worker.state_dir, cli_timeout=10)
        self.assertEqual(switched.state['provider'], 'claude')
        _, command = self.run_cli(worker=switched)
        self.assertEqual(switched.state['codex_session'], codex)
        self.assertNotEqual(command[command.index('--claude-session') + 1], SESSION_ID)


class ClaudeWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'claude'
        fake.write_text('#!/usr/bin/env bash\n'
                        'printf "%s\\n" "$@" > "$ARGS_OUT"\n'
                        'cat <&3 > "$TOKEN_OUT"\n'
                        'env > "$ENV_OUT"\n')
        fake.chmod(0o755)
        self.token = self.root / 'oauth-token'
        self.token.write_text('sk-ant-oat-secret\n')
        self.config = self.root / 'claude-home'
        self.env = {'PATH': f'{bin_dir}:/usr/bin:/bin', 'CLAUDE_CONFIG_DIR': str(self.config),
                    'CLAUDE_CODING_TOKEN_FILE': str(self.token), 'ARGS_OUT': str(self.root / 'args'),
                    'TOKEN_OUT': str(self.root / 'token'), 'ENV_OUT': str(self.root / 'env')}

    def run_wrapper(self, *args):
        return subprocess.run(['bash', str(WRAPPER), *args], env=self.env, capture_output=True, text=True)

    def test_new_session_then_resume_with_token_on_fd_only(self):
        result = self.run_wrapper('--claude-session', SESSION_ID, '-p', '--model', 'claude-opus-5-5')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'args').read_text().split('\n')[:3], ['--session-id', SESSION_ID, '-p'])
        self.assertEqual((self.root / 'token').read_text(), 'sk-ant-oat-secret')
        environment = (self.root / 'env').read_text()
        self.assertNotIn('sk-ant-oat-secret', environment)
        self.assertIn('CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3', environment)
        self.assertEqual(oct(self.config.stat().st_mode & 0o777), '0o700')
        project = self.config / 'projects' / '-workspace-repo'
        project.mkdir(parents=True)
        (project / f'{SESSION_ID}.jsonl').write_text('{}\n')
        result = self.run_wrapper('--claude-session', SESSION_ID, '-p')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'args').read_text().split('\n')[:2], ['--resume', SESSION_ID])

    def test_missing_login_or_invalid_session_fails_before_claude(self):
        self.assertEqual(self.run_wrapper('--claude-session', '--last', '-p').returncode, 2)
        self.token.unlink()
        self.assertEqual(self.run_wrapper('--claude-session', SESSION_ID, '-p').returncode, 3)
        self.assertFalse((self.root / 'args').exists())


if __name__ == '__main__':
    unittest.main()
