# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#     "httpx>=0.28,<0.29",
#     "python-dotenv>=1.1,<2",
#     "questionary>=2.1,<3",
#     "yadisk[async-defaults]==3.2.0",
# ]
# ///

import argparse
import asyncio
import csv
import hashlib
import logging
import math
import os
import re
import stat
import sys
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from random import uniform
from tempfile import NamedTemporaryFile
from threading import Event, Lock, Thread
from time import monotonic, time
from types import TracebackType

import httpx
import questionary
from dotenv import load_dotenv, set_key
from yadisk import AsyncClient
from yadisk.exceptions import RequestTimeoutError, YaDiskConnectionError
from yadisk.objects import DiskInfoObject, ResourceObject
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession


MAX_STREAMS = 10
STATUS_INTERVAL = 10.0
TOKEN_LIFETIME = 3600.0
TOKEN_REFRESH_MARGIN = 300.0
TOKEN_REFRESH_RETRY_INTERVAL = 30.0
RETRY_DELAYS = (2, 4, 8, 16, 32)
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
NETWORK_ERRORS = (
    TimeoutError, ConnectionError, RequestTimeoutError, YaDiskConnectionError,
    httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
    httpx.ProxyError, httpx.DecodingError,
)
directories: list[ResourceObject] = []

log = logging.getLogger('Downloader')
log.setLevel(logging.DEBUG)
log_handler = logging.StreamHandler(sys.stdout)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(message)s'))
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
            retryable, retry_after, reason = retry_details(error)
            if not retryable or attempt == max_retries:
                raise
            base = RETRY_DELAYS[attempt]
            delay = max(base + uniform(0.0, base * 0.25), retry_after)
            attempt += 1
            log.warning('Повтор %s/%s через %.2f с: %s (%s)',
                        attempt, max_retries, delay, label, reason)
            await asyncio.sleep(delay)


async def check_transient_response(response: httpx.Response) -> None:
    if response.status_code in RETRY_STATUSES:
        response.raise_for_status()


@dataclass(frozen=True)
class ServiceToken:
    access_token: str = field(repr=False)
    expires_at: float
    refresh_at: float


async def get_service_token(client_id: str, client_secret: str, email: str) -> ServiceToken:
    async with httpx.AsyncClient(timeout=30) as client:
        async def request() -> ServiceToken:
            started = monotonic()
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
            if expires_at <= monotonic():
                raise ValueError('Service app token has already expired')
            return ServiceToken(token, expires_at, expires_at - min(TOKEN_REFRESH_MARGIN, lifetime * 0.1))

        return await with_retries(request, 'получение токена')


class TokenRefreshError(Exception):
    pass


class ServiceTokenManager:
    def __init__(self, client_id: str, client_secret: str, email: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._email = email
        self._token: ServiceToken | None = None
        self._next_refresh_at = 0.0
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

    async def get_token(self) -> str:
        async with self._lock:
            now = monotonic()
            if now < self._next_refresh_at:
                if self._token is not None and now < self._token.expires_at:
                    return self._token.access_token
                raise TokenRefreshError('Нет действующего сервисного токена')
            refreshing = self._token is not None
            if refreshing:
                log.info('Обновление сервисного токена...')
            try:
                token = await get_service_token(self._client_id, self._client_secret, self._email)
            except Exception as error:
                self._next_refresh_at = monotonic() + TOKEN_REFRESH_RETRY_INTERVAL
                if refreshing:
                    log.warning('Не удалось обновить сервисный токен (%s); следующая проверка через %.0f с',
                                type(error).__name__, TOKEN_REFRESH_RETRY_INTERVAL)
                if self._token is not None and monotonic() < self._token.expires_at:
                    return self._token.access_token
                raise TokenRefreshError('Нет действующего сервисного токена') from None
            self._token = token
            self._next_refresh_at = token.refresh_at
            log.info('Сервисный токен %s; обновление через %.0f с',
                     'обновлён' if refreshing else 'получен', max(0, token.refresh_at - monotonic()))
            return token.access_token

    async def authorize(self, request: httpx.Request) -> None:
        if request.headers.get('Authorization', '').startswith('OAuth '):
            request.headers['Authorization'] = f'OAuth {await self.get_token()}'

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0, self._next_refresh_at - monotonic()))
                return
            except TimeoutError:
                pass
            try:
                await self.get_token()
            except TokenRefreshError:
                pass


def disk_session(tokens: ServiceTokenManager | None = None) -> AsyncHTTPXSession:
    hooks = {'response': [check_transient_response]}
    if tokens is not None:
        hooks['request'] = [tokens.authorize]
    return AsyncHTTPXSession(event_hooks=hooks)


async def directory_items(client: AsyncClient, path: str) -> list[ResourceObject]:
    async def request() -> list[ResourceObject]:
        return [item async for item in client.listdir(path)]

    return await with_retries(request, f'чтение папки {path!r}')


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
        self.report()
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
        self.report()

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
        if phase == 'поиск папок':
            details = f" | папок найдено: {counts.get('directories', 0)}, просмотрено: {counts.get('directories_scanned', 0)}"
        elif phase == 'поиск файлов':
            details = f" | файлов найдено: {counts.get('files', 0)}, папок просмотрено: {counts.get('file_directories_scanned', 0)}"
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
        log.info('Статус %s | этап: %s | прошло: %.0f с%s | без новых результатов: %.0f с',
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
    def __init__(self, root: Path):
        self.root = root
        self._paths: dict[tuple[str, ...], Path] = {(): root}
        self._used: set[tuple[Path, str]] = set()

    def get(self, remote: str) -> Path:
        relative = remote.removeprefix('disk:').strip('/')
        parts = tuple(relative.split('/')) if relative else ()
        for depth, original in enumerate(parts, 1):
            key = parts[:depth]
            if key in self._paths:
                continue
            parent = self._paths[key[:-1]]
            base = normalize_name(original)
            name = base
            digest = hashlib.sha256('/'.join(key).encode('utf-8')).hexdigest()[:8]
            attempt = 0
            while (parent, name.casefold()) in self._used:
                attempt += 1
                suffix = f'__{digest}' + (f'_{attempt}' if attempt > 1 else '')
                name = shorten_name(base, suffix)
            self._used.add((parent, name.casefold()))
            self._paths[key] = parent / name
            if name != original:
                log.info('Normalized local path: %r -> %r', '/'.join(key), str(self._paths[key]))
        return self._paths[parts]


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
) -> bool:
    start_time = time()
    directories.clear()

    try:
        client_id, client_secret = load_credentials()
        if not client_id or not client_secret:
            raise ValueError('CLIENT_ID or CLIENT_SECRET is not in environment or .env file')
        tokens = ServiceTokenManager(client_id, client_secret, email)
        token = await tokens.get_token()
    except Exception as error:
        log.error('Error getting service app token (%s)', type(error).__name__)
        return False

    if vdhash is not None:
        client = SharedDiskClient(token, vdhash, tokens)
    else:
        session = disk_session(tokens)
        client = AsyncClient(token=token, session=session, default_args={'n_retries': 0})

    async with client, tokens:
        progress.phase('проверка токена')
        if await with_retries(client.check_token, 'проверка токена'):
            log.info('Test service app token: Success')
        else:
            log.error('Test service app token: Failed')
            return False

        progress.phase('получение информации о Диске')
        disk_info: DiskInfoObject = await with_retries(client.get_disk_info, 'информация о Диске')

        log.info('Starting files download: %s', f'shared/{vdhash} (via {email})' if vdhash is not None else email)
        if disk_info.used_space:
            log.info(f'Used disk space: {round(disk_info.used_space / 1024 ** 2, 2)} MB')

        log.info('Listing user directories...')
        progress.phase('поиск папок')
        await list_directory(client, '/', progress)

        log.info(f'Found directories count: {len(directories)}')
        log.info('Listing user files...')
        progress.phase('поиск файлов')
        files = await list_files(client, progress)

        filtered_files = []
        for file in files:
            if file.path and not is_excluded(file.path):
                filtered_files.append(file)
            else:
                log.debug(f'Skipping file from Messenger directory: {file.path}')

        files_count = len(filtered_files)
        progress.total_files(files_count)
        log.info(f'Found files to download: {files_count} (after filtering)')
        progress.phase('подготовка путей')

        default_root = Path('shared', normalize_name(vdhash)) if vdhash is not None else Path(normalize_name(email))
        root = (output_dir if output_dir is not None else default_root).absolute()
        if os.name == 'nt':
            root = Path(windows_long_path(str(root)))
        local_paths = LocalPaths(root)
        for remote in sorted({
            item.path for item in [*directories, *filtered_files]
            if item.path and not is_excluded(item.path)
        }):
            progress.current(remote)
            local_paths.get(remote)
            progress.count(paths=1)

        failed_directories = 0
        directory_paths = [root] + [
            local_paths.get(directory.path) for directory in directories
            if directory.path and not is_excluded(directory.path)
        ]
        progress.phase('создание папок')
        for local_path in directory_paths:
            progress.current(str(local_path))
            try:
                local_path.mkdir(parents=True, exist_ok=True)
                progress.count(prepared=1)
            except OSError as error:
                failed_directories += 1
                progress.count(failed_directories=1)
                log.error('Cannot create directory %r: %s', str(local_path), error)

        log.info('Downloading user files...')
        progress.phase('скачивание')
        tasks = []
        for file_position, file in enumerate(filtered_files, 1):
            file_position_str = f'[{file_position}/{files_count}]'
            if file.path:
                path = file.path.removeprefix('disk:')
                tasks.append(asyncio.create_task(
                    safe_download(
                        client=client,
                        path=path,
                        local_path=local_paths.get(path),
                        file_position_of=file_position_str,
                        progress=progress,
                        resource=file,
                    )
                ))
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
            'Downloaded %s files; failed files: %s; failed directories: %s; skipped files: %s; time: %s minutes',
            successful - skipped, len(results) - successful, failed_directories, skipped,
            round((end_time - start_time) / 60, 2),
        )
        return successful == len(results) and failed_directories == 0


async def download_users(users: list[str], status_interval: float = STATUS_INTERVAL) -> bool:
    destinations = LocalPaths(Path.cwd())
    for email in sorted(users):
        destinations.get('/' + email)
    successful = 0
    for position, email in enumerate(users, 1):
        log.info('User [%s/%s]: %s', position, len(users), email)
        try:
            completed = await main(email, output_dir=destinations.get('/' + email), status_interval=status_interval)
        except Exception as error:
            log.error('Failed user %s (%s)', email, type(error).__name__)
            completed = False
        if completed:
            successful += 1
        else:
            log.error('Download incomplete for user: %s', email)
    log.info('Users: %s; successful: %s; failed: %s', len(users), successful, len(users) - successful)
    return successful == len(users)


async def download_shared_disks(email: str, vdhashes: list[str], status_interval: float = STATUS_INTERVAL) -> bool:
    destinations = LocalPaths(Path.cwd() / 'shared')
    for vdhash in sorted(vdhashes):
        destinations.get('/' + vdhash)
    successful = 0
    for position, vdhash in enumerate(vdhashes, 1):
        log.info('Shared disk [%s/%s]: %s (via %s)', position, len(vdhashes), vdhash, email)
        try:
            completed = await main(email, output_dir=destinations.get('/' + vdhash),
                                   status_interval=status_interval, vdhash=vdhash)
        except Exception as error:
            log.error('Failed shared disk %s (%s)', vdhash, type(error).__name__)
            completed = False
        if completed:
            successful += 1
        else:
            log.error('Download incomplete for shared disk: %s', vdhash)
    log.info('Shared disks: %s; successful: %s; failed: %s',
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
) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=local_path.parent, prefix='.disk-download-', suffix='.part', delete=False) as file:
        temporary_path = Path(file.name)
    try:
        async def attempt() -> None:
            await client.download(path, str(temporary_path))
            if await check_local_file(temporary_path, resource) is False:
                raise DownloadIntegrityError('Downloaded file does not match resource metadata')

        await with_retries(attempt, f'скачивание {path!r}')
        temporary_path.replace(local_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    log.info('Downloaded file %s: %r -> %r', file_position_of, path, str(local_path))


async def safe_download(
    client: AsyncClient, path: str, local_path: Path, file_position_of: str,
    progress: ProgressReporter | None = None,
    resource: ResourceObject | None = None,
) -> bool:
    async with sem:
        if progress is not None:
            progress.start_file(path)
        success = None
        skipped = False
        try:
            complete = await check_local_file(local_path, resource)
            if complete is None:
                log.error('Cannot verify existing file without size or checksum: %r', str(local_path))
                raise ValueError('Missing resource metadata')
            if complete:
                skipped = True
                log.info('Already downloaded %s: %r -> %r', file_position_of, path, str(local_path))
            else:
                await download_file(client, path, local_path, file_position_of, resource)
        except Exception as error:
            log.error('Failed file %s: %r -> %r (%s)',
                      file_position_of, path, str(local_path), type(error).__name__)
            success = False
        else:
            success = True
        finally:
            if progress is not None:
                progress.finish_file(path, success, skipped=skipped)
        return success is True


async def list_directory(client: AsyncClient, path: str, progress: ProgressReporter | None = None) -> None:
    directories_list: list[ResourceObject] = []
    if progress is not None:
        progress.current(path)

    for item in await directory_items(client, path):
        if item.type == 'dir' and item.path:
            if is_excluded(item.path):
                log.debug(f'Skipping Messenger directory: {item.path}')
                continue
            directories_list.append(item)
            if progress is not None:
                progress.count(directories=1)
    if progress is not None:
        progress.count(directories_scanned=1)
    directories.extend(directories_list)
    for directory in directories_list:
        await list_directory(client, directory.path, progress)


async def list_files(client: AsyncClient, progress: ProgressReporter | None = None) -> list[ResourceObject]:
    files_list: list[ResourceObject] = []

    for path in ['/', *(directory.path for directory in directories if directory.path)]:
        if progress is not None:
            progress.current(path)
        for item in await directory_items(client, path):
            if item.type == 'file':
                files_list.append(item)
                if progress is not None:
                    progress.count(files=1)
        if progress is not None:
            progress.count(file_directories_scanned=1)
    return files_list


def load_credentials() -> tuple[str, str]:
    load_dotenv(Path.cwd() / '.env', interpolate=False)
    return os.getenv('CLIENT_ID', ''), os.getenv('CLIENT_SECRET', '')


def retry_limit() -> int:
    value = os.getenv('MAX_RETRIES', '5').strip()
    if value not in ('1', '2', '3', '4', '5'):
        raise ValueError('MAX_RETRIES должен быть целым числом от 1 до 5')
    return int(value)


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
        current = 5
    selected = questionary.select(
        'Количество повторов после первой попытки:', choices=['1', '2', '3', '4', '5'],
        default=str(current), instruction='(↑/↓ — выбор, Enter — подтвердить)',
    ).unsafe_ask()
    if selected not in ('1', '2', '3', '4', '5'):
        raise ValueError('Количество повторов должно быть от 1 до 5')
    save_settings({'MAX_RETRIES': selected})
    print(f'Количество повторов: {selected}. Настройка сохранена в {Path.cwd() / ".env"}')


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
        questionary.Choice('Выход', 'exit'),
    ]
    while True:
        client_id, client_secret = load_credentials()
        configured = bool(client_id and client_secret)
        print('\nЯндекс Диск · загрузчик')
        print(f'Папка сохранения: {Path.cwd()}')
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
            retry_limit()
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
                        help='Progress update interval in seconds (default: 10)')
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
        log.error('Не удалось создать файл журнала в папке logs (%s)', type(error).__name__)
        return 1
    log.addHandler(handler)
    status = 1
    try:
        log.info('Лог запуска: %s', handler.baseFilename)
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
        log.error('Непредвиденная ошибка (%s)', type(error).__name__)
        raise
    finally:
        log.info('Завершение работы; код: %s', status)
        log.removeHandler(handler)
        handler.close()


if __name__ == '__main__':
    raise SystemExit(cli())
