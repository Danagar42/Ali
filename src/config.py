import os
import random
from typing import Dict, Tuple, List, Optional, Any

# Набір реалістичних профілів браузерів із відповідними Client Hints
BROWSER_PROFILES: List[Dict[str, Any]] = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "client_hints": {
            "sec-ch-ua": '"Google Chrome";v="140", "Not=A?Brand";v="8", "Chromium";v="140"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "client_hints": {
            "sec-ch-ua": '"Google Chrome";v="138", "Not=A?Brand";v="8", "Chromium";v="138"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "client_hints": {
            "sec-ch-ua": '"Google Chrome";v="140", "Not=A?Brand";v="8", "Chromium";v="140"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        }
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
        "client_hints": {}  # Firefox не надсилає sec-ch-ua заголовки
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0",
        "client_hints": {
            "sec-ch-ua": '"Microsoft Edge";v="138", "Not=A?Brand";v="8", "Chromium";v="138"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    },
    {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "client_hints": {
            "sec-ch-ua": '"Google Chrome";v="140", "Not=A?Brand";v="8", "Chromium";v="140"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
        }
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        "client_hints": {}  # Safari не надсилає sec-ch-ua
    },
    {
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
        "client_hints": {}  # Mobile Safari не надсилає sec-ch-ua
    },
    {
        "user_agent": "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.6668.70 Mobile Safari/537.36",
        "client_hints": {
            "sec-ch-ua": '"Google Chrome";v="138", "Not=A?Brand";v="8", "Chromium";v="138"',
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

def _env_int(name: str, default: int) -> int:
    """Безпечно парсить цілочисельне значення зі змінної оточення."""
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default

# Налаштування початкового рандомного зсуву часу:
# У CI за замовчуванням обмежуємо 180 сек (3 хв), щоб не спалювати ліміти GitHub Actions;
# Локально/за розкладом — до 1800 сек (30 хв).
SKIP_RANDOM_SLEEP = os.getenv("SKIP_RANDOM_SLEEP", "0").lower() in ("1", "true", "yes")
_default_max_sleep = 180 if IS_CI else 1800
MAX_START_SLEEP_SECONDS = _env_int("MAX_START_SLEEP_SECONDS", _default_max_sleep)

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
TASK_SCENE_ID = _env_int("TASK_SCENE_ID", 149)
TASK_APP_KEY = os.getenv("TASK_APP_KEY", "llcvfnh8tuknrw80cgstljy4h2y7i1rh")
PROJECT_APP_KEY = os.getenv("PROJECT_APP_KEY", os.getenv("TASK_APP_KEY", "llcvfnh8tuknrw80cgstljy4h2y7i1rh"))
ASAC_TASK_CAMPAIGN = os.getenv("ASAC_TASK_CAMPAIGN", "2E247188QJLF705B9VMWWJ")
TASK_DELIVERY_QUERY_API = "mtop.aliexpress.interactive.task.delivery.query"
TASK_PROCESS_COMPLETE_API = "mtop.aliexpress.interactive.task.process.complete"
TASK_PROCESS_ISSUE_API = "mtop.aliexpress.interactive.task.process.issue"
TASK_LOCALE = os.getenv("TASK_LOCALE", "UA")
TASK_LANG = os.getenv("TASK_LANG", "en_US")
TASK_CURRENCY = os.getenv("TASK_CURRENCY", "UAH")
TASK_TTID = os.getenv("TASK_TTID", "@ae_msite_1")
TASK_PLATFORM = os.getenv("TASK_PLATFORM", "android")
TASK_UMID_TOKEN = os.getenv("TASK_UMID_TOKEN", "T2gA1jIALVYecdoa1onvC24McgCfk9c4nhceoLAzw36kxqGPO6h_HOQJGtmoGclimx8=")
TASK_BX_UMIDTOKEN = os.getenv("TASK_BX_UMIDTOKEN", "T2gAJf8iJkyJCy1iYOyIw3Mx_GChuoEw_ds8gFo3GJfC_qzqm2SJRF4_f2cB9u2NtoI=")
TASK_MTOP_APP_KEY = os.getenv("TASK_MTOP_APP_KEY", "24815441")
TASK_PLATFORM_CANDIDATES = ["android", "wireless", "h5", "app", "iOS"]
MAX_SAFE_TASKS_PER_RUN = _env_int("MAX_SAFE_TASKS_PER_RUN", 25)

