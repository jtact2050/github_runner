import contextlib
import io
from http.client import IncompleteRead
import signal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dispatch import Dispatcher, arguments, drain_on_interrupt, run_dispatcher, docker
from common import issue_digest, load_json, save_json
from common import GitHub, JobError
from types import SimpleNamespace


class QueueAPI:
    repo = 'jtact2050/test'

    def __init__(self):
        self.issues = [dict(number=n, title=f'Implement {n}', state='open', milestone={'number': m})
                       for n, m in [(1, 1), (2, 1), (3, 1), (4, 2)]]
        self.blockers = {}
        self.open_prs = []

    def pages(self, path):
        if path == 'pulls?state=open':
            return self.open_prs
        if path.startswith('milestones?'):
            return [{'number': 1, 'title': 'First'}, {'number': 2, 'title': 'Second'}]
        if path.startswith('issues?'):
            return self.issues
        if path.endswith('/dependencies/blocked_by'):
            return self.blockers.get(int(path.split('/')[1]), [])
        raise AssertionError(path)

    def request(self, path):
        if path.startswith('issues/'):
            return next(issue for issue in self.issues if issue['number'] == int(path.split('/')[1]))
        raise AssertionError(path)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root_patch = patch('dispatch.ROOT', Path(self.temp.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.api = QueueAPI()
        self.args = arguments(['--repo', self.api.repo, '--dry-run'])
        self.queue = Dispatcher(self.api, self.args)

    def tick(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.queue.tick()

    def test_backlog_and_default_two_slots(self):
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1, 2])
        self.assertFalse(self.queue.directory.exists())

    def test_exhausted_attempts_do_not_relaunch_or_hold_up_other_work(self):
        self.assertEqual(self.args.max_rounds, 8)
        for number, phase in ((1, 'blocked'), (2, 'coding')):
            self.queue.job_dir(number).mkdir(parents=True)
            save_json(self.queue.job_dir(number) / 'worker.json', {'phase': phase, 'rounds': 8})
        for _ in range(2):
            report = self.tick()
            self.assertEqual([i['number'] for i in report['next_issues']], [3, 4])
            self.assertEqual(report['coding_budget_exhausted'], [
                {'number': 1, 'rounds': 8, 'max_rounds': 8},
                {'number': 2, 'rounds': 8, 'max_rounds': 8}])
        self.assertEqual(self.queue.jobs, {})
        self.args.max_rounds = 9
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1, 2])
        self.assertEqual(load_json(self.queue.job_dir(1) / 'worker.json')['rounds'], 8)

    def test_review_only_resume_and_new_attempt_are_not_coding_budget_blocked(self):
        for number, phase in ((1, 'waiting_review'), (2, 'done')):
            self.queue.job_dir(number).mkdir(parents=True)
            save_json(self.queue.job_dir(number) / 'worker.json', {'phase': phase, 'rounds': 10})
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1, 2])

    def test_seventh_round_can_resume_for_eighth_round(self):
        self.queue.job_dir(1).mkdir(parents=True)
        save_json(self.queue.job_dir(1) / 'worker.json', {'phase': 'blocked', 'rounds': 7})
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1, 2])

    def test_legacy_manual_blocker_is_selected_and_migrated_without_scope_changes(self):
        directory = self.queue.job_dir(1)
        directory.mkdir(parents=True)
        state = {'phase': 'blocked', 'rounds': 2, 'manual_retry_required': True,
                 'blocked_issue_digest': issue_digest(self.api.issues[0]), 'error': 'External service unavailable'}
        save_json(directory / 'worker.json', state)
        for _ in range(2):
            report = self.tick()
            self.assertEqual([i['number'] for i in report['next_issues']], [1, 2])
            self.assertNotIn('waiting_for_input', report)
        self.assertEqual(load_json(directory / 'worker.json'), state)  # dry run does not mutate
        with contextlib.redirect_stdout(io.StringIO()):
            self.queue.prepare_resume(1)
        migrated = load_json(directory / 'worker.json')
        self.assertNotIn('manual_retry_required', migrated)
        self.assertNotIn('blocked_issue_digest', migrated)
        self.assertEqual(migrated['rounds'], 2)
        self.assertEqual(migrated['phase'], 'coding')

    def test_automatic_retry_does_not_bypass_github_blockers_or_budget(self):
        directory = self.queue.job_dir(1)
        directory.mkdir(parents=True)
        save_json(directory / 'worker.json', {'phase': 'blocked', 'rounds': 8, 'manual_retry_required': True})
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [2, 3])
        self.args.max_rounds = 9
        self.api.issues[0]['labels'] = [{'name': 'status:blocked'}]
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [2, 3])
        self.api.issues[0]['labels'] = []
        self.api.blockers[1] = [{'number': 99, 'state': 'open'}]
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [2, 3])
        self.api.blockers[1][0].update(state='closed', state_reason='completed')
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1, 2])

    def test_prepare_resume_preserves_legacy_work_and_feedback(self):
        directory = self.queue.job_dir(1)
        directory.mkdir(parents=True)
        preserved = {'branch': 'codex/issue-1-saved', 'rounds': 2, 'pr': 7,
                     'codex_session': {'id': 'saved-session'},
                     'continuation': {'reason': 'Resolve latest base', 'feedback': [{'key': 'review:1'}]},
                     'pending_feedback': ['review:1'], 'handled': []}
        self.api.open_prs = [{'number': 7, 'state': 'open'}]
        save_json(directory / 'worker.json', dict(preserved, phase='blocked', resume_phase='blocked',
            manual_retry_required=True, blocked_issue_digest=issue_digest(self.api.issues[0]), blocker_pending={}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.queue.prepare_resume(1)
        resumed = load_json(directory / 'worker.json')
        self.assertEqual(resumed['phase'], 'coding')
        for key, value in preserved.items():
            self.assertEqual(resumed[key], value)
        for key in ('manual_retry_required', 'blocked_issue_digest', 'blocker_pending'):
            self.assertNotIn(key, resumed)

    def test_manual_retry_option_is_removed(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            arguments(['--retry', '1'])

    def test_restart_launches_legacy_blocked_jobs_with_and_without_open_pr(self):
        self.args.dry_run = False
        self.api.issues = self.api.issues[:2]
        self.api.open_prs = [{'number': 7, 'state': 'open'}]
        for number in (1, 2):
            workspace = self.queue.prepare_workspace(number)
            (workspace / 'saved-work.txt').write_text(f'issue {number} work')
            path = self.queue.job_dir(number) / 'worker.json'
            state = load_json(path)
            state.update(phase='blocked', resume_phase='blocked', rounds=number,
                         manual_retry_required=True,
                         blocked_issue_digest=issue_digest(self.api.issues[number - 1]),
                         codex_session={'id': f'session-{number}'},
                         continuation={'reason': 'Resolve latest base', 'feedback': []})
            if number == 2:
                state['pr'] = 7
            save_json(path, state)
        restarted = Dispatcher(self.api, self.args)
        with patch.object(restarted, 'reconcile'), \
                patch('dispatch.inspect_container', return_value=None), \
                patch('dispatch.docker', return_value=SimpleNamespace(stdout='container-id')) as docker, \
                contextlib.redirect_stdout(io.StringIO()):
            report = restarted.tick()
        self.assertEqual([i['number'] for i in report['next_issues']], [1, 2])
        self.assertEqual(docker.call_count, 2)
        for number in (1, 2):
            state = load_json(restarted.job_dir(number) / 'worker.json')
            self.assertNotIn('manual_retry_required', state)
            self.assertNotIn('blocked_issue_digest', state)
            self.assertEqual(state['phase'], 'coding')
            self.assertEqual(state['rounds'], number)
            self.assertEqual(state['codex_session']['id'], f'session-{number}')
            self.assertEqual(state['continuation']['reason'], 'Resolve latest base')
            self.assertEqual(state.get('pr'), 7 if number == 2 else None)
            self.assertEqual((restarted.workspace_dir(number) / 'saved-work.txt').read_text(), f'issue {number} work')
            self.assertEqual(restarted.jobs[str(number)]['status'], 'running')

    def test_three_parallel_slots(self):
        self.args.concurrency = 3
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1, 2, 3])
        self.queue.jobs['1'] = {'status': 'running', 'milestone': 1}
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [2, 3])

    def test_drain_never_admits_new_or_queued_issues(self):
        self.args.drain = True
        self.queue.jobs['1'] = {'status': 'running', 'milestone': 1}
        self.queue.jobs['2'] = {'status': 'queued', 'milestone': 1}
        with patch.object(self.api, 'pages') as pages:
            self.assertEqual(self.tick()['running'], 1)
            self.assertEqual(self.tick()['next_issues'], [])
            self.queue.jobs['1']['status'] = 'done'
            self.queue.jobs['3'] = {'status': 'blocked', 'milestone': 1}
            self.assertEqual(self.tick()['running'], 0)
            pages.assert_not_called()

    def test_drain_requires_cleanup_before_reporting_complete(self):
        self.args.drain = True
        self.args.dry_run = False
        with patch.object(self.queue, 'reconcile', side_effect=JobError('cleanup failed')):
            with self.assertRaisesRegex(JobError, 'cleanup failed'):
                self.tick()

    def test_sigint_during_selection_prevents_all_new_launches(self):
        self.args.dry_run = False
        self.queue.directory.mkdir(parents=True)
        original = self.api.pages
        def pages(path):
            if path.startswith('issues?'):
                signal.raise_signal(signal.SIGINT)
            return original(path)
        with drain_on_interrupt(self.args), patch('dispatch.os.write'), \
                patch.object(self.api, 'pages', side_effect=pages), \
                patch.object(self.queue, 'reconcile'), patch.object(self.queue, 'launch') as launch:
            self.tick()
        launch.assert_not_called()
        self.assertTrue(self.args.drain)

    def test_sigint_between_parallel_launches_leaves_only_started_worker(self):
        self.args.dry_run = False
        self.queue.directory.mkdir(parents=True)
        self.args.concurrency = 3
        def launch(number, milestone):
            self.queue.jobs[str(number)] = {'status': 'running', 'milestone': milestone}
            signal.raise_signal(signal.SIGINT)
        with drain_on_interrupt(self.args), patch('dispatch.os.write'), \
                patch.object(self.queue, 'reconcile'), \
                patch.object(self.queue, 'launch', side_effect=launch) as launched:
            self.tick()
        self.assertEqual(launched.call_count, 1)
        self.assertEqual(self.queue.jobs['1']['status'], 'running')

    def test_sigint_waits_through_existing_workers_and_cleanup_failure(self):
        self.args.once = True
        self.args.poll_seconds = 0
        self.queue.jobs['1'] = {'status': 'running', 'milestone': 1}
        original_handler = signal.getsignal(signal.SIGINT)
        turns = []
        def tick():
            turns.append(1)
            if len(turns) == 1:
                signal.raise_signal(signal.SIGINT)
                # A stale report must not cause exit while a job is still running.
                return {'running': 0}
            if len(turns) == 2:
                signal.raise_signal(signal.SIGINT)
                self.queue.jobs['1']['status'] = 'done'
                raise JobError('cleanup not yet complete')
            return {'running': 0}
        with patch('dispatch.os.write'), patch.object(self.queue, 'reconcile'), \
                patch.object(self.queue, 'tick', side_effect=tick), \
                patch.object(self.queue, 'log') as log:
            run_dispatcher(self.queue, self.args)
        self.assertEqual(len(turns), 3)
        self.assertFalse(self.args.once)
        self.assertEqual(signal.getsignal(signal.SIGINT), original_handler)
        self.assertIn('Drain complete', log.call_args.args[0])

    def test_dispatcher_continues_after_incomplete_response_exhausts_retries(self):
        self.args.poll_seconds = 0
        api = GitHub.__new__(GitHub)
        api.repo, api.token = self.api.repo, 'test-token'
        responses = []
        for _ in range(7):
            response = io.BytesIO()
            response.read = Mock(side_effect=IncompleteRead(b'partial', 10))
            responses.append(response)
        responses.append(io.BytesIO(b'[]'))

        def tick():
            api.request('issues?state=open')
            self.args.once = True

        with patch('common.urllib.request.urlopen', side_effect=responses) as urlopen, \
                patch('common.time.sleep'), patch.object(self.queue, 'reconcile'), \
                patch.object(self.queue, 'tick', side_effect=tick) as ticks, \
                patch.object(self.queue, 'log') as log:
            run_dispatcher(self.queue, self.args)
        self.assertEqual(ticks.call_count, 2)
        self.assertEqual(urlopen.call_count, 8)
        self.assertIn('GitHub network error', log.call_args.args[0])

    def test_docker_cli_is_isolated_from_terminal_sigint(self):
        with patch('dispatch.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            docker('ps')
        self.assertTrue(run.call_args.kwargs['start_new_session'])

    def test_active_job_uses_slot_and_is_not_duplicated(self):
        self.queue.jobs['1'] = {'status': 'running', 'milestone': 1}
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [2])
        self.queue.jobs['2'] = {'status': 'running', 'milestone': 1}
        self.assertEqual(self.tick()['next_issues'], [])

    def test_saved_terminal_status_never_excludes_open_issue(self):
        self.args.concurrency = 1
        for status in ('blocked', 'done', 'queued', 'finished'):
            self.queue.jobs['1'] = {'status': status, 'milestone': 99}
            self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1])

    def test_selection_uses_current_dependencies_and_running_workers(self):
        self.args.concurrency = 1
        self.api.blockers[1] = [{'number': 99, 'state': 'open'}]
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [2])
        self.api.blockers.clear()
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1])
        self.queue.jobs['3'] = {'status': 'running', 'milestone': 1}
        self.assertEqual(self.tick()['next_issues'], [])

    def test_github_blocked_labels_allow_independent_later_milestone(self):
        self.args.concurrency = 1
        for issue in self.api.issues[:3]:
            issue['labels'] = [{'name': 'status:blocked'}]
        report = self.tick()
        self.assertEqual([i['number'] for i in report['next_issues']], [4])
        self.assertEqual(report['github_blocked_issues'], [1, 2, 3])

    def test_new_issues_mount_different_workspaces_and_private_codex_home(self):
        with patch('dispatch.inspect_container', return_value=None), patch('dispatch.docker', return_value=SimpleNamespace(stdout='container-id')) as docker, \
                contextlib.redirect_stdout(io.StringIO()):
            self.queue.launch(1, 1)
            self.queue.launch(2, 1)
        calls = [c.args for c in docker.call_args_list]
        workspaces = [[a for a in call if isinstance(a, str) and a.endswith('target=/workspace')] for call in calls]
        self.assertNotEqual(workspaces[0], workspaces[1])
        self.assertEqual(workspaces[0], [f'type=bind,source={self.queue.job_dir(1) / "workspace"},target=/workspace'])
        for call in calls:
            self.assertFalse(any(isinstance(a, str) and a.startswith('type=volume,') for a in call))
            self.assertFalse(any(isinstance(a, str) and a.endswith('target=/home/codex/.codex') for a in call))
            self.assertTrue(any(isinstance(a, str) and a.endswith('target=/home/codex/.codex-auth') for a in call))

    def test_apparmor_profile_is_added_without_removing_seccomp(self):
        self.args.apparmor_profile = 'codex-runner'
        with patch('dispatch.inspect_container', return_value=None), \
                patch('dispatch.docker', return_value=SimpleNamespace(stdout='container-id')) as launch, \
                contextlib.redirect_stdout(io.StringIO()):
            self.queue.launch(1, 1)
        args = launch.call_args.args
        self.assertIn('apparmor=codex-runner', args)
        self.assertTrue(any(str(arg).startswith('seccomp=') for arg in args))
        self.assertNotIn('--privileged', args)
        self.assertNotIn('--cap-add', args)

    def test_apparmor_configuration_from_dotenv_and_cli_override(self):
        (Path(self.temp.name) / '.env').write_text('CODEX_APPARMOR_PROFILE=codex-runner\n')
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(arguments([]).apparmor_profile, 'codex-runner')
            self.assertEqual(arguments(['--apparmor-profile', 'custom']).apparmor_profile, 'custom')

    def mount_failure(self):
        return JobError('Docker run failed: invalid mount config for type "bind": stat '
                        + str(self.queue.job_dir(1)) + ': operation not permitted')

    def test_desktop_mount_failure_refreshes_parent_and_retries_same_launch(self):
        with patch('dispatch.sys.platform', 'darwin'), \
                patch('dispatch.inspect_container', return_value=None), \
                patch('dispatch.docker', side_effect=[self.mount_failure(),
                    SimpleNamespace(stdout='stat'), SimpleNamespace(stdout='container-id')]) as docker, \
                contextlib.redirect_stdout(io.StringIO()):
            self.queue.launch(1, 1)
        first, refresh, retry = [call.args for call in docker.call_args_list]
        self.assertEqual(first, retry)
        self.assertIn('--rm', refresh)
        self.assertIn('none', refresh)
        self.assertIn('--read-only', refresh)
        self.assertIn(f'type=bind,source={self.queue.directory.parent.parent},target=/host-root,readonly', refresh)
        self.assertIn('stat', refresh)
        self.assertIn('/host-root/' + str(self.queue.workspace_dir(1).relative_to(self.queue.directory.parent.parent)), refresh)
        self.assertEqual(self.queue.jobs['1']['status'], 'running')
        history = (self.queue.job_dir(1) / 'issue-log.jsonl').read_text()
        self.assertIn('bind_mount_refreshed', history)
        self.assertIn('container_launched', history)

    def test_mount_recovery_retries_only_once(self):
        self.queue.prepare_workspace(1)
        with patch('dispatch.sys.platform', 'darwin'), \
                patch('dispatch.inspect_container', return_value=None), \
                patch('dispatch.docker', side_effect=[self.mount_failure(),
                      SimpleNamespace(stdout='stat'), self.mount_failure()]) as docker, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(JobError):
            self.queue.run_worker_container(1, ['run', 'worker'])
        self.assertEqual(docker.call_count, 3)

    def test_mount_refresh_failure_does_not_launch_worker(self):
        self.queue.prepare_workspace(1)
        with patch('dispatch.sys.platform', 'darwin'), \
                patch('dispatch.inspect_container', return_value=None), \
                patch('dispatch.docker', side_effect=[self.mount_failure(), JobError('parent inaccessible')]) as docker, \
                self.assertRaisesRegex(JobError, 'parent inaccessible'):
            self.queue.run_worker_container(1, ['run', 'worker'])
        self.assertEqual(docker.call_count, 2)

    def test_mount_recovery_does_not_retry_unrelated_failures_or_existing_containers(self):
        self.queue.prepare_workspace(1)
        cases = [('linux', self.mount_failure(), None),
                 ('darwin', JobError('image unavailable'), None),
                 ('darwin', self.mount_failure(), {'Id': 'existing'}),
                 ('darwin', JobError('invalid mount config for type "bind": stat /unrelated: operation not permitted'), None)]
        for platform, error, container in cases:
            with self.subTest(platform=platform, error=str(error), container=container), \
                    patch('dispatch.sys.platform', platform), \
                    patch('dispatch.inspect_container', return_value=container), \
                    patch('dispatch.docker', side_effect=error) as docker, self.assertRaises(JobError):
                self.queue.run_worker_container(1, ['run', 'worker'])
            self.assertEqual(docker.call_count, 1)

    def test_mount_recovery_does_not_replace_missing_host_workspace(self):
        with patch('dispatch.sys.platform', 'darwin'), \
                patch('dispatch.docker', side_effect=self.mount_failure()) as docker, self.assertRaises(JobError):
            self.queue.run_worker_container(1, ['run', 'worker'])
        self.assertEqual(docker.call_count, 1)

    def test_explicit_closed_pr_restart_archives_old_attempt_and_uses_fresh_folder(self):
        self.api.login = 'jtact2050'
        self.queue.job_dir(1).mkdir(parents=True)
        self.queue.jobs['1'] = {'status': 'blocked', 'milestone': 1}
        previous = {'repo': self.api.repo, 'issue': 1, 'phase': 'blocked', 'pr': 7,
                    'branch': 'codex/issue-1', 'rounds': 4}
        save_json(self.queue.job_dir(1) / 'worker.json', previous)
        (self.queue.job_dir(1) / 'codex-round-1.log').write_text('old work')
        pr = {'state': 'closed', 'merged': False, 'user': {'login': self.api.login},
              'head': {'ref': previous['branch'], 'repo': {'full_name': self.api.repo}},
              'body': f'<!-- codex-issue-worker:{self.api.repo}#1 -->'}
        def request(path):
            return pr if path == 'pulls/7' else self.api.issues[0]
        with patch.object(self.api, 'request', side_effect=request, create=True), \
                patch.object(self.api, 'pages', return_value=[]), \
                patch('dispatch.inspect_container', return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.queue.restart_closed_pr(1)
        fresh = load_json(self.queue.job_dir(1) / 'worker.json')
        self.assertNotEqual(fresh['branch'], previous['branch'])
        self.assertEqual(fresh['workspace_storage'], 'bind')
        self.assertNotIn('workspace_volume', fresh)
        self.assertNotIn('pr', fresh)
        self.assertEqual(fresh['rounds'], 0)
        self.assertEqual(load_json(Path(fresh['previous_attempt']) / 'worker.json'), previous)
        self.assertNotIn('1', load_json(self.queue.path)['jobs'])

    def test_restart_cannot_replace_open_or_merged_pr(self):
        self.api.login = 'jtact2050'
        self.queue.job_dir(1).mkdir(parents=True)
        self.queue.jobs['1'] = {'status': 'blocked', 'milestone': 1}
        save_json(self.queue.job_dir(1) / 'worker.json', {'pr': 7, 'branch': 'codex/issue-1'})
        for pr in [{'state': 'open'}, {'state': 'closed', 'merged': True}]:
            with patch.object(self.api, 'request', return_value=pr, create=True), self.assertRaises(JobError):
                self.queue.restart_closed_pr(1)

    def test_saved_blocked_attempt_is_selected_when_github_is_ready(self):
        self.api.issues = [self.api.issues[0], self.api.issues[3]]
        self.queue.jobs['1'] = {'status': 'blocked', 'milestone': 1}
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1, 4])

    def test_active_worker_uses_slot_but_allows_independent_next_milestone(self):
        self.api.issues = [self.api.issues[3]]
        self.queue.jobs['1'] = {'status': 'running', 'milestone': 1}
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [4])
        self.args.concurrency = 1
        self.assertEqual(self.tick()['next_issues'], [])
        self.queue.jobs['1']['status'] = 'done'
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [4])

    def test_closed_blocked_issue_does_not_pin_saved_milestone(self):
        self.api.issues = [self.api.issues[3]]
        self.queue.jobs['1'] = {'status': 'blocked', 'milestone': 1}
        for saved in (None, 1):
            with self.subTest(saved=saved):
                self.queue.state['active_milestone'] = saved
                report = self.tick()
                self.assertNotIn('active_milestone', self.queue.state)
                self.assertEqual([i['number'] for i in report['next_issues']], [4])
                self.assertEqual(report['open_issues'], 1)
                self.assertNotIn('blocked_jobs', report)
                self.assertEqual(self.queue.jobs['1'], {'status': 'blocked', 'milestone': 1})

    def test_moved_issue_uses_current_milestone_despite_saved_failure(self):
        self.api.issues = [self.api.issues[0], self.api.issues[3]]
        self.api.issues[0]['milestone'] = {'number': 2}
        self.queue.jobs['1'] = {'status': 'blocked', 'milestone': 1}
        self.queue.state['active_milestone'] = 1
        report = self.tick()
        self.assertNotIn('active_milestone', self.queue.state)
        self.assertEqual([i['number'] for i in report['next_issues']], [1, 4])
        self.assertEqual(self.queue.jobs['1']['status'], 'blocked')

    def test_saved_retry_never_adds_closed_issue_to_live_query(self):
        self.api.issues = [self.api.issues[3]]
        self.queue.jobs['1'] = {'status': 'queued', 'milestone': 1}
        with patch.object(self.api, 'request', create=True) as request:
            report = self.tick()
        self.assertEqual([i['number'] for i in report['next_issues']], [4])
        request.assert_not_called()

    def test_cross_milestone_dependencies_override_milestone_priority(self):
        self.api.issues = [self.api.issues[0], self.api.issues[3]]
        self.api.blockers[1] = [self.api.issues[1]]
        report = self.tick()
        self.assertEqual([i['number'] for i in report['next_issues']], [4])
        self.assertEqual(report['waiting_on_dependencies'], [1])
        # A selected prerequisite is not considered closed in the same batch.
        self.api.blockers = {4: [self.api.issues[0]]}
        report = self.tick()
        self.assertEqual([i['number'] for i in report['next_issues']], [1])
        self.assertEqual(report['waiting_on_dependencies'], [4])

    def test_blocked_attempt_still_blocks_its_actual_dependents(self):
        self.api.issues = [self.api.issues[0], self.api.issues[3]]
        self.queue.jobs['1'] = {'status': 'blocked', 'milestone': 1}
        self.api.blockers[4] = [self.api.issues[0]]
        report = self.tick()
        self.assertEqual([i['number'] for i in report['next_issues']], [1])
        self.assertEqual(report['waiting_on_dependencies'], [4])

    def test_completion_text_is_not_a_pickup_dependency(self):
        self.api.issues = [self.api.issues[0], self.api.issues[3]]
        self.api.issues[0]['body'] = 'Feature completion requires: #4.'
        report = self.tick()
        self.assertEqual([i['number'] for i in report['next_issues']], [1, 4])
        self.assertEqual(report['waiting_on_dependencies'], [])

    def test_dependency_cycle_stays_deferred(self):
        self.api.issues = [self.api.issues[0], self.api.issues[3]]
        self.api.blockers = {1: [self.api.issues[1]], 4: [self.api.issues[0]]}
        report = self.tick()
        self.assertEqual(report['next_issues'], [])
        self.assertEqual(report['waiting_on_dependencies'], [1, 4])

    def test_completed_prerequisites_release_dependent_but_abandoned_ones_do_not(self):
        self.api.blockers[1] = [dict(number=99, state='closed', state_reason='completed')]
        self.assertEqual(self.tick()['next_issues'][0]['number'], 1)
        for reason in ('not_planned', 'duplicate', 'unexpected'):
            self.api.blockers[1][0]['state_reason'] = reason
            report = self.tick()
            self.assertNotIn(1, report['ready_issues'])
            self.assertEqual(report['dependency_blockers'][0]['blocked_by'][0]['state_reason'], reason)
        self.api.blockers[1][0]['state_reason'] = None  # legacy completion
        self.assertEqual(self.tick()['next_issues'][0]['number'], 1)

    def test_every_prerequisite_must_complete_even_outside_selected_milestones(self):
        self.args.milestones = [1]
        self.api.blockers[1] = [dict(number=99, state='closed', state_reason='completed'),
                                dict(number=100, state='open', milestone=None)]
        report = self.tick()
        self.assertNotIn(1, report['ready_issues'])
        self.assertEqual([b['number'] for b in report['dependency_blockers'][0]['blocked_by']], [100])
        self.api.blockers[1][1]['state'] = 'closed'
        self.assertEqual(self.tick()['next_issues'][0]['number'], 1)

    def test_dependency_chain_only_selects_ready_producer_and_refreshes_on_completion(self):
        self.api.blockers = {1: [self.api.issues[1]], 2: [self.api.issues[3]]}
        self.assertEqual(self.tick()['ready_issues'], [3, 4])
        producer = self.api.issues.pop()
        producer.update(state='closed', state_reason='completed')
        self.assertEqual(self.tick()['ready_issues'], [2, 3])
        middle = self.api.issues.pop(1)
        middle.update(state='closed', state_reason='completed')
        self.assertEqual(self.tick()['ready_issues'], [1, 3])

    def test_dry_run_explains_dependencies_and_ready_work_when_slots_are_full(self):
        self.args.concurrency = 1
        self.queue.jobs['4'] = {'status': 'running', 'milestone': 2}
        self.api.blockers[1] = [self.api.issues[3]]
        report = self.tick()
        self.assertEqual(report['next_issues'], [])
        self.assertEqual(report['ready_issues'], [2, 3])
        self.assertEqual(report['available_slots'], 0)
        self.assertTrue(report['selection_scan_complete'])
        self.assertEqual(report['dependency_blockers'][0]['blocked_by'][0]['number'], 4)
        self.assertFalse(self.queue.directory.exists())

    def test_normal_poll_reports_partial_scan_without_fetching_full_backlog(self):
        self.args.dry_run = False
        self.args.concurrency = 1
        self.queue.directory.mkdir(parents=True)
        self.queue.jobs['4'] = {'status': 'running', 'milestone': 2}
        with patch.object(self.queue, 'reconcile'), patch.object(self.api, 'pages', wraps=self.api.pages) as pages:
            report = self.tick()
        self.assertFalse(report['selection_scan_complete'])
        self.assertFalse(any('/dependencies/' in call.args[0] for call in pages.call_args_list))

    def test_dry_run_flags_active_workers_with_new_blockers_without_interrupting(self):
        self.args.concurrency = 1
        self.args.milestones = [1]
        self.queue.jobs['4'] = {'status': 'running', 'milestone': 2}
        self.api.blockers[4] = [self.api.issues[0]]
        with patch('dispatch.docker') as docker:
            report = self.tick()
        self.assertEqual(report['running_dependency_conflicts'][0]['number'], 4)
        self.assertEqual(report['running_dependency_conflicts'][0]['blocked_by'][0]['number'], 1)
        self.assertEqual(report['next_issues'], [])
        self.assertEqual(self.queue.jobs['4']['status'], 'running')
        docker.assert_not_called()

    def test_launch_rechecks_dependency_added_after_selection_without_creating_workspace(self):
        self.assertEqual(self.tick()['next_issues'][0]['number'], 1)
        self.api.blockers[1] = [self.api.issues[3]]
        with patch('dispatch.docker') as docker, contextlib.redirect_stdout(io.StringIO()):
            self.queue.launch(1, 1)
        docker.assert_not_called()
        self.assertEqual(self.queue.jobs, {})
        self.assertFalse(self.queue.job_dir(1).exists())

    def test_launch_rechecks_closure_label_and_milestone(self):
        original = dict(self.api.issues[0])
        for changes in ({'state': 'closed'}, {'labels': [{'name': 'status:blocked'}]},
                        {'milestone': None}, {'milestone': {'number': 2}},
                        {'milestone': {'number': 1, 'state': 'closed'}}):
            self.api.issues[0] = dict(original, **changes)
            with patch('dispatch.docker') as docker, contextlib.redirect_stdout(io.StringIO()):
                self.queue.launch(1, 1)
            docker.assert_not_called()
            self.assertFalse(self.queue.job_dir(1).exists())

    def test_failed_dependency_lookup_cannot_launch(self):
        with patch.object(self.api, 'pages', side_effect=JobError('GitHub unavailable')), \
                patch('dispatch.docker') as docker, self.assertRaisesRegex(JobError, 'unavailable'):
            self.queue.launch(1, 1)
        docker.assert_not_called()
        self.assertEqual(self.queue.jobs, {})

    def test_priority_override_and_filter_use_current_milestones(self):
        self.args.milestones = [2, 1]
        self.queue.state['active_milestone'] = 1
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [4, 1])
        self.args.milestones = [2]
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [4])

    def test_open_retry_uses_moved_milestone(self):
        self.api.issues[0]['milestone'] = {'number': 2}
        self.queue.jobs['1'] = {'status': 'queued', 'milestone': 1}
        self.args.milestones = [2]
        self.args.dry_run = False
        self.queue.directory.mkdir(parents=True)
        with patch.object(self.queue, 'reconcile'), patch.object(self.queue, 'launch') as launch:
            self.tick()
        self.assertEqual([c.args for c in launch.call_args_list], [(1, 2), (4, 2)])

    def test_cross_milestone_launch_records_each_issues_own_milestone(self):
        self.api.issues = [self.api.issues[0], self.api.issues[3]]
        self.args.dry_run = False
        self.queue.directory.mkdir(parents=True)
        with patch.object(self.queue, 'reconcile'), patch.object(self.queue, 'launch') as launch:
            self.tick()
        self.assertEqual([c.args for c in launch.call_args_list], [(1, 1), (4, 2)])

    def test_native_dependency_defers_issue(self):
        self.api.blockers[1] = [{'state': 'open'}]
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [2, 3])

    def test_completion_and_final_gate_text_do_not_override_native_dependencies(self):
        self.api.issues[0]['body'] = 'Feature completion requires: #2.'
        self.api.issues[1]['body'] = 'Final gate assessment requires the relevant outputs/evidence from #3.'
        report = self.tick()
        self.assertEqual([i['number'] for i in report['next_issues']], [1, 2])
        self.assertEqual(report['waiting_on_dependencies'], [])

    def test_child_does_not_wait_for_parent_reference(self):
        self.api.issues[0]['body'] = 'Feature/completion owner: [#2](https://github.com/jtact2050/test/issues/2). Parent closure is not a prerequisite for this child.'
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1, 2])

    def test_tracker_becomes_eligible_when_children_closed(self):
        self.api.issues[0]['body'] = 'Feature completion requires: #2, #3.'
        self.api.issues = [self.api.issues[0], self.api.issues[3]]
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1, 4])

    def test_state_roundtrip_preserves_jobs(self):
        self.queue.directory.mkdir(parents=True)
        self.queue.jobs['1'] = {'status': 'running', 'milestone': 1}
        self.queue.save()
        other = Dispatcher(self.api, self.args)
        self.assertEqual(other.jobs, self.queue.jobs)

    def test_launch_resumes_stopped_worker_without_persisting_a_retry_queue(self):
        self.api.open_prs = [{'number': 2, 'state': 'open'}]
        self.queue.job_dir(1).mkdir(parents=True)
        self.queue.workspace_dir(1).mkdir()
        path = self.queue.job_dir(1) / 'worker.json'
        save_json(path, {'phase': 'blocked', 'resume_phase': 'waiting_review', 'pr': 2,
                         'rounds': 2, 'error': 'interrupted', 'workspace_storage': 'bind'})
        with patch('dispatch.inspect_container', return_value=None), patch('dispatch.docker', return_value=SimpleNamespace(stdout='container-id')) as docker, \
                contextlib.redirect_stdout(io.StringIO()):
            self.queue.launch(1, 1)
        worker = load_json(path)
        self.assertEqual(worker['phase'], 'waiting_review')
        self.assertEqual(worker['rounds'], 2)
        self.assertNotIn('error', worker)
        self.assertIn(f'type=bind,source={self.queue.workspace_dir(1)},target=/workspace', docker.call_args.args)
        self.assertEqual(load_json(self.queue.path)['jobs']['1']['status'], 'running')

    def test_closed_pr_automatically_gets_fresh_attempt_despite_old_budget_and_blocker(self):
        workspace = self.queue.prepare_workspace(1)
        (workspace / 'unfinished.txt').write_text('preserve this work')
        path = self.queue.job_dir(1) / 'worker.json'
        previous = load_json(path)
        previous.update(pr=671, phase='blocked', rounds=20, manual_retry_required=True,
                        blocked_issue_digest=issue_digest(self.api.issues[0]),
                        error='Managed PR was closed without merging; no new PR created')
        save_json(path, previous)
        self.assertEqual(self.tick()['next_issues'][0]['number'], 1)
        self.assertEqual(load_json(path), previous)  # dry run does not archive
        with contextlib.redirect_stdout(io.StringIO()):
            self.queue.prepare_resume(1)
        fresh = load_json(path)
        self.assertNotIn('pr', fresh)
        self.assertNotIn('manual_retry_required', fresh)
        self.assertEqual(fresh['rounds'], 0)
        self.assertNotEqual(fresh['branch'], previous['branch'])
        archive = Path(fresh['previous_attempt'])
        self.assertEqual(load_json(archive / 'worker.json'), previous)
        self.assertEqual((archive / 'workspace/unfinished.txt').read_text(), 'preserve this work')
        self.assertFalse(workspace.exists())
        # Another poll resumes the fresh attempt rather than repeatedly archiving it.
        self.queue.prepare_resume(1)
        self.assertEqual(load_json(path), fresh)

    def test_open_pr_still_obeys_attempt_budget(self):
        self.queue.job_dir(1).mkdir(parents=True)
        self.api.open_prs = [{'number': 7, 'state': 'open'}]
        save_json(self.queue.job_dir(1) / 'worker.json', {'pr': 7, 'phase': 'blocked', 'rounds': 20})
        self.assertEqual(self.tick()['coding_budget_exhausted'][0]['number'], 1)

    def test_open_pr_lookup_failure_does_not_reset_attempt(self):
        self.queue.job_dir(1).mkdir(parents=True)
        path = self.queue.job_dir(1) / 'worker.json'
        previous = {'pr': 7, 'phase': 'blocked', 'rounds': 20}
        save_json(path, previous)
        with patch.object(self.api, 'pages', side_effect=JobError('GitHub unavailable')), self.assertRaises(JobError):
            self.queue.prepare_resume(1)
        self.assertEqual(load_json(path), previous)

    def test_missing_and_legacy_workspaces_are_not_silently_replaced(self):
        self.queue.job_dir(1).mkdir(parents=True)
        for state in ({'rounds': 2, 'workspace_storage': 'bind'},
                      {'rounds': 2, 'workspace_volume': 'old-volume'}):
            save_json(self.queue.job_dir(1) / 'worker.json', state)
            with self.assertRaises(JobError):
                self.queue.prepare_workspace(1)
            self.assertFalse(self.queue.workspace_dir(1).exists())

    def test_fresh_attempt_moves_workspace_and_preserves_symlinks_inside_issue_folder(self):
        directory = self.queue.job_dir(1)
        workspace = self.queue.prepare_workspace(1)
        (workspace / 'repo').mkdir()
        (workspace / 'repo/unfinished.txt').write_text('saved work')
        (workspace / '.codex').mkdir()
        (workspace / '.codex/auth.json').symlink_to('/home/codex/.codex-auth/auth.json')
        old = load_json(directory / 'worker.json')
        old.update(phase='done', rounds=5, codex_session={'id': 'old-session'})
        save_json(directory / 'worker.json', old)
        self.queue.prepare_resume(1)
        fresh = load_json(directory / 'worker.json')
        archive = Path(fresh['previous_attempt'])
        self.assertEqual(archive.parent, directory / 'attempts')
        self.assertEqual((archive / 'workspace/repo/unfinished.txt').read_text(), 'saved work')
        self.assertTrue((archive / 'workspace/.codex/auth.json').is_symlink())
        self.assertEqual(load_json(archive / 'worker.json'), old)
        self.assertNotIn('codex_session', fresh)
        self.assertEqual(list(self.queue.prepare_workspace(1).iterdir()), [])

    def test_interrupted_workspace_move_recovers_before_creating_fresh_workspace(self):
        directory = self.queue.job_dir(1)
        workspace = self.queue.prepare_workspace(1)
        (workspace / 'pending.txt').write_text('keep')
        state = load_json(directory / 'worker.json')
        state['phase'] = 'done'
        save_json(directory / 'worker.json', state)
        original_save = save_json
        def fail_new_state(path, value):
            if Path(path).name == 'worker.json' and value.get('previous_attempt'):
                raise OSError('simulated crash after workspace move')
            original_save(path, value)
        with patch('dispatch.save_json', side_effect=fail_new_state), self.assertRaises(OSError):
            self.queue.prepare_resume(1)
        self.assertFalse(workspace.exists())
        self.assertTrue((directory / 'workspace-transition.json').exists())
        self.queue.prepare_resume(1)
        fresh = load_json(directory / 'worker.json')
        self.assertEqual((Path(fresh['previous_attempt']) / 'workspace/pending.txt').read_text(), 'keep')
        self.assertEqual(list(self.queue.prepare_workspace(1).iterdir()), [])
        self.assertFalse((directory / 'workspace-transition.json').exists())

    def test_restart_discards_legacy_blocked_done_and_queued_scheduler_entries(self):
        self.queue.directory.mkdir(parents=True)
        save_json(self.queue.path, {'repo': self.api.repo, 'jobs': {
            '1': {'status': 'blocked', 'milestone': 99},
            '2': {'status': 'done', 'milestone': 99},
            '3': {'status': 'queued', 'milestone': 99},
            '4': {'status': 'running', 'milestone': 2}}})
        self.queue = Dispatcher(self.api, self.args)
        self.assertEqual(list(self.queue.jobs), ['4'])
        self.queue.save()
        self.assertEqual(list(load_json(self.queue.path)['jobs']), ['4'])
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1])

    def test_every_tick_observes_live_issue_and_dependency_changes(self):
        self.args.concurrency = 1
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [1])
        self.api.issues[0]['labels'] = [{'name': 'status:blocked'}]
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [2])
        self.api.blockers[2] = [{'number': 99, 'state': 'open'}]
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [3])
        self.api.issues = self.api.issues[3:]
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [4])

    def test_finished_attempt_of_reopened_issue_gets_new_branch_and_workspace(self):
        self.queue.job_dir(1).mkdir(parents=True)
        path = self.queue.job_dir(1) / 'worker.json'
        previous = {'phase': 'done', 'pr': 2, 'branch': 'codex/issue-1', 'rounds': 4}
        save_json(path, previous)
        with patch('dispatch.inspect_container', return_value=None), patch('dispatch.docker', return_value=SimpleNamespace(stdout='container-id')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.queue.launch(1, 1)
        worker = load_json(path)
        self.assertNotEqual(worker['branch'], previous['branch'])
        self.assertNotIn('pr', worker)
        self.assertEqual(worker['rounds'], 0)
        self.assertEqual(load_json(Path(worker['previous_attempt']) / 'worker.json'), previous)

    def test_missing_container_returns_to_live_selection_without_saved_relaunch(self):
        self.queue.directory.mkdir(parents=True)
        self.queue.jobs['1'] = {'status': 'launching', 'milestone': 1}
        with patch('dispatch.docker', return_value=SimpleNamespace(stdout='')), \
                patch('dispatch.inspect_container', return_value=None), \
                patch.object(self.queue, 'launch') as launch:
            self.queue.reconcile()
        launch.assert_not_called()
        self.assertEqual(self.queue.jobs, {})
        self.assertEqual(load_json(self.queue.path)['jobs'], {})
        self.api.issues = self.api.issues[3:]
        self.assertEqual([i['number'] for i in self.tick()['next_issues']], [4])

    def test_finished_attempt_is_cleaned_and_not_saved_as_blocked(self):
        self.queue.directory.mkdir(parents=True)
        self.queue.jobs['1'] = {'status': 'running', 'milestone': 1}
        self.queue.job_dir(1).mkdir(parents=True)
        save_json(self.queue.job_dir(1) / 'worker.json', {'phase': 'blocked', 'error': 'missing input'})
        with patch('dispatch.docker', return_value=SimpleNamespace(stdout='', stderr='')), \
                patch('dispatch.inspect_container', return_value=self.stopped_container()), \
                patch.object(self.queue, 'cleanup_finished_containers') as cleanup, \
                contextlib.redirect_stdout(io.StringIO()):
            self.queue.reconcile()
        cleanup.assert_called_once()
        self.assertEqual(self.queue.jobs, {})
        self.assertEqual(load_json(self.queue.path)['jobs'], {})

    def test_dry_run_never_launches(self):
        with patch.object(self.queue, 'launch') as launch:
            self.tick()
        launch.assert_not_called()

    def stopped_container(self, number=1):
        return {'Id': 'test-container', 'Config': {'Labels': {
            'codex.repo': self.api.repo, 'codex.issue': str(number)}},
            'State': {'Status': 'exited', 'Running': False, 'ExitCode': 1},
            'Mounts': [{'Type': 'volume', 'Name': 'saved-workspace'}]}

    def test_terminal_cleanup_preserves_logs_state_and_volume_for_retry(self):
        for status in ('done', 'blocked'):
            self.queue.jobs['1'] = {'status': status, 'milestone': 1}
            self.queue.job_dir(1).mkdir(parents=True, exist_ok=True)
            save_json(self.queue.job_dir(1) / 'worker.json', {'phase': status, 'rounds': 2})
            name = self.queue.name(1)
            def docker_call(*args, **kwargs):
                if args[0] == 'rm':
                    self.assertEqual(args, ('rm', name))
                    self.assertEqual((self.queue.job_dir(1) / 'container.log').read_text(), 'stdoutstderr')
                return SimpleNamespace(stdout=name if args[0] == 'ps' else 'stdout', stderr='stderr')
            with patch('dispatch.docker', side_effect=docker_call), \
                    patch('dispatch.inspect_container', side_effect=[self.stopped_container(), None]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.queue.cleanup_finished_containers()
            self.assertEqual(load_json(self.queue.job_dir(1) / 'worker.json')['rounds'], 2)
            self.assertEqual(self.queue.jobs['1']['status'], status)
            self.assertEqual(load_json(self.queue.job_dir(1) / 'container-removal.json')['workspace_volumes'], ['saved-workspace'])

    def test_cleanup_does_not_touch_active_worker(self):
        self.queue.jobs['1'] = {'status': 'running', 'milestone': 1}
        container = self.stopped_container()
        container['State'] = {'Status': 'running', 'Running': True}
        with patch('dispatch.docker', return_value=SimpleNamespace(stdout=self.queue.name(1))) as docker, \
                patch('dispatch.inspect_container', return_value=container):
            self.queue.cleanup_finished_containers()
        self.assertEqual([c.args[0] for c in docker.call_args_list], ['ps'])

    def test_container_history_survives_multiple_retries(self):
        directory = self.queue.job_dir(1)
        directory.mkdir(parents=True)
        (directory / 'container.log').write_text('legacy evidence')
        for attempt in (1, 2):
            container = self.stopped_container()
            container['Id'] = f'container-{attempt}'
            with patch('dispatch.docker', return_value=SimpleNamespace(
                    stdout=f'attempt {attempt}', stderr=f'error {attempt}')):
                self.queue.archive_container(1, self.queue.name(1), container, directory)
        snapshots = list((directory / 'containers').iterdir())
        self.assertEqual(len(snapshots), 2)
        self.assertEqual({(p / 'stdout.log').read_text() for p in snapshots}, {'attempt 1', 'attempt 2'})
        self.assertEqual({load_json(p / 'container.json')['id'] for p in snapshots}, {'container-1', 'container-2'})
        self.assertEqual((directory / 'legacy-container.log').read_text(), 'legacy evidence')
        self.assertEqual((directory / 'container.log').read_text(), 'attempt 2error 2')

    def test_cleanup_failure_prevents_next_launch_and_retries_on_next_tick(self):
        self.args.dry_run = False
        self.args.concurrency = 1
        self.queue.jobs['1'] = {'status': 'done', 'milestone': 1}
        name = self.queue.name(1)
        def docker_call(*args, **kwargs):
            if args[0] == 'rm':
                raise JobError('removal failed')
            return SimpleNamespace(stdout=name if args[0] == 'ps' else 'logs', stderr='')
        with patch('dispatch.docker', side_effect=docker_call) as docker, \
                patch('dispatch.inspect_container', return_value=self.stopped_container()), \
                patch.object(self.queue, 'launch') as launch:
            for _ in range(2):
                with self.assertRaisesRegex(JobError, 'removal failed'):
                    self.tick()
            launch.assert_not_called()
            self.assertEqual(sum(c.args[0] == 'rm' for c in docker.call_args_list), 2)

    def test_log_archive_failure_prevents_deletion(self):
        self.queue.jobs['1'] = {'status': 'blocked', 'milestone': 1}
        def docker_call(*args, **kwargs):
            if args[0] == 'logs':
                raise JobError('logs unavailable')
            return SimpleNamespace(stdout=self.queue.name(1))
        with patch('dispatch.docker', side_effect=docker_call) as docker, \
                patch('dispatch.inspect_container', return_value=self.stopped_container()), \
                self.assertRaisesRegex(JobError, 'logs unavailable'):
            self.queue.cleanup_finished_containers()
        self.assertFalse(any(c.args[0] == 'rm' for c in docker.call_args_list))


if __name__ == '__main__':
    unittest.main()
