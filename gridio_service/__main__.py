import argparse
import base64
import getpass
import hmac
import json
import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .core import Config, Service, Unavailable

HTML = b'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>Gridio login</title>
<h1>Gridio login</h1><form method="post" action="/login/request">
<label>Email <input name="email" type="email" required></label>
<button>Request code</button></form>
<form method="post" action="/login/verify"><label>Email <input name="email" type="email" required></label>
<label>Code <input name="code" inputmode="numeric" autocomplete="one-time-code" required></label>
<button>Sign in</button></form><p><a href="/login/status">Authentication status</a></p></html>'''


def handler_for(service, config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logging.getLogger('http').debug('%s %s', self.command, urlsplit(self.path).path)

        def reply(self, status, body, content_type='text/plain; charset=utf-8'):
            if not isinstance(body, bytes):
                body = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if not config.basic_auth_username:
                return True
            expected = 'Basic ' + base64.b64encode(
                f'{config.basic_auth_username}:{config.basic_auth_password}'.encode()).decode()
            if hmac.compare_digest(self.headers.get('Authorization', ''), expected):
                return True
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="Gridio"')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return False

        def do_GET(self):
            path = urlsplit(self.path).path
            # Probes must work without credentials and without upstream calls.
            if path == '/health/live':
                return self.reply(200, 'ok')
            if path in ('/', '/login/status') and config.web_enabled and not self.authorized():
                return
            if path == '/metrics':
                return self.reply(200, service.prometheus(), 'text/plain; version=0.0.4; charset=utf-8')
            if path == '/health/ready':
                ready = service.auth_status().get('state') == 'authenticated'
                return self.reply(200 if ready else 503, 'ready' if ready else 'not ready')
            if path in ('/', '/login/status') and config.web_enabled:
                if path == '/':
                    return self.reply(200, HTML, 'text/html; charset=utf-8')
                return self.reply(200, json.dumps(service.status()), 'application/json')
            if path.startswith('/evcc/vehicles/'):
                vehicle_id = path[len('/evcc/vehicles/'):]
                if not vehicle_id or '/' in vehicle_id:
                    return self.reply(404, 'Not found')
                try:
                    data = service.metrics(None if vehicle_id == 'default' else vehicle_id)
                    return self.reply(200, json.dumps(data), 'application/json')
                except Exception:
                    # evcc matches this exact body, before attempting jq extraction.
                    return self.reply(503, 'ErrNotAvailable')
            self.reply(404, 'Not found')

        def do_POST(self):
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            if not config.web_enabled or path not in ('/login/request', '/login/verify'):
                return self.reply(404, 'Not found')
            # Prevent cross-origin form submissions even when Basic Auth is cached.
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.reply(403, 'Cross-origin submission rejected')
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    return self.reply(400, 'Invalid request size')
                fields = parse_qs(self.rfile.read(length).decode())
                email = fields.get('email', [''])[0].strip()
                if not email or len(email) > 320:
                    return self.reply(400, 'Email required')
                if path == '/login/request':
                    service.request_otp(email)
                    return self.reply(200, 'Code requested. Return to the login page to enter it.')
                code = fields.get('code', [''])[0].strip()
                if len(code) != 6 or not code.isdigit():
                    return self.reply(400, 'A six digit code is required')
                service.verify_otp(email, code)
                self.reply(200, 'Signed in. Vehicle selection is configured with GRIDIO_VEHICLE_ID.')
            except Exception:
                self.reply(400, 'Login failed. Check the code, cooldown and upstream availability.')
    return Handler


def serve(service, config):
    stop = threading.Event()
    server = ThreadingHTTPServer((config.host, config.port), handler_for(service, config))
    server.daemon_threads = True

    def worker():
        while not stop.is_set():
            try:
                service.tick()
            except Exception:
                logging.warning('Background update failed; retrying on next scheduled attempt')
            stop.wait(1)

    threading.Thread(target=worker, daemon=True).start()

    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description='Read-only Gridio evcc bridge')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('serve')
    login = commands.add_parser('login').add_subparsers(dest='action', required=True)
    for action in ('request', 'verify'):
        command = login.add_parser(action)
        command.add_argument('--email', required=True)
    commands.add_parser('status')
    commands.add_parser('vehicles')
    args = parser.parse_args()
    try:
        config = Config.from_env()
        logging.basicConfig(level=config.log_level.upper())
        service = Service(config)
        if args.command in (None, 'serve'):
            serve(service, config)
        elif args.command == 'login':
            if args.action == 'request':
                service.request_otp(args.email)
                print('Code requested.')
            else:
                service.verify_otp(args.email, getpass.getpass('Email code: '))
                print('Signed in.')
        elif args.command == 'vehicles':
            print(json.dumps(service.vehicles(), indent=2))
        else:
            print(json.dumps(service.status(), indent=2))
    except Exception:
        logging.error('Operation failed. Check configuration, login and upstream availability.')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
