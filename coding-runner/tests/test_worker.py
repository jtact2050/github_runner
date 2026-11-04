import copy
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import JobError, load_json, save_json
from worker import Worker, ModelRunError, issue_digest, validate_result
from result_fixture import event_stream, model_result


class FakeAPI:
    repo = 'jtact2050/test'
    login = 'jtact2050'
    user_id = 1

    def __init__(self):
        self.calls = []
        self.issue = {'number': 1, 'state': 'open', 'state_reason': None, 'title': 'Test'}
        self.pull = {'number': 2, 'node_id': 'node', 'state': 'open', 'draft': False,
                     'user': {'login': self.login}, 'body': '<!-- codex-issue-worker:jtact2050/test#1 -->',
                     'head': {'sha': 'abc', 'ref': 'codex/issue-1', 'repo': {'full_name': self.repo}},
                     'base': {'ref': 'main'}, 'merged': False, 'html_url': 'https://github.com/jtact2050/test/pull/2'}
        self.clean = 'CLEAN'

    def repository(self):
        return {'default_branch': 'main', 'allow_squash_merge': True}

    def pages(self, path):
        self.calls.append((path, None, None))
        if path == 'pulls?state=open':
            return [copy.deepcopy(self.pull)] if self.pull['state'] == 'open' else []
        raise AssertionError(path)

    def request(self, path, payload=None, method=None):
        self.calls.append((path, payload, method))
        if path == 'pulls/2':
            return copy.deepcopy(self.pull)
        if path == 'pulls/2/merge':
            assert payload['sha'] == self.pull['head']['sha']
            self.pull.update(merged=True, state='closed', merge_commit_sha='merged-sha')
            return {'merged': True}
        if path == 'issues/1':
            if payload:
                self.issue.update(payload)
            return self.issue.copy()
        if path == 'pulls/2/requested_reviewers':
            return {}
        if path == 'actions/workflows/codex-review.yml/dispatches':
            return None
        raise AssertionError(path)

    def graphql(self, query, **kwargs):
        self.calls.append(('graphql', kwargs, None))
        if 'markPullRequestReadyForReview' in query:
            self.pull['draft'] = False
            return {}
        return {'repository': {'pullRequest': {'mergeStateStatus': self.clean}}}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = FakeAPI()
        self.worker = Worker(self.api, 1, state_dir=Path(self.temp.name))
        self.worker.save(pr=2, issue_digest=issue_digest(self.api.issue))

    def test_resume_fetches_remote_history_without_changing_worktree(self):
        self.worker.root = Path(self.temp.name) / 'repo'
        (self.worker.root / '.git').mkdir(parents=True)
        pending = self.worker.root / 'pending.txt'
        pending.write_text('preserved work')
        with patch.object(self.worker, 'git', return_value=SimpleNamespace(
                stdout=self.worker.branch + '\n', returncode=0)) as git:
            self.worker.setup_repo()
        self.assertEqual([c.args for c in git.call_args_list], [
            ('branch', '--show-current'),
            ('fetch', '--no-tags', 'origin', '+refs/heads/*:refs/remotes/origin/*'),
        ])
        self.assertTrue(git.call_args.kwargs['auth'])
        self.assertEqual(pending.read_text(), 'preserved work')

    def test_merge_then_close_and_confirm_issue(self):
        with patch.object(self.worker, 'snapshot', return_value=('approved', [], [], {})):
            self.assertTrue(self.worker.merge_if_approved(self.api.pull.copy()))
        paths = [c[0] for c in self.api.calls]
        closure = next(i for i, c in enumerate(self.api.calls) if c[0] == 'issues/1' and c[2] == 'PATCH')
        self.assertLess(paths.index('pulls/2/merge'), closure)
        self.assertEqual(self.api.issue['state_reason'], 'completed')
        self.assertEqual(self.worker.state['phase'], 'done')

    def test_never_merge_without_fresh_approval_or_with_failed_check(self):
        for decision in ['waiting', 'changes']:
            with patch.object(self.worker, 'snapshot', return_value=(decision, [], [], {})):
                self.assertFalse(self.worker.merge_if_approved(self.api.pull))
        check = {'name': 'ci', 'status': 'completed', 'conclusion': 'failure'}
        with patch.object(self.worker, 'snapshot', return_value=('approved', [], [check], {})):
            self.assertFalse(self.worker.merge_if_approved(self.api.pull))
        self.assertFalse(any(c[0].endswith('/merge') for c in self.api.calls))

    def test_head_change_and_branch_protection_block_merge(self):
        original = copy.deepcopy(self.api.pull)
        self.api.pull['head']['sha'] = 'new'
        self.assertFalse(self.worker.merge_if_approved(original))
        self.api.clean = 'BLOCKED'
        with patch.object(self.worker, 'snapshot', return_value=('approved', [], [], {})):
            self.assertFalse(self.worker.merge_if_approved(self.api.pull))

    def test_issue_not_closed_if_pr_not_merged(self):
        with self.assertRaises(JobError):
            self.worker.finish(self.api.pull)
        self.assertEqual(self.api.issue['state'], 'open')

    def test_changed_issue_blocks_even_current_head_approval(self):
        self.api.issue['body'] = 'New acceptance criteria'
        with patch.object(self.worker, 'snapshot', return_value=('approved', [], [], {})):
            self.assertFalse(self.worker.merge_if_approved(self.api.pull.copy()))
        self.assertFalse(any(c[0].endswith('/merge') for c in self.api.calls))

    def test_issue_changed_during_coding_saves_new_scope_for_automatic_retry(self):
        seen = []
        def coding(issue, reason, feedback):
            seen.append(issue.get('body'))
            self.api.issue['body'] = 'Updated scope'
            return model_result()
        with patch.object(self.worker, 'integrate_base'), patch.object(self.worker, 'run_codex', side_effect=coding), \
                patch.object(self.worker, 'publish') as publish, self.assertRaisesRegex(JobError, 'requirements changed'):
            self.worker.work(self.api.issue, 'Implement', [])
        self.assertEqual(seen, [None])
        publish.assert_not_called()
        self.assertEqual(self.worker.state['continuation']['issue_digest'], issue_digest(self.api.issue))
        self.assertNotIn('manual_retry_required', self.worker.state)

    def test_closed_pr_cannot_receive_more_pushes(self):
        self.api.pull['state'] = 'closed'
        self.worker.save(pending_issue_digest=issue_digest(self.api.issue))
        with patch.object(self.worker, 'git') as git, self.assertRaisesRegex(JobError, 'PR closed before publish'):
            self.worker.publish({'summary': 'Ready', 'tests': []})
        git.assert_not_called()

    def test_changed_scope_without_file_edits_requires_new_commit(self):
        self.api.issue['body'] = 'New scope validated by existing implementation'
        self.worker.save(pending_issue_digest=issue_digest(self.api.issue))
        with patch.object(self.worker, 'git', return_value=SimpleNamespace(returncode=0, stdout='newsha')) as git, \
                patch.object(self.worker, 'request_review') as review:
            self.worker.publish({'summary': 'Ready', 'tests': []})
        self.assertTrue(any(c.args[:2] == ('commit', '--allow-empty') for c in git.call_args_list))
        review.assert_called_once()

    def test_ready_and_request_correct_reviewer(self):
        self.api.pull['draft'] = True
        self.worker.request_review()
        self.assertFalse(self.api.pull['draft'])
        self.assertIn(('pulls/2/requested_reviewers', {'reviewers': ['jacktian8888']}, None), self.api.calls)

    def test_stalled_review_dispatches_at_fifteen_minutes_and_survives_restart(self):
        self.worker.save(review_started=1000)
        endpoint = 'actions/workflows/codex-review.yml/dispatches'
        with patch('worker.time.time', return_value=1899):
            self.worker.retry_stalled_review(self.api.pull, 'waiting')
        self.assertFalse(any(c[0] == endpoint for c in self.api.calls))
        with patch('worker.time.time', return_value=1900):
            self.worker.retry_stalled_review(self.api.pull, 'waiting')
        self.assertIn((endpoint, {'ref': 'main', 'inputs': {'pr_number': '2'}}, None), self.api.calls)
        resumed = Worker(self.api, 1, state_dir=Path(self.temp.name))
        with patch('worker.time.time', return_value=2799):
            resumed.retry_stalled_review(self.api.pull, 'waiting')
        self.assertEqual(sum(c[0] == endpoint for c in self.api.calls), 1)
        with patch('worker.time.time', return_value=2800):
            resumed.retry_stalled_review(self.api.pull, 'waiting')
        self.assertEqual(sum(c[0] == endpoint for c in self.api.calls), 2)
        self.assertEqual(resumed.state['review_started'], 1000)
        self.assertEqual(resumed.state['rounds'], 0)

    def test_review_retry_failure_is_persisted_before_write_and_rate_limited(self):
        self.worker.save(review_started=1000)
        def fail(*args, **kwargs):
            saved = load_json(self.worker.path)
            self.assertEqual(saved['review_retry']['attempted_at'], 1900)
            raise JobError('ambiguous network failure')
        with patch.object(self.api, 'request', side_effect=fail) as request:
            with patch('worker.time.time', return_value=1900):
                self.worker.retry_stalled_review(self.api.pull, 'waiting')
            with patch('worker.time.time', return_value=1901):
                self.worker.retry_stalled_review(self.api.pull, 'waiting')
            request.assert_called_once()

    def test_review_retry_skips_completed_reviews_and_ineligible_prs(self):
        self.worker.save(review_started=1000)
        for signal_, updates in [('approved', {}), ('changes', {}), ('waiting', {'draft': True}),
                                 ('waiting', {'state': 'closed'}), ('waiting', {'merged': True})]:
            with self.subTest(signal=signal_, updates=updates), patch('worker.time.time', return_value=1900), \
                    patch.object(self.api, 'request') as request:
                pr = {**self.api.pull, **updates}
                self.worker.retry_stalled_review(pr, signal_)
                request.assert_not_called()

    def test_new_review_request_resets_retry_timer(self):
        self.worker.save(review_started=1000, review_retry={'attempted_at': 1900})
        with patch('worker.time.time', return_value=2000):
            self.worker.request_review()
        self.assertIsNone(self.worker.state['review_retry'])
        with patch('worker.time.time', return_value=2899), patch.object(self.api, 'request') as request:
            self.worker.retry_stalled_review(self.api.pull, 'waiting')
            request.assert_not_called()

    def test_poll_recovers_missing_workflow_even_with_green_head_checks(self):
        self.worker.save(phase='waiting_review', review_started=1000)
        with patch.object(self.worker, 'discover_pr', return_value=self.api.pull), \
                patch.object(self.worker, 'setup_repo'), \
                patch.object(self.worker, 'git', return_value=SimpleNamespace(stdout='abc')), \
                patch.object(self.worker, 'needs_base_integration', return_value=False), \
                patch.object(self.worker, 'snapshot', return_value=('waiting', [], [
                    {'name': 'Verify', 'status': 'completed', 'conclusion': 'success'}], {})), \
                patch.object(self.worker, 'work') as work, \
                patch('worker.time.time', return_value=1900), \
                patch('worker.time.sleep', side_effect=JobError('end poll')):
            with self.assertRaisesRegex(JobError, 'end poll'):
                self.worker.run_workflow()
        work.assert_not_called()
        self.assertTrue(any(c[0] == 'actions/workflows/codex-review.yml/dispatches' for c in self.api.calls))

    def test_other_author_or_branch_rejected(self):
        self.api.pull['user']['login'] = 'someone'
        with self.assertRaises(JobError):
            self.worker.verify_pr(self.api.pull)

    def test_parallel_workers_poll_only_their_own_pr_and_commit(self):
        other = Worker(self.api, 3, state_dir=Path(self.temp.name) / 'issue-3')
        other.save(pr=4)
        other_pr = copy.deepcopy(self.api.pull)
        other_pr.update(number=4, body=other.marker)
        other_pr['head'].update(ref=other.branch, sha='other-head')
        calls = {2: [], 4: []}

        def poll(worker, pr):
            # Separate clients, as in the real containers, against one fake repo.
            api = FakeAPI()
            worker.api = api
            def pages(path, key=None):
                calls[pr['number']].append(path)
                if path == 'pulls/2/reviews':
                    return [{'id': 1, 'state': 'APPROVED', 'commit_id': 'abc',
                             'user': {'login': 'jacktian8888'}, 'submitted_at': '2026-10-30T01:00:00Z'}]
                if path == 'pulls/4/reviews':
                    return [{'id': 2, 'state': 'CHANGES_REQUESTED', 'body': 'Fix issue 3 only',
                             'commit_id': 'other-head', 'user': {'login': 'jacktian8888'},
                             'submitted_at': '2026-10-30T01:00:00Z'}]
                if path.endswith('/comments'):
                    return []
                if path.startswith('commits/'):
                    return [{'name': 'test', 'status': 'completed',
                             'conclusion': 'success' if path.startswith('commits/abc/') else 'failure'}]
                raise AssertionError(path)
            def request(path):
                calls[pr['number']].append(path)
                self.assertEqual(path, f"commits/{pr['head']['sha']}/status")
                return {'total_count': 0}
            with patch.object(api, 'pages', side_effect=pages, create=True), \
                    patch.object(api, 'request', side_effect=request):
                worker.verify_pr(pr)
                return worker.snapshot(pr)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(poll, self.worker, self.api.pull)
            second = pool.submit(poll, other, other_pr)
            approved, changes = first.result(), second.result()
        self.assertEqual(approved[0], 'approved')
        self.assertEqual(approved[1], [])
        self.assertEqual(changes[0], 'changes')
        self.assertEqual(changes[1][0]['body'], 'Fix issue 3 only')
        self.assertEqual(approved[2][0]['conclusion'], 'success')
        self.assertEqual(changes[2][0]['conclusion'], 'failure')
        for number, head in ((2, 'abc'), (4, 'other-head')):
            self.assertEqual(calls[number], [f'pulls/{number}/reviews',
                f'pulls/{number}/comments', f'issues/{number}/comments',
                f'commits/{head}/check-runs?filter=latest', f'commits/{head}/status'])

    def test_another_issues_pr_is_rejected_before_review_merge_or_closure(self):
        foreign = copy.deepcopy(self.api.pull)
        foreign.update(number=4, body='<!-- codex-issue-worker:jtact2050/test#3 -->', merged=True)
        foreign['head']['ref'] = 'codex/issue-3'
        with patch.object(self.worker, 'pr', return_value=foreign):
            for action in (self.worker.request_review,
                           lambda: self.worker.merge_if_approved(foreign),
                           lambda: self.worker.finish(foreign)):
                with self.subTest(action=action), self.assertRaisesRegex(JobError, 'does not match'):
                    action()
        self.assertEqual(self.api.calls, [])

    def test_deleted_state_ignores_closed_pr_and_uses_unique_branch(self):
        self.worker.state.pop('pr')
        self.api.pull['state'] = 'closed'
        original_branch = self.worker.branch
        self.assertIsNone(self.worker.discover_pr())
        self.assertNotEqual(self.worker.branch, original_branch)
        self.assertEqual([call[0] for call in self.api.calls], ['pulls?state=open'])
        self.assertNotIn('pr', load_json(self.worker.path))
        # A crash before workspace setup must not change the chosen branch again.
        fresh_branch = self.worker.branch
        self.assertIsNone(self.worker.discover_pr())
        self.assertEqual(self.worker.branch, fresh_branch)

    def test_fresh_workspace_recovers_open_pr_on_previous_attempt_branch(self):
        self.worker.state.pop('pr')
        self.api.pull['head']['ref'] = 'codex/issue-1-retry-previous'
        pr = self.worker.discover_pr()
        self.assertEqual(pr['number'], 2)
        self.assertEqual(self.worker.branch, 'codex/issue-1-retry-previous')
        self.assertEqual(load_json(self.worker.path)['pr'], 2)
        self.assertEqual([call[0] for call in self.api.calls], ['pulls?state=open', 'pulls/2'])

    def test_saved_closed_pr_is_not_fetched_at_startup(self):
        self.api.pull['state'] = 'closed'
        with self.assertRaisesRegex(JobError, 'next dispatch will prepare a fresh attempt'):
            self.worker.discover_pr()
        self.assertEqual([call[0] for call in self.api.calls], ['pulls?state=open'])

    def test_existing_open_pr_is_resumed(self):
        self.worker.save(workspace_ready=True, rounds=3)
        self.assertEqual(self.worker.discover_pr()['number'], 2)
        self.assertEqual(self.worker.branch, 'codex/issue-1')
        self.assertEqual(self.worker.state['rounds'], 3)

    def test_open_pr_discovery_keeps_ownership_and_duplicate_checks(self):
        self.api.pull['user']['login'] = 'someone-else'
        with self.assertRaisesRegex(JobError, 'does not match'):
            self.worker.discover_pr()
        with patch.object(self.api, 'pages', return_value=[self.api.pull, dict(self.api.pull, number=3)]), \
                self.assertRaisesRegex(JobError, 'Multiple open PRs'):
            self.worker.discover_pr()

    def test_another_issues_worker_state_is_rejected_on_restart(self):
        with self.assertRaisesRegex(JobError, 'Job state belongs to another issue'):
            Worker(self.api, 3, state_dir=self.worker.state_dir)

    def test_round_limit_blocks_before_execution(self):
        self.assertEqual(self.worker.max_rounds, 8)
        self.worker.state['rounds'] = 8
        with self.assertRaisesRegex(JobError, '8/8'), patch.object(self.worker, 'run_codex') as run:
            self.worker.work(self.api.issue, 'retry', [])
        run.assert_not_called()

    def test_one_completed_cli_run_publishes_and_marks_feedback_handled(self):
        feedback = [{'key': 'review:1', 'body': 'Fix cancellation'}]
        ready = model_result()
        with patch.object(self.worker, 'integrate_base') as integrate, \
                patch.object(self.worker, 'run_codex', return_value=ready) as run, \
                patch.object(self.worker, 'publish') as publish:
            self.worker.work(self.api.issue, 'Fix review feedback', feedback)
        run.assert_called_once()
        integrate.assert_called_once()
        publish.assert_called_once_with(ready)
        self.assertEqual(self.worker.state['handled'], ['review:1'])
        self.assertNotIn('continuation', self.worker.state)

    def test_failed_missing_or_unfinished_validation_can_resume_on_next_pickup(self):
        results = [model_result(test_results=[]), model_result('in_progress', next_steps=['Fix tests'])]
        results += [model_result(test_results=[{'command': 'pnpm test', 'exit_code': code, 'summary': 'Not passed'}])
                    for code in (None, 1)]
        for result in results:
            with self.subTest(result=result):
                self.worker.state['rounds'] = 0
                with patch.object(self.worker, 'integrate_base'), \
                        patch.object(self.worker, 'run_codex', return_value=result) as run, \
                        patch.object(self.worker, 'publish') as publish, self.assertRaisesRegex(JobError, 'before the issue'):
                    self.worker.work(self.api.issue, 'Implement', [])
                run.assert_called_once()
                publish.assert_not_called()
                self.assertNotIn('manual_retry_required', self.worker.state)
                self.assertEqual(self.worker.state['last_result'], result)
                with patch.object(self.worker, 'integrate_base'), \
                        patch.object(self.worker, 'run_codex', return_value=model_result()) as retry, \
                        patch.object(self.worker, 'publish') as publish:
                    self.worker.work(self.api.issue, 'Resume saved work', [])
                retry.assert_called_once()
                publish.assert_called_once()
                self.assertEqual(self.worker.state['rounds'], 2)

    def test_external_blocker_is_preserved_without_manual_retry_gate(self):
        blocked = model_result('blocked', blocker={'reason': 'Unavailable external service',
            'owner': 'Service operator', 'unblock_condition': 'Restore service', 'evidence': 'Endpoint returned 503'})
        with patch.object(self.worker, 'integrate_base'), \
                patch.object(self.worker, 'run_codex', return_value=blocked) as run, \
                patch.object(self.worker, 'publish') as publish, self.assertRaisesRegex(JobError, 'External blocker'):
            self.worker.work(self.api.issue, 'Implement', [])
        run.assert_called_once()
        publish.assert_not_called()
        state = load_json(self.worker.path)
        self.assertNotIn('manual_retry_required', state)
        self.assertEqual(state['phase'], 'blocked')
        self.assertEqual(state['last_result']['blocker'], blocked['blocker'])

    def test_failed_conflict_validation_does_not_publish(self):
        self.api.pull.update(mergeable=False, mergeable_state='dirty')
        failed = model_result(test_results=[{'command': 'test', 'exit_code': 1, 'summary': 'Failed'}])
        with patch.object(self.worker, 'integrate_base'), \
                patch.object(self.worker, 'run_codex', return_value=failed) as run, \
                patch.object(self.worker, 'publish') as publish, \
                self.assertRaisesRegex(JobError, 'before the issue'):
            self.worker.work(self.api.issue, 'Resolve merge conflicts', [])
        run.assert_called_once()
        publish.assert_not_called()
        self.assertNotIn('manual_retry_required', self.worker.state)

    def test_real_git_only_reported_conflicts_integrate_main(self):
        # Main advances before and during coding. Only a reported PR conflict
        # triggers integration; clean advances never add a validation round.
        base = Path(self.temp.name)
        remote, upstream = base / 'remote.git', base / 'upstream'
        self.worker.root = base / 'workspace'
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
        def git_at(root, *args):
            return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                                   '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                                   '-C', str(root), *args], env=env, capture_output=True, text=True)
        def checked(root, *args):
            result = git_at(root, *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()
        checked(base, 'init', '--bare', str(remote))
        checked(base, 'init', '-b', 'main', str(upstream))
        (upstream / 'shared.txt').write_text('original\n')
        checked(upstream, 'add', '.')
        checked(upstream, 'commit', '-m', 'Initial base')
        checked(upstream, 'remote', 'add', 'origin', str(remote))
        checked(upstream, 'push', 'origin', 'main')
        checked(base, 'clone', '-b', 'main', str(remote), str(self.worker.root))
        checked(self.worker.root, 'checkout', '-b', self.worker.branch)
        (upstream / 'before-round.txt').write_text('latest main before coding\n')
        checked(upstream, 'add', '.')
        checked(upstream, 'commit', '-m', 'Advance main before first round')
        checked(upstream, 'push', 'origin', 'main')
        pushes = []
        def local_git(*args, auth=False, allow_failure=False):
            if args[0] == 'push':
                pushes.append(self.worker.state['rounds'])
            result = git_at(self.worker.root, *args)
            if result.returncode and not allow_failure:
                raise JobError(result.stderr)
            return result
        def coding(issue, reason, feedback):
            shared = self.worker.root / 'shared.txt'
            if self.worker.state['rounds'] == 1:
                self.assertFalse((self.worker.root / 'before-round.txt').exists())
                self.assertFalse((self.worker.root / '.git/MERGE_HEAD').exists())
                shared.write_text('issue behavior\n')
                (upstream / 'shared.txt').write_text('upstream behavior\n')
            else:
                self.assertEqual(self.worker.state['rounds'], 2)
                self.assertTrue((self.worker.root / '.git/MERGE_HEAD').exists())
                self.assertIn('<<<<<<<', shared.read_text())
                self.assertTrue((self.worker.root / 'before-round.txt').exists())
                shared.write_text('issue behavior\nupstream behavior\n')
                (upstream / 'new.txt').write_text('another clean upstream change\n')
            checked(upstream, 'add', '.')
            checked(upstream, 'commit', '-m', 'Advance main during coding')
            checked(upstream, 'push', 'origin', 'main')
            return model_result()
        with patch.object(self.worker, 'git', side_effect=local_git), \
                patch.object(self.worker, 'run_codex', side_effect=coding) as run, \
                patch.object(self.worker, 'request_review') as review:
            self.worker.work(self.api.issue, 'Implement', [])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(pushes, [1])
            # GitHub computes conflict status after the validated head is pushed.
            self.api.pull.update(mergeable=False, mergeable_state='dirty')
            self.api.pull['head']['sha'] = self.worker.state['last_head']
            self.worker.work(self.api.issue, 'Resolve merge conflicts', [])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(pushes, [1, 2])
        self.assertEqual(review.call_count, 2)
        self.assertFalse((self.worker.root / '.git/MERGE_HEAD').exists())
        self.assertFalse((self.worker.root / 'new.txt').exists())
        self.assertEqual(checked(self.worker.root, 'status', '--porcelain'), '')
        self.assertEqual(checked(remote, 'rev-parse', self.worker.branch), self.worker.state['last_head'])
        checked(self.worker.root, 'fetch', 'origin', 'main')
        self.assertEqual(git_at(self.worker.root, 'merge-base', '--is-ancestor', 'origin/main', 'HEAD').returncode, 1)
        self.api.pull.update(mergeable=True, mergeable_state='clean')
        self.api.pull['head']['sha'] = self.worker.state['last_head']
        with patch.object(self.worker, 'git', side_effect=local_git), \
                patch.object(self.worker, 'snapshot', return_value=('approved', [], [], {})):
            self.assertFalse(self.worker.integrate_base())
            self.assertTrue(self.worker.merge_if_approved(self.api.pull))
        self.assertEqual(self.worker.state['phase'], 'done')

    def test_publish_failure_defers_to_next_pickup_without_losing_result(self):
        with patch.object(self.worker, 'integrate_base'), \
                patch.object(self.worker, 'run_codex', return_value=model_result()) as run, \
                patch.object(self.worker, 'publish', side_effect=JobError('GitHub unavailable')), \
                self.assertRaisesRegex(JobError, 'Post-CLI validation/publishing stopped'):
            self.worker.work(self.api.issue, 'Implement', [])
        run.assert_called_once()
        self.assertNotIn('manual_retry_required', self.worker.state)
        self.assertEqual(self.worker.state['last_result']['status'], 'completed')
        with patch.object(self.worker, 'integrate_base'), \
                patch.object(self.worker, 'run_codex', return_value=model_result()) as retry, \
                patch.object(self.worker, 'publish'):
            self.worker.work(self.api.issue, 'Automatic retry', [])
        retry.assert_called_once()

    def test_interrupted_worker_recovers_last_result_and_original_review_feedback(self):
        feedback = [{'key': 'ci:sha', 'checks': ['test failed']}]
        self.worker.max_rounds = 1
        unfinished = model_result('in_progress', next_steps=['Fix test failure'])
        with patch.object(self.worker, 'integrate_base'), \
                patch.object(self.worker, 'run_codex', return_value=unfinished), self.assertRaises(JobError):
            self.worker.work(self.api.issue, 'Fix CI', feedback)
        resumed = Worker(self.api, 1, state_dir=self.worker.state_dir, max_rounds=2)
        with patch.object(resumed, 'integrate_base'), \
                patch.object(resumed, 'run_codex', return_value=model_result()) as run, \
                patch.object(resumed, 'publish'):
            resumed.work(self.api.issue, 'Implement', [])
        self.assertEqual(run.call_args.args[2], feedback)
        self.assertIn('Continue', run.call_args.args[1])
        self.assertEqual(resumed.state['handled'], ['ci:sha'])

    def test_old_worker_result_is_included_in_first_upgraded_call(self):
        previous = {'status': 'blocked', 'summary': 'Incomplete source correction', 'tests': []}
        self.worker.state['rounds'] = 4
        save_json(self.worker.state_dir / 'result-4.json', previous)
        def coding(*args):
            self.assertEqual(self.worker.state['last_result'], previous)
            return model_result()
        with patch.object(self.worker, 'integrate_base'), patch.object(self.worker, 'run_codex', side_effect=coding), \
                patch.object(self.worker, 'publish'):
            self.worker.work(self.api.issue, 'Resume', [])

    def test_first_model_error_defers_retry_without_losing_prior_progress(self):
        previous = model_result('in_progress', next_steps=['Finish tests'])
        self.worker.save(last_result=previous)
        with patch.object(self.worker, 'integrate_base'), \
                patch.object(self.worker, 'run_codex', side_effect=ModelRunError('invalid JSON')) as run, \
                patch.object(self.worker, 'publish') as publish, self.assertRaisesRegex(JobError, 'CLI run stopped'):
            self.worker.work(self.api.issue, 'Continue', [])
        run.assert_called_once()
        publish.assert_not_called()
        self.assertEqual(self.worker.state['last_result'], previous)
        self.assertNotIn('manual_retry_required', self.worker.state)

    def test_worker_migrates_legacy_manual_retry_state_without_resetting_session(self):
        self.worker.save(manual_retry_required=True, blocked_issue_digest=issue_digest(self.api.issue),
                         blocker_pending={}, rounds=2, phase='blocked',
                         codex_session={'id': 'saved-session'})
        resumed = Worker(self.api, 1, state_dir=self.worker.state_dir)
        state = load_json(resumed.path)
        for key in ('manual_retry_required', 'blocked_issue_digest', 'blocker_pending'):
            self.assertNotIn(key, state)
        self.assertEqual(state['rounds'], 2)
        self.assertEqual(state['codex_session'], {'id': 'saved-session'})
        with patch.object(resumed, 'integrate_base'), \
                patch.object(resumed, 'run_codex', return_value=model_result()) as run, \
                patch.object(resumed, 'publish'):
            resumed.work(self.api.issue, 'Resume saved work', [])
        run.assert_called_once()

    def test_result_validator_rejects_missing_blocker_evidence_and_boolean_exit_codes(self):
        for result in (model_result('blocked'), model_result('blocked', blocker={'reason': 'missing'}),
                       model_result(test_results=[{'command': 'test', 'exit_code': False, 'summary': 'pass'}])):
            with self.assertRaises(ModelRunError):
                validate_result(result)

    def test_model_gets_writable_caches_but_no_github_environment(self):
        import json
        result = model_result()
        with patch('worker.subprocess.Popen') as popen:
            def communicate(*args, **kwargs):
                popen.call_args.kwargs['stdout'].write(event_stream(json.dumps(result)).encode())
                return None, None
            popen.return_value.communicate.side_effect = communicate
            popen.return_value.returncode = 0
            popen.return_value.pid = 12345
            self.worker.run_codex(self.api.issue, 'test', [])
        command = popen.call_args.args[0]
        self.assertIn('XDG_DATA_HOME=/tmp/codex-data', command)
        self.assertIn('npm_config_cache=/tmp/codex-npm', command)
        self.assertIn('PNPM_CONFIG_STORE_DIR=/tmp/codex-pnpm-store', command)
        self.assertIn('CODEX_WORKSPACE_GROUP_WRITE=1', command)
        self.assertNotIn('GH_TOKEN', popen.call_args.kwargs['env'])
        self.assertNotIn('GITHUB_TOKEN', popen.call_args.kwargs['env'])
        self.assertFalse(any(arg.startswith('GH_CONFIG_DIR=') for arg in command))

    def test_dirty_retry_is_checkpointed_before_new_base_merge(self):
        self.api.pull.update(mergeable=False, mergeable_state='dirty')
        calls = []
        def git(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=1 if args[0] == 'merge-base' else 0,
                                   stdout='newbase' if args[0] == 'rev-parse' else ' M fix.py' if args[0] == 'status' else '')
        with patch.object(self.worker, 'git', side_effect=git):
            self.assertTrue(self.worker.integrate_base())
        operations = [c[0] for c in calls]
        self.assertLess(operations.index('commit'), operations.index('merge'))
        self.assertNotIn('reset', operations)
        self.assertEqual(self.worker.state['base_sha'], 'newbase')

    def test_preserved_merge_target_is_not_mistaken_for_new_base(self):
        self.worker.root = Path(self.temp.name) / 'repo'
        (self.worker.root / '.git').mkdir(parents=True)
        (self.worker.root / '.git/MERGE_HEAD').write_text('oldbase')
        def git(*args, **kwargs):
            return SimpleNamespace(returncode=0, stdout='oldbase' if args == ('rev-parse', 'MERGE_HEAD') else 'newbase')
        with patch.object(self.worker, 'git', side_effect=git):
            self.assertTrue(self.worker.integrate_base())
        self.assertEqual(self.worker.state['base_sha'], 'oldbase')

    def test_publish_does_not_integrate_base_using_old_pr_conflict_status(self):
        self.api.pull.update(mergeable=False, mergeable_state='dirty')
        self.worker.save(pending_issue_digest=issue_digest(self.api.issue))
        with patch.object(self.worker, 'git', return_value=SimpleNamespace(returncode=0, stdout='head')) as git, \
                patch.object(self.worker, 'integrate_base') as integrate, patch.object(self.worker, 'request_review') as review:
            self.worker.publish({'summary': 'Ready', 'tests': []})
        self.assertTrue(any(c.args[0] == 'push' for c in git.call_args_list))
        integrate.assert_not_called()
        review.assert_called_once()

    def test_conflicts_start_repair_before_waiting_for_ci_or_approval(self):
        self.api.pull.update(mergeable=False, mergeable_state='dirty')
        self.worker.save(phase='waiting_review', review_started=1)
        with patch.object(self.worker, 'discover_pr', return_value=self.api.pull), \
                patch.object(self.worker, 'setup_repo'), \
                patch.object(self.worker, 'git', return_value=SimpleNamespace(stdout='abc')), \
                patch.object(self.worker, 'needs_base_integration', return_value=False), \
                patch.object(self.worker, 'snapshot', return_value=('waiting', [], [], {})), \
                patch.object(self.worker, 'work', side_effect=JobError('repair started')) as work:
            with self.assertRaisesRegex(JobError, 'repair started'):
                self.worker.run()
        self.assertIn('merge conflicts', work.call_args.args[1])

    def test_review_poll_leaves_clean_unknown_and_behind_prs_unchanged(self):
        import time
        for mergeable, state in ((True, 'clean'), (None, 'unknown'), (True, 'behind'), (True, 'blocked')):
            with self.subTest(mergeable=mergeable, state=state):
                self.api.pull.update(mergeable=mergeable, mergeable_state=state)
                self.worker.save(phase='waiting_review', review_started=time.time())
                with patch.object(self.worker, 'discover_pr', return_value=self.api.pull), \
                        patch.object(self.worker, 'setup_repo'), \
                        patch.object(self.worker, 'git', return_value=SimpleNamespace(stdout='abc')), \
                        patch.object(self.worker, 'fetch_base') as fetch, \
                        patch.object(self.worker, 'snapshot', return_value=('waiting', [], [], {})), \
                        patch.object(self.worker, 'work') as work, \
                        patch('worker.time.sleep', side_effect=JobError('waiting')):
                    with self.assertRaisesRegex(JobError, 'waiting'):
                        self.worker.run()
                work.assert_not_called()
                fetch.assert_not_called()

    def test_conflicts_review_feedback_and_ci_are_repaired_together(self):
        self.api.pull.update(mergeable=False, mergeable_state='dirty')
        self.worker.save(phase='waiting_review', review_started=1)
        feedback = [{'key': 'review:1', 'body': 'Fix cancellation'}]
        checks = [{'name': 'test', 'status': 'completed', 'conclusion': 'failure'}]
        with patch.object(self.worker, 'discover_pr', return_value=self.api.pull), \
                patch.object(self.worker, 'setup_repo'), \
                patch.object(self.worker, 'git', return_value=SimpleNamespace(stdout='abc')), \
                patch.object(self.worker, 'needs_base_integration', return_value=True), \
                patch.object(self.worker, 'snapshot', return_value=('changes', feedback, checks, {})), \
                patch.object(self.worker, 'work', side_effect=JobError('repair started')) as work:
            with self.assertRaisesRegex(JobError, 'repair started'):
                self.worker.run()
        work.assert_called_once()
        reason, actual_feedback = work.call_args.args[1:]
        for text in ('merge conflicts', 'reviewer feedback', 'failing CI'):
            self.assertIn(text, reason)
        self.assertEqual(actual_feedback[0], feedback[0])
        self.assertEqual(actual_feedback[1]['key'], 'ci:abc')
        self.assertEqual(self.worker.state['handled'], [])

    def test_only_reported_conflicts_or_pending_merges_need_integration(self):
        for mergeable, state, needed in ((True, 'clean', False), (None, 'unknown', False),
                                         (True, 'behind', False), (True, 'blocked', False),
                                         (False, 'unknown', True), (None, 'dirty', True)):
            with self.subTest(mergeable=mergeable, state=state):
                self.api.pull.update(mergeable=mergeable, mergeable_state=state)
                with patch.object(self.worker, 'git') as git:
                    self.assertIs(self.worker.needs_base_integration(), needed)
                    if not needed:
                        self.assertFalse(self.worker.integrate_base())
                git.assert_not_called()
        self.worker.root = Path(self.temp.name) / 'repo'
        (self.worker.root / '.git').mkdir(parents=True)
        (self.worker.root / '.git/MERGE_HEAD').write_text('preserved')
        self.assertTrue(self.worker.needs_base_integration({**self.api.pull, 'mergeable': True, 'mergeable_state': 'clean'}))

    def test_no_pr_does_not_integrate_new_base_before_coding(self):
        self.worker.state.pop('pr')
        with patch.object(self.worker, 'git') as git:
            self.assertFalse(self.worker.integrate_base())
        git.assert_not_called()

    def test_review_poll_conflict_respects_budget_before_merging(self):
        self.api.pull.update(mergeable=False, mergeable_state='dirty')
        self.worker.save(phase='waiting_review', review_started=1, rounds=self.worker.max_rounds)
        with patch.object(self.worker, 'discover_pr', return_value=self.api.pull), \
                patch.object(self.worker, 'setup_repo'), \
                patch.object(self.worker, 'git', return_value=SimpleNamespace(stdout='abc')), \
                patch.object(self.worker, 'needs_base_integration', return_value=True), \
                patch.object(self.worker, 'snapshot', return_value=('approved', [], [], {})), \
                patch.object(self.worker, 'integrate_base') as integrate, \
                patch.object(self.worker, 'run_codex') as run:
            with self.assertRaisesRegex(JobError, 'Maximum coding/revision rounds'):
                self.worker.run()
        integrate.assert_not_called()
        run.assert_not_called()

    def test_current_base_waits_without_starting_another_round(self):
        import time
        self.worker.save(phase='waiting_review', review_started=time.time())
        with patch.object(self.worker, 'discover_pr', return_value=self.api.pull), \
                patch.object(self.worker, 'setup_repo'), \
                patch.object(self.worker, 'git', return_value=SimpleNamespace(stdout='abc')), \
                patch.object(self.worker, 'needs_base_integration', return_value=False) as check, \
                patch.object(self.worker, 'snapshot', return_value=('waiting', [], [], {})), \
                patch.object(self.worker, 'work') as work, \
                patch('worker.time.sleep', side_effect=JobError('waiting')):
            with self.assertRaisesRegex(JobError, 'waiting'):
                self.worker.run()
        check.assert_called_once()
        work.assert_not_called()

    def test_approved_clean_pr_merges_without_fetching_or_integrating_main(self):
        self.api.pull.update(mergeable=True, mergeable_state='clean')
        self.worker.save(phase='waiting_review', review_started=1)
        with patch.object(self.worker, 'discover_pr', return_value=self.api.pull), \
                patch.object(self.worker, 'setup_repo'), \
                patch.object(self.worker, 'git', return_value=SimpleNamespace(stdout='abc')), \
                patch.object(self.worker, 'fetch_base') as fetch, \
                patch.object(self.worker, 'snapshot', return_value=('approved', [], [], {})), \
                patch.object(self.worker, 'work') as work:
            self.worker.run()
        fetch.assert_not_called()
        work.assert_not_called()
        self.assertEqual(self.worker.state['phase'], 'done')

    def test_conflict_appearing_at_final_merge_check_waits_for_next_poll(self):
        self.api.pull.update(mergeable=True, mergeable_state='clean')
        self.api.clean = 'DIRTY'
        with patch.object(self.worker, 'snapshot', return_value=('approved', [], [], {})):
            self.assertFalse(self.worker.merge_if_approved(self.api.pull))
        self.assertFalse(any(c[0].endswith('/merge') for c in self.api.calls))

    def test_full_create_review_change_approve_merge_flow(self):
        self.worker.state.pop('pr')
        created, pushes, reviews = [], [], []
        original_request = self.api.request
        def request(path, payload=None, method=None):
            if path == 'pulls':
                self.api.pull.update(draft=payload['draft'], body=payload['body'])
                created.append(payload)
                return self.api.pull.copy()
            if path == 'pulls/2/requested_reviewers':
                reviews.append(self.api.pull['head']['sha'])
            return original_request(path, payload, method)
        def git(*args, **kwargs):
            code, stdout = 0, ''
            if args[0] == 'diff':
                code = 1
            if args[0] == 'rev-parse':
                stdout = 'head' + str(self.worker.state['rounds'])
            if args[0] == 'push':
                pushes.append(args)
                self.api.pull['head']['sha'] = 'head' + str(self.worker.state['rounds'])
            return SimpleNamespace(returncode=code, stdout=stdout)
        def snapshot(pr):
            if self.worker.state['rounds'] == 1:
                return 'changes', [{'key': 'review:10', 'body': 'Fix the race'}], [], {}
            return 'approved', [], [], {}
        result = model_result()
        with patch.object(self.worker, 'discover_pr', return_value=None), patch.object(self.worker, 'setup_repo'), \
                patch.object(self.worker, 'needs_base_integration', return_value=False), \
                patch.object(self.worker, 'integrate_base', return_value=False), patch.object(self.worker, 'git', side_effect=git), \
                patch.object(self.worker, 'run_codex', return_value=result), patch.object(self.worker, 'snapshot', side_effect=snapshot), \
                patch.object(self.api, 'request', side_effect=request):
            self.worker.run()
        self.assertEqual(len(created), 1)
        self.assertIn('Closes #1', created[0]['body'])
        self.assertEqual(len(pushes), 2)
        self.assertEqual(reviews, ['head1', 'head2'])
        self.assertEqual(self.worker.state['handled'], ['review:10'])
        self.assertEqual(self.worker.state['phase'], 'done')
        self.assertEqual(self.api.issue['state_reason'], 'completed')


if __name__ == '__main__':
    unittest.main()
