# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#     "httpx>=0.28,<0.29",
#     "python-dotenv>=1.2.4,<2",
#     "questionary>=2.1,<3",
#     "yadisk[async-defaults]==3.2.0",
# ]
# ///

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import math
import ntpath
import os
import posixpath
import re
import stat
import sys
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from random import uniform
from tempfile import NamedTemporaryFile
from threading import Event, Lock, Thread
from time import monotonic, time
from types import TracebackType
from urllib.parse import parse_qs, quote, quote_plus

import httpx
import questionary
from dotenv import load_dotenv, set_key
from yadisk import AsyncClient
from yadisk.exceptions import RequestTimeoutError, YaDiskConnectionError
from yadisk.objects import DiskInfoObject, ResourceObject
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession


VERSION = '0.2.0'
MAX_STREAMS = 10
STATUS_INTERVAL = 30.0
TOKEN_LIFETIME = 3600.0
TOKEN_REFRESH_MARGIN = 300.0
TOKEN_REFRESH_RETRY_INTERVAL = 30.0
RETRY_DELAYS = (2, 4, 8)
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
NETWORK_ERRORS = (
    TimeoutError, ConnectionError, RequestTimeoutError, YaDiskConnectionError,
    httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
    httpx.ProxyError, httpx.DecodingError,
)

class LogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return message.replace('\n', f'\n{record.asctime} - ')


log = logging.getLogger('Downloader')
log.setLevel(logging.DEBUG)
log_handler = logging.StreamHandler(sys.stdout)
log_handler.setFormatter(LogFormatter('%(asctime)s - %(message)s'))
log.addHandler(log_handler)

sem = asyncio.Semaphore(MAX_STREAMS)


INVALID_NAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    f'{prefix}{digit}' for prefix in ('COM', 'LPT') for digit in '123456789¹²³'
}
MAX_NAME_BYTES = 240
EMAIL_PATTERN = re.compile(r'[^@\s<>"/\\,;\x00-\x1f\x7f]+@[^@\s<>"/\\,;\x00-\x1f\x7f]+')


class DownloadIntegrityError(Exception):
    pass


class UnsafeDestinationError(OSError):
    pass


class PathMappingError(ValueError):
    pass


class DestinationBusyError(OSError):
    pass


def sensitive_field(name: str) -> bool:
    name = re.sub(r'[-_\s]', '', name.casefold())
    return (name in {'authorization', 'proxyauthorization', 'cookie', 'setcookie', 'apikey',
                     'secret', 'password', 'passwd', 'credential', 'signature', 'sig'}
            or name.endswith(('token', 'secret', 'password', 'signature', 'credential', 'apikey'))
            or name.startswith(('xamz', 'xgoog')))


def response_diagnostic(response: httpx.Response) -> str:
    secrets: set[str] = set()
    content: str | None = None
    payload = None
    parsed_json = False

    def collect_json(value) -> None:
        pending = [(value, False)]
        while pending:
            item, sensitive = pending.pop()
            if isinstance(item, dict):
                pending.extend((child, sensitive or sensitive_field(key)) for key, child in item.items())
            elif isinstance(item, list):
                pending.extend((child, sensitive) for child in item)
            elif sensitive and isinstance(item, str) and item:
                secrets.add(item)

    def collect_fields(fields) -> None:
        for key, value in fields:
            if sensitive_field(key) and value:
                secrets.add(value)
                if key.casefold().endswith('authorization'):
                    secrets.add(value.partition(' ')[2])
                if 'cookie' in key.casefold():
                    secrets.update(part.partition('=')[2].strip() for part in value.split(';') if '=' in part)

    try:
        content = response.text
    except httpx.ResponseNotRead:
        pass
    if content is not None:
        try:
            payload = json.loads(content)
            parsed_json = True
            collect_json(payload)
        except (ValueError, RecursionError):
            pass
        for match in re.finditer(r'(?s)(?=<([A-Za-z_][\w.:-]*)\b[^>]*>(.*?)</\1\s*>)', content):
            if sensitive_field(match[1].rsplit(':', 1)[-1]):
                value = match[2].strip()
                if value:
                    secrets.update((value, unescape(value)))
    collect_fields(response.headers.multi_items())
    try:
        request = response.request
    except RuntimeError:
        request = None
    if request is not None:
        collect_fields(request.headers.multi_items())
        collect_fields(request.url.params.multi_items())
        content_type = request.headers.get('Content-Type', '').split(';')[0].strip().casefold()
        if content_type == 'application/x-www-form-urlencoded':
            try:
                form = parse_qs(request.content.decode('utf-8'))
                collect_fields((key, value) for key, values in form.items() for value in values)
            except (httpx.RequestNotRead, UnicodeError, ValueError):
                pass
        elif content_type == 'application/json':
            try:
                collect_json(json.loads(request.content))
            except (httpx.RequestNotRead, ValueError, RecursionError):
                pass
    replacements = sorted({encoded for value in secrets if value
                           for encoded in (value, quote(value, safe='', errors='replace'), quote_plus(value, errors='replace'),
                                           escape(value), json.dumps(value, ensure_ascii=False)[1:-1],
                                           json.dumps(value, ensure_ascii=True)[1:-1])}, key=len, reverse=True)

    def redact_text(value: str) -> str:
        value = re.sub(r'https?:(?:\\?/){2}[^\s<>"\']+', '[URL скрыт]', value, flags=re.IGNORECASE)
        for secret in replacements:
            value = value.replace(secret, '[скрыто]')
        value = re.sub(r'\b(?:Bearer|OAuth|Api-Key)\s+[^\s,;}\]]+', '[авторизация скрыта]', value, flags=re.IGNORECASE)
        value = re.sub(
            r'(?i)(\b(?:client_secret|access_token|refresh_token|id_token|subject_token|password|api_key|token|secret|signature|credential)\b["\']?\s*[:=]\s*)'
            r'(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;}\]]+)', r'\1[скрыто]', value,
        )
        value = re.sub(r'\b(?:y[01]_[\w.-]+|t[01]\.[\w.-]+|AQAD[\w.-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b', '[токен скрыт]', value)
        return value

    def redact_json(value):
        if isinstance(value, dict):
            return {redact_text(key): '[скрыто]' if sensitive_field(key) else redact_json(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [redact_json(item) for item in value]
        return redact_text(value) if isinstance(value, str) else value

    headers = json.dumps(redact_json(dict(response.headers)), ensure_ascii=False)
    if content is None:
        failure = response.extensions.get('downloader_body_error')
        unavailable = 'тело ответа недоступно'
        if isinstance(failure, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', failure):
            unavailable += f' (ошибка чтения: {failure})'
        return f'заголовки ответа={headers}; тело ответа={unavailable}'
    try:
        body = json.dumps(redact_json(payload) if parsed_json else redact_text(content), ensure_ascii=False)
    except RecursionError:
        body = json.dumps(redact_text(content), ensure_ascii=False)
    return f'заголовки ответа={headers}; тело ответа={body}'


def sanitized_error(error: BaseException) -> str:
    details = [type(error).__name__]
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, 'response', None)
        response = getattr(response, '_response', response)
        status = getattr(response, 'status_code', getattr(response, 'status', None))
        if isinstance(status, int) and 100 <= status <= 599:
            details.append(f'HTTP {status}')
        code = getattr(current, 'error_type', None)
        if code is None and isinstance(response, httpx.Response) and response.is_stream_consumed:
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    code = payload.get('error')
            except (ValueError, RecursionError, httpx.ResponseNotRead):
                pass
        if isinstance(code, str) and (
            re.fullmatch(r'[A-Z][A-Za-z0-9]{0,79}Error', code)
            or code in {
                'invalid_request', 'invalid_client', 'invalid_grant', 'invalid_scope',
                'unauthorized_client', 'unsupported_grant_type', 'access_denied',
                'temporarily_unavailable', 'server_error', 'unsupported_token_type',
            }
        ):
            details.append(code)
        if isinstance(response, httpx.Response):
            details.append(response_diagnostic(response))
        if isinstance(current, OSError) and isinstance(current.errno, int):
            details.append(f'errno={current.errno}')
        if isinstance(current, TokenRefreshError):
            details.append(current.diagnostic)
        current = current.__cause__
    return '; '.join(dict.fromkeys(value for value in details if value))


def retry_after_seconds(value: str | None) -> float:
    if value is None:
        return 0.0
    value = value.strip()
    try:
        if re.fullmatch(r'[0-9]+', value):
            delay = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - time()
    except (ValueError, TypeError, OverflowError):
        return 0.0
    return max(0.0, delay) if math.isfinite(delay) else 0.0


def retry_details(error: Exception) -> tuple[bool, float, str]:
    current: BaseException | None = error
    seen: set[int] = set()
    network_error = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, 'response', None)
        response = getattr(response, '_response', response)
        status = getattr(response, 'status_code', getattr(response, 'status', None))
        if status is not None:
            headers = getattr(response, 'headers', {})
            return status in RETRY_STATUSES, retry_after_seconds(headers.get('Retry-After')), f'HTTP {status}'
        network_error |= isinstance(current, (*NETWORK_ERRORS, DownloadIntegrityError))
        current = current.__cause__
    return network_error, 0.0, type(error).__name__


async def with_retries[T](operation: Callable[[], Awaitable[T]], label: str) -> T:
    max_retries = retry_limit()
    attempt = 0
    while True:
        try:
            return await operation()
        except Exception as error:
            retryable, retry_after, _ = retry_details(error)
            if not retryable or attempt == max_retries:
                raise
            base = RETRY_DELAYS[attempt]
            delay = max(base + uniform(0.0, base * 0.25), retry_after)
            attempt += 1
            log.warning('Повтор %s/%s через %.2f с: %s (%s)',
                        attempt, max_retries, delay, label, sanitized_error(error))
            await asyncio.sleep(delay)


async def check_transient_response(response: httpx.Response) -> None:
    if not response.is_error:
        return
    try:
        await response.aread()
    except Exception as error:
        response.extensions['downloader_body_error'] = type(error).__name__
        response.raise_for_status()
    if response.status_code == 404 and response.request.url.path == '/v1/disk/operations/0000':
        # SDK проверяет токен запросом несуществующей операции.
        try:
            payload = response.json()
        except (ValueError, RecursionError):
            payload = None
        if isinstance(payload, dict) and payload.get('error') == 'DiskOperationNotFoundError':
            return
    response.raise_for_status()


@dataclass(frozen=True)
class ServiceToken:
    access_token: str = field(repr=False)
    expires_at: float
    refresh_at: float
    expires_at_wall: float = math.inf
    refresh_at_wall: float = math.inf


async def get_service_token(client_id: str, client_secret: str, email: str) -> ServiceToken:
    async with httpx.AsyncClient(timeout=30, event_hooks={'response': [check_transient_response]}) as client:
        async def request() -> ServiceToken:
            started = monotonic()
            started_wall = time()
            response = await client.post('https://oauth.yandex.ru/token', data={
                'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
                'client_id': client_id,
                'client_secret': client_secret,
                'subject_token': email,
                'subject_token_type': 'urn:yandex:params:oauth:token-type:email',
            })
            response.raise_for_status()
            payload = response.json()
            token = payload['access_token']
            if not isinstance(token, str) or not token:
                raise ValueError('Invalid service app token response')
            lifetime = payload.get('expires_in', TOKEN_LIFETIME)
            if isinstance(lifetime, bool):
                raise ValueError('Invalid service app token lifetime')
            try:
                lifetime = float(lifetime)
            except (TypeError, ValueError, OverflowError):
                raise ValueError('Invalid service app token lifetime') from None
            if not math.isfinite(lifetime) or lifetime <= 0:
                raise ValueError('Invalid service app token lifetime')
            expires_at = started + lifetime
            expires_at_wall = started_wall + lifetime
            if expires_at <= monotonic() or expires_at_wall <= time():
                raise ValueError('Service app token has already expired')
            margin = min(TOKEN_REFRESH_MARGIN, lifetime * 0.1)
            return ServiceToken(token, expires_at, expires_at - margin,
                                expires_at_wall, expires_at_wall - margin)

        return await with_retries(request, 'получение токена')


class TokenRefreshError(Exception):
    def __init__(self, diagnostic: str = '', *, terminal: bool = False):
        self.diagnostic = diagnostic
        self.terminal = terminal
        super().__init__('Нет действующего сервисного токена')


class ServiceTokenManager:
    def __init__(self, client_id: str, client_secret: str, email: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._email = email
        self._token: ServiceToken | None = None
        self._next_refresh_at = 0.0
        self._next_refresh_at_wall = 0.0
        self._failure: str | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> 'ServiceTokenManager':
        await self.get_token()
        self._task = asyncio.create_task(self._run(), name='downloader-token-refresh')
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

    def _valid_token(self) -> bool:
        return (self._token is not None and monotonic() < self._token.expires_at
                and time() < self._token.expires_at_wall)

    def _refresh_delay(self) -> float:
        return max(0.0, min(self._next_refresh_at - monotonic(),
                            self._next_refresh_at_wall - time()))

    async def get_token(self) -> str:
        async with self._lock:
            if self._failure is not None:
                raise TokenRefreshError(self._failure, terminal=True) from None
            delay = self._refresh_delay()
            if delay > 0:
                if self._valid_token():
                    return self._token.access_token
                log.warning('Ожидание восстановления сервисного токена: %.0f с', delay)
                await asyncio.sleep(delay)
            refreshing = self._token is not None
            if refreshing:
                log.info('Обновление сервисного токена...')
            try:
                token = await get_service_token(self._client_id, self._client_secret, self._email)
            except Exception as error:
                reason = sanitized_error(error)
                retryable, retry_after, _ = retry_details(error)
                cooldown = max(TOKEN_REFRESH_RETRY_INTERVAL, retry_after)
                self._next_refresh_at = monotonic() + cooldown
                self._next_refresh_at_wall = time() + cooldown
                if retryable and self._valid_token():
                    log.warning('Не удалось обновить сервисный токен (%s); следующая проверка через %.0f с',
                                reason, cooldown)
                    return self._token.access_token
                if retryable:
                    log.error('Нет действующего токена (%s); следующая общая попытка через %.0f с',
                              reason, cooldown)
                else:
                    self._failure = reason
                    log.error('Восстановление авторизации завершилось постоянной ошибкой (%s)', reason)
                raise TokenRefreshError(reason, terminal=not retryable) from None
            self._token = token
            self._next_refresh_at = token.refresh_at
            self._next_refresh_at_wall = token.refresh_at_wall
            log.info('Сервисный токен %s; обновление через %.0f с',
                     'обновлён' if refreshing else 'получен', max(0, token.refresh_at - monotonic()))
            return token.access_token

    async def authorize(self, request: httpx.Request) -> None:
        if request.headers.get('Authorization', '').startswith('OAuth '):
            request.headers['Authorization'] = f'OAuth {await self.get_token()}'

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(TOKEN_REFRESH_RETRY_INTERVAL, self._refresh_delay()))
                return
            except TimeoutError:
                pass
            try:
                await self.get_token()
            except TokenRefreshError as error:
                if error.terminal:
                    return


def disk_session(tokens: ServiceTokenManager | None = None) -> AsyncHTTPXSession:
    hooks = {'response': [check_transient_response]}
    if tokens is not None:
        hooks['request'] = [tokens.authorize]
    return AsyncHTTPXSession(event_hooks=hooks)


class SharedDiskClient(AsyncClient):
    def __init__(self, token: str, vdhash: str, tokens: ServiceTokenManager | None = None):
        self.vdhash = validate_vdhash(vdhash)
        session = disk_session(tokens)
        self.http = session.httpx_session
        super().__init__(token=token, session=session, default_args={
            'n_retries': 0, 'headers': {'Authorization': f'OAuth {token}'},
        })

    def inner_path(self, path: str) -> str:
        if path.startswith('vd:'):
            for prefix in (f'vd:{self.vdhash}:disk:', f'vd:/{self.vdhash}/disk'):
                if path == prefix or path.startswith(prefix + '/'):
                    path = path[len(prefix):] or '/'
                    break
            else:
                raise ValueError('Resource belongs to a different shared disk')
        path = path.removeprefix('disk:')
        if not path.startswith('/'):
            raise ValueError('Invalid shared disk resource path')
        return path

    async def get_json(self, resource: str, params: dict[str, str | int]) -> dict:
        response = await self.http.get(
            f'https://cloud-api.yandex.net/v1/disk/{resource}', params=params,
            headers={'Authorization': f'OAuth {self.token}'}, timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('Invalid shared disk response')
        return payload

    async def get_disk_info(self, **kwargs) -> DiskInfoObject:
        return DiskInfoObject(await self.get_json('virtual-disks', {'vd_hash': self.vdhash}))

    async def listdir(self, path: str, /, **kwargs) -> AsyncIterator[ResourceObject]:
        offset = 0
        while True:
            payload = await self.get_json('virtual-disks/resources', {
                'path': f'vd:{self.vdhash}:disk:{self.inner_path(path)}',
                'limit': 500, 'offset': offset, 'sort': 'path',
            })
            embedded = payload.get('_embedded')
            if payload.get('type') != 'dir' or not isinstance(embedded, dict):
                raise ValueError('Invalid shared disk directory response')
            items, total = embedded.get('items'), embedded.get('total')
            if not isinstance(items, list) or type(total) is not int or total < 0:
                raise ValueError('Invalid shared disk listing')
            if not items and offset < total:
                raise ValueError('Incomplete shared disk listing')
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get('path'), str) or item.get('type') not in ('file', 'dir'):
                    raise ValueError('Invalid shared disk resource')
                yield ResourceObject({**item, 'path': 'disk:' + self.inner_path(item['path'])})
            offset += len(items)
            if offset >= total:
                break

    async def get_download_link(self, path: str, /, **kwargs) -> str:
        payload = await self.get_json('virtual-disks/resources/download', {
            'path': f'vd:{self.vdhash}:disk:{self.inner_path(path)}',
        })
        href = payload.get('href')
        if not isinstance(href, str) or httpx.URL(href).scheme != 'https' or not httpx.URL(href).host:
            raise ValueError('Invalid shared disk download link')
        return href


class ProgressReporter:
    def __init__(self, email: str, interval: float):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError('Status interval must be a positive number')
        self.email = email
        self.interval = interval
        self._started = monotonic()
        self._last_result = self._started
        self._phase = 'получение токена'
        self._current = ''
        self._total_files = 0
        self._counts: dict[str, int] = {}
        self._active: dict[str, float] = {}
        self._lock = Lock()
        self._stop = Event()
        self._thread = Thread(target=self._run, name='downloader-status', daemon=True)

    def __enter__(self) -> 'ProgressReporter':
        self._thread.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None,
        exc_value: BaseException | None, traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()

    def phase(self, value: str) -> None:
        with self._lock:
            self._phase = value
            self._current = ''
            self._last_result = monotonic()

    def current(self, path: str) -> None:
        with self._lock:
            self._current = path

    def count(self, **values: int) -> None:
        with self._lock:
            for name, amount in values.items():
                self._counts[name] = self._counts.get(name, 0) + amount
            self._last_result = monotonic()

    def total_files(self, total: int) -> None:
        with self._lock:
            self._total_files = total

    def start_file(self, path: str) -> None:
        with self._lock:
            self._active[path] = monotonic()

    def finish_file(self, path: str, success: bool | None, *, skipped: bool = False) -> None:
        with self._lock:
            self._active.pop(path, None)
            if success is not None:
                key = ('skipped' if skipped else 'downloaded') if success else 'failed_files'
                self._counts[key] = self._counts.get(key, 0) + 1
                self._last_result = monotonic()

    @property
    def skipped_files(self) -> int:
        with self._lock:
            return self._counts.get('skipped', 0)

    def report(self) -> None:
        with self._lock:
            phase, current = self._phase, self._current
            counts, active = self._counts.copy(), self._active.copy()
            total = self._total_files
            now = monotonic()
            elapsed, idle = now - self._started, now - self._last_result
        details = ''
        if phase == 'поиск файлов и папок':
            details = (
                f" | папок найдено: {counts.get('directories', 0)}, просмотрено: {counts.get('directories_scanned', 0)}"
                f" | файлов найдено: {counts.get('files', 0)}, ошибок чтения: {counts.get('failed_listings', 0)}"
            )
        elif phase == 'подготовка путей':
            details = f" | путей подготовлено: {counts.get('paths', 0)}"
        elif phase == 'создание папок':
            details = f" | создано или уже существует: {counts.get('prepared', 0)}, ошибок: {counts.get('failed_directories', 0)}"
        elif phase == 'скачивание':
            downloaded, failed = counts.get('downloaded', 0), counts.get('failed_files', 0)
            skipped = counts.get('skipped', 0)
            completed = downloaded + skipped + failed
            percent = completed / total * 100 if total else 100
            queued = max(0, total - completed - len(active))
            details = (
                f' | файлов завершено: {completed}/{total} ({percent:.1f}%)'
                f' | скачано: {downloaded}, ошибок: {failed}'
                f', уже скачано: {skipped}'
                f' | в работе: {len(active)}, в очереди: {queued}'
            )
        if active:
            oldest = min(active, key=active.__getitem__)
            details += f' | самый долгий файл: {oldest!r} ({now - active[oldest]:.0f} с)'
        elif current:
            details += f' | текущий путь: {current!r}'
        details = details.removeprefix(' | ')
        if details:
            details += ' | '
        log.info('Статус %s | этап: %s | прошло: %.0f с\n%sбез новых результатов: %.0f с',
                 self.email, phase, elapsed, details, idle)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.report()


def positive_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Status interval must be a positive number') from None
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError('Status interval must be a positive number')
    return interval


def validate_email(value: str) -> str:
    email = value.strip()
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError('Invalid email address')
    return email


def validate_vdhash(value: str) -> str:
    vdhash = value.strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', vdhash):
        raise ValueError('Invalid VDHASH; use the disk ID from /vd/<VDHASH>/ in its URL')
    return vdhash


def read_csv_values(
    path: Path, column_name: str, validator: Callable[[str], str],
    key: Callable[[str], str], empty_label: str,
) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    with path.open(encoding='utf-8-sig', newline='') as source:
        first_line = next((line for line in source if line.strip()), '')
        source.seek(0)
        try:
            delimiter = csv.Sniffer().sniff(first_line, delimiters=',;').delimiter
        except csv.Error:
            delimiter = ','
        reader = csv.reader(source, delimiter=delimiter, strict=True)
        value_column: int | None = None
        first_row = True
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            if first_row:
                first_row = False
                columns = [cell.strip().casefold().replace('_', '') for cell in row]
                if columns.count(column_name) > 1:
                    raise ValueError(f'CSV contains more than one {column_name} column')
                if column_name in columns:
                    value_column = columns.index(column_name)
                    continue
            if value_column is None and len(row) != 1:
                raise ValueError(f'CSV line {reader.line_num}: expected one {column_name} or a {column_name} column header')
            column = value_column if value_column is not None else 0
            if column >= len(row):
                raise ValueError(f'CSV line {reader.line_num}: missing {column_name} column')
            try:
                value = validator(row[column])
            except ValueError:
                raise ValueError(f'CSV line {reader.line_num}: invalid or empty {column_name}') from None
            identifier = key(value)
            if identifier not in seen:
                seen.add(identifier)
                values.append(value)
    if not values:
        raise ValueError(f'CSV contains no {empty_label}')
    return values


def read_users_csv(path: Path) -> list[str]:
    return read_csv_values(path, 'email', validate_email, str.casefold, 'users')


def read_shared_disks_csv(path: Path) -> list[str]:
    return read_csv_values(path, 'vdhash', validate_vdhash, lambda value: value, 'disks')


def shorten_name(name: str, suffix: str = '') -> str:
    stem, dot, extension = name.rpartition('.')
    if not stem or len(extension.encode('utf-8')) > 32:
        stem, dot, extension = name, '', ''
    ending = suffix + dot + extension
    budget = MAX_NAME_BYTES - len(ending.encode('utf-8'))
    stem = stem.encode('utf-8')[:budget].decode('utf-8', errors='ignore').rstrip(' .')
    return (stem or '_') + ending


def normalize_name(name: str) -> str:
    name = unicodedata.normalize('NFC', name)
    name = INVALID_NAME_CHARACTERS.sub('_', name).rstrip(' .') or '_'
    if name.split('.')[0].rstrip(' ').upper() in RESERVED_NAMES:
        name = '_' + name
    return shorten_name(name)


def windows_long_path(path: str) -> str:
    if path.startswith('\\\\?\\'):
        return path
    if path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path


class LocalPaths:
    def __init__(self, root: Path, *, persistent: bool = False, state_dir: Path | None = None):
        self.root = root
        self._paths: dict[tuple[str, ...], Path] = {(): root}
        self._used: set[tuple[Path, str]] = set()
        self._persistent = persistent
        self._loaded = False
        self._lock_file = None
        self._protect_existing: set[tuple[str, ...]] = set()
        self._state_dir = state_dir if state_dir is not None else root.parent / '.disk-downloader'
        scope = str(root.relative_to(self._state_dir.parent))
        self._state_path = self._state_dir / (hashlib.sha256(scope.encode()).hexdigest() + '.json')
        if self._state_dir.parent == root:
            self._used.add((root, self._state_dir.name.casefold()))

    def __enter__(self) -> 'LocalPaths':
        if not self._persistent:
            return self
        ensure_safe_path(self._state_dir, self._state_dir.parent, include_leaf=True)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._state_path.with_suffix('.lock')
        ensure_safe_path(lock_path, self._state_dir, include_leaf=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        self._lock_file = os.fdopen(descriptor, 'r+b')
        try:
            try:
                if os.name == 'nt':
                    import msvcrt
                    if self._lock_file.seek(0, os.SEEK_END) == 0:
                        self._lock_file.write(b'\0')
                        self._lock_file.flush()
                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, PermissionError) as error:
                raise DestinationBusyError('Download directory is locked by another process') from error
            self._load()
        except BaseException:
            self._lock_file.close()
            self._lock_file = None
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    def _load(self) -> None:
        ensure_safe_path(self._state_path, self._state_dir, include_leaf=True)
        if not self._state_path.exists():
            return
        with self._state_path.open(encoding='utf-8') as source:
            payload = json.load(source)
        if not isinstance(payload, dict) or payload.get('version') != 1 or not isinstance(payload.get('paths'), list):
            raise PathMappingError('Invalid saved path mapping')
        entries = []
        for entry in payload['paths']:
            if not isinstance(entry, dict):
                raise PathMappingError('Invalid saved path mapping')
            remote, local = entry.get('remote'), entry.get('local')
            if not isinstance(remote, list) or not isinstance(local, list) or not remote or len(remote) != len(local):
                raise PathMappingError('Invalid saved path mapping')
            if any(not isinstance(part, str) or not part or '/' in part or '\x00' in part for part in remote):
                raise PathMappingError('Invalid saved remote path')
            if any(not isinstance(part, str) or not part or normalize_name(part) != part for part in local):
                raise PathMappingError('Invalid saved local path')
            entries.append((tuple(remote), self.root.joinpath(*local)))
        for key, path in sorted(entries, key=lambda entry: len(entry[0])):
            if key in self._paths or path.parent != self._paths.get(key[:-1]) or (path.parent, path.name.casefold()) in self._used:
                raise PathMappingError('Conflicting saved path mapping')
            self._paths[key] = path
            self._used.add((path.parent, path.name.casefold()))
        self._loaded = True

    def prepare(self, remotes: Iterable[str]) -> None:
        keys = set()
        for remote in remotes:
            parts = self._parts(remote)
            keys.update(parts[:depth] for depth in range(1, len(parts) + 1))
        groups: dict[tuple[tuple[str, ...], str], int] = {}
        for key in keys:
            group = key[:-1], normalize_name(key[-1]).casefold()
            groups[group] = groups.get(group, 0) + 1
        for key in sorted(keys):
            parent = self._paths[key[:-1]]
            name = normalize_name(key[-1])
            if self._persistent and not self._loaded and (name != key[-1] or groups[key[:-1], name.casefold()] > 1):
                self._protect_existing.add(key)
                if (parent / name).exists() or (parent / name).is_symlink():
                    log.warning('Сохранена прежняя копия с неоднозначным именем: %r', str(parent / name))
            self.get('/' + '/'.join(key))

    def save(self) -> None:
        if not self._persistent:
            return
        if self._lock_file is None:
            raise RuntimeError('Path mapping must be locked before saving')
        payload = {'version': 1, 'paths': [
            {'remote': list(key), 'local': list(path.relative_to(self.root).parts)}
            for key, path in self._paths.items() if key
        ]}
        temporary_path = None
        try:
            ensure_safe_path(self._state_dir, self._state_dir.parent, include_leaf=True)
            with NamedTemporaryFile(dir=self._state_dir, prefix='.paths-', suffix='.tmp',
                                    mode='w', encoding='utf-8', delete=False) as file:
                temporary_path = Path(file.name)
                json.dump(payload, file, ensure_ascii=True)
                file.flush()
                os.fsync(file.fileno())
            ensure_safe_path(self._state_path, self._state_dir, include_leaf=True)
            temporary_path.replace(self._state_path)
        finally:
            if temporary_path is not None:
                remove_temporary_file(temporary_path, self._state_dir)
        self._loaded = True

    @staticmethod
    def _parts(remote: str) -> tuple[str, ...]:
        relative = remote.removeprefix('disk:').strip('/')
        return tuple(relative.split('/')) if relative else ()

    def get(self, remote: str) -> Path:
        parts = self._parts(remote)
        for depth, original in enumerate(parts, 1):
            key = parts[:depth]
            if key in self._paths:
                continue
            parent = self._paths[key[:-1]]
            base = normalize_name(original)
            name = base
            digest = hashlib.sha256('/'.join(key).encode('utf-8')).hexdigest()[:8]
            attempt = 0
            while (parent, name.casefold()) in self._used or (
                self._persistent and (self._loaded or key in self._protect_existing)
                and ((parent / name).exists() or (parent / name).is_symlink())
            ):
                attempt += 1
                suffix = f'__{digest}' + (f'_{attempt}' if attempt > 1 else '')
                name = shorten_name(base, suffix)
            self._used.add((parent, name.casefold()))
            self._paths[key] = parent / name
            if name != original:
                log.info('Локальный путь изменён: %r -> %r', '/'.join(key), str(self._paths[key]))
        return self._paths[parts]


def ensure_safe_path(path: Path, root: Path, *, include_leaf: bool = False) -> None:
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise UnsafeDestinationError('Destination is outside download directory') from None
    current = root
    components = relative.parts if include_leaf else relative.parts[:-1]
    for component in (None, *components):
        if component is not None:
            current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
            raise UnsafeDestinationError('Destination contains a symbolic link or reparse point')


def remove_temporary_file(path: Path, root: Path) -> None:
    try:
        ensure_safe_path(path, root, include_leaf=True)
        path.unlink(missing_ok=True)
    except OSError as error:
        log.warning('Не удалось удалить временный файл %r (%s)', str(path), sanitized_error(error))


def is_excluded(path: str) -> bool:
    return 'Файлы Мессенджера' in path.removeprefix('disk:').strip('/').split('/')


async def main(
    email: str, output_dir: Path | None = None, status_interval: float = STATUS_INTERVAL,
    vdhash: str | None = None,
) -> bool:
    global sem
    sem = asyncio.Semaphore(MAX_STREAMS)
    label = f'Общий Диск {vdhash} ({email})' if vdhash is not None else email
    with ProgressReporter(label, status_interval) as progress:
        return await download_user(email, output_dir, progress, vdhash)


async def download_user(
    email: str, output_dir: Path | None, progress: ProgressReporter, vdhash: str | None = None,
    *, local_paths: LocalPaths | None = None,
) -> bool:
    start_time = time()
    if output_dir is None:
        base = download_directory()
        output_dir = base / 'shared' / normalize_name(vdhash) if vdhash is not None else base / normalize_name(email)
    root = Path(os.path.abspath(output_dir))
    if os.name == 'nt':
        root = Path(windows_long_path(str(root)))
    ensure_safe_path(root, root, include_leaf=True)
    if local_paths is None:
        with LocalPaths(root, persistent=True) as saved_paths:
            return await download_user(email, root, progress, vdhash, local_paths=saved_paths)

    try:
        client_id, client_secret = load_credentials()
        if not client_id or not client_secret:
            raise ValueError('CLIENT_ID or CLIENT_SECRET is not in environment or .env file')
        tokens = ServiceTokenManager(client_id, client_secret, email)
        token = await tokens.get_token()
    except Exception as error:
        log.error('Не удалось получить сервисный токен (%s)', sanitized_error(error))
        return False

    if vdhash is not None:
        client = SharedDiskClient(token, vdhash, tokens)
    else:
        session = disk_session(tokens)
        client = AsyncClient(token=token, session=session, default_args={'n_retries': 0})

    async with client, tokens:
        progress.phase('проверка токена')
        if await with_retries(client.check_token, 'проверка токена'):
            log.info('Проверка сервисного токена: успешно')
        else:
            log.error('Проверка сервисного токена: ошибка')
            return False

        progress.phase('получение информации о Диске')
        disk_info: DiskInfoObject = await with_retries(client.get_disk_info, 'информация о Диске')

        log.info('Начало выгрузки: %s', f'Общий Диск {vdhash} (через {email})' if vdhash is not None else email)
        if disk_info.used_space:
            log.info(f'Занято на Диске: {round(disk_info.used_space / 1024 ** 2, 2)} МБ')

        log.info('Поиск файлов и папок...')
        progress.phase('поиск файлов и папок')
        listing = await list_resources(client, progress)
        files_count = len(listing.files)
        progress.total_files(files_count)
        log.info('Найдено папок: %s; файлов к скачиванию: %s; ошибок чтения: %s',
                 len(listing.directories), files_count, listing.failed_directories)
        if listing.failed_directories and not listing.files and not listing.directories:
            return False
        progress.phase('подготовка путей')

        remotes = sorted(item.path for item in listing.directories + listing.files)
        local_paths.prepare(remotes)
        local_paths.save()
        for remote in remotes:
            progress.current(remote)
            local_paths.get(remote)
            progress.count(paths=1)

        failed_directories = 0
        directory_paths = [root] + [
            local_paths.get(directory.path) for directory in listing.directories
        ]
        progress.phase('создание папок')
        for local_path in directory_paths:
            progress.current(str(local_path))
            try:
                ensure_safe_path(local_path, root, include_leaf=True)
                local_path.mkdir(parents=True, exist_ok=True)
                progress.count(prepared=1)
            except OSError as error:
                failed_directories += 1
                progress.count(failed_directories=1)
                log.error('Не удалось создать папку %r: %s', str(local_path), sanitized_error(error))

        log.info('Скачивание файлов...')
        progress.phase('скачивание')
        pending_files = iter(enumerate(listing.files, 1))

        async def worker() -> int:
            successful = 0
            for file_position, file in pending_files:
                path = file.path.removeprefix('disk:')
                successful += await safe_download(
                    client=client,
                    path=path,
                    local_path=local_paths.get(path),
                    file_position_of=f'[{file_position}/{files_count}]',
                    progress=progress,
                    resource=file,
                    destination_root=root,
                )
            return successful

        tasks = [asyncio.create_task(worker(), name=f'downloader-worker-{index}')
                 for index in range(min(MAX_STREAMS, files_count))]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        progress.report()
        successful = sum(results)
        skipped = progress.skipped_files
        end_time = time()
        log.info(
            'Скачано файлов: %s; ошибок скачивания: %s; ошибок создания папок: %s; уже скачано: %s; время: %s мин; ошибок чтения: %s',
            successful - skipped, files_count - successful, failed_directories, skipped,
            round((end_time - start_time) / 60, 2), listing.failed_directories,
        )
        return successful == files_count and failed_directories == 0 and listing.failed_directories == 0


async def download_users(users: list[str], status_interval: float = STATUS_INTERVAL) -> bool:
    base = download_directory().resolve()
    with LocalPaths(base, persistent=True, state_dir=base / '.disk-downloader') as destinations:
        destinations.prepare('/' + email for email in users)
        destinations.save()
        successful = 0
        for position, email in enumerate(users, 1):
            log.info('Пользователь [%s/%s]: %s', position, len(users), email)
            try:
                ensure_safe_path(destinations.get('/' + email), base, include_leaf=True)
                completed = await main(email, output_dir=destinations.get('/' + email), status_interval=status_interval)
            except Exception as error:
                log.error('Ошибка выгрузки пользователя %s (%s)', email, sanitized_error(error))
                completed = False
            if completed:
                successful += 1
            else:
                log.error('Выгрузка пользователя не завершена: %s', email)
        log.info('Пользователей: %s; успешно: %s; с ошибками: %s', len(users), successful, len(users) - successful)
        return successful == len(users)


async def download_shared_disks(email: str, vdhashes: list[str], status_interval: float = STATUS_INTERVAL) -> bool:
    base = download_directory().resolve()
    shared_root = base / 'shared'
    ensure_safe_path(shared_root, base, include_leaf=True)
    with LocalPaths(shared_root, persistent=True, state_dir=shared_root / '.disk-downloader') as destinations:
        destinations.prepare('/' + vdhash for vdhash in vdhashes)
        destinations.save()
        successful = 0
        for position, vdhash in enumerate(vdhashes, 1):
            log.info('Общий Диск [%s/%s]: %s (через %s)', position, len(vdhashes), vdhash, email)
            try:
                ensure_safe_path(destinations.get('/' + vdhash), shared_root, include_leaf=True)
                completed = await main(email, output_dir=destinations.get('/' + vdhash),
                                       status_interval=status_interval, vdhash=vdhash)
            except Exception as error:
                log.error('Ошибка выгрузки Общего Диска %s (%s)', vdhash, sanitized_error(error))
                completed = False
            if completed:
                successful += 1
            else:
                log.error('Выгрузка Общего Диска не завершена: %s', vdhash)
        log.info('Общих Дисков: %s; успешно: %s; с ошибками: %s',
                 len(vdhashes), successful, len(vdhashes) - successful)
        return successful == len(vdhashes)


def matches_resource(local_path: Path, resource: ResourceObject | None, stop: Event) -> bool | None:
    try:
        info = local_path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise OSError('Destination is not a regular file')
    size = getattr(resource, 'size', None)
    known_size = type(size) is int and size >= 0
    if known_size and info.st_size != size:
        return False
    for algorithm, length in [('sha256', 64), ('md5', 32)]:
        expected = getattr(resource, algorithm, None)
        if not expected:
            continue
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{' + str(length) + '}', expected):
            raise ValueError('Invalid resource checksum')
        digest = hashlib.new(algorithm, usedforsecurity=False)
        with local_path.open('rb') as file:
            while not stop.is_set():
                chunk = file.read(1024 * 1024)
                if not chunk:
                    return digest.hexdigest() == expected.lower()
                digest.update(chunk)
        return False
    return True if known_size else None


async def check_local_file(local_path: Path, resource: ResourceObject | None) -> bool | None:
    stop = Event()
    task = asyncio.create_task(asyncio.to_thread(matches_resource, local_path, resource, stop))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
        raise


async def download_file(
    client: AsyncClient, path: str, local_path: Path, file_position_of: str,
    resource: ResourceObject | None = None,
    *, destination_root: Path | None = None,
) -> None:
    root = destination_root if destination_root is not None else local_path.parent
    ensure_safe_path(local_path, root, include_leaf=True)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=local_path.parent, prefix='.disk-download-', suffix='.part', delete=False) as file:
        temporary_path = Path(file.name)
    try:
        async def attempt() -> None:
            ensure_safe_path(temporary_path, root, include_leaf=True)
            await client.download(path, str(temporary_path))
            ensure_safe_path(temporary_path, root, include_leaf=True)
            if await check_local_file(temporary_path, resource) is False:
                raise DownloadIntegrityError('Downloaded file does not match resource metadata')

        await with_retries(attempt, f'скачивание {path!r}')
        ensure_safe_path(temporary_path, root, include_leaf=True)
        ensure_safe_path(local_path, root, include_leaf=True)
        temporary_path.replace(local_path)
    finally:
        remove_temporary_file(temporary_path, root)
    log.info('Скачан файл %s: %r -> %r', file_position_of, path, str(local_path))


async def safe_download(
    client: AsyncClient, path: str, local_path: Path, file_position_of: str,
    progress: ProgressReporter | None = None,
    resource: ResourceObject | None = None,
    *, destination_root: Path | None = None,
) -> bool:
    async with sem:
        if progress is not None:
            progress.start_file(path)
        success = None
        skipped = False
        try:
            root = destination_root if destination_root is not None else local_path.parent
            ensure_safe_path(local_path, root, include_leaf=True)
            complete = await check_local_file(local_path, resource)
            ensure_safe_path(local_path, root, include_leaf=True)
            if complete is None:
                log.error('Нельзя проверить существующий файл без размера или контрольной суммы: %r', str(local_path))
                raise ValueError('Missing resource metadata')
            if complete:
                skipped = True
                log.info('Уже скачан файл %s: %r -> %r', file_position_of, path, str(local_path))
            else:
                await download_file(client, path, local_path, file_position_of, resource, destination_root=root)
        except Exception as error:
            log.error('Ошибка скачивания файла %s: %r -> %r (%s)',
                      file_position_of, path, str(local_path), sanitized_error(error))
            if isinstance(error, TokenRefreshError) and error.terminal:
                raise
            success = False
        else:
            success = True
        finally:
            if progress is not None:
                progress.finish_file(path, success, skipped=skipped)
        return success is True


@dataclass
class DiskListing:
    directories: list[ResourceObject] = field(default_factory=list)
    files: list[ResourceObject] = field(default_factory=list)
    failed_directories: int = 0


async def list_resources(client: AsyncClient, progress: ProgressReporter | None = None) -> DiskListing:
    listing = DiskListing()
    pending = ['/']
    seen = {'disk:/'}
    while pending:
        path = pending.pop()
        if progress is not None:
            progress.current(path)
        observed: dict[str, ResourceObject] = {}

        async def request() -> None:
            async for item in client.listdir(path):
                if not isinstance(item.path, str) or not item.path or item.type not in ('file', 'dir'):
                    raise ValueError('Invalid directory resource')
                remote = 'disk:' + item.path.removeprefix('disk:')
                observed[remote] = item

        try:
            await with_retries(request, f'чтение папки {path!r}')
        except Exception as error:
            if isinstance(error, TokenRefreshError) and getattr(error, 'terminal', True):
                raise
            listing.failed_directories += 1
            log.error('Не удалось прочитать папку %r (%s)', path, sanitized_error(error))
            if progress is not None:
                progress.count(failed_listings=1)
        for remote, item in observed.items():
            if remote in seen:
                continue
            seen.add(remote)
            if is_excluded(remote):
                log.debug('Пропуск данных Мессенджера: %r', remote)
            elif item.type == 'dir':
                listing.directories.append(item)
                pending.append(item.path)
                if progress is not None:
                    progress.count(directories=1)
            else:
                listing.files.append(item)
                if progress is not None:
                    progress.count(files=1)
        if progress is not None:
            progress.count(directories_scanned=1)
    return listing


def load_credentials() -> tuple[str, str]:
    load_dotenv(Path.cwd() / '.env', interpolate=False)
    return os.getenv('CLIENT_ID', ''), os.getenv('CLIENT_SECRET', '')


def retry_limit() -> int:
    value = os.getenv('MAX_RETRIES', '0').strip()
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError('MAX_RETRIES должен быть неотрицательным целым числом')
    value = value.lstrip('0') or '0'
    maximum = len(RETRY_DELAYS)
    if len(value) > len(str(maximum)):
        return maximum
    return min(int(value), maximum)


def resolve_download_path(value: str, base: PurePath) -> PurePath:
    value = value.strip()
    if any(char in value for char in '\r\n\x00'):
        raise ValueError('Путь загрузки не должен содержать переносы строк или нулевой символ')
    windows_path = PureWindowsPath(value)
    if isinstance(base, PureWindowsPath):
        if windows_path.drive and not windows_path.root:
            raise ValueError('Укажите полный путь с буквой диска, например D:\\Backups, вместо D:Backups')
        return PureWindowsPath(ntpath.normpath(str(base / windows_path)))
    if windows_path.drive or value.startswith('\\'):
        raise ValueError('Путь Windows можно использовать только при запуске на Windows')
    return PurePosixPath(posixpath.normpath(str(base / value)))


def download_directory() -> Path:
    load_dotenv(Path.cwd() / '.env', interpolate=False)
    value = os.path.expanduser(os.getenv('DOWNLOAD_DIR', '').strip())
    path = Path(resolve_download_path(value, Path.cwd()))
    if path.exists() and not path.is_dir():
        raise ValueError('Путь загрузки указывает на файл, а не на папку')
    return path


def save_settings(values: dict[str, str]) -> None:
    destination = Path.cwd() / '.env'
    with NamedTemporaryFile(dir=destination.parent, prefix='.env-', suffix='.tmp', delete=False) as file:
        temporary_path = Path(file.name)
    try:
        if destination.exists():
            temporary_path.write_bytes(destination.read_bytes())
        for key, value in values.items():
            set_key(temporary_path, key, value)
        if os.name != 'nt':
            temporary_path.chmod(0o600)
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    os.environ.update(values)


def save_credentials(client_id: str, client_secret: str) -> None:
    if not all(value and not any(char in value for char in '\r\n\x00') for value in (client_id, client_secret)):
        raise ValueError('CLIENT_ID и CLIENT_SECRET должны быть непустыми строками без переноса строки')
    save_settings({'CLIENT_ID': client_id, 'CLIENT_SECRET': client_secret})


def configure_retries() -> None:
    load_dotenv(Path.cwd() / '.env', interpolate=False)
    try:
        current = retry_limit()
    except ValueError:
        current = 0
    choices = [str(value) for value in range(len(RETRY_DELAYS) + 1)]
    selected = questionary.select(
        'Количество повторов после первой попытки (0 — без повторов):', choices=choices,
        default=str(current), instruction='(↑/↓ — выбор, Enter — подтвердить)',
    ).unsafe_ask()
    if selected not in choices:
        raise ValueError(f'Количество повторов должно быть от 0 до {len(RETRY_DELAYS)}')
    save_settings({'MAX_RETRIES': selected})
    print(f'Количество повторов: {selected}. Настройка сохранена в {Path.cwd() / ".env"}')


def configure_download_directory() -> None:
    load_dotenv(Path.cwd() / '.env', interpolate=False)
    action = questionary.select('Папка загрузки:', choices=[
        questionary.Choice('Указать путь', 'custom'),
        questionary.Choice('Папка запуска (по умолчанию)', 'default'),
    ]).unsafe_ask()
    if action == 'default':
        value = ''
    elif action == 'custom':
        value = questionary.text('Путь к папке загрузки:', default=os.getenv('DOWNLOAD_DIR', '')).unsafe_ask().strip()
    else:
        raise ValueError('Неизвестный вариант папки загрузки')
    path = Path(resolve_download_path(os.path.expanduser(value), Path.cwd()))
    if path.exists() and not path.is_dir():
        raise ValueError('Путь загрузки указывает на файл, а не на папку')
    save_settings({'DOWNLOAD_DIR': value})
    print(f'Папка загрузки: {path}. Настройка сохранена в {Path.cwd() / ".env"}')


def configure_credentials() -> None:
    current_id, current_secret = load_credentials()
    client_id = questionary.text('CLIENT_ID:', default=current_id).unsafe_ask().strip()
    prompt = 'CLIENT_SECRET (Enter — оставить текущий):' if current_secret else 'CLIENT_SECRET:'
    client_secret = questionary.password(prompt).unsafe_ask() or current_secret
    save_credentials(client_id, client_secret)
    print(f'Настройки сохранены в {Path.cwd() / ".env"}')


def terminal_menu(status_interval: float) -> int:
    result = 0
    choices = [
        questionary.Choice('Личный Диск — один пользователь', 'user'),
        questionary.Choice('Личные Диски — список email из CSV', 'users'),
        questionary.Choice('Общий Диск — один VDHASH', 'shared'),
        questionary.Choice('Общие Диски — список VDHASH из CSV', 'shared_csv'),
        questionary.Choice('Настроить CLIENT_ID и CLIENT_SECRET', 'configure'),
        questionary.Choice('Настроить количество повторов (retry)', 'retries'),
        questionary.Choice('Настроить папку загрузки', 'download_directory'),
        questionary.Choice('Выход', 'exit'),
    ]
    while True:
        client_id, client_secret = load_credentials()
        configured = bool(client_id and client_secret)
        print('\nЯндекс Диск · загрузчик')
        try:
            print(f'Папка сохранения: {download_directory()}')
        except (ValueError, OSError) as error:
            print(f'Не удалось определить папку загрузки ({type(error).__name__}); измените настройку папки')
        print('Сервисное приложение: настроено' if configured else 'Сервисное приложение: не настроено')
        try:
            print(f'Повторов после первой попытки: {retry_limit()}')
        except ValueError as error:
            print(str(error))
        action = questionary.select('Выберите действие:', choices=choices,
                                    instruction='(↑/↓ — выбор, Enter — подтвердить)').unsafe_ask()
        if action == 'exit':
            return result
        try:
            if action == 'configure':
                configure_credentials()
                continue
            if action == 'retries':
                configure_retries()
                continue
            if action == 'download_directory':
                configure_download_directory()
                continue
            retry_limit()
            download_directory()
            if action == 'users':
                path = Path(questionary.path('CSV со списком email:').unsafe_ask()).expanduser()
                users = read_users_csv(path)
            elif action == 'user':
                users = [validate_email(questionary.text('Email пользователя:').unsafe_ask())]
            elif action in ('shared', 'shared_csv'):
                email = validate_email(questionary.text('Email сотрудника с доступом к Общим Дискам:').unsafe_ask())
                if action == 'shared':
                    vdhashes = [validate_vdhash(questionary.text('VDHASH (метка после /vd/ в URL диска):').unsafe_ask())]
                else:
                    path = Path(questionary.path('CSV со списком VDHASH:').unsafe_ask()).expanduser()
                    vdhashes = read_shared_disks_csv(path)
            else:
                raise ValueError('Неизвестный пункт меню')
            if not configured:
                configure_credentials()
            if action in ('user', 'users'):
                completed = asyncio.run(download_users(users, status_interval=status_interval))
            else:
                completed = asyncio.run(download_shared_disks(email, vdhashes, status_interval=status_interval))
            if not completed:
                result = 1
        except UnicodeError:
            print('Не удалось прочитать файл: требуется UTF-8.')
        except csv.Error:
            print('Некорректный формат CSV.')
        except ValueError as error:
            print(str(error))
        except OSError as error:
            print(f'Ошибка доступа к файлу ({type(error).__name__}).')


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Download personal or shared Yandex Disks')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--csv', type=Path, metavar='PATH', help='UTF-8 CSV with an email column or one email per row')
    modes.add_argument('--vdhash', metavar='HASH', help='Shared disk ID; requires --email with reader access')
    modes.add_argument('--vdhash-csv', type=Path, metavar='PATH', help='UTF-8 CSV with a VDHASH column; requires --email')
    modes.add_argument('--configure', action='store_true', help='Configure CLIENT_ID and CLIENT_SECRET interactively')
    parser.add_argument('--email', help='Personal disk owner or shared disk reader email')
    parser.add_argument('--status-interval', type=positive_interval, default=STATUS_INTERVAL, metavar='SECONDS',
                        help=f'Progress update interval in seconds (default: {STATUS_INTERVAL:g})')
    args = parser.parse_args(argv)
    if args.email is not None and (args.csv is not None or args.configure):
        parser.error('--email cannot be combined with --csv or --configure')
    shared = args.vdhash is not None or args.vdhash_csv is not None
    if shared and args.email is None:
        parser.error('--email is required: use an employee with read access to the shared disks')
    interactive = args.configure or not any((args.email is not None, args.csv is not None, shared))
    if interactive and not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error('Menu requires a terminal; use --email, --csv or --email with --vdhash / --vdhash-csv')
    try:
        if args.configure:
            configure_credentials()
            return 0
        if interactive:
            return terminal_menu(args.status_interval)
        load_dotenv(Path.cwd() / '.env', interpolate=False)
        retry_limit()
        download_directory()
        if shared:
            email = validate_email(args.email)
            vdhashes = read_shared_disks_csv(args.vdhash_csv) if args.vdhash_csv is not None else [validate_vdhash(args.vdhash)]
            return 0 if asyncio.run(download_shared_disks(email, vdhashes, status_interval=args.status_interval)) else 1
        users = read_users_csv(args.csv) if args.csv is not None else [validate_email(args.email)]
        return 0 if asyncio.run(download_users(users, status_interval=args.status_interval)) else 1
    except UnicodeError:
        parser.error('CSV and .env must be encoded as UTF-8')
    except OSError as error:
        parser.error(f'Cannot access file ({type(error).__name__})')
    except csv.Error:
        parser.error('Invalid CSV syntax')
    except ValueError as error:
        parser.error(str(error))
    except EOFError:
        parser.error('Input closed')
    except KeyboardInterrupt:
        log.info('Работа прервана пользователем')
        return 130


def create_run_log() -> logging.FileHandler:
    directory = Path.cwd() / 'logs'
    directory.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime('downloader_%Y-%m-%d_%H-%M-%S_%f')
    suffix = 0
    while True:
        path = directory / f'{name}{"_" + str(suffix) if suffix else ""}.log'
        try:
            handler = logging.FileHandler(path, mode='x', encoding='utf-8', errors='backslashreplace')
        except FileExistsError:
            suffix += 1
            continue
        handler.setFormatter(log_handler.formatter)
        return handler


def cli(argv: list[str] | None = None) -> int:
    try:
        handler = create_run_log()
    except OSError as error:
        log.error('Версия %s | Не удалось создать файл журнала в папке logs (%s)', VERSION, type(error).__name__)
        return 1
    log.addHandler(handler)
    status = 1
    try:
        log.info('Версия %s | Лог запуска: %s', VERSION, handler.baseFilename)
        status = run_cli(argv)
        return status
    except SystemExit as error:
        status = error.code if isinstance(error.code, int) else 1
        raise
    except KeyboardInterrupt:
        status = 130
        log.info('Работа прервана пользователем')
        return status
    except Exception as error:
        log.error('Непредвиденная ошибка (%s)', sanitized_error(error))
        raise
    finally:
        log.info('Завершение работы; код: %s', status)
        log.removeHandler(handler)
        handler.close()


if __name__ == '__main__':
    raise SystemExit(cli())
