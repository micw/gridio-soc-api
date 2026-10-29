"""Gridio read-only core, independent of HTTP and CLI.

Injected transport: callable(method, path, form=None, token=None) returning
(status_code, response_headers, decoded_json). Paths are relative to BASE_URL.
State is exclusively stored under Config.data_dir; no external sessions are read.
"""
from __future__ import annotations

import base64
import calendar
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import fcntl
import gzip
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

BASE_URL = "https://private.gridio.energy/api/v1"
BUSINESS_ID = "3d019e05-35c5-4d2f-9643-22866f2bf000"
STATIC_HEADERS = {
    "X-Client-Name": "gridioapp", "X-Client-Version": "4.15.7",
    "User-Agent": "4.15.7/1353 (Pixel 7a; 15)",
    "Accept": "application/json, text/plain, */*", "Accept-Encoding": "gzip, deflate",
}


class Unavailable(Exception):
    """No sufficiently fresh data is available (message contains no upstream secrets)."""


def duration(value):
    """Parse seconds or compound durations such as '1h30m', '30 days'."""
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = str(value).strip().lower()
        try:
            result = float(text)
        except ValueError:
            units = {'s': 1, 'sec': 1, 'second': 1, 'seconds': 1,
                     'm': 60, 'min': 60, 'minute': 60, 'minutes': 60,
                     'h': 3600, 'hour': 3600, 'hours': 3600,
                     'd': 86400, 'day': 86400, 'days': 86400}
            result, pos = 0., 0
            for match in re.finditer(r'\s*(\d+(?:\.\d+)?)\s*([a-z]+)\s*', text):
                if match.start() != pos or match[2] not in units:
                    raise ValueError('Invalid duration')
                result += float(match[1]) * units[match[2]]
                pos = match.end()
            if not pos or pos != len(text):
                raise ValueError('Invalid duration')
    if not math.isfinite(result) or result < 0:
        raise ValueError('Duration must be finite and nonnegative')
    return result


@dataclass
class Config:
    data_dir: Path = Path('/data')
    min_fetch_interval: float = 30
    poll_interval: float = 3600
    max_cache_age: float = 86400
    recording_enabled: bool = False
    recording_retention: float = 30 * 86400
    vehicle_id: str | None = None
    host: str = '0.0.0.0'
    port: int = 8080
    web_enabled: bool = False
    basic_auth_username: str | None = None
    basic_auth_password: str | None = None
    log_level: str = 'INFO'

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        for name in ('min_fetch_interval', 'poll_interval', 'max_cache_age',
                     'recording_retention',):
            setattr(self, name, duration(getattr(self, name)))
        if bool(self.basic_auth_username) != bool(self.basic_auth_password):
            raise ValueError('Both basic auth username and password are required')
        if self.min_fetch_interval < 30:
            raise ValueError('Fetch and recording intervals must be at least 30 seconds')
        if not 30 <= self.poll_interval <= 3600:
            raise ValueError('Poll interval must be between 30 and 3600 seconds')

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        values = {}
        for name in cls.__dataclass_fields__:
            key = 'GRIDIO_' + name.upper()
            value = env.get(key)
            if value is not None:
                if name in ('recording_enabled', 'web_enabled'):
                    if value.lower() not in ('true', 'false', '1', '0', 'yes', 'no', 'on', 'off'):
                        raise ValueError('Invalid boolean for ' + key)
                    value = value.lower() in ('true', '1', 'yes', 'on')
                elif name == 'port':
                    value = int(value)
                values[name] = value
        return cls(**values)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_transport(method, path, form=None, token=None):
    headers = dict(STATIC_HEADERS)
    headers['Content-Type'] = 'application/x-www-form-urlencoded' if form is not None else 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(BASE_URL + path, data=body, headers=headers, method=method)
    try:
        response = urllib.request.build_opener(_NoRedirect()).open(request, timeout=20)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        encoding = response.headers.get('Content-Encoding', '').lower()
        if encoding == 'gzip':
            raw = gzip.decompress(raw)
        elif encoding == 'deflate':
            raw = zlib.decompress(raw)
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            payload = {}
        return response.code, dict(response.headers), payload


def _claims(token):
    try:
        part = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
    except (ValueError, IndexError, TypeError):
        return {}


class Service:
    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport or http_transport
        self._mutex = threading.RLock()
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._state_path = config.data_dir / 'state.json'
        self._lock_path = config.data_dir / 'state.lock'
        self._state = {}

    @contextmanager
    def _locked(self):
        # Stable inode: never replace the lock file when committing state.
        with self._mutex:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    self._state = json.loads(self._state_path.read_text())
                except FileNotFoundError:
                    self._state = {}
                except (ValueError, UnicodeError):
                    raise Unavailable('Stored service state is invalid') from None
                before = json.dumps(self._state, sort_keys=True)
                try:
                    yield
                finally:
                    if json.dumps(self._state, sort_keys=True) != before:
                        self._save()
            finally:
                os.close(fd)

    def _save(self):
        fd, name = tempfile.mkstemp(prefix='.state-', dir=self.config.data_dir)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(self._state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._state_path)
            directory = os.open(self.config.data_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _call(self, method, path, form=None, token=None):
        if time.time() < self._state.get('retry_at', 0):
            raise Unavailable('Upstream rate limit backoff active')
        self._state['upstream_requests_total'] = self._state.get('upstream_requests_total', 0) + 1
        try:
            code, headers, payload = self.transport(method, path, form=form, token=token)
        except Exception:
            self._state['last_error'] = 'transport_error'
            self._state['upstream_errors_total'] = self._state.get('upstream_errors_total', 0) + 1
            self._state['retry_at'] = time.time() + 60
            raise Unavailable('Upstream request failed') from None
        if code >= 400:
            self._state['upstream_errors_total'] = self._state.get('upstream_errors_total', 0) + 1
        if code >= 500:
            self._state['retry_at'] = time.time() + 60
        if code == 429:
            retry = next((v for k, v in headers.items() if k.lower() == 'retry-after'), '60')
            try:
                delay = max(30, float(retry))
            except (TypeError, ValueError):
                try:
                    delay = max(30, parsedate_to_datetime(retry).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    delay = 60
            self._state['retry_at'] = time.time() + delay
            self._state['last_error'] = 'rate_limited'
            raise Unavailable('Upstream rate limited')
        return code, payload

    def _tokens(self, payload):
        if not isinstance(payload, dict) or not payload.get('access_token') or not payload.get('refresh_token'):
            raise Unavailable('Invalid token response')
        claims = _claims(payload['access_token'])
        self._state.update(access_token=payload['access_token'], refresh_token=payload['refresh_token'],
                           expires_at=claims.get('exp', time.time() + float(payload.get('expires_in', 900))),
                           customer_id=claims.get('sub'), auth_state='authenticated', last_error=None,
                           token_generation=self._state.get('token_generation', 0) + 1)
        self._save()  # Rotation must survive even a later failed data request.

    def request_otp(self, email):
        with self._locked():
            if time.time() - self._state.get('otp_attempt', -1e20) < 60:
                raise Unavailable('OTP cooldown active')
            self._state['otp_attempt'] = time.time()
            code, _ = self._call('POST', '/oauth2/otp/request',
                                 {'username': email.strip().lower(), 'business_id': BUSINESS_ID, 'language': 'en'})
            if code != 200:
                raise Unavailable('OTP request rejected')
            return {'ok': True}

    def verify_otp(self, email, code):
        with self._locked():
            status, payload = self._call('POST', '/oauth2/token',
                {'grant_type': 'otp', 'username': email.strip().lower(), 'code': str(code),
                 'business_id': BUSINESS_ID, 'language': 'en'})
            if status != 200:
                raise Unavailable('OTP verification rejected')
            if not isinstance(payload, dict) or not payload.get('access_token') or not payload.get('refresh_token'):
                raise Unavailable('Invalid token response')
            self._state.clear()  # Never expose a previous account's vehicles or cache.
            self._tokens(payload)
            return self._auth_status()

    def _refresh(self, generation=None):
        # All callers hold the process-shared lock and have reloaded state. A
        # generation check additionally avoids rotating a token already replaced.
        if generation is not None and generation != self._state.get('token_generation', 0):
            return
        refresh = self._state.get('refresh_token')
        if not refresh:
            self._state['auth_state'] = 'login_required'
            raise Unavailable('Login required')
        try:
            status, payload = self._call('POST', '/oauth2/token',
                                        {'grant_type': 'refresh_token', 'refresh_token': refresh})
        except Unavailable:
            self._state['auth_state'] = 'temporary_error'
            raise
        if status != 200:
            self._state['auth_state'] = 'login_required' if status in (400, 401, 403) else 'refresh_failed'
            self._state['last_error'] = 'refresh_failed'
            raise Unavailable('Token refresh failed')
        self._tokens(payload)

    def _get(self, path):
        generation = self._state.get('token_generation', 0)
        status, payload = self._call('GET', path, token=self._state['access_token'])
        if status == 401:
            self._refresh(generation)
            status, payload = self._call('GET', path, token=self._state['access_token'])
        if status != 200:
            if status in (401, 403):
                self._state['auth_state'] = 'login_required'
            self._state['last_error'] = 'upstream_error'
            raise Unavailable('Upstream data unavailable')
        self._state['auth_state'] = 'authenticated'
        return payload

    def _devices(self):
        now = time.time()
        if now - self._state.get('devices_attempt', 0) >= self.config.min_fetch_interval:
            self._state['devices_attempt'] = now
            self._ensure_auth()
            payload = self._get('/customers/' + urllib.parse.quote(str(self._state['customer_id']), safe='') + '/devices')
            devices = payload.get('data', []) if isinstance(payload, dict) else payload
            if not isinstance(devices, list):
                raise Unavailable('Invalid vehicle response')
            self._state['devices'] = [d for d in devices if isinstance(d, dict) and
                (d.get('__typename') == 'Vehicle' or d.get('dev_type') == 'Electric vehicle')]
        return self._state.get('devices', [])

    def _ensure_auth(self):
        if not self._state.get('access_token') or self._state.get('expires_at', 0) <= time.time() + 60:
            self._refresh()
        if not self._state.get('customer_id'):
            raise Unavailable('Token has no customer identity')

    def vehicles(self):
        with self._locked():
            return self._devices()

    def _select(self, vehicle_id):
        selected = vehicle_id or self.config.vehicle_id
        if selected:
            return str(selected)
        devices = self._state.get('devices') or self._devices()
        if len(devices) != 1 or not devices[0].get('id'):
            raise Unavailable('Configure a vehicle ID unless exactly one vehicle exists')
        return str(devices[0]['id'])

    def metrics(self, vehicle_id=None):
        with self._locked():
            return self._metrics(vehicle_id)

    def _metrics(self, vehicle_id=None):
        selected = self._select(vehicle_id)
        now = time.time()
        caches = self._state.setdefault('metrics', {})
        entry = caches.setdefault(selected, {})
        fetched = False
        error = None
        if now - entry.get('attempt', 0) >= self.config.min_fetch_interval:
            entry['attempt'] = now  # Persisted even if auth, transport or parsing fails.
            entry['attempts_total'] = entry.get('attempts_total', 0) + 1
            try:
                self._ensure_auth()
                cid = urllib.parse.quote(str(self._state['customer_id']), safe='')
                did = urllib.parse.quote(selected, safe='')
                raw = self._get(f'/customers/{cid}/devices/{did}/metrics/latest')
                data = raw.get('data', raw) if isinstance(raw, dict) else None
                if not isinstance(data, dict):
                    raise Unavailable('Invalid metrics response')
                soc = data.get('state_of_charge')
                if isinstance(soc, bool) or not isinstance(soc, (float, int)) or not math.isfinite(soc) or not 0 <= soc <= 100:
                    raise Unavailable('State of charge missing or invalid')
                power = data.get('capacity_available')
                if isinstance(power, bool) or not isinstance(power, (float, int)) or not math.isfinite(power):
                    power = None
                result = dict(soc=soc, power_w=power, status=data.get('device_status'),
                              source_timestamp=data.get('latest_data_point'), fetched_at=time.time())
                entry['value'] = result
                entry['last_attempt_success'] = True
                fetched = True
                self._state['last_error'] = None
                if self.config.recording_enabled:
                    self._record(selected, raw, result['fetched_at'], request_started_at=now,
                                 response_at=result['fetched_at'], status=200)
            except Unavailable as exc:
                entry['last_attempt_success'] = False
                self._state['last_error'] = self._state.get('last_error') or 'metrics_unavailable'
                if self.config.recording_enabled:
                    self._record(selected, {'error': 'metrics_unavailable', 'request_started_at': now,
                                            'response_at': time.time()}, time.time())
                error = exc
        value = entry.get('value')
        if value is None or now - value['fetched_at'] > self.config.max_cache_age:
            raise error or Unavailable('No fresh cached metrics available')
        return dict(value, available=True, cached=not fetched)

    def _record(self, vehicle, raw, now, **metadata):
        directory = self.config.data_dir / 'recordings'
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (time.strftime('%Y-%m-%d', time.gmtime(now)) + '.jsonl')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as stream:
            stream.write(json.dumps(dict(vehicle_id=vehicle, fetched_at=now, raw=raw, **metadata)) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        for old in directory.glob('*.jsonl'):
            try:
                day = calendar.timegm(time.strptime(old.stem, '%Y-%m-%d'))
            except ValueError:
                continue
            if day + 86400 < now - self.config.recording_retention:
                old.unlink()

    def _auth_status(self):
        return {'authenticated': self._state.get('auth_state') == 'authenticated',
                'state': self._state.get('auth_state', 'login_required'),
                'expires_at': self._state.get('expires_at'),
                'refresh_available': bool(self._state.get('refresh_token'))}

    def auth_status(self):
        with self._locked():
            return self._auth_status()

    def status(self):
        with self._locked():
            return {'auth': self._auth_status(), 'last_error': self._state.get('last_error'),
                    'retry_at': self._state.get('retry_at'), 'recording_enabled': self.config.recording_enabled,
                    'vehicles_cached': len(self._state.get('devices', []))}

    def tick(self):
        with self._locked():
            interval = self.config.poll_interval
            selected = self.config.vehicle_id
            devices = self._state.get('devices', [])
            if not selected and len(devices) == 1:
                selected = str(devices[0]['id'])
            attempt = self._state.get('metrics', {}).get(selected, {}).get('attempt', -1e20)
            last = max(attempt, self._state.get('tick_attempt', -1e20))
            if time.time() - last < interval:
                return None
            self._state['tick_attempt'] = time.time()
            try:
                # Session maintenance must also work before vehicle selection.
                self._ensure_auth()
                return self._metrics()
            except Unavailable:
                return None

    def prometheus(self):
        """Render a persisted snapshot; scraping never makes an upstream request."""
        with self._locked():
            auth = self._auth_status()
            caches = self._state.get('metrics', {})
            selected = self.config.vehicle_id
            if not selected and len(self._state.get('devices', [])) == 1:
                selected = str(self._state['devices'][0]['id'])
            entry = caches.get(selected, {})
            if not selected and len(caches) == 1:
                entry = next(iter(caches.values()))
            value = entry.get('value')
            available = bool(value and time.time() - value['fetched_at'] <= self.config.max_cache_age)
            lines = ['gridio_available ' + str(int(available)),
                     'gridio_auth_authenticated ' + str(int(auth['authenticated'])),
                     'gridio_auth_login_required ' + str(int(auth['state'] == 'login_required')),
                     'gridio_upstream_last_attempt_success ' + str(int(entry.get('last_attempt_success', False))),
                     'gridio_upstream_last_attempt_seconds ' + str(entry.get('attempt', 0)),
                     'gridio_upstream_attempts_total ' + str(entry.get('attempts_total', 0)),
                     'gridio_error ' + str(int(bool(self._state.get('last_error')))),
                     'gridio_auth_refresh_failed ' + str(int(auth['state'] in ('refresh_failed', 'temporary_error'))),
                     'gridio_upstream_requests_total ' + str(self._state.get('upstream_requests_total', 0)),
                     'gridio_upstream_errors_total ' + str(self._state.get('upstream_errors_total', 0))]
            for reason in ('transport_error', 'rate_limited', 'refresh_failed', 'upstream_error', 'metrics_unavailable'):
                lines.append('gridio_error_reason{reason="' + reason + '"} ' +
                             str(int(self._state.get('last_error') == reason)))
            if value:
                lines.append('gridio_fetched_at_seconds ' + str(value['fetched_at']))
                try:
                    from datetime import datetime
                    source = datetime.fromisoformat(value['source_timestamp'].replace('Z', '+00:00'))
                    lines.append('gridio_source_timestamp_seconds ' + str(source.timestamp()))
                except (TypeError, ValueError, AttributeError):
                    pass
            if available:
                lines.append('gridio_soc_percent ' + str(value['soc']))
                if value['power_w'] is not None:
                    lines.append('gridio_power_watts ' + str(value['power_w']))
            return '\n'.join(lines) + '\n'
