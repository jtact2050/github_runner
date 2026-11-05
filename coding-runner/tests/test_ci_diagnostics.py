import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import APIError, JobError, issue_digest, load_json
from policy import failed_checks
from result_fixture import model_result
import test_worker
from worker import Worker


class CIDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        test_worker.WorkerTests.setUp(self)
        self.api.env = {'PATH': '/usr/bin'}
        self.api.token = 'private-test-token'
        self.check = {'id': 101, 'name': 'build', 'status': 'completed', 'conclusion': 'failure',
                      'html_url': 'https://github.com/jtact2050/test/actions/runs/10/job/20'}
        self.feedback = [{'key': 'ci:old', 'checks': [{'name': 'build', 'url': 'old-job'}]}]

    def test_job_logs_do_not_wait_for_other_workflow_jobs(self):
        logs = SimpleNamespace(returncode=0, stdout='TS2345: string | null; private-test-token', stderr='')
        with patch('worker.subprocess.run', return_value=logs) as command:
            result = self.worker.ci_feedback(failed_checks([self.check], []))
        self.assertEqual(command.call_args.args[0],
                         ['gh', 'api', 'repos/jtact2050/test/actions/jobs/20/logs'])
        self.assertEqual(command.call_args.kwargs['env'], self.api.env)
        self.assertFalse(result[0]['diagnostics_pending'])
        self.assertIn('TS2345', result[0]['log_excerpt'])
        self.assertNotIn(self.api.token, str(result))

    def test_annotations_supply_error_when_job_logs_are_unavailable(self):
        logs = SimpleNamespace(returncode=1, stdout='', stderr='HTTP 404')
        annotations = [{'path': 'src/helper.ts', 'start_line': 246, 'annotation_level': 'failure',
                        'message': 'TS2345: string | null is not assignable to string'}]
        with patch('worker.subprocess.run', return_value=logs), \
                patch.object(self.api, 'pages', return_value=annotations) as pages:
            result = self.worker.ci_feedback(failed_checks([self.check], []))
        pages.assert_called_once_with('check-runs/101/annotations')
        self.assertIn('src/helper.ts:246: TS2345', result[0]['log_excerpt'])
        self.assertFalse(result[0]['diagnostics_pending'])

    def test_failed_empty_and_timed_out_log_fetches_are_not_diagnostics(self):
        for outcome in (SimpleNamespace(returncode=1, stdout='', stderr='HTTP 403'),
                        SimpleNamespace(returncode=0, stdout='\n', stderr=''),
                        subprocess.TimeoutExpired('gh', 90), OSError('gh unavailable')):
            with self.subTest(outcome=outcome), \
                    patch('worker.subprocess.run', **({'side_effect': outcome} if isinstance(outcome, Exception)
                                                     else {'return_value': outcome})), \
                    patch.object(self.api, 'pages', side_effect=APIError(403, 'annotations')):
                result = self.worker.ci_feedback(failed_checks([self.check], []))
            self.assertTrue(result[0]['diagnostics_pending'])
            self.assertNotIn('log_excerpt', result[0])

    def test_wait_refreshes_rerun_job_then_coder_receives_error_once(self):
        newer = dict(self.check, id=102, html_url=self.check['html_url'].replace('/20', '/21'))
        logs = [SimpleNamespace(returncode=1, stdout='', stderr='Logs not ready'),
                SimpleNamespace(returncode=0, stdout='TS2345 at helper.ts:246', stderr='')]
        def wait(_):
            self.assertEqual(self.worker.state['rounds'], 0)
            self.assertEqual(self.worker.state['phase'], 'waiting_ci_diagnostics')
            run.assert_not_called()
        with patch.object(self.worker, 'snapshot', side_effect=[('waiting', [], [self.check], {}),
                                                               ('waiting', [], [newer], {})]), \
                patch('worker.subprocess.run', side_effect=logs) as command, \
                patch.object(self.api, 'pages', return_value=[]), \
                patch('worker.time.sleep', side_effect=wait) as sleep, \
                patch.object(self.worker, 'integrate_base'), \
                patch.object(self.worker, 'run_codex', return_value=model_result()) as run, \
                patch.object(self.worker, 'publish') as publish:
            self.worker.work(self.api.issue, 'Fix CI', self.feedback)
        sleep.assert_called_once()
        run.assert_called_once()
        publish.assert_called_once()
        received = run.call_args.args[2][0]
        self.assertEqual(received['key'], 'ci:abc')
        self.assertIn('TS2345', received['checks'][0]['log_excerpt'])
        self.assertTrue(command.call_args.args[0][-1].endswith('/21/logs'))
        self.assertEqual(self.worker.state['rounds'], 1)
        self.assertEqual(self.worker.state['handled'], ['ci:abc'])

    def test_restart_refreshes_legacy_blocked_feedback_even_if_issue_changed(self):
        self.worker.save(rounds=4, phase='coding',
                         continuation={'issue_digest': issue_digest(self.api.issue),
                                       'reason': 'Fix CI', 'feedback': self.feedback},
                         last_result=model_result('blocked', blocker={
                             'reason': 'No logs', 'owner': 'Controller',
                             'unblock_condition': 'Provide logs', 'evidence': 'HTTP 404'}))
        self.api.issue['body'] = 'Updated requirements'
        resumed = Worker(self.api, 1, state_dir=self.worker.state_dir)
        with patch.object(resumed, 'snapshot', return_value=('waiting', [], [self.check], {})), \
                patch('worker.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout='TS2345: fix nullable argument', stderr='')), \
                patch.object(resumed, 'integrate_base'), \
                patch.object(resumed, 'run_codex', return_value=model_result()) as run, \
                patch.object(resumed, 'publish'):
            resumed.work(self.api.issue, 'Resume interrupted implementation', [])
        self.assertEqual(resumed.state['rounds'], 5)
        self.assertIn('TS2345', run.call_args.args[2][0]['checks'][0]['log_excerpt'])

    def test_wait_survives_restart_and_times_out_without_using_rounds(self):
        self.worker.save(rounds=3, ci_diagnostics_started=1)
        with patch.object(self.worker, 'snapshot', return_value=('waiting', [], [self.check], {})), \
                patch('worker.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='404')), \
                patch.object(self.api, 'pages', return_value=[]), \
                patch('worker.time.time', return_value=self.worker.review_timeout + 2), \
                patch.object(self.worker, 'run_codex') as run:
            with self.assertRaisesRegex(JobError, 'without spending a coding round'):
                self.worker.work(self.api.issue, 'Fix CI', self.feedback)
        run.assert_not_called()
        state = load_json(self.worker.path)
        self.assertEqual(state['rounds'], 3)
        self.assertEqual(state['ci_diagnostics_started'], 1)
        self.assertEqual(state['phase'], 'waiting_ci_diagnostics')
        self.assertEqual(state['continuation']['feedback'][0]['key'], 'ci:abc')

    def test_passing_rerun_drops_stale_failure_without_coding(self):
        self.worker.save(last_head='abc')
        with patch.object(self.worker, 'snapshot', return_value=('waiting', [], [], {})), \
                patch.object(self.worker, 'needs_base_integration', return_value=False), \
                patch.object(self.worker, 'git', side_effect=[SimpleNamespace(stdout=''), SimpleNamespace(stdout='abc')]), \
                patch.object(self.worker, 'run_codex') as run:
            self.worker.work(self.api.issue, 'Fix CI', self.feedback)
        run.assert_not_called()
        self.assertEqual(self.worker.state['rounds'], 0)
        self.assertEqual(self.worker.state['phase'], 'waiting_review')
        self.assertNotIn('continuation', self.worker.state)

    def test_passing_rerun_does_not_discard_conflict_or_changed_scope_work(self):
        for changed_scope in (True, False):
            with self.subTest(changed_scope=changed_scope):
                self.api.issue.pop('body', None)
                self.api.pull['mergeable'] = changed_scope
                if changed_scope:
                    self.api.issue['body'] = 'New requirements'
                with patch.object(self.worker, 'snapshot', return_value=('waiting', [], [], {})), \
                        patch.object(self.worker, 'integrate_base'), \
                        patch.object(self.worker, 'run_codex', return_value=model_result()) as run, \
                        patch.object(self.worker, 'publish'):
                    self.worker.work(self.api.issue, 'Resolve conflicts and fix CI', self.feedback)
                run.assert_called_once()

    def test_passing_rerun_preserves_unpublished_edits_and_commits(self):
        self.worker.save(last_head='abc')
        for status, head in ((' M helper.ts', 'abc'), ('', 'unpublished')):
            with self.subTest(status=status, head=head), \
                    patch.object(self.worker, 'needs_base_integration', return_value=False), \
                    patch.object(self.worker, 'git', side_effect=[SimpleNamespace(stdout=status), SimpleNamespace(stdout=head)]):
                self.assertFalse(self.worker.ci_repair_is_obsolete())

    def test_review_feedback_is_preserved_with_fresh_ci_diagnostics(self):
        old = {'key': 'review:1', 'body': 'Preserve this requested fix'}
        new = {'key': 'review:2', 'body': 'Also fix cancellation'}
        with patch.object(self.worker, 'snapshot', return_value=('changes', [new], [self.check], {})), \
                patch('worker.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='TS2345', stderr='')):
            feedback = self.worker.wait_for_ci_diagnostics('Fix CI', [old, *self.feedback])
        self.assertEqual(feedback[:2], [old, new])
        self.assertEqual(feedback[2]['key'], 'ci:abc')

    def test_all_failed_jobs_are_included(self):
        checks = [dict(self.check, id=n, name=f'job-{n}') for n in range(1, 8)]
        with patch('worker.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='error', stderr='')):
            result = self.worker.ci_feedback(failed_checks(checks, []))
        self.assertEqual(len(result), 7)
