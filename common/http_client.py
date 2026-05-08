from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.settings import settings


class HttpClient:
    def __init__(self, base_url: Optional[str] = None, default_headers: Optional[Dict[str, str]] = None):
        self.session = requests.Session()
        self._use_settings_base_url = base_url is None
        self._use_settings_headers = default_headers is None
        self.base_url = (base_url or settings.get_api_base_url()).rstrip("/")
        self.timeout = settings.get_timeout()

        headers = default_headers if default_headers is not None else settings.get_headers()
        if headers:
            self.session.headers.update(headers)

        retry_strategy = Retry(
            total=settings.get_retry_count(),
            backoff_factor=settings.get_retry_interval(),
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def refresh_runtime_config(self) -> None:
        settings.load_config()
        self.timeout = settings.get_timeout()
        if self._use_settings_base_url:
            self.base_url = settings.get_api_base_url().rstrip("/")
        if self._use_settings_headers:
            self.session.headers.update(settings.get_headers())

    def _build_url(self, endpoint: str) -> str:
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    def request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        self.refresh_runtime_config()
        url = self._build_url(endpoint)
        kwargs.setdefault("timeout", self.timeout)

        response = self.session.request(method, url, **kwargs)
        print(f"HTTP_RESPONSE method={method} url={url} status={response.status_code}")
        return response

    def post(self, endpoint: str, json: Optional[Dict[str, Any]] = None, **kwargs) -> requests.Response:
        return self.request("POST", endpoint, json=json, **kwargs)

    def set_header(self, key: str, value: str) -> None:
        self.session.headers[key] = value

    def remove_header(self, key: str) -> None:
        self.session.headers.pop(key, None)

    def close(self) -> None:
        self.session.close()


http_client = HttpClient()


def create_http_client(base_url: Optional[str] = None, default_headers: Optional[Dict[str, str]] = None) -> HttpClient:
    return HttpClient(base_url=base_url, default_headers=default_headers)
