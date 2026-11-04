#!/usr/bin/env python3
"""Host dispatcher: poll milestones and launch resumable, isolated coding jobs."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

from common import GitHub, JobError, load_json, remove_manual_retry_state, save_json, stamp
from diagnostics import Diagnostics
from policy import linked_issue, queue_issues, ordered_milestones

ROOT = Path(__file__).resolve().parent.parent
IMAGE = 'codex-issue-worker:local'
ACTIVE = {'launching', 'running'}


def unsatisfied_dependencies(blockers):
    # Legacy completed issues can have no state_reason. An abandoned/duplicate
    # prerequisite does not supply its output; fix the link instead of running.
    return [issue for issue in blockers if issue.get('state') != 'closed'
            or issue.get('state_reason') not in (None, 'completed')]


def dependency_detail(number, blockers):
    return {'number': number, 'blocked_by': [
        {key: blocker.get(key) for key in ('number', 'title', 'state', 'state_reason', 'html_url')}
        for blocker in blockers]}


def docker(*args, check=True):
    # Terminal Ctrl+C belongs to the dispatcher, not an in-flight Docker command.
    result = subprocess.run(['docker', *map(str, args)], capture_output=True, text=True,
                            timeout=120, start_new_session=True)
    if check and result.returncode:
        raise JobError(f'Docker {args[0]} failed: {result.stderr.strip()[:1000]}')
    return result


def inspect_container(name):
    result = docker('container', 'inspect', name, check=False)
    if result.returncode:
        if 'No such container' in result.stderr or 'No such object' in result.stderr:
            return None
        raise JobError('Cannot inspect Docker containers; no new jobs launched')
    return json.loads(result.stdout)[0]


def default_repo():
    # Read only the one non-secret setting; never source shell code from .env.
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith('GITHUB_URL='):
                return line.partition('=')[2].strip().strip('\"\'').removeprefix('https://github.com/').rstrip('/').removesuffix('.git')
    return 'jtact2050/oaxontax'


class Dispatcher:
    def __init__(self, api, args):
        self.api, self.args = api, args
        self.key = hashlib.sha256(api.repo.lower().encode()).hexdigest()[:12]
        self.directory = ROOT / '.coding-state' / self.key
        self.path = self.directory / 'dispatcher.json'
        self.state = load_json(self.path, {'repo': api.repo, 'jobs': {}})
        if self.state['repo'].lower() != api.repo.lower():
            raise JobError('Dispatcher state belongs to another repository')
        # Persist only in-flight ownership, never a backlog or failed-job blacklist.
        self.jobs = {n: j for n, j in self.state['jobs'].items() if j['status'] in ACTIVE}
        self.state['jobs'] = self.jobs
        self.last_report = None

    def save(self):
        self.state['updated_at'] = stamp()
        save_json(self.path, dict(self.state, jobs={n: j for n, j in self.jobs.items()
                                                   if j['status'] in ACTIVE}))

    def log(self, text):
        print(f'[{stamp()}] {text}', flush=True)

    def name(self, number):
        return f'codex-code-{self.key}-{number}'

    def job_dir(self, number):
        return self.directory / 'jobs' / str(number)

    def diagnostics(self, number):
        return Diagnostics(self.job_dir(number), self.api.repo, number, token=getattr(self.api, 'token', None))

    def workspace_dir(self, number):
        return self.job_dir(number) / 'workspace'

    def prepare_workspace(self, number):
        directory = self.workspace_dir(number)
        path = self.job_dir(number) / 'worker.json'
        worker = load_json(path, {})
        if directory.is_symlink():
            raise JobError('Workspace must be a real directory inside this issue folder')
        used = worker.get('rounds', 0) or worker.get('workspace_ready') or worker.get('codex_session')
        if worker.get('workspace_storage') != 'bind' and (used or worker.get('workspace_volume')):
            raise JobError('Legacy volume-based job state must be archived or migrated before starting with host folders')
        if used and not directory.is_dir():
            raise JobError('Saved workspace folder is missing; restore it before resuming this issue')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not worker:
            worker = {'repo': self.api.repo, 'issue': number, 'rounds': 0, 'handled': [],
                      'branch': f'codex/issue-{number}'}
        worker['workspace_storage'] = 'bind'
        save_json(path, worker)
        return directory

    def complete_workspace_transition(self, number):
        directory = self.job_dir(number)
        marker = directory / 'workspace-transition.json'
        transition = load_json(marker)
        if not transition:
            return
        archive = (directory / transition['archive']).resolve()
        if not archive.is_relative_to((directory / 'attempts').resolve()):
            raise JobError('Invalid workspace archive path')
        fresh = transition['next_state']
        if fresh.get('repo') != self.api.repo or fresh.get('issue') != number:
            raise JobError('Workspace transition belongs to a different issue')
        workspace = self.workspace_dir(number)
        if workspace.is_symlink():
            raise JobError('Refusing to archive a workspace symlink')
        if workspace.exists():
            if (archive / 'workspace').exists():
                raise JobError('Both old and archived workspaces exist; inspect before retrying')
            workspace.rename(archive / 'workspace')
        save_json(directory / 'worker.json', fresh)
        marker.unlink()

    def archive_attempt(self, number, attempt, fresh):
        directory = self.job_dir(number)
        archive = directory / 'attempts' / attempt
        archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Do not follow credential symlinks or copy large workspaces/earlier archives.
        shutil.copytree(directory, archive, symlinks=True,
                        ignore=shutil.ignore_patterns('attempts', 'workspace', 'legacy-workspaces',
                                                     'workspace-transition.json'))
        fresh.update(workspace_storage='bind', previous_attempt=str(archive))
        save_json(directory / 'workspace-transition.json', {
            'archive': str(archive.relative_to(directory)), 'next_state': fresh})
        self.complete_workspace_transition(number)
        return archive

    def archive_container(self, number, name, container, directory):
        """Persist each collection separately before removing any container."""
        diagnostics = self.diagnostics(number)
        log = docker('logs', name)
        snapshot = directory / 'containers' / uuid.uuid4().hex
        metadata = {
            'container': name, 'id': container['Id'], 'state': container['State'],
            'workspace_volumes': [m['Name'] for m in container.get('Mounts', [])
                                  if m.get('Type') == 'volume'],
            'workspace_folders': [m['Source'] for m in container.get('Mounts', [])
                                  if m.get('Type') == 'bind' and m.get('Destination') == '/workspace'],
            'archived_at': stamp(),
        }
        diagnostics.write(snapshot / 'stdout.log', log.stdout)
        diagnostics.write(snapshot / 'stderr.log', log.stderr)
        diagnostics.json(snapshot / 'container.json', metadata)
        # Keep compatibility with existing operational commands; history lives above.
        latest = directory / 'container.log'
        if latest.exists() and not (directory / 'legacy-container.log').exists():
            diagnostics.write(directory / 'legacy-container.log', latest.read_text())
        diagnostics.write(latest, log.stdout + log.stderr)
        diagnostics.json(directory / 'container-removal.json', metadata)
        diagnostics.event('container_archived', artifacts=str(snapshot.relative_to(self.directory)), **metadata)

    def validate_container(self, item, number):
        labels = item['Config'].get('Labels') or {}
        if labels.get('codex.repo') != self.api.repo or labels.get('codex.issue') != str(number):
            raise JobError(f'Container {self.name(number)} belongs to another job')

    def reconcile(self):
        # Recover jobs even if the dispatcher stopped immediately after docker run.
        names = docker('ps', '--all', '--filter', f'label=codex.repo={self.api.repo}', '--format', '{{.Names}}').stdout.splitlines()
        for name in names:
            container = inspect_container(name)
            if not container:
                continue
            labels = container['Config'].get('Labels') or {}
            number = labels.get('codex.issue', '')
            if not number.isdigit() or name != self.name(number):
                continue
            if number not in self.jobs:
                self.jobs[number] = {'status': 'running', 'container': name,
                                     'milestone': int(labels['codex.milestone'])}
                self.save()
        for number, job in self.jobs.items():
            if job['status'] not in ACTIVE:
                continue
            container = inspect_container(self.name(number))
            if container is None:
                # Without a container, the next live query decides whether to launch.
                job.update(status='finished', error='Container disappeared; workspace retained')
                self.diagnostics(number).event('container_missing', container=self.name(number))
            else:
                self.validate_container(container, number)
                runtime = container['State']
                if runtime['Status'] == 'created':
                    docker('start', self.name(number))
                    job['status'] = 'running'
                elif runtime.get('Running'):
                    job['status'] = 'running'
                else:
                    worker = load_json(self.job_dir(number) / 'worker.json', {})
                    success = runtime['ExitCode'] == 0 and worker.get('phase') in ('done', 'skipped')
                    job.update(status='finished', finished_at=stamp(),
                               error=None if success else worker.get('error', f"Container exit {runtime['ExitCode']}"))
                    self.log(f"Issue #{number}: {job['status']}" + (f" — {job['error']}" if job.get('error') else ''))
            self.save()
        self.cleanup_finished_containers()
        for number in list(self.jobs):
            if self.jobs[number]['status'] not in ACTIVE:
                del self.jobs[number]
        self.save()

    def cleanup_finished_containers(self):
        """Archive diagnostics and remove stopped workers before admitting more work.

        Workspace folders and job state survive for same-issue retries.
        Any cleanup failure aborts this dispatch cycle, including after a restart.
        """
        names = docker('ps', '--all', '--filter', f'label=codex.repo={self.api.repo}',
                       '--format', '{{.Names}}').stdout.splitlines()
        for name in names:
            container = inspect_container(name)
            if not container:
                continue
            labels = container['Config'].get('Labels') or {}
            number = labels.get('codex.issue', '')
            if not number.isdigit():
                continue
            canonical = self.name(number)
            archived = name.startswith(canonical + '-archived-')
            if name != canonical and not archived:
                continue
            self.validate_container(container, number)
            job = self.jobs.get(number, {})
            if not archived and job.get('status') in ACTIVE and container['State'].get('Running'):
                continue
            if container['State'].get('Running') or container['State']['Status'] not in ('exited', 'dead'):
                raise JobError(f'Refusing to remove unfinished container {name}; no new jobs launched')
            directory = (self.directory / 'removed-containers' / name) if archived else self.job_dir(number)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.archive_container(number, name, container, directory)
            docker('rm', name)  # No --force or --volumes: preserve unfinished work.
            if inspect_container(name) is not None:
                raise JobError(f'Container {name} still exists; no new jobs launched')
            self.diagnostics(number).event('container_removed', container=name, container_id=container['Id'])
            self.log(f'Removed finished coding container {name}; logs and workspace retained')

    def launch(self, number, milestone):
        if self.args.drain and self.jobs.get(str(number), {}).get('status') not in ACTIVE:
            return
        # Selection can precede launch by other API calls/container startups.
        # Recheck before creating job state or a container, including resumed jobs.
        issue = self.api.request(f'issues/{number}')
        if (issue.get('state') != 'open'
                or (issue.get('milestone') or {}).get('number') != milestone
                or (issue.get('milestone') or {}).get('state', 'open') != 'open'
                or any(label['name'].lower() == 'status:blocked'
                       for label in issue.get('labels', []))):
            self.log(f'Issue #{number}: admission changed; reconsider next poll')
            return
        blockers = unsatisfied_dependencies(self.api.pages(f'issues/{number}/dependencies/blocked_by'))
        if blockers:
            self.log(f'Issue #{number}: prerequisites not satisfied; reconsider next poll')
            return
        if self.args.drain:
            return
        name = self.name(number)
        job_dir = self.job_dir(number)
        job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs[str(number)] = {'status': 'launching', 'milestone': milestone,
                                  'container': name, 'started_at': stamp()}
        self.save()
        existing = inspect_container(name)
        if existing:
            self.validate_container(existing, number)
            if existing['State']['Status'] == 'created':
                docker('start', name)
            elif not existing['State'].get('Running'):
                raise JobError(f'Issue #{number} has a stopped container; cleanup must finish before launch')
        else:
            self.prepare_resume(number)
            workspace = self.prepare_workspace(number)
            args = ['run', '--detach', '--name', name, '--init', '--restart', 'no',
                    '--network', 'host',
                    '--label', f'codex.repo={self.api.repo}', '--label', f'codex.issue={number}',
                    '--label', f'codex.milestone={milestone}',
                    '--cpus', str(self.args.cpus), '--memory', self.args.memory, '--pids-limit', '1024',
                    '--security-opt', f'seccomp={ROOT / "codex-review/seccomp.json"}',
                    '--mount', 'type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock',
                    '--mount', f'type=bind,source={ROOT / "credentials/codex"},target=/home/codex/.codex-auth',
                    '--mount', f'type=bind,source={ROOT / "credentials/gh-coding"},target=/home/runner/.config/gh',
                    '--mount', f'type=bind,source={job_dir},target=/job-state',
                    '--mount', f'type=bind,source={workspace},target=/workspace',
                    '--tmpfs', '/workspace/.codex/tmp:rw,exec,nosuid,nodev,mode=0700,uid=1002,gid=1002,size=64m',
                    IMAGE, '--repo', self.api.repo, '--issue', str(number), '--reviewer', 'jacktian8888',
                    '--model', self.args.model, '--effort', self.args.effort,
                    '--max-rounds', str(self.args.max_rounds), '--review-timeout', str(self.args.review_timeout),
                    '--cli-timeout', str(self.args.cli_timeout), '--progress-interval', str(self.args.progress_interval),
                    '--poll-seconds', str(self.args.review_poll_seconds)]
            self.diagnostics(number).event('container_launch_requested', container=name,
                                           workspace_folder=str(workspace), milestone=milestone,
                                           model=self.args.model, effort=self.args.effort)
            try:
                launched = self.run_worker_container(number, args)
            except Exception as error:
                self.diagnostics(number).event('container_launch_failed', container=name,
                                               error=str(error), error_type=type(error).__name__)
                raise
            self.diagnostics(number).event('container_launched', container=name,
                                           container_id=launched.stdout.strip())
        self.jobs[str(number)]['status'] = 'running'
        self.save()
        self.log(f'Started issue #{number}: {name}')

    def run_worker_container(self, number, args):
        try:
            return docker(*args)
        except JobError as error:
            paths = (self.job_dir(number), self.workspace_dir(number))
            message = str(error)
            # Docker Desktop can retain stale directory entries after an issue
            # folder is deleted and recreated. Refresh through a stable parent;
            # do not change host permissions or retry arbitrary launch failures.
            if (sys.platform != 'darwin'
                    or 'invalid mount config for type "bind"' not in message
                    or not any(reason in message for reason in
                               ('operation not permitted', 'bind source path does not exist'))
                    or not any(str(path) in message for path in paths)
                    or not all(path.is_dir() for path in paths)
                    or inspect_container(self.name(number)) is not None):
                raise
            self.diagnostics(number).event('bind_mount_refresh_requested', error=message)
            docker('run', '--rm', '--network', 'none', '--read-only',
                   '--mount', f'type=bind,source={ROOT},target=/host-root,readonly',
                   '--entrypoint', 'stat', IMAGE, '--',
                   *(str(Path('/host-root') / path.relative_to(ROOT)) for path in paths))
            self.diagnostics(number).event('bind_mount_refreshed')
            self.log(f'Issue #{number}: refreshed Docker Desktop folder access; retrying launch once')
            return docker(*args)

    def prepare_resume(self, number):
        """Resume this issue's workspace after live GitHub admission checks."""
        self.complete_workspace_transition(number)
        path = self.job_dir(number) / 'worker.json'
        worker = load_json(path, {})
        refresh = worker.get('phase') == 'done'
        if worker.get('pr'):
            refresh = refresh or self.attempt_needs_refresh(worker, self.api.pages('pulls?state=open'))
        if refresh:
            attempt = uuid.uuid4().hex[:12]
            fresh = {'repo': self.api.repo, 'issue': number, 'rounds': 0, 'handled': [],
                     'phase': 'coding', 'branch': f'codex/issue-{number}-retry-{attempt}',
                     'updated_at': stamp()}
            archive = self.archive_attempt(number, attempt, fresh)
            self.diagnostics(number).event('attempt_refreshed', previous_attempt=str(archive), state=fresh)
            self.log(f'Issue #{number}: previous attempt is no longer active; archived work and prepared a fresh workspace')
            return
        if remove_manual_retry_state(worker):
            save_json(path, worker)
        if worker and worker.get('phase') == 'blocked':
            worker['phase'] = worker.pop('resume_phase', 'coding')
            if worker['phase'] == 'blocked':
                worker['phase'] = 'coding'
            worker['review_started'] = time.time()
            worker.pop('error', None)
            save_json(path, worker)
            self.diagnostics(number).event('worker_resumed', state=worker)

    @staticmethod
    def attempt_needs_refresh(worker, open_prs):
        return worker.get('phase') == 'done' or bool(worker.get('pr') and not any(
            pr['number'] == worker['pr'] and pr.get('state', 'open') == 'open' for pr in open_prs))

    def restart_closed_pr(self, number):
        """Explicitly start a new attempt; never resurrect a manually closed PR."""
        job = self.jobs.get(str(number))
        if job and job['status'] in ACTIVE:
            raise JobError(f'Issue #{number} must be stopped before restarting')
        self.complete_workspace_transition(number)
        path = self.job_dir(number) / 'worker.json'
        worker = load_json(path, {})
        if not worker.get('pr'):
            raise JobError('No previous managed PR to restart')
        pr = self.api.request(f"pulls/{worker['pr']}")
        marker = f'<!-- codex-issue-worker:{self.api.repo}#{number} -->'
        if (pr['state'] != 'closed' or pr.get('merged')
                or pr['user']['login'].lower() != self.api.login.lower()
                or marker not in (pr.get('body') or '')
                or pr['head']['ref'] != worker['branch']
                or (pr['head'].get('repo') or {}).get('full_name', '').lower() != self.api.repo.lower()):
            raise JobError('Restart requires this worker\'s closed, unmerged PR')
        issue = self.api.request(f'issues/{number}')
        if issue['state'] != 'open' or not issue.get('milestone'):
            raise JobError('Restart requires an open issue with a milestone')
        for other in self.api.pages('pulls?state=open'):
            if marker in (other.get('body') or '') or linked_issue(other.get('body'), number, self.api.repo):
                raise JobError(f"Issue already has open PR #{other['number']}")
        container = inspect_container(self.name(number))
        if container:
            self.validate_container(container, number)
            if container['State'].get('Running'):
                raise JobError('Refusing to restart a running worker')
        attempt = uuid.uuid4().hex[:12]
        if container:
            self.archive_container(number, self.name(number), container, self.job_dir(number))
        if container:
            docker('rm', self.name(number))  # Workspace data is retained.
        fresh = {'repo': self.api.repo, 'issue': number, 'rounds': 0, 'handled': [],
                 'phase': 'coding', 'branch': f'codex/issue-{number}-retry-{attempt}',
                 'previous_pr': worker['pr'],
                 'updated_at': stamp()}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive = self.archive_attempt(number, attempt, fresh)
        self.diagnostics(number).event('closed_pr_restarted', previous_attempt=str(archive), state=fresh)
        self.save()
        self.log(f"Issue #{number} prepared with a fresh branch/workspace; PR #{worker['pr']} and old work preserved")

    def tick(self):
        if not self.args.dry_run:
            self.reconcile()
        if self.args.drain:
            report = {'repo': self.api.repo, 'draining': True,
                      'running': sum(j['status'] in ACTIVE for j in self.jobs.values()),
                      'concurrency': self.args.concurrency, 'next_issues': []}
            if report != self.last_report:
                print(json.dumps(report, indent=2), flush=True)
                self.last_report = report
            return report
        milestones = list(self.api.pages('milestones?state=open'))
        issues = [i for i in self.api.pages('issues?state=open&sort=created&direction=asc') if 'pull_request' not in i]
        active_jobs = [j for j in self.jobs.values() if j['status'] in ACTIVE]
        milestones = ordered_milestones(milestones, self.args.milestones)
        # Rebuild priority from live GitHub metadata on every poll. Old dispatcher
        # state must not hold independent work behind a single milestone.
        self.state.pop('active_milestone', None)
        if not self.args.dry_run:
            self.save()
        candidates = [issue for milestone in milestones
                      for issue in queue_issues(issues, milestone['number'])]
        slots = max(0, self.args.concurrency - len(active_jobs))
        # Stopped attempts do not consume slots or block unrelated milestones.
        # Their open issues still block actual dependents, including across milestones.
        eligible, deferred, labeled_blocked, exhausted = [], [], [], []
        open_prs = None
        dependency_blockers = []
        running_dependency_conflicts = []
        if self.args.dry_run:
            # Active workers keep their loaded scope; surface newly added
            # blockers without stopping them or pretending they can launch now.
            for number, job in self.jobs.items():
                if job['status'] in ACTIVE:
                    blockers = unsatisfied_dependencies(
                        self.api.pages(f'issues/{number}/dependencies/blocked_by'))
                    if blockers:
                        running_dependency_conflicts.append(dependency_detail(int(number), blockers))
        scan_complete = True
        for issue in candidates:
            job = self.jobs.get(str(issue['number']))
            if job and job['status'] in ACTIVE:
                continue
            # A one-shot dry run explains the whole queue even when slots are
            # occupied. Normal polling stops once slots fill to bound API use.
            if not self.args.dry_run and len(eligible) >= slots:
                scan_complete = False
                break
            if any(label['name'].lower() == 'status:blocked' for label in issue.get('labels', [])):
                labeled_blocked.append(issue['number'])
                continue
            blockers = unsatisfied_dependencies(
                self.api.pages(f"issues/{issue['number']}/dependencies/blocked_by"))
            if blockers:
                deferred.append(issue['number'])
                dependency_blockers.append(dependency_detail(issue['number'], blockers))
                continue
            # Read the current numeric budget, not a saved scheduling blacklist.
            # Live workers and resumable review waits can finish without coding;
            # stopped coding attempts cannot spend another round at this limit.
            worker = load_json(self.job_dir(issue['number']) / 'worker.json', {})
            if worker.get('pr') and open_prs is None:
                open_prs = list(self.api.pages('pulls?state=open'))
            if self.attempt_needs_refresh(worker, open_prs or []):
                # The old attempt's blockers/budget do not apply to its replacement.
                worker = {}
            if (worker.get('phase') in ('blocked', 'coding')
                    and worker.get('rounds', 0) >= self.args.max_rounds):
                exhausted.append({'number': issue['number'], 'rounds': worker['rounds'],
                                  'max_rounds': self.args.max_rounds})
                continue
            eligible.append(issue)
        report = {'repo': self.api.repo,
                  'milestones': [{'number': m['number'], 'title': m['title'],
                                  'open_issues': sum(1 for i in issues if i.get('milestone')
                                                     and i['milestone']['number'] == m['number'])}
                                 for m in milestones],
                  'open_issues': len(issues),
                  'running': len(active_jobs), 'concurrency': self.args.concurrency,
                  'available_slots': slots,
                  'selection_scan_complete': scan_complete,
                  'ready_issues': [i['number'] for i in eligible],
                  'next_issues': [{'number': i['number'], 'title': i['title'],
                                   'milestone': (i.get('milestone') or {}).get('number')}
                                  for i in eligible[:slots]],
                  'github_blocked_issues': labeled_blocked,
                  'waiting_on_dependencies': deferred,
                  'dependency_blockers': dependency_blockers,
                  'coding_budget_exhausted': exhausted,
                  'unmilestoned_issues_ignored': sum(not i.get('milestone') for i in issues)}
        if self.args.dry_run:
            report['running_dependency_conflicts'] = running_dependency_conflicts
        if self.args.dry_run or report != self.last_report:
            print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
            self.last_report = report
        if not self.args.dry_run:
            for issue in eligible:
                if self.args.drain:
                    break
                self.launch(issue['number'], issue['milestone']['number'])
        return report


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return number


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=default_repo())
    parser.add_argument('--concurrency', type=positive, default=2)
    parser.add_argument('--poll-seconds', type=positive, default=60)
    parser.add_argument('--review-poll-seconds', type=positive, default=30)
    parser.add_argument('--review-timeout', type=positive, default=86400, help='seconds per review/CI wait (default 24h)')
    parser.add_argument('--cli-timeout', type=positive, default=7200, help='seconds per uninterrupted CLI run (default 7200 = 2h)')
    parser.add_argument('--progress-interval', type=positive, default=30, help='seconds between CLI progress heartbeats (default 30)')
    parser.add_argument('--max-rounds', type=positive, default=8,
                        help='total CLI runs for retries and CI/review revisions across restarts (default 8)')
    parser.add_argument('--milestones', type=lambda value: [int(n) for n in value.split(',')], help='eligible milestone numbers in priority order, e.g. 2,3,4')
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--effort', choices=['low', 'medium', 'high', 'xhigh'], default='high')
    parser.add_argument('--cpus', type=positive, default=2)
    parser.add_argument('--memory', default='8g')
    parser.add_argument('--dry-run', action='store_true', help='read GitHub and show next jobs, with no Docker/GitHub changes')
    parser.add_argument('--once', action='store_true', help='one dispatch cycle; launched containers keep working')
    parser.add_argument('--drain', action='store_true',
                        help='finish and clean up existing workers, start no new issues, then exit')
    parser.add_argument('--restart-closed-pr', type=positive, action='append', default=[], metavar='ISSUE',
                        help='legacy explicit restart; normal runs automatically replace attempts with no open PR')
    parser.add_argument('--build-only', action='store_true', help='build review base and coding image, then exit')
    args = parser.parse_args(argv)
    if args.dry_run and (args.restart_closed_pr or args.build_only):
        parser.error('--dry-run cannot be combined with restart or build')
    if args.drain and (args.restart_closed_pr or args.build_only or args.once):
        parser.error('--drain cannot be combined with restart, build, or once')
    return args


@contextmanager
def drain_on_interrupt(args):
    def request_drain(signum, frame):
        args.drain = True
        args.once = False
        os.write(2, b'\nCtrl+C: no new issues; waiting for existing workers and container cleanup.\n')

    previous = signal.signal(signal.SIGINT, request_drain)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def run_dispatcher(dispatcher, args):
    with drain_on_interrupt(args):
        dispatcher.reconcile()
        for number in args.restart_closed_pr:
            if args.drain:
                break
            dispatcher.restart_closed_pr(number)
        while True:
            try:
                dispatcher.tick()
                # A signal may arrive during a tick, after its report was built.
                if args.drain and not any(j['status'] in ACTIVE for j in dispatcher.jobs.values()):
                    dispatcher.log('Drain complete; finished containers removed; dispatcher stopped')
                    return
            except JobError as error:
                dispatcher.log(str(error))
                if args.once:
                    raise
            if args.once:
                return
            was_draining = args.drain
            deadline = time.monotonic() + args.poll_seconds
            while time.monotonic() < deadline:
                if args.drain and not was_draining:
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))


def main():
    args = arguments()
    if args.build_only:
        subprocess.run(['docker', 'compose', '-f', str(ROOT / 'compose.codex-review.yml'), 'build'], cwd=ROOT, check=True)
        subprocess.run(['docker', 'build', '-f', str(ROOT / 'Dockerfile.codex-coding'), '-t', IMAGE, str(ROOT)], check=True)
        return
    api = GitHub(args.repo, ROOT / 'credentials/gh-coding', 'jtact2050')
    dispatcher = Dispatcher(api, args)
    if args.dry_run:
        dispatcher.tick()
        return
    for required in ('credentials/codex/auth.json', 'credentials/gh-coding/hosts.yml'):
        if not (ROOT / required).is_file():
            raise JobError(f'Missing {required}; complete the one-time login first')
    if docker('image', 'inspect', IMAGE, check=False).returncode:
        raise JobError('Coding image unavailable; run ./run-issues.sh --build-only first')
    dispatcher.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (dispatcher.directory / 'dispatcher.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JobError('Another dispatcher already owns this repository') from None
        # Load state again under lock to avoid racing a previous process.
        dispatcher = Dispatcher(api, args)
        run_dispatcher(dispatcher, args)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nDispatcher stopped; existing Docker jobs continue. Restart this script to resume monitoring.')
    except (JobError, subprocess.SubprocessError) as error:
        print(f'Dispatcher stopped: {error}')
        raise SystemExit(1)
