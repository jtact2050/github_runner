#!/usr/bin/env python3
"""Archive legacy coding volumes next to issue logs, without starting jobs or deleting volumes."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from common import JobError, load_json, save_json, stamp

ROOT = Path(__file__).resolve().parent.parent
IMAGE = 'codex-issue-worker:local'


def docker(*args):
    result = subprocess.run(['docker', *map(str, args)], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise JobError(f'Docker {args[0]} failed: {result.stderr.strip()}')
    return result.stdout


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def record_paths(root, repo, key, volume, number):
    patterns = (f'*/jobs/{number}/worker.json', f'*/attempts/{number}/*/worker.json')
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            state = load_json(path, {})
            expected = state.get('workspace_volume', f'codex-code-{key}-{number}-workspace')
            if state.get('repo') == repo and state.get('workspace_storage') != 'bind' and expected == volume:
                yield path.parent


def archive_volume(root, repo, key, volume, image=IMAGE):
    match = re.fullmatch(r'codex-code-' + re.escape(key) + r'-([1-9][0-9]*)-workspace(?:-[a-f0-9]+)?', volume)
    if not match:
        raise JobError('Refusing to archive a volume outside this repository')
    number = int(match[1])
    if docker('ps', '-q', '--filter', f'volume={volume}').strip():
        raise JobError(f'{volume} is in use by a running container; stop it before archiving')
    info = json.loads(docker('volume', 'inspect', volume))[0]
    directory = root / key / 'jobs' / str(number) / 'legacy-workspaces' / volume
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = directory / 'workspace.tar'
    partial = directory / 'workspace.tar.partial'
    metadata_path = directory / 'manifest.json'
    metadata = load_json(metadata_path)
    if archive.exists() and not metadata:
        raise JobError(f'Unverified archive exists at {archive}; inspect before retrying')
    source = ['--mount', f'type=volume,source={volume},target=/source,readonly']
    if not archive.exists():
        # du counts hard links once, matching tar's default hard-link preservation.
        size = int(docker('run', '--rm', '--network', 'none', '--read-only', *source,
                          '--entrypoint', 'du', image, '-sb', '/source').split()[0])
        if shutil.disk_usage(directory).free < size * 1.1 + 1024 ** 3:
            raise JobError(f'Insufficient free disk space to archive {volume}')
        print(f'Archiving {volume} ({size / 1024 ** 3:.2f} GiB)', flush=True)
        with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as stream:
            subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', *source,
                            '--entrypoint', 'tar', image, '--sparse', '-C', '/source', '-cpf', '-', '.'],
                           stdout=stream, check=True)
            stream.flush()
            os.fsync(stream.fileno())
        candidate = partial
    else:
        if metadata.get('sha256') != file_digest(archive):
            raise JobError(f'Archive checksum mismatch: {archive}')
        candidate = archive
    # Compare every archived file against the unchanged read-only source volume.
    # This also checks modes, ownership, timestamps, symlinks, and file contents.
    print(f'Verifying {volume}', flush=True)
    subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', *source,
                    '--mount', f'type=bind,source={directory},target=/backup,readonly',
                    '--entrypoint', 'tar', image, '-C', '/source', '--compare',
                    '--file', '/backup/' + candidate.name], check=True)
    digest = file_digest(candidate)
    records = []
    for path in record_paths(root, repo, key, volume, number):
        relative = path.relative_to(root)
        target = directory / 'historical-records' / relative
        if not target.exists():
            shutil.copytree(path, target, symlinks=True,
                            ignore=shutil.ignore_patterns('workspace', 'attempts', 'legacy-workspaces'))
        records.append(str(relative))
    metadata = {'repo': repo, 'issue': number, 'volume': volume,
                'volume_created_at': info.get('CreatedAt'), 'archive': 'workspace.tar',
                'bytes': candidate.stat().st_size, 'sha256': digest,
                'verified_at': stamp(), 'verification': 'GNU tar compare against read-only source volume',
                'historical_records': records, 'original_volume_retained': True}
    save_json(metadata_path, metadata)
    if candidate == partial:
        partial.replace(archive)
    print(f'Verified: {archive}', flush=True)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--volume', action='append', help='archive only this volume; repeatable')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        raise JobError('Repository must be OWNER/REPO')
    key = hashlib.sha256(args.repo.lower().encode()).hexdigest()[:12]
    root = ROOT / '.coding-state'
    directory = root / key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'dispatcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JobError('Stop the dispatcher before archiving workspaces') from None
        if docker('ps', '-q', '--filter', f'label=codex.repo={args.repo}').strip():
            raise JobError('Stop this repository\'s coding workers before archiving')
        volumes = args.volume or docker('volume', 'ls', '--format', '{{.Name}}',
                                       '--filter', f'name=codex-code-{key}-').splitlines()
        records = [archive_volume(root, args.repo, key, volume) for volume in volumes]
        save_json(directory / ('workspace-archive-' + uuid.uuid4().hex[:12] + '.json'),
                  {'repo': args.repo, 'completed_at': stamp(), 'archives': records})
        print(f'Archived and verified {len(records)} volumes. Originals retained. No jobs started.', flush=True)


if __name__ == '__main__':
    main()
