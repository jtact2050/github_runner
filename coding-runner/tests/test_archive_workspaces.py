import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive_workspaces as archive
from common import JobError, load_json, save_json


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.key = 'owner/repo', '123abc'
        self.volume = 'codex-code-123abc-16-workspace'
        self.directory = self.root / self.key / 'jobs/16/legacy-workspaces' / self.volume
        self.calls = []

    def docker(self, *args):
        self.calls.append(args)
        if args[0] == 'ps':
            return ''
        if args[:2] == ('volume', 'inspect'):
            return '[{"CreatedAt":"test-time"}]'
        if args[0] == 'run':
            return '1024 /source'
        raise AssertionError(args)

    def mock_run(self, args, **kwargs):
        self.calls.append(tuple(args))
        if 'stdout' in kwargs:
            kwargs['stdout'].write(b'fake tar content')
        return SimpleNamespace(returncode=0)

    def perform(self):
        with patch.object(archive, 'docker', side_effect=self.docker), \
                patch.object(archive.subprocess, 'run', side_effect=self.mock_run), \
                patch.object(archive.shutil, 'disk_usage', return_value=SimpleNamespace(free=10**12)), \
                contextlib.redirect_stdout(io.StringIO()):
            return archive.archive_volume(self.root, self.repo, self.key, self.volume)

    def test_archive_verified_before_publish_and_history_is_copied_without_restoring(self):
        historical = self.root / 'old/jobs/16'
        historical.mkdir(parents=True)
        save_json(historical / 'worker.json', {'repo': self.repo, 'rounds': 4})
        (historical / 'issue-log.jsonl').write_text('history')
        (historical / 'auth-link').symlink_to('/missing/shared/auth')
        result = self.perform()
        target = self.directory / 'workspace.tar'
        self.assertEqual(result['sha256'], archive.file_digest(target))
        self.assertTrue(result['original_volume_retained'])
        self.assertEqual(result['historical_records'], ['old/jobs/16'])
        self.assertTrue((self.directory / 'historical-records/old/jobs/16/auth-link').is_symlink())
        self.assertFalse((self.root / self.key / 'jobs/16/worker.json').exists())
        self.assertFalse((self.directory / 'workspace.tar.partial').exists())
        self.assertTrue(any('--compare' in call for call in self.calls))
        self.assertFalse(any('rm' in call or 'prune' in call for call in self.calls))
        self.calls.clear()
        self.perform()
        self.assertFalse(any('--sparse' in call for call in self.calls))

    def test_compare_failure_keeps_only_partial_and_original(self):
        export = self.mock_run
        def fail_compare(args, **kwargs):
            if '--compare' in args:
                raise subprocess.CalledProcessError(1, args)
            return export(args, **kwargs)
        with patch.object(self, 'mock_run', side_effect=fail_compare), self.assertRaises(subprocess.CalledProcessError):
            self.perform()
        self.assertTrue((self.directory / 'workspace.tar.partial').exists())
        self.assertFalse((self.directory / 'workspace.tar').exists())
        self.assertFalse((self.directory / 'manifest.json').exists())

    def test_checksum_mismatch_is_not_overwritten(self):
        self.perform()
        (self.directory / 'workspace.tar').write_text('corrupt')
        with self.assertRaisesRegex(JobError, 'checksum mismatch'):
            self.perform()
        self.assertEqual((self.directory / 'workspace.tar').read_text(), 'corrupt')

    def test_active_or_unrelated_volume_is_rejected(self):
        with patch.object(archive, 'docker', return_value='running'), self.assertRaisesRegex(JobError, 'running'):
            archive.archive_volume(self.root, self.repo, self.key, self.volume)
        with patch.object(archive, 'docker') as docker, self.assertRaisesRegex(JobError, 'outside'):
            archive.archive_volume(self.root, self.repo, self.key, 'another-volume')
        docker.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_record_mapping_excludes_other_attempts_and_new_bind_jobs(self):
        for name, state in {
            'old/jobs/16': {'repo': self.repo},
            'old/attempts/16/retry': {'repo': self.repo, 'workspace_volume': self.volume + '-abc'},
            'new/jobs/16': {'repo': self.repo, 'workspace_storage': 'bind'},
            'other/jobs/16': {'repo': 'other/repo'},
        }.items():
            path = self.root / name
            path.mkdir(parents=True)
            save_json(path / 'worker.json', state)
        self.assertEqual(list(archive.record_paths(self.root, self.repo, self.key, self.volume, 16)),
                         [self.root / 'old/jobs/16'])


if __name__ == '__main__':
    unittest.main()
