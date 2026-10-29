"""Offline core regression tests; all persistent state uses temporary directories."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gridio_service.core import Config, Service, Unavailable, duration


def jwt(exp=10000):
    payload = base64.urlsafe_b64encode(json.dumps({'sub': 'customer', 'exp': exp}).encode()).decode().rstrip('=')
    return 'x.' + payload + '.x'


class Fake:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.soc = 53
        self.devices = [{'id': 'car', '__typename': 'Vehicle'}]
        self.refreshes = 0

    def __call__(self, method, path, form=None, token=None):
        self.calls.append((method, path, form, token))
        if path == '/oauth2/otp/request':
            return 200, {}, {}
        if path == '/oauth2/token':
            self.refreshes += 1
            return 200, {}, {'access_token': jwt(), 'refresh_token': 'rotation-' + str(self.refreshes)}
        if self.failure:
            return self.failure
        if path.endswith('/devices'):
            return 200, {}, {'data': self.devices}
        return 200, {}, {'state_of_charge': self.soc, 'capacity_available': 1700,
                         'device_status': 'plugged_in_charging', 'latest_data_point': '2026-01-01T00:00:00Z'}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = patch('gridio_service.core.time.time', return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.fake = Fake()
        self.config = Config(data_dir=Path(self.tmp.name))
        self.service = Service(self.config, self.fake)

    def login(self):
        self.service.verify_otp('USER@Example.org', '123456')

    def test_config(self):
        config = Config.from_env({'GRIDIO_DATA_DIR': self.tmp.name, 'GRIDIO_MIN_FETCH_INTERVAL': '1m',
                                 'GRIDIO_RECORDING_ENABLED': 'true', 'GRIDIO_RECORDING_RETENTION': '30 days'})
        self.assertEqual(config.min_fetch_interval, 60)
        self.assertTrue(config.recording_enabled)
        self.assertEqual(config.recording_retention, 2592000)
        self.assertEqual(duration('1h30m'), 5400)
        for kwargs in ({'min_fetch_interval': 29}, {'poll_interval': 3601},
                       {'poll_interval': 0}, {'max_cache_age': 'nan'}):
            with self.assertRaises(ValueError):
                Config(**kwargs)

    def test_login_and_metrics(self):
        self.service.request_otp('USER@Example.org')
        self.assertEqual(self.fake.calls[0][2]['username'], 'user@example.org')
        self.login()
        value = self.service.metrics()
        self.assertEqual(value['soc'], 53)
        self.assertEqual(value['power_w'], 1700)
        self.assertFalse(value['cached'])
        before = len(self.fake.calls)
        self.assertTrue(self.service.metrics()['cached'])
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(self.service.auth_status()['state'], 'authenticated')
        self.assertEqual((Path(self.tmp.name) / 'state.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('rotation', json.dumps(self.service.status()))

    def test_shared_cache_and_failed_attempt_rate_limit(self):
        self.login()
        self.service.metrics()
        other = Service(self.config, self.fake)
        self.assertTrue(other.metrics()['cached'])
        self.now.return_value = 1031
        self.fake.failure = (503, {}, {})
        self.assertTrue(other.metrics()['cached'])
        before = len(self.fake.calls)
        self.service.metrics()
        self.assertEqual(len(self.fake.calls), before)
        self.now.return_value = 1000 + 86401
        with self.assertRaises(Unavailable):
            self.service.metrics()

    def test_invalid_soc_does_not_replace_good_cache(self):
        self.login()
        self.service.metrics()
        for index, invalid in enumerate((None, -1, 101, True, '50')):
            self.now.return_value = 1031 + index * 31
            self.fake.soc = invalid
            result = self.service.metrics()
            self.assertEqual(result['soc'], 53)
            self.assertTrue(result['cached'])
        self.now.return_value = 1300
        self.fake.soc = 0
        self.assertEqual(self.service.metrics()['soc'], 0)

    def test_refresh_and_rotation(self):
        self.login()
        self.now.return_value = 9950
        self.service.metrics('car')
        self.assertEqual(self.fake.refreshes, 2)
        state = json.loads((Path(self.tmp.name) / 'state.json').read_text())
        self.assertEqual(state['refresh_token'], 'rotation-2')

    def test_401_retries_once(self):
        self.login()
        original = self.fake
        seen = []
        def transport(method, path, form=None, token=None):
            if method == 'GET' and not seen:
                seen.append(True)
                return 401, {}, {}
            return original(method, path, form, token)
        self.service.transport = transport
        self.assertEqual(self.service.metrics('car')['soc'], 53)
        self.assertEqual(self.fake.refreshes, 2)

    def test_429_backoff_and_cache(self):
        self.login()
        self.service.metrics()
        self.now.return_value = 1031
        self.fake.failure = (429, {'Retry-After': '120'}, {})
        self.assertTrue(self.service.metrics()['cached'])
        self.now.return_value = 1062
        before = len(self.fake.calls)
        self.assertTrue(self.service.metrics()['cached'])
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(self.service.status()['retry_at'], 1151)

    def test_no_auth_tick_is_throttled(self):
        self.service.tick()
        self.now.return_value = 1001
        self.service.tick()
        state = json.loads((Path(self.tmp.name) / 'state.json').read_text())
        self.assertEqual(state['tick_attempt'], 1000)
        self.assertEqual(self.fake.calls, [])

    def test_ambiguous_discovery(self):
        self.login()
        self.fake.devices *= 2
        with self.assertRaises(Unavailable):
            self.service.metrics()
        self.assertEqual(self.service.metrics('car')['soc'], 53)

    def test_scrape_never_fetches_and_reports_auth_failure(self):
        self.assertIn('gridio_auth_login_required 1', self.service.prometheus())
        self.assertEqual(self.fake.calls, [])
        self.login()
        self.service.metrics('car')
        before = len(self.fake.calls)
        self.now.return_value = 9950
        self.service.prometheus()
        self.assertEqual(len(self.fake.calls), before)
        original = self.fake
        def failed_refresh(method, path, form=None, token=None):
            if path == '/oauth2/token':
                return 401, {}, {}
            return original(method, path, form, token)
        self.service.transport = failed_refresh
        self.assertTrue(self.service.metrics('car')['cached'])
        self.assertIn('gridio_auth_login_required 1', self.service.prometheus())
        self.now.return_value = 100000
        with self.assertRaises(Unavailable):
            self.service.metrics('car')

    def test_otp_cooldown_and_new_login_clears_cache(self):
        self.service.request_otp('u@example.org')
        with self.assertRaises(Unavailable):
            self.service.request_otp('u@example.org')
        self.login()
        self.service.metrics('car')
        self.service.verify_otp('other@example.org', '123456')
        self.assertNotIn('metrics', json.loads((Path(self.tmp.name) / 'state.json').read_text()))

    def test_other_vehicle_does_not_starve_keepalive(self):
        self.config.vehicle_id = 'car'
        self.login()
        self.service.metrics('car')
        self.now.return_value = 4700
        self.service.metrics('other-car')
        self.service.tick()
        self.assertTrue('/devices/car/metrics/latest' in self.fake.calls[-1][1])

    def test_auth_recovers_after_transient_refresh_failure(self):
        self.login()
        original = self.fake
        def failing(method, path, form=None, token=None):
            return (503, {}, {}) if form else (401, {}, {})
        self.service.transport = failing
        with self.assertRaises(Unavailable):
            self.service.metrics('car')
        self.assertEqual(self.service.auth_status()['state'], 'refresh_failed')
        self.now.return_value = 1061
        self.service.transport = original
        self.service.metrics('car')
        self.assertEqual(self.service.auth_status()['state'], 'authenticated')

    def test_record_raw_and_prometheus(self):
        self.config.recording_enabled = True
        self.login()
        self.service.metrics()
        files = list((Path(self.tmp.name) / 'recordings').glob('*.jsonl'))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())['raw']['capacity_available'], 1700)
        self.assertIn('gridio_soc_percent 53', self.service.prometheus())


if __name__ == '__main__':
    unittest.main()
