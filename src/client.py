import os
import re
import time
import random
import logging
from typing import Dict, Any, Optional, Union
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from src.auth import parse_cookie_string
from src.config import (
    BROWSER_PROFILES,
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
    - збереження стану сесії та cookies,
    - автоматичний повтор запитів при мережевих збоях (500, 502, 503, 504).
    """
    def __init__(
        self,
        cookies: Union[str, Dict[str, str]],
        account_name: str = "Default",
        user_agent: Optional[str] = None,
        client_hints: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None
    ):
        self.account_name = account_name
        self.session = requests.Session()

        # Автоматичний повтор запитів при мережевих помилках та 500/502/503/504
        retry_strategy = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST", "HEAD", "OPTIONS"],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        cookie_dict = parse_cookie_string(cookies) if isinstance(cookies, str) else (cookies or {})
        for k, v in cookie_dict.items():
            self.session.cookies.set(k, v, domain=".aliexpress.com", path="/")

        # Проксі: переданий явно, або зі змінної ALI_PROXY_{account_name}, або ALI_PROXY_{suffix}, або глобальної ALI_PROXY
        resolved_proxy = (
            proxy
            or os.getenv(f"ALI_PROXY_{account_name}")
            or os.getenv(f"ALI_PROXY_{account_name.removeprefix('Account_')}")
            or os.getenv("ALI_PROXY")
        )
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
            if account_name == "Default":
                self.user_agent, profile_hints = get_random_browser_profile()
            else:
                profile = random.Random(account_name).choice(BROWSER_PROFILES)
                self.user_agent, profile_hints = profile["user_agent"], dict(profile["client_hints"])
            self.client_hints = client_hints if client_hints is not None else profile_hints

        self._ensure_region_kr()
        self._init_headers()

    def _ensure_region_kr(self):
        """
        Забезпечує наявність субпараметра region=KR у cookie aep_usuc_f.
        Необхідно для обходу регіонального блокування чекіну в JS-бандлах AliExpress.
        """
        aep_val = self.get_cookie("aep_usuc_f")
        if aep_val:
            if "region=" in aep_val:
                new_aep = re.sub(r"region=[a-zA-Z0-9_-]+", "region=KR", aep_val)
            else:
                new_aep = f"{aep_val}&region=KR"
        else:
            new_aep = "region=KR&b_locale=en_KR&c_tp=USD"

        # Очищуємо попередні екземпляри cookie щоб уникнути дублікатів та конфліктів у RequestsCookieJar
        for c in list(self.session.cookies):
            if c.name == "aep_usuc_f":
                self.session.cookies.clear(c.domain, c.path, c.name)

        self.session.cookies.set("aep_usuc_f", new_aep, domain=".aliexpress.com", path="/")

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
        """Отримує актуальне значення cookie за ім'ям із поточної сесії."""
        matching = [c for c in self.session.cookies if c.name == name]
        if not matching:
            try:
                return self.session.cookies.get(name)
            except Exception:
                return self.session.cookies.get_dict().get(name)

        # Пріоритет домену .aliexpress.com або найновішому значенню
        for c in reversed(matching):
            if c.domain and "aliexpress.com" in c.domain:
                return c.value
        return matching[-1].value

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
