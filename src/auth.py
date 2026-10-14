import os
import re
import json
import logging
from typing import Dict, List, Tuple, Set, Any, Optional

logger = logging.getLogger(__name__)

ALIEXPRESS_CORE_COOKIES = {
    "aep_usuc_f", "xman_us_f", "_m_h5_tk", "xman_t",
    "login_aliyunid", "cna", "ali_apache_tracktmp", "acs_usuc_t"
}


class AccountConfig:
    """
    Конфігурація акаунта, яка підтримує:
    - доступ за властивостями: acc.name, acc.cookies, acc.proxy
    - повну зворотну сумісність із кортежем (name, cookies):
      `name, cookies = acc` та `acc[0]`, `acc[1]`.
    """
    def __init__(self, name: str, cookies: Dict[str, str], proxy: Optional[str] = None):
        self.name = name
        self.cookies = cookies
        self.proxy = proxy

    def __iter__(self):
        return iter((self.name, self.cookies))

    def __getitem__(self, index: int):
        return (self.name, self.cookies)[index]

    def __len__(self):
        return 2

    def __repr__(self):
        proxy_str = f", proxy='{self.proxy}'" if self.proxy else ""
        return f"AccountConfig(name='{self.name}', cookies={len(self.cookies)} keys{proxy_str})"


def validate_aliexpress_cookies(account_name: str, cookies: Dict[str, str]) -> bool:
    """
    Перевіряє, чи містить словник типові cookies AliExpress,
    і попереджає користувача у випадку підозрілих даних.
    """
    if not cookies:
        return False
    keys_lower = {k.lower() for k in cookies.keys()}
    common = keys_lower.intersection(ALIEXPRESS_CORE_COOKIES)
    if not common:
        logger.warning(
            f"⚠️ [{account_name}] Cookies не містять типових ключів сесії AliExpress "
            f"(xman_us_f, aep_usuc_f, _m_h5_tk). Можливо, вказано невірні дані."
        )
        return False
    return True


def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """
    Перетворює рядок cookies (скопійований з браузера DevTools Header або document.cookie)
    у словник для requests.
    """
    cookies: Dict[str, str] = {}
    if not cookie_str:
        return cookies

    cleaned = cookie_str.strip()
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned[7:].strip()

    items = cleaned.split(";")
    for item in items:
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, val = item.split("=", 1)
        k = key.strip()
        v = val.strip()
        if k:
            cookies[k] = v

    return cookies


def _cookie_fingerprint(cookies: Dict[str, str]) -> str:
    """
    Створює нормалізований відбиток cookies для надійної дедуплікації.
    Ігнорує динамічні та часові токени (isg, l, cf_clearance), а також
    нормалізує timestamp у _m_h5_tk.
    """
    volatile_keys = {"isg", "l", "cf_clearance", "cna", "ali_apache_tracktmp"}
    normalized_items = []
    for k, v in sorted(cookies.items(), key=lambda x: x[0].lower()):
        k_lower = k.lower()
        if k_lower in volatile_keys:
            continue
        if k_lower == "_m_h5_tk":
            v = v.split("_")[0] if "_" in v else v
        normalized_items.append(f"{k_lower}={v}")

    return ";".join(normalized_items)


def _natural_sort_key(s: str):
    """
    Ключ сортування для натурального числового порядку:
    'ALI_COOKIES_2' йтиме раніше за 'ALI_COOKIES_10'.
    """
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def load_accounts() -> List[AccountConfig]:
    """
    Завантажує конфігурації акаунтів з оточення:
      - ALI_COOKIES (основний акаунт або JSON рядок)
      - ALI_COOKIES_JSON (гнучка передача списку або словника акаунтів через один секрет)
      - ALI_COOKIES_1, ALI_COOKIES_2, ..., ALI_COOKIES_10... (для багатьох окремих секретів)

    Підтримує індивідуальні проксі:
      - У JSON: {"Account_1": {"cookies": "...", "proxy": "http://user:pass@ip:port"}}
      - Через змінні оточення: ALI_PROXY_1, ALI_PROXY_2... або глобальний ALI_PROXY.

    Забезпечує:
      - Природне числове сортування (1, 2, ..., 10)
      - Дедуплікацію акаунтів за вмістом cookies
      - Валідацію наявності cookies
    """
    accounts: List[AccountConfig] = []
    seen_fingerprints: Set[str] = set()

    # 0. Гнучка передача через ALI_COOKIES_JSON або JSON у ALI_COOKIES
    json_secrets: List[str] = []
    json_env = os.getenv("ALI_COOKIES_JSON")
    if json_env:
        json_secrets.append(json_env)

    single_cookie = os.getenv("ALI_COOKIES")
    if single_cookie and single_cookie.strip().startswith(("{", "[")):
        json_secrets.append(single_cookie)

    for raw_json in json_secrets:
        try:
            parsed_data = json.loads(raw_json)
            if isinstance(parsed_data, dict):
                for acc_id, c_data in parsed_data.items():
                    name = f"Account_{acc_id}" if not str(acc_id).startswith("Account_") else str(acc_id)
                    proxy = None
                    if isinstance(c_data, dict):
                        if "cookies" in c_data:
                            raw_cookies = c_data["cookies"]
                            proxy = c_data.get("proxy")
                            c_dict = parse_cookie_string(raw_cookies) if isinstance(raw_cookies, str) else (raw_cookies if isinstance(raw_cookies, dict) else {})
                        else:
                            c_dict = c_data
                    elif isinstance(c_data, str):
                        c_dict = parse_cookie_string(c_data)
                    else:
                        c_dict = {}

                    if c_dict:
                        fp = _cookie_fingerprint(c_dict)
                        if fp not in seen_fingerprints:
                            seen_fingerprints.add(fp)
                            acc = AccountConfig(name, c_dict, proxy=proxy)
                            accounts.append(acc)
                            validate_aliexpress_cookies(name, c_dict)
                            logger.info(f"Завантажено акаунт із JSON: {name}")
            elif isinstance(parsed_data, list):
                for idx, c_data in enumerate(parsed_data, 1):
                    name = f"Account_JSON_{idx}"
                    proxy = None
                    if isinstance(c_data, dict):
                        if "cookies" in c_data:
                            raw_cookies = c_data["cookies"]
                            proxy = c_data.get("proxy")
                            c_dict = parse_cookie_string(raw_cookies) if isinstance(raw_cookies, str) else (raw_cookies if isinstance(raw_cookies, dict) else {})
                        else:
                            c_dict = c_data
                    elif isinstance(c_data, str):
                        c_dict = parse_cookie_string(c_data)
                    else:
                        c_dict = {}

                    if c_dict:
                        fp = _cookie_fingerprint(c_dict)
                        if fp not in seen_fingerprints:
                            seen_fingerprints.add(fp)
                            acc = AccountConfig(name, c_dict, proxy=proxy)
                            accounts.append(acc)
                            validate_aliexpress_cookies(name, c_dict)
                            logger.info(f"Завантажено акаунт із JSON: {name}")
        except Exception as e:
            logger.warning(f"Не вдалося розібрати JSON зі змінної акаунтів: {e}")

    # 1. Перевірка основного акаунта ALI_COOKIES (якщо він не був JSON)
    if single_cookie and not single_cookie.strip().startswith(("{", "[")):
        parsed = parse_cookie_string(single_cookie)
        if parsed:
            fp = _cookie_fingerprint(parsed)
            if fp not in seen_fingerprints:
                seen_fingerprints.add(fp)
                proxy = os.getenv("ALI_PROXY_PRIMARY") or os.getenv("ALI_PROXY")
                acc = AccountConfig("Account_Primary", parsed, proxy=proxy)
                accounts.append(acc)
                validate_aliexpress_cookies("Account_Primary", parsed)
                logger.info("Завантажено основний акаунт: Account_Primary")
            else:
                logger.warning("Account_Primary пропущено: ідентичні cookies уже завантажені.")
        else:
            logger.warning("Змінна ALI_COOKIES знайдена, але не містить валідних пар ключ=значення.")

    # 2. Перевірка нумерованих змінних ALI_COOKIES_*
    pattern = re.compile(r"^ALI_COOKIES_(\w+)$", re.IGNORECASE)
    env_keys = [k for k in os.environ.keys() if pattern.match(k) and k.upper() != "ALI_COOKIES_JSON"]
    env_keys.sort(key=_natural_sort_key)

    for key in env_keys:
        match = pattern.match(key)
        if not match:
            continue

        suffix = match.group(1)
        account_name = f"Account_{suffix}"
        raw_val = os.environ[key]
        parsed = parse_cookie_string(raw_val)

        if not parsed:
            logger.warning(f"Змінна {key} порожня або не містить валідних cookies. Пропущено.")
            continue

        fp = _cookie_fingerprint(parsed)
        if fp in seen_fingerprints:
            logger.warning(f"⚠️ Пропущено {account_name}: ідентичні cookies вже додані під іншим ім'ям.")
            continue

        seen_fingerprints.add(fp)
        proxy = os.getenv(f"ALI_PROXY_{suffix}") or os.getenv("ALI_PROXY")
        acc = AccountConfig(account_name, parsed, proxy=proxy)
        accounts.append(acc)
        validate_aliexpress_cookies(account_name, parsed)
        logger.info(f"Завантажено акаунт: {account_name}")

    return accounts
