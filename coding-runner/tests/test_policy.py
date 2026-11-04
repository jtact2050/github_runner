import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policy import checks_pass, current_review_checks, failed_checks, linked_issue, queue_issues, review_signal, safe_summary, ordered_milestones


def review(number=1, state='APPROVED', head='abc', user='jacktian8888', body=''):
    return dict(id=number, state=state, commit_id=head, user={'login': user}, body=body,
                submitted_at=f'2026-10-29T00:00:{number:02d}Z')


class PolicyTests(unittest.TestCase):
    def signal(self, reviews, inline=None, comments=None, handled=None):
        return review_signal(reviews, inline or [], comments or [], 'jacktian8888', 'abc', handled or [])[0]

    def test_named_reviewer_and_current_commit_required(self):
        self.assertEqual(self.signal([review()]), 'approved')
        self.assertEqual(self.signal([review(user='jtact2050')]), 'waiting')
        self.assertEqual(self.signal([review(head='old')]), 'waiting')

    def test_dismissed_or_later_comment_invalidates_approval(self):
        self.assertEqual(self.signal([review(state='DISMISSED')]), 'waiting')
        self.assertEqual(self.signal([review(), review(2, 'COMMENTED', body='fix this')]), 'changes')
        self.assertEqual(self.signal([review(), review(2, 'CHANGES_REQUESTED')]), 'changes')

    def test_new_approval_supersedes_old_feedback(self):
        self.assertEqual(self.signal([review(1, 'CHANGES_REQUESTED', body='bug'), review(2)]), 'approved')

    def test_inline_and_general_comments_after_approval_need_revision(self):
        comment = {'id': 8, 'user': {'login': 'jacktian8888'}, 'body': 'race here',
                   'updated_at': '2026-10-29T00:00:03Z'}
        self.assertEqual(self.signal([review()], inline=[comment]), 'changes')
        self.assertEqual(self.signal([review()], comments=[comment]), 'changes')
        # Already handled feedback does not run the same revision again.
        self.assertEqual(self.signal([], comments=[comment], handled=['comment:8:2026-10-29T00:00:03Z']), 'waiting')
        comment['updated_at'] = '2026-10-29T00:00:00Z'
        self.assertEqual(self.signal([review()], comments=[comment]), 'approved')

    def test_checks_and_statuses(self):
        check = dict(id=1, name='test', status='in_progress', conclusion=None)
        self.assertFalse(checks_pass([check], {'total_count': 0}))
        check.update(status='completed', conclusion='success')
        self.assertTrue(checks_pass([check], {'total_count': 0}))
        self.assertFalse(checks_pass([check], {'total_count': 1, 'state': 'pending'}))
        check['conclusion'] = 'failure'
        self.assertFalse(checks_pass([check], {}))
        self.assertEqual(len(failed_checks([check], [])), 1)

    def test_identical_job_names_do_not_hide_a_failed_workflow(self):
        old = dict(id=1, name='review', status='completed', conclusion='cancelled', app={'id': 1})
        new = dict(old, id=2, conclusion='success')
        self.assertFalse(checks_pass([old, new], {}))
        self.assertEqual(len(failed_checks([old, new], [])), 1)
        new['app'] = {'id': 2}
        self.assertFalse(checks_pass([old, new], {}))

    def test_only_verified_superseded_cancelled_review_is_ignored(self):
        checks = [dict(name='review', conclusion='cancelled', app={'slug': 'github-actions'}, check_suite={'id': 10}),
                  dict(name='review', conclusion='failure', app={'slug': 'github-actions'}, check_suite={'id': 11})]
        old = dict(id=1, check_suite_id=10, path='.github/workflows/codex-review.yml', event='pull_request_target',
                   head_sha='abc', pull_requests=[{'number': 7, 'head': {'sha': 'abc'}}], status='completed', conclusion='cancelled')
        newer = dict(old, id=2, check_suite_id=12, conclusion='success')
        self.assertEqual(current_review_checks(checks, [old, newer], 7, 'abc'), checks[1:])
        self.assertEqual(current_review_checks(checks, [old, newer], 8, 'abc'), checks)
        self.assertEqual(current_review_checks(checks, [old, newer], 7, 'different'), checks)
        self.assertEqual(current_review_checks(checks, [old, dict(newer, conclusion='failure')], 7, 'abc'), checks)
        self.assertEqual(current_review_checks(checks, [old, dict(newer, id=0)], 7, 'abc'), checks)
        self.assertEqual(current_review_checks(checks, [old, dict(newer, path='.github/workflows/other.yml')], 7, 'abc'), checks)

    def test_all_open_milestones_in_default_or_explicit_priority_order(self):
        milestones = [{'number': 3}, {'number': 1}, {'number': 2}, {'number': 4, 'state': 'closed'}]
        self.assertEqual([m['number'] for m in ordered_milestones(milestones)], [1, 2, 3])
        self.assertEqual([m['number'] for m in ordered_milestones(milestones, [3, 2, 3, 4, 99])], [3, 2])

    def test_issue_number_orders_pickup_without_title_heuristics(self):
        issues = [dict(number=4, title='[P1] Foundations', milestone={'number': 2}),
                  dict(number=13, title='[P1-I01] Implementation', milestone={'number': 2})]
        self.assertEqual([i['number'] for i in queue_issues(issues, 2)], [4, 13])
        self.assertEqual([i['number'] for i in queue_issues(issues[:1], 2)], [4])

    def test_generated_summary_cannot_close_other_issues(self):
        self.assertEqual(safe_summary('Fixes #99 and resolves org/repo#20'), 'Addresses #99 and Addresses org/repo#20')

    def test_duplicate_detection_supports_qualified_references(self):
        for body in ['Fixes #13', 'Closes: jtact2050/test#13', 'Resolves https://github.com/jtact2050/test/issues/13']:
            self.assertTrue(linked_issue(body, 13, 'jtact2050/test'))
        self.assertFalse(linked_issue('Fixes #130', 13, 'jtact2050/test'))
        self.assertFalse(linked_issue('Closes other/test#13', 13, 'jtact2050/test'))


if __name__ == '__main__':
    unittest.main()
