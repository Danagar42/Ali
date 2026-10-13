import os
import random

# Список актуальних та реалістичних User-Agent рядків (Desktop & Mobile Chrome/Edge/Safari)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/129.0.6668.69 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.70 Mobile Safari/537.36"
]

def get_random_user_agent() -> str:
    """Повертає випадковий User-Agent."""
    return random.choice(USER_AGENTS)

# Налаштування затримок (в секундах)
REQUEST_DELAY_MIN = 2.0
REQUEST_DELAY_MAX = 5.5

# Максимальний рандомний зсув початку виконання (0..1800 сек = до 30 хв)
# Можна відключити прапорцем SKIP_RANDOM_SLEEP=1 для локального тесту
SKIP_RANDOM_SLEEP = os.getenv("SKIP_RANDOM_SLEEP", "0").lower() in ("1", "true", "yes")
MAX_START_SLEEP_SECONDS = 1800

# Базові налаштування AliExpress
ALIEXPRESS_COIN_PAGE = "https://coins.aliexpress.com/"
DEFAULT_REFERER = "https://www.aliexpress.com/"
