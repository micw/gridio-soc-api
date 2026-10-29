import json
import threading
import unittest
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from gridio_service.__main__ import handler_for


class Fake:
    available = False
    def metrics(self, vehicle):
        if not self.available:
            raise RuntimeError('not logged in')
        return {'soc': 53, 'power_w': 10000}
    def prometheus(self):
        return 'gridio_auth_login_required 1\n'
    def auth_status(self):
        return {'state': 'login_required'}


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.service = Fake()
        self.config = SimpleNamespace(web_enabled=False, basic_auth_username='', basic_auth_password='')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.service, self.config))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
    def test_error_then_recovery(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url + '/evcc/vehicles/default')
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(caught.exception.read(), b'ErrNotAvailable')
        caught.exception.close()
        self.service.available = True
        with urlopen(self.url + '/evcc/vehicles/default') as r:
            self.assertEqual(json.load(r)['soc'], 53)
    def test_metrics_without_login(self):
        with urlopen(self.url + '/metrics') as r:
            self.assertIn(b'gridio_auth_login_required 1', r.read())
    def test_ui_disabled(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url + '/')
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()
    def test_optional_login_ui_and_cross_origin_rejection(self):
        self.config.web_enabled = True
        with urlopen(self.url + '/') as r:
            self.assertIn(b'Request code', r.read())
        req = Request(self.url + '/login/request', data=b'email=u%40example.org',
                      headers={'Origin': 'https://attacker.invalid'})
        with self.assertRaises(HTTPError) as caught:
            urlopen(req)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_basic_auth(self):
        self.config.basic_auth_username = 'test'
        self.config.basic_auth_password = 'secret'
        self.config.web_enabled = True
        with urlopen(self.url + '/metrics') as r:
            self.assertEqual(r.status, 200)
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url + '/')
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        with urlopen(self.url + '/health/live') as r:
            self.assertEqual(r.status, 200)
