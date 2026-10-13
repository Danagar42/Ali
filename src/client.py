import time
import random
import logging
from typing import Dict, Any, Optional
import requests
from src.config import (
    get_random_user_agent,
    REQUEST_DELAY_MIN,
    REQUEST_DELAY_MAX,
    DEFAULT_REFERER
)

logger = logging.getLogger(__name__)

class AliClient:
    """
    HTTP-клієнт для взаємодії з AliExpress з захистом від детекту (рандомні паузи, ротація UA, правдоподібні хедери).
    """
    def __init__(self, cookies: Dict[str, str], account_name: str = "Default"):
        self.account_name = account_name
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self.user_agent = get_random_user_agent()
        self._init_headers()

    def _init_headers(self):
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,uk;q=0.8,ru;q=0.7",
            "Referer": DEFAULT_REFERER,
            "Origin": "https://www.aliexpress.com",
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        })

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
