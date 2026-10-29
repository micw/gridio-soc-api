"""Real process contention, using a fake OAuth server and no external network."""
import base64
import json
import multiprocessing as mp
from pathlib import Path
import tempfile
import time
import unittest
from gridio_service.core import Config, Service


def token(exp):
    body = base64.urlsafe_b64encode(json.dumps({'sub': 'customer', 'exp': exp}).encode()).decode().rstrip('=')
    return 'x.' + body + '.x'


def fetch(directory, start, results):
    def transport(method, path, form=None, token=None):
        with (Path(directory) / 'calls').open('a') as f:
            f.write(path + '\n')
        if form:
            # The old refresh token may only be consumed once.
            marker = Path(directory) / 'consumed'
            with marker.open('x'):
                pass
            time.sleep(.1)
            return 200, {}, {'access_token': globals()['token'](time.time() + 900),
                             'refresh_token': 'new-refresh'}
        return 200, {}, {'state_of_charge': 55, 'capacity_available': 11000}
    start.wait()
    try:
        service = Service(Config(data_dir=Path(directory), vehicle_id='car'), transport)
        results.put(service.metrics()['soc'])
    except Exception as e:
        results.put(type(e).__name__)


class ProcessTests(unittest.TestCase):
    def test_two_processes_share_rotation_and_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'state.json').write_text(json.dumps({
                'access_token': token(1), 'refresh_token': 'old-refresh',
                'customer_id': 'customer', 'expires_at': 1,
                'auth_state': 'authenticated'}))
            ctx = mp.get_context('spawn')
            start, results = ctx.Event(), ctx.Queue()
            workers = [ctx.Process(target=fetch, args=(directory, start, results)) for _ in range(2)]
            try:
                for p in workers:
                    p.start()
                start.set()
                for p in workers:
                    p.join(10)
                    self.assertEqual(p.exitcode, 0)
                self.assertEqual([results.get(timeout=2) for _ in workers], [55, 55])
                calls = Path(directory, 'calls').read_text().splitlines()
                self.assertEqual(calls.count('/oauth2/token'), 1)
                self.assertEqual(sum(p.endswith('/metrics/latest') for p in calls), 1)
                self.assertEqual(json.loads(Path(directory, 'state.json').read_text())['refresh_token'], 'new-refresh')
            finally:
                for p in workers:
                    if p.is_alive():
                        p.terminate()
                        p.join()
                results.close()
