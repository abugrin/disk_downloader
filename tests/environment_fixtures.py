import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from unittest.mock import patch


APPLICATION_VARIABLES = ('CLIENT_ID', 'CLIENT_SECRET', 'MAX_RETRIES', 'DOWNLOAD_DIR', 'MODIFIED_SINCE', 'VERIFY_TLS')


@contextmanager
def isolated_environment(values: Mapping[str, str] | None = None) -> Generator[None]:
    with patch.dict(os.environ):
        for name in APPLICATION_VARIABLES:
            os.environ.pop(name, None)
        if values:
            os.environ.update(values)
        yield
