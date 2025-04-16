import asyncio
import logging
import os
import sys
from time import time

import yadisk
from dotenv import load_dotenv
from yadisk.objects import ResourceObject, DiskInfoObject
from pathlib import Path
from y360_orglib import ServiceAppClient


MAX_STREAMS = 10
directories: list[ResourceObject] = []

log = logging.getLogger('Downloader')
log.setLevel(logging.DEBUG)
log_handler = logging.StreamHandler(sys.stdout)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(message)s'))
log.addHandler(log_handler)

sem = asyncio.Semaphore(MAX_STREAMS)


async def main(email: str):
    load_dotenv()
    start_time = time()

    token = ''
    try:
        service_apps_client: ServiceAppClient = ServiceAppClient(os.getenv('CLIENT_ID'), os.getenv('CLIENT_SECRET'))
        token = service_apps_client.get_service_app_token(subject_token=email).access_token
    except Exception as e:
        log.error('Error getting service app token')
        log.error(e)
        exit(1)

    client = yadisk.AsyncClient(token=token)

    async with client:
        if await client.check_token():
            log.info('Test service app token: Success')
        else:
            log.error('Test service app token: Failed')
            exit(1)

        disk_info: DiskInfoObject = await client.get_disk_info()

        log.info(f'Starting files download for user: {email}')
        if disk_info.used_space:
            log.info(f'Used disk space: {round(disk_info.used_space / 1024 ** 2, 2)} MB')

        log.info('Listing user directories...')

        await list_directory(client, '/')

        log.info(f'Found directories count: {len(directories)}')
        log.info('Listing user files...')
        files = await list_files(client)
        files_count = len(files)
        log.info(f'Found files to download: {files_count}')
        log.info('Downloading user files...')


        for directory in directories:
            if directory.path:
                path = email + directory.path.removeprefix('disk:')
                log.debug(f'Creating directory: {path}')
                Path(path).mkdir(parents=True, exist_ok=True)

        file_position = 1
        tasks = []
        for file in files:
            file_position_str = f'[{file_position}/{files_count}]'
            file_position += 1
            if file.path:
                path = file.path.removeprefix('disk:')
                tasks.append(asyncio.create_task(
                        safe_download(
                            client=client,
                            path=path,
                            email=email,
                            file_position_of=file_position_str
                        )
                    )
                )
        await asyncio.gather(*tasks)
        end_time = time()
        log.info(f'Downloaded {len(files)} files in {round((end_time - start_time) / 60, 2)} minutes')


async def download_file(client: yadisk.AsyncClient, path, email, file_position_of):
    # log.info(f'Downloading file {file_position_of}: {path}')
    await client.download(path, f'{email}{path}')
    log.info(f'Downloaded file {file_position_of}: {path}')


async def safe_download(*args, **kwargs):
    async with sem:  # semaphore limits num of simultaneous downloads
        return await download_file(*args, **kwargs)

async def process_directories(client: yadisk.AsyncClient, directories_list: list[ResourceObject]):
    if len(directories_list) > 0:
        # log.debug(f'Starting to process {len(directories_list)} directories')

        for directory in directories_list:
            # log.debug(f'Process directory: {directory.path}')
            # log.debug('PD: Waiting to keep rps')
            # await asyncio.sleep(0.1)
            # log.debug('PD: Done waiting sleep')
            await list_directory(client, directory.path)
            # log.debug('PD: Done process dir')


async def list_directory(client, path):
    global directories
    directories_list: list[ResourceObject] = []

    async for item in client.listdir(path):
        if item.type == 'dir':
            # log.debug(f'Add directory: {item.path}')
            directories_list.append(item)
    directories.extend(directories_list)
    await process_directories(client, directories_list)

async def list_files(client: yadisk.AsyncClient) -> list[ResourceObject]:
    files_list: list[ResourceObject] = []
    async for item in client.listdir('/'):
        if item.type == 'file':
            files_list.append(item)
            # log.debug(f'Add file: {item.path}')
    for directory in directories:
        if directory.path:
            async for item in client.listdir(directory.path):
                if item.type == 'file':
                    files_list.append(item)
                    # log.debug(f'Add file: {item.path}')
            # await asyncio.sleep(0.1)
    return files_list


if __name__ == "__main__":
    user_email = input('Enter user email: ')
    asyncio.run(main(email=user_email))