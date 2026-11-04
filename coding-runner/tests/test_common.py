import io
from http.client import IncompleteRead, RemoteDisconnected
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import APIError, GitHub, JobError


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.api = GitHub.__new__(GitHub)
        self.api.repo = 'owner/repo'
        self.api.token = 'test-token'

    def unavailable(self, status=503):
        error = HTTPError('https://api.github.com/test', status, 'error', {}, None)
        self.addCleanup(error.close)
        return error

    def interrupted_response(self, error):
        response = io.BytesIO()
        response.read = Mock(side_effect=error)
        return response

    @patch('common.time.sleep')
    @patch('common.urllib.request.urlopen')
    def test_body_transport_errors_retry_and_discard_partial_response(self, urlopen, sleep):
        for error in (IncompleteRead(b'{"state":', 10), RemoteDisconnected(),
                      TimeoutError(), ConnectionResetError()):
            with self.subTest(error=type(error).__name__):
                urlopen.reset_mock()
                sleep.reset_mock()
                response = self.interrupted_response(error)
                urlopen.side_effect = [response, io.BytesIO(b'{"state":"open"}')]
                self.assertEqual(self.api.request('pulls/1'), {'state': 'open'})
                self.assertTrue(response.closed)
                self.assertEqual(urlopen.call_count, 2)
                sleep.assert_called_once_with(1)

    @patch('common.time.sleep')
    @patch('common.urllib.request.urlopen')
    def test_incomplete_read_exhaustion_becomes_recoverable_job_error(self, urlopen, sleep):
        urlopen.side_effect = [self.interrupted_response(IncompleteRead(b'partial', 10))
                              for _ in range(7)]
        with self.assertRaisesRegex(JobError, 'GitHub network error: /repos/owner/repo/issues'):
            self.api.request('issues')
        self.assertEqual(urlopen.call_count, 7)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4, 8, 16, 32])

    @patch('common.time.sleep')
    @patch('common.urllib.request.urlopen')
    def test_interrupted_write_response_is_never_repeated(self, urlopen, sleep):
        for method in ('POST', 'PATCH', 'PUT', 'DELETE'):
            with self.subTest(method=method):
                urlopen.reset_mock()
                urlopen.return_value = self.interrupted_response(IncompleteRead(b'partial', 10))
                with self.assertRaisesRegex(JobError, 'retry the job to reconcile state'):
                    self.api.request('issues/1', {'state': 'closed'}, method=method)
                self.assertEqual(urlopen.call_count, 1)
                sleep.assert_not_called()

    @patch('common.time.sleep')
    @patch('common.urllib.request.urlopen')
    def test_interrupted_later_page_is_retried_without_losing_or_duplicating_rows(self, urlopen, sleep):
        first_page = [dict(number=n) for n in range(100)]
        urlopen.side_effect = [io.BytesIO(json.dumps(first_page).encode()),
                              self.interrupted_response(IncompleteRead(b'[{"number":', 10)),
                              io.BytesIO(b'[{"number":100}]')]
        rows = list(self.api.pages('issues?state=open'))
        self.assertEqual(rows, first_page + [{'number': 100}])
        self.assertEqual([c.args[0].full_url for c in urlopen.call_args_list], [
            'https://api.github.com/repos/owner/repo/issues?state=open&per_page=100&page=1',
            'https://api.github.com/repos/owner/repo/issues?state=open&per_page=100&page=2',
            'https://api.github.com/repos/owner/repo/issues?state=open&per_page=100&page=2'])

    def test_dependency_pagination_includes_open_blocker_on_later_page(self):
        closed = [dict(number=n, state='closed') for n in range(100)]
        with patch.object(self.api, 'request', side_effect=[closed, [dict(number=101, state='open')]]) as request:
            rows = list(self.api.pages('issues/16/dependencies/blocked_by'))
        self.assertEqual(rows[-1], dict(number=101, state='open'))
        self.assertEqual(len(rows), 101)
        self.assertTrue(request.call_args.args[0].endswith('per_page=100&page=2'))

    @patch('common.time.sleep')
    @patch('common.urllib.request.urlopen')
    def test_read_recovers_after_original_retry_window(self, urlopen, sleep):
        urlopen.side_effect = [self.unavailable() for _ in range(4)] + [io.BytesIO(b'{"state":"open"}')]
        self.assertEqual(self.api.request('pulls/1'), {'state': 'open'})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4, 8])

    @patch('common.time.sleep')
    @patch('common.urllib.request.urlopen')
    def test_observer_records_response_and_retry_without_authorization(self, urlopen, sleep):
        self.api.observer = Mock()
        urlopen.side_effect = [self.unavailable(), io.BytesIO(b'{"state":"open"}')]
        self.api.request('pulls/1')
        calls = self.api.observer.call_args_list
        self.assertEqual([c.args[0] for c in calls], [
            'github_request', 'github_error', 'github_retry', 'github_request', 'github_response'])
        self.assertEqual(calls[-1].kwargs['response'], {'state': 'open'})
        self.assertNotIn('test-token', str(calls))

    @patch('common.time.sleep')
    @patch('common.urllib.request.urlopen')
    def test_persistent_failure_is_bounded(self, urlopen, sleep):
        urlopen.side_effect = self.unavailable()
        with self.assertRaises(APIError):
            self.api.request('pulls/1')
        self.assertEqual(urlopen.call_count, 7)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4, 8, 16, 32])

    @patch('common.time.sleep')
    @patch('common.urllib.request.urlopen')
    def test_uncertain_write_is_never_repeated(self, urlopen, sleep):
        urlopen.side_effect = self.unavailable()
        with self.assertRaises(APIError):
            self.api.request('pulls/1/merge', {'sha': 'exact-sha'}, method='PUT')
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    @patch('common.time.sleep')
    @patch('common.urllib.request.urlopen')
    def test_permission_failure_is_not_retried(self, urlopen, sleep):
        urlopen.side_effect = self.unavailable(403)
        with self.assertRaises(APIError):
            self.api.request('pulls/1')
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
