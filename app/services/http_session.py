"""
Shared HTTP session manager with connection pooling for external API calls.
Replaces direct `requests.get/post` with pooled sessions for better efficiency.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import threading
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Thread-safe HTTP session manager with connection pooling.

    Features:
    - Connection pooling per host
    - Automatic retry with exponential backoff
    - Thread-local sessions for thread safety
    - Configurable pool size and timeouts
    """

    _instance: Optional["SessionManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        # Thread-local storage for sessions
        self._local = threading.local()

        # Global configuration
        self._pool_connections = 10
        self._pool_maxsize = 20
        self._max_retries = 3
        self._backoff_factor = 0.5
        self._status_forcelist = (429, 500, 502, 503, 504)
        self._timeout = (5, 30)  # (connect, read)
        self._default_headers = {
            "User-Agent": "MoneyPrinterTurbo/1.3.5",
            "Accept": "application/json",
        }

        # Per-host session cache
        self._sessions: dict[str, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def configure(
        self,
        pool_connections: int = 10,
        pool_maxsize: int = 20,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        timeout: tuple = (5, 30),
        default_headers: dict = None,
    ):
        """Configure global session defaults."""
        self._pool_connections = pool_connections
        self._pool_maxsize = pool_maxsize
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._timeout = timeout
        if default_headers:
            self._default_headers.update(default_headers)

    def _create_session(self) -> requests.Session:
        """Create a new session with retry strategy and connection pooling."""
        session = requests.Session()

        retry_strategy = Retry(
            total=self._max_retries,
            backoff_factor=self._backoff_factor,
            status_forcelist=self._status_forcelist,
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"],
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            pool_connections=self._pool_connections,
            pool_maxsize=self._pool_maxsize,
            max_retries=retry_strategy,
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update(self._default_headers)

        return session

    def get_session(self, base_url: str = None) -> requests.Session:
        """
        Get a session for the given base_url.
        Uses thread-local storage for thread safety.
        """
        # Use base_url host as key, or "default" for general use
        host_key = "default"
        if base_url:
            from urllib.parse import urlparse
            host_key = urlparse(base_url).netloc

        # Check thread-local first
        if not hasattr(self._local, "sessions"):
            self._local.sessions = {}

        if host_key not in self._local.sessions:
            with self._sessions_lock:
                if host_key not in self._sessions:
                    self._sessions[host_key] = self._create_session()
                self._local.sessions[host_key] = self._sessions[host_key]

        return self._local.sessions[host_key]

    def request(
        self,
        method: str,
        url: str,
        base_url: str = None,
        **kwargs,
    ) -> requests.Response:
        """Make a request using the pooled session."""
        session = self.get_session(base_url)
        timeout = kwargs.pop("timeout", self._timeout)
        return session.request(method, url, timeout=timeout, **kwargs)

    def get(self, url: str, base_url: str = None, **kwargs) -> requests.Response:
        return self.request("GET", url, base_url=base_url, **kwargs)

    def post(self, url: str, base_url: str = None, **kwargs) -> requests.Response:
        return self.request("POST", url, base_url=base_url, **kwargs)

    def put(self, url: str, base_url: str = None, **kwargs) -> requests.Response:
        return self.request("PUT", url, base_url=base_url, **kwargs)

    def delete(self, url: str, base_url: str = None, **kwargs) -> requests.Response:
        return self.request("DELETE", url, base_url=base_url, **kwargs)

    def close_all(self):
        """Close all sessions (useful for shutdown)."""
        with self._sessions_lock:
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception:
                    pass
            self._sessions.clear()

        # Also clear thread-local sessions
        if hasattr(self._local, "sessions"):
            self._local.sessions.clear()


# Global instance
session_manager = SessionManager()


def get_session(base_url: str = None) -> requests.Session:
    """Get a pooled session for the given base_url."""
    return session_manager.get_session(base_url)


def request(method: str, url: str, base_url: str = None, **kwargs) -> requests.Response:
    """Make a request using the global session manager."""
    return session_manager.request(method, url, base_url=base_url, **kwargs)


def get(url: str, base_url: str = None, **kwargs) -> requests.Response:
    return session_manager.get(url, base_url=base_url, **kwargs)


def post(url: str, base_url: str = None, **kwargs) -> requests.Response:
    return session_manager.post(url, base_url=base_url, **kwargs)


def put(url: str, base_url: str = None, **kwargs) -> requests.Response:
    return session_manager.put(url, base_url=base_url, **kwargs)


def delete(url: str, base_url: str = None, **kwargs) -> requests.Response:
    return session_manager.delete(url, base_url=base_url, **kwargs)