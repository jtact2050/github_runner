"""Pure decisions for queue ordering and the exact-commit review gate."""
import re


def ordered_milestones(milestones, order=None):
    """Milestones are queue priorities, never barriers between independent work."""
    available = {m['number']: m for m in milestones if m.get('state', 'open') == 'open'}
    sequence = order if order is not None else sorted(available)
    return [available[n] for n in dict.fromkeys(sequence) if n in available]


def queue_issues(issues, milestone):
    members = [i for i in issues if 'pull_request' not in i and i.get('milestone')
               and i['milestone']['number'] == milestone]
    return sorted(members, key=lambda i: i['number'])


def review_signal(reviews, inline_comments, comments, reviewer, head, handled):
    """Never accept another reviewer, an old approval, or an approval superseded by feedback."""
    mine = sorted((r for r in reviews if r.get('user', {}).get('login', '').lower() == reviewer.lower()
                   and r.get('state') != 'PENDING'), key=lambda r: r['id'])
    latest = mine[-1] if mine else None
    feedback = []
    approval_time = latest.get('submitted_at', '') if latest and latest['state'] == 'APPROVED' and latest.get('commit_id') == head else ''
    for review in mine:
        key = f"review:{review['id']}"
        if (review['state'] in ('CHANGES_REQUESTED', 'COMMENTED') and ((review.get('body') or '').strip() or review['state'] == 'CHANGES_REQUESTED')
                and key not in handled and review.get('submitted_at', '') > approval_time):
            feedback.append({'key': key, 'body': review.get('body') or 'Changes requested; inspect inline feedback. If no actionable feedback is available, report blocked.'})
    for kind, entries in (('inline', inline_comments), ('comment', comments)):
        for comment in entries:
            key = f"{kind}:{comment['id']}:{comment.get('updated_at', '')}"
            if (comment.get('user', {}).get('login', '').lower() == reviewer.lower()
                    and key not in handled and (comment.get('body') or '').strip()
                    and comment.get('updated_at', comment.get('created_at', '')) > approval_time):
                feedback.append({'key': key, 'body': comment['body'],
                                 'path': comment.get('path'), 'line': comment.get('line')})
    if feedback:
        return 'changes', feedback
    if latest and latest['state'] == 'APPROVED' and latest.get('commit_id') == head:
        return 'approved', []
    return 'waiting', []


def checks_pass(checks, status):
    # GitHub already supplies filter=latest. Never collapse distinct workflows
    # just because they used the same job name and GitHub App.
    if any(c['status'] != 'completed' or c.get('conclusion') not in ('success', 'neutral', 'skipped') for c in checks):
        return False
    return not status.get('total_count') or status['state'] == 'success'


def current_review_checks(checks, runs, number, head):
    """Ignore only a cancelled Codex review replaced by a successful same-PR run.

    Names alone are insufficient: use the actual workflow, check-suite, PR and
    commit identities. Failures and unrelated workflows remain merge blockers.
    """
    relevant = [r for r in runs if r.get('path') == '.github/workflows/codex-review.yml'
                and r.get('event') == 'pull_request_target' and r.get('head_sha') == head
                and any(p.get('number') == number and p.get('head', {}).get('sha') == head
                        for p in r.get('pull_requests', []))]
    successful = [r['id'] for r in relevant if r.get('status') == 'completed' and r.get('conclusion') == 'success']
    if not successful:
        return checks
    superseded = {r['check_suite_id'] for r in relevant if r.get('conclusion') == 'cancelled'
                  and r['id'] < max(successful)}
    return [c for c in checks if not (c.get('name') == 'review' and c.get('conclusion') == 'cancelled'
            and c.get('app', {}).get('slug') == 'github-actions'
            and c.get('check_suite', {}).get('id') in superseded)]


def failed_checks(checks, statuses):
    result = [{'name': c['name'], 'url': c.get('html_url'), 'conclusion': c.get('conclusion')}
              for c in checks if c['status'] == 'completed' and c.get('conclusion') not in ('success', 'neutral', 'skipped')]
    result += [{'name': c['context'], 'url': c.get('target_url'), 'conclusion': c['state']}
               for c in statuses if c['state'] in ('failure', 'error')]
    return result


def linked_issue(body, number, repo=None):
    reference = '#' + str(number) + r'\b'
    if repo:
        reference = '(?:' + reference + '|' + re.escape(repo) + '#' + str(number) + r'\b|https://github\.com/' + re.escape(repo) + '/issues/' + str(number) + r'\b)'
    return bool(re.search(r'\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?):?\s+' + reference, body or '', re.I))


def safe_summary(text):
    # Only the controller's explicit Closes #N line may auto-close an issue.
    return re.sub(r'\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?):?\s+(?=(?:#|https://github.com/|[\w.-]+/[\w.-]+#))',
                  'Addresses ', text, flags=re.I)
