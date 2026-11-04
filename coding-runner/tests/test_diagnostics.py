import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import JobError
from diagnostics import Diagnostics
from worker import Worker
from result_fixture import event_stream, model_result


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.api = SimpleNamespace(repo='owner/repo', login='owner', user_id=1, token='private-token',
                                   repository=lambda: {'default_branch': 'main'})
        self.worker = Worker(self.api, 1, root=self.directory / 'repo', state_dir=self.directory / 'state')
        self.worker.root.mkdir()
        self.issue = {'number': 1, 'title': 'Fix regression', 'body': 'Expected behavior', 'state': 'open'}

    def events(self):
        return [json.loads(line) for line in (self.worker.state_dir / 'issue-log.jsonl').read_text().splitlines()]

    def rounds(self):
        return sorted((self.worker.state_dir / 'runs').glob('*/round-*'))

    def run_fake_cli(self, stdout, stderr='', code=0):
        # Exercise actual OS file descriptors and process IO without invoking an AI or GitHub.
        popen = subprocess.Popen
        stdout = event_stream(stdout)
        script = ('import sys; sys.stdin.read(); '
                  f'sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r}); sys.exit({code})')
        def start(command, **kwargs):
            if command[0] != 'sudo':
                return popen(command, **kwargs)
            return popen([sys.executable, '-c', script], **kwargs)
        with patch('worker.subprocess.Popen', side_effect=start):
            return self.worker.run_codex(self.issue, 'Fix CI', [{'checks': ['failed test']}])

    def test_failed_and_retried_calls_keep_distinct_complete_artifacts(self):
        with self.assertRaisesRegex(JobError, 'exited 2'):
            self.run_fake_cli('partial answer', 'test failed\n', 2)
        first = self.rounds()[0]
        original = {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()}
        result = model_result()
        self.assertEqual(self.run_fake_cli(json.dumps(result)), result)
        self.assertEqual(len(self.rounds()), 2)
        self.assertEqual(original, {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()})
        self.assertEqual((first / 'stdout.log').read_text(), event_stream('partial answer'))
        self.assertEqual((first / 'stderr.log').read_text(), 'test failed\n')
        context = json.loads((first / 'context.json').read_text())
        self.assertEqual(context['issue'], self.issue)
        self.assertEqual(context['reason'], 'Fix CI')
        self.assertIn('failed test', (first / 'prompt.txt').read_text())
        self.assertEqual(json.loads((first / 'outcome.json').read_text())['status'], 'failed')
        self.assertIn('codex_failed', [e['event'] for e in self.events()])
        for directory in self.rounds():
            for path in directory.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_oversized_and_blocked_results_remain_available(self):
        outputs = ['{bad JSON', 'x' * 100001]
        for output in outputs:
            with self.assertRaises((ValueError, JobError)):
                self.run_fake_cli(output)
        blocked_result = model_result('blocked', blocker={
            'reason': 'Missing external test database', 'owner': 'Database team',
            'unblock_condition': 'Provide access', 'evidence': 'Connection refused'})
        output = json.dumps(blocked_result)
        self.assertEqual(self.run_fake_cli(output), blocked_result)
        outputs.append(output)
        self.assertEqual({(p / 'stdout.log').read_text() for p in self.rounds()}, {event_stream(o) for o in outputs})
        self.assertTrue(all((p / 'outcome.json').exists() for p in self.rounds()))
        blocked = next(p for p in self.rounds() if (p / 'result.json').exists())
        self.assertEqual(json.loads((blocked / 'result.json').read_text())['status'], 'blocked')

    def test_timeout_and_sigterm_keep_partial_output_and_restore_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        for interrupt in ('timeout', 'sigterm'):
            process = Mock(pid=123, returncode=-15)
            process.poll.return_value = None
            def start(command, **kwargs):
                written = False
                def communicate(*args, **options):
                    nonlocal written
                    if not written:
                        kwargs['stdout'].write(event_stream('partial output', completed=False).encode())
                        kwargs['stdout'].flush()
                        kwargs['stderr'].write(b'last command failed')
                        written = True
                    if interrupt == 'timeout':
                        raise subprocess.TimeoutExpired(command, 7200)
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                process.communicate.side_effect = communicate
                return process
            with patch('worker.subprocess.Popen', side_effect=start), patch('worker.os.killpg') as kill, \
                    patch.object(self.worker, 'cli_timeout', 0.02):
                with self.assertRaises((subprocess.TimeoutExpired, JobError)):
                    self.worker.run_codex(self.issue, interrupt, [])
                kill.assert_called_with(123, signal.SIGTERM)
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        for directory in self.rounds():
            self.assertEqual((directory / 'stdout.log').read_text(), event_stream('partial output', completed=False))
            self.assertEqual((directory / 'stderr.log').read_text(), 'last command failed')
            self.assertEqual(json.loads((directory / 'outcome.json').read_text())['status'], 'failed')

    def test_new_worker_appends_to_same_issue_history_with_new_run_id(self):
        self.worker.save(rounds=2)
        before = (self.worker.state_dir / 'issue-log.jsonl').read_bytes()
        other = Worker(self.api, 1, root=self.worker.root, state_dir=self.worker.state_dir)
        self.assertNotEqual(other.run_id, self.worker.run_id)
        self.assertTrue((self.worker.state_dir / 'issue-log.jsonl').read_bytes().startswith(before))
        self.assertEqual(self.events()[-1]['round'], 2)

    def test_workspace_snapshot_preserves_tracked_and_new_files_on_failure(self):
        self.worker.git('init')
        tracked = self.worker.root / 'tracked.txt'
        tracked.write_text('old\n')
        self.worker.git('add', '.')
        self.worker.git('commit', '-m', 'baseline')
        tracked.write_text('new\n')
        (self.worker.root / 'new.txt').write_text('untracked work\n')
        with self.assertRaises(JobError):
            self.run_fake_cli('failed', code=1)
        snapshot = self.rounds()[0] / 'after'
        self.assertIn('+new', (snapshot / 'changes.patch').read_text())
        self.assertIn('+untracked work', (snapshot / 'untracked.patch').read_text())
        self.assertTrue((snapshot / 'head.txt').read_text().strip())

    def test_history_redacts_controller_credentials_and_serializes_concurrent_writes(self):
        secret = 'sensitive"token'
        encoded = base64.b64encode(f'x-access-token:{secret}'.encode()).decode()
        log = Diagnostics(self.directory, 'owner/repo', 7, token=secret)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: log.event('test', index=i, output={'raw': secret, 'auth': encoded}), range(40)))
        rows = [json.loads(line) for line in (self.directory / 'issue-log.jsonl').read_text().splitlines()]
        self.assertEqual({r['index'] for r in rows}, set(range(40)))
        self.assertTrue(all(r['output'] == {'raw': '[redacted]', 'auth': '[redacted]'} for r in rows))
        self.assertEqual((self.directory / 'issue-log.jsonl').stat().st_mode & 0o777, 0o600)

    def test_pre_model_failure_has_round_context_and_traceback(self):
        self.api.request = lambda path: self.issue
        with patch.object(self.worker, 'integrate_base', side_effect=JobError('fetch failed')):
            with self.assertRaises(JobError):
                self.worker.work(self.issue, 'Resume after CI failure', [{'key': 'ci:sha'}])
        start = next(e for e in self.events() if e['event'] == 'round_started')
        failure = next(e for e in self.events() if e['event'] == 'round_failed')
        self.assertEqual(start['issue_snapshot'], self.issue)
        self.assertEqual(start['feedback'], [{'key': 'ci:sha'}])
        self.assertIn('fetch failed', failure['traceback'])
