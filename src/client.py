import os
import re
import time
import random
import logging
from typing import Dict, Any, Optional
import requests
from src.config import (
    get_random_browser_profile,
    get_client_hints_for_ua,
    REQUEST_DELAY_MIN,
    REQUEST_DELAY_MAX,
    DEFAULT_REFERER
)

logger = logging.getLogger(__name__)


def mask_proxy_url(proxy_url: str) -> str:
    """Маскує облікові дані у URL проксі для безпечного логування."""
    if not proxy_url:
        return ""
    return re.sub(r"://([^:@]+):([^@]+)@", r"://\1:***@", proxy_url)


class AliClient:
    """
    HTTP-клієнт для взаємодії з AliExpress із захистом від детекту:
    - синхронізовані User-Agent та Client Hints під реальні платформи (Chrome, Firefox, Safari, Edge, Android),
    - рандомізовані затримки між запитами,
    - підтримка проксі (HTTP, HTTPS, SOCKS5),
    - збереження стану сесії та cookies.
    """
    def __init__(
        self,
        cookies: Dict[str, str],
        account_name: str = "Default",
        user_agent: Optional[str] = None,
        client_hints: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None
    ):
        self.account_name = account_name
        self.session = requests.Session()
        self.session.cookies.update(cookies)

        # Проксі: переданий явно, або зі змінної ALI_PROXY_{account_name}, або глобальної ALI_PROXY
        resolved_proxy = proxy or os.getenv(f"ALI_PROXY_{account_name}") or os.getenv("ALI_PROXY")
        if resolved_proxy:
            self.session.proxies.update({
                "http": resolved_proxy,
                "https": resolved_proxy
            })
            logger.info(f"[{account_name}] 🌐 Налаштовано проксі: {mask_proxy_url(resolved_proxy)}")

        if user_agent:
            self.user_agent = user_agent
            self.client_hints = client_hints if client_hints is not None else get_client_hints_for_ua(user_agent)
        else:
            self.user_agent, profile_hints = get_random_browser_profile()
            self.client_hints = client_hints if client_hints is not None else profile_hints

        self._init_headers()

    def _init_headers(self):
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,uk;q=0.8,ru;q=0.7",
            "Referer": DEFAULT_REFERER,
            "Origin": "https://www.aliexpress.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        if self.client_hints:
            headers.update(self.client_hints)

        self.session.headers.update(headers)

    def get_cookie(self, name: str) -> Optional[str]:
        """Отримує значення cookie за ім'ям із поточної сесії."""
        return self.session.cookies.get(name)

    def sleep_random(self, min_s: float = REQUEST_DELAY_MIN, max_s: float = REQUEST_DELAY_MAX):
        """Рандомна затримка між запитами для імітації людини."""
        delay = random.uniform(min_s, max_s)
        logger.debug(f"[{self.account_name}] Анти-детект пауза {delay:.2f} сек...")
        time.sleep(delay)

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, **kwargs) -> Optional[requests.Response]:
        self.sleep_random()
        try:
            resp = self.session.get(url, params=params, timeout=20, **kwargs)
            return resp
        except requests.RequestException as e:
            logger.error(f"[{self.account_name}] Помилка GET запиту до {url}: {e}")
            return None

    def post(self, url: str, data: Optional[Any] = None, json: Optional[Dict[str, Any]] = None, **kwargs) -> Optional[requests.Response]:
        self.sleep_random()
        try:
            resp = self.session.post(url, data=data, json=json, timeout=20, **kwargs)
            return resp
        except requests.RequestException as e:
            logger.error(f"[{self.account_name}] Помилка POST запиту до {url}: {e}")
            return None
