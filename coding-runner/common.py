"""GitHub transport and durable JSON; no third-party Python dependencies."""
import json
import hashlib
import http.client
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


class JobError(RuntimeError):
    pass


def issue_digest(issue):
    """Only requirement text invalidates work or a recorded external blocker."""
    return hashlib.sha256(json.dumps([issue['title'], issue.get('body') or ''],
                                     ensure_ascii=False).encode()).hexdigest()


class APIError(JobError):
    def __init__(self, status, path):
        self.status = status
        super().__init__(f"GitHub HTTP {status}: {path}")


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def remove_manual_retry_state(state):
    """Migrate old worker records without changing their work or round budget."""
    changed = False
    for key in ('manual_retry_required', 'blocked_issue_digest', 'blocker_pending'):
        if key in state:
            del state[key]
            changed = True
    return changed


class GitHub:
    def observe(self, event, **fields):
        observer = getattr(self, 'observer', None)
        if observer:
            observer(event, **fields)

    def __init__(self, repo, config_dir, expected_user='jtact2050'):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise JobError('Repository must be OWNER/REPO')
        self.repo = repo
        self.env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
        self.env['GH_CONFIG_DIR'] = str(config_dir)
        result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'],
                                env=self.env, capture_output=True, text=True, timeout=30)
        if result.returncode or not result.stdout.strip():
            raise JobError(f'GitHub login unavailable in {config_dir}')
        self.token = result.stdout.strip()
        user = self.request('/user')
        self.login, self.user_id = user['login'], user['id']
        if self.login.lower() != expected_user.lower():
            raise JobError(f'Expected {expected_user}, authenticated as {self.login}')

    def request(self, path, payload=None, method=None):
        method = method or ('GET' if payload is None else 'POST')
        endpoint = path if path.startswith('/') else f'/repos/{self.repo}/{path}'
        request = urllib.request.Request('https://api.github.com' + endpoint,
            data=None if payload is None else json.dumps(payload).encode(), method=method,
            headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json',
                     'User-Agent': 'codex-issue-worker'})
        # Brief GitHub outages can outlast the original 1/2/4 second window.
        # Allow bounded exponential backoff for reads; never replay writes.
        for attempt in range(7):
            self.observe('github_request', method=method, path=endpoint, attempt=attempt + 1, payload=payload)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = response.read()
                    result = json.loads(data) if data else None
                    self.observe('github_response', method=method, path=endpoint, attempt=attempt + 1, response=result)
                    return result
            except urllib.error.HTTPError as error:
                self.observe('github_error', method=method, path=endpoint, attempt=attempt + 1, status=error.code)
                # Retry reads only. A failed write may already have succeeded.
                if method == 'GET' and error.code in (429, 500, 502, 503, 504) and attempt < 6:
                    delay = min(int(error.headers.get('Retry-After', 2 ** attempt)), 60)
                    self.observe('github_retry', path=endpoint, delay_seconds=delay)
                    time.sleep(delay)
                    continue
                raise APIError(error.code, endpoint) from None
            except (urllib.error.URLError, http.client.HTTPException,
                    TimeoutError, ConnectionError) as error:
                # Opening a URL wraps many transport failures in URLError,
                # but reading its body can raise these exceptions directly.
                self.observe('github_error', method=method, path=endpoint, attempt=attempt + 1, error=str(error))
                if method == 'GET' and attempt < 6:
                    self.observe('github_retry', path=endpoint, delay_seconds=2 ** attempt)
                    time.sleep(2 ** attempt)
                    continue
                raise JobError(f'GitHub network error: {endpoint}; retry the job to reconcile state') from None

    def pages(self, path, key=None):
        page = 1
        while True:
            result = self.request(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            rows = result[key] if key else result
            yield from rows
            if len(rows) < 100:
                return
            page += 1

    def graphql(self, query, **variables):
        response = self.request('/graphql', {'query': query, 'variables': variables})
        if response.get('errors'):
            raise JobError('GitHub GraphQL request failed')
        return response['data']

    def repository(self):
        return self.request(f'/repos/{self.repo}')


def stamp():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
