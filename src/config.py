import os
import random
from typing import Dict, Tuple, List, Optional, Any

# Набір реалістичних профілів браузерів із відповідними Client Hints
BROWSER_PROFILES: List[Dict[str, Any]] = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "client_hints": {
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "client_hints": {
            "sec-ch-ua": '"Google Chrome";v="128", "Not=A?Brand";v="8", "Chromium";v="128"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "client_hints": {
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        }
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "client_hints": {}  # Firefox не надсилає sec-ch-ua заголовки
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "client_hints": {
            "sec-ch-ua": '"Microsoft Edge";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    },
    {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "client_hints": {
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
        }
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "client_hints": {}  # Safari не надсилає sec-ch-ua
    },
    {
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "client_hints": {}  # Mobile Safari не надсилає sec-ch-ua
    },
    {
        "user_agent": "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.70 Mobile Safari/537.36",
        "client_hints": {
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
        }
    }
]

# Список User-Agent для зворотної сумісності
USER_AGENTS = [profile["user_agent"] for profile in BROWSER_PROFILES]

def get_random_browser_profile() -> Tuple[str, Dict[str, str]]:
    """Повертає пару (User-Agent, відповідні Client Hints)."""
    profile = random.choice(BROWSER_PROFILES)
    return profile["user_agent"], dict(profile["client_hints"])

def get_client_hints_for_ua(user_agent: str) -> Dict[str, str]:
    """Визначає узгоджені Client Hints для конкретного User-Agent."""
    for profile in BROWSER_PROFILES:
        if profile["user_agent"] == user_agent:
            return dict(profile["client_hints"])

    # Fallback-детекція, якщо передано сторонній User-Agent
    ua_lower = user_agent.lower()
    if "firefox" in ua_lower or ("safari" in ua_lower and "chrome" not in ua_lower and "crios" not in ua_lower):
        return {}

    hints = {
        "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
        "sec-ch-ua-mobile": "?1" if ("mobile" in ua_lower or "android" in ua_lower) else "?0",
        "sec-ch-ua-platform": '"Android"' if "android" in ua_lower else (
            '"macOS"' if ("macintosh" in ua_lower or "mac os" in ua_lower) else (
                '"Linux"' if "linux" in ua_lower else '"Windows"'
            )
        )
    }
    return hints

def get_random_user_agent() -> str:
    """Повертає випадковий User-Agent."""
    return random.choice(USER_AGENTS)

# Налаштування затримок між HTTP-запитами (в секундах)
REQUEST_DELAY_MIN = 2.0
REQUEST_DELAY_MAX = 5.5

# Визначення середовища виконання
IS_CI = os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("CI") == "true"

# Налаштування початкового рандомного зсуву часу:
# У CI за замовчуванням обмежуємо 180 сек (3 хв), щоб не спалювати ліміти GitHub Actions;
# Локально/за розкладом — до 1800 сек (30 хв).
SKIP_RANDOM_SLEEP = os.getenv("SKIP_RANDOM_SLEEP", "0").lower() in ("1", "true", "yes")
_default_max_sleep = "180" if IS_CI else "1800"
MAX_START_SLEEP_SECONDS = int(os.getenv("MAX_START_SLEEP_SECONDS", _default_max_sleep))

# Базові налаштування AliExpress
ALIEXPRESS_COIN_PAGE = "https://www.aliexpress.com/p/coin-pc-index/index.html"
ALIEXPRESS_COIN_PAGE_LEGACY = "https://coins.aliexpress.com/"
DEFAULT_REFERER = "https://www.aliexpress.com/"
COIN_PC_REFERER = "https://www.aliexpress.com/p/coin-pc-index/index.html"
ORIGIN = "https://www.aliexpress.com"

# MTOP константи
MTOP_APP_KEY = "12574478"
MTOP_JSV = "2.7.5"
ASAC_CAMPAIGN = "2E227052O27XFLCLMIUIOA"
ASAC_MOBILE_CAMPAIGN = "2E2280406IM61M8MG1F2FB"

# Інтерактивна платформа завдань AliExpress (Coin Tasks)
TASK_SCENE_ID = 149
TASK_APP_KEY = "llcvfnh8tuknrw80cgstljy4h2y7i1rh"
PROJECT_APP_KEY = "llcvfnh8tuknrw80cgstljy4h2y7i1rh"
ASAC_TASK_CAMPAIGN = "2E247188QJLF705B9VMWWJ"
TASK_DELIVERY_QUERY_API = "mtop.aliexpress.interactive.task.delivery.query"
TASK_PROCESS_COMPLETE_API = "mtop.aliexpress.interactive.task.process.complete"
TASK_PROCESS_ISSUE_API = "mtop.aliexpress.interactive.task.process.issue"
TASK_LOCALE = os.getenv("TASK_LOCALE", "en_US")
TASK_LANG = os.getenv("TASK_LANG", "en_US")
TASK_CURRENCY = os.getenv("TASK_CURRENCY", "USD")
TASK_TTID = os.getenv("TASK_TTID", "201200@aliexpress_android_8.100.0")

