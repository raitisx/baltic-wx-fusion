"""Shared HTTP session with retries and honest UA."""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s
