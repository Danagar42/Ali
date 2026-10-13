import os
import re
from typing import Dict, List, Tuple

def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """
    Перетворює рядок cookies (скопійований з браузера DevTools Header або document.cookie)
    у словник для requests.
    """
    cookies: Dict[str, str] = {}
    if not cookie_str:
        return cookies

    # Очищуємо зайві пробіли та переноси рядків
    cleaned = cookie_str.strip()
    # Якщо скопійовано разом зі словом 'Cookie: '
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned[7:].strip()

    items = cleaned.split(";")
    for item in items:
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, val = item.split("=", 1)
        cookies[key.strip()] = val.strip()

    return cookies


def load_accounts() -> List[Tuple[str, Dict[str, str]]]:
    """
    Завантажує акаунти зі змінних середовища.
    Підтримує:
      - ALI_COOKIES (для одиночного акаунта)
      - ALI_COOKIES_1, ALI_COOKIES_2, ALI_COOKIES_3... (для багатьох)
    
    Повертає список пар: (ім'я_акаунта, словник_cookies)
    """
    accounts: List[Tuple[str, Dict[str, str]]] = []

    # 1. Перевірка одиничної змінної
    single_cookie = os.getenv("ALI_COOKIES")
    if single_cookie:
        parsed = parse_cookie_string(single_cookie)
        if parsed:
            accounts.append(("Account_Primary", parsed))

    # 2. Перевірка нумерованих змінних ALI_COOKIES_*
    pattern = re.compile(r"^ALI_COOKIES_(\w+)$", re.IGNORECASE)
    env_keys = sorted(os.environ.keys())

    for key in env_keys:
        match = pattern.match(key)
        if match:
            suffix = match.group(1)
            raw_val = os.environ[key]
            parsed = parse_cookie_string(raw_val)
            if parsed:
                account_name = f"Account_{suffix}"
                # Уникаємо дублювання, якщо ALI_COOKIES_1 збігається
                if not any(name == account_name for name, _ in accounts):
                    accounts.append((account_name, parsed))

    return accounts
