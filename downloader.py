import argparse
import asyncio
import csv
import hashlib
import logging
import math
import os
import re
import sys
import unicodedata
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, time
from types import TracebackType

from dotenv import load_dotenv
from y360_orglib import ServiceAppClient
from yadisk import AsyncClient
from yadisk.objects import DiskInfoObject, ResourceObject


MAX_STREAMS = 10
STATUS_INTERVAL = 10.0
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

    def finish_file(self, path: str, success: bool | None) -> None:
        with self._lock:
            self._active.pop(path, None)
            if success is not None:
                key = 'downloaded' if success else 'failed_files'
                self._counts[key] = self._counts.get(key, 0) + 1
                self._last_result = monotonic()

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
            completed = downloaded + failed
            percent = completed / total * 100 if total else 100
            queued = max(0, total - completed - len(active))
            details = (
                f' | файлов завершено: {completed}/{total} ({percent:.1f}%)'
                f' | скачано: {downloaded}, ошибок: {failed}'
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


def read_users_csv(path: Path) -> list[str]:
    users: list[str] = []
    seen: set[str] = set()
    with path.open(encoding='utf-8-sig', newline='') as source:
        first_line = next((line for line in source if line.strip()), '')
        source.seek(0)
        try:
            delimiter = csv.Sniffer().sniff(first_line, delimiters=',;').delimiter
        except csv.Error:
            delimiter = ','
        reader = csv.reader(source, delimiter=delimiter, strict=True)
        email_column: int | None = None
        first_row = True
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            if first_row:
                first_row = False
                columns = [cell.strip().casefold() for cell in row]
                if columns.count('email') > 1:
                    raise ValueError('CSV contains more than one email column')
                if 'email' in columns:
                    email_column = columns.index('email')
                    continue
            if email_column is None and len(row) != 1:
                raise ValueError(f'CSV line {reader.line_num}: expected one email or an email column header')
            column = email_column if email_column is not None else 0
            if column >= len(row):
                raise ValueError(f'CSV line {reader.line_num}: missing email column')
            try:
                email = validate_email(row[column])
            except ValueError:
                raise ValueError(f'CSV line {reader.line_num}: invalid or empty email') from None
            key = email.casefold()
            if key not in seen:
                seen.add(key)
                users.append(email)
    if not users:
        raise ValueError('CSV contains no users')
    return users


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


async def main(email: str, output_dir: Path | None = None, status_interval: float = STATUS_INTERVAL) -> bool:
    with ProgressReporter(email, status_interval) as progress:
        return await download_user(email, output_dir, progress)


async def download_user(email: str, output_dir: Path | None, progress: ProgressReporter) -> bool:
    load_dotenv()
    start_time = time()
    directories.clear()

    try:
        client_id = os.getenv('CLIENT_ID')
        client_secret = os.getenv('CLIENT_SECRET')
        if not client_id or not client_secret:
            raise ValueError('CLIENT_ID or CLIENT_SECRET is not in environment or .env file')
        service_apps_client: ServiceAppClient = ServiceAppClient(client_id, client_secret)
        try:
            token = service_apps_client.get_service_app_token(subject_token=email).access_token
        finally:
            service_apps_client.session.close()
    except Exception as error:
        log.error('Error getting service app token (%s)', type(error).__name__)
        return False

    client = AsyncClient(token=token)

    async with client:
        progress.phase('проверка токена')
        if await client.check_token():
            log.info('Test service app token: Success')
        else:
            log.error('Test service app token: Failed')
            return False

        progress.phase('получение информации о Диске')
        disk_info: DiskInfoObject = await client.get_disk_info()

        log.info(f'Starting files download for user: {email}')
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

        root = (output_dir if output_dir is not None else Path(normalize_name(email))).absolute()
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
                    )
                ))
        results = await asyncio.gather(*tasks)
        progress.report()
        downloaded = sum(results)
        end_time = time()
        log.info(
            'Downloaded %s files; failed files: %s; failed directories: %s; time: %s minutes',
            downloaded, len(results) - downloaded, failed_directories,
            round((end_time - start_time) / 60, 2),
        )
        return downloaded == len(results) and failed_directories == 0


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


async def download_file(client: AsyncClient, path: str, local_path: Path, file_position_of: str) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    await client.download(path, str(local_path))
    log.info('Downloaded file %s: %r -> %r', file_position_of, path, str(local_path))


async def safe_download(
    client: AsyncClient, path: str, local_path: Path, file_position_of: str,
    progress: ProgressReporter | None = None,
) -> bool:
    async with sem:
        if progress is not None:
            progress.start_file(path)
        success = None
        try:
            await download_file(client, path, local_path, file_position_of)
        except Exception as error:
            log.error('Failed file %s: %r -> %r (%s)',
                      file_position_of, path, str(local_path), type(error).__name__)
            success = False
        else:
            success = True
        finally:
            if progress is not None:
                progress.finish_file(path, success)
        return success is True


async def list_directory(client: AsyncClient, path: str, progress: ProgressReporter | None = None) -> None:
    directories_list: list[ResourceObject] = []
    if progress is not None:
        progress.current(path)

    async for item in client.listdir(path):
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
        async for item in client.listdir(path):
            if item.type == 'file':
                files_list.append(item)
                if progress is not None:
                    progress.count(files=1)
        if progress is not None:
            progress.count(file_directories_scanned=1)
    return files_list


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Download user files from Yandex Disk')
    parser.add_argument('--csv', type=Path, metavar='PATH', help='UTF-8 CSV with an email column or one email per row')
    parser.add_argument('--status-interval', type=positive_interval, default=STATUS_INTERVAL, metavar='SECONDS',
                        help='Progress update interval in seconds (default: 10)')
    args = parser.parse_args(argv)
    try:
        if args.csv is not None:
            users = read_users_csv(args.csv)
        else:
            users = [validate_email(input('Enter user email: '))]
    except UnicodeError:
        parser.error('CSV must be encoded as UTF-8')
    except OSError as error:
        parser.error(f'Cannot read CSV ({type(error).__name__})')
    except csv.Error:
        parser.error('Invalid CSV syntax')
    except ValueError as error:
        parser.error(str(error))
    except EOFError:
        parser.error('No email provided; enter an email or use --csv PATH')
    try:
        return 0 if asyncio.run(download_users(users, status_interval=args.status_interval)) else 1
    except KeyboardInterrupt:
        log.info('Выгрузка прервана пользователем')
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
