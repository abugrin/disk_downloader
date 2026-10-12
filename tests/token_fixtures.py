from time import monotonic

from downloader import ServiceToken


def make_token(value='test-token'):
    now = monotonic()
    return ServiceToken(value, now + 3600, now + 3300)
