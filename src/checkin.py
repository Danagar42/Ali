import re
import time
import hashlib
import logging
from typing import Dict, Any, Optional
from src.client import AliClient

logger = logging.getLogger(__name__)


def generate_mtop_sign(token: str, t: str, app_key: str, data: str) -> str:
    """Генерує MD5 підпис для запитів до MTOP шлюзу Alibaba."""
    raw = f"{token}&{t}&{app_key}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def is_already_checked_in(res_data: Any) -> bool:
    """
    Визначає, чи було чекін уже зараховано раніше сьогодні.
    """
    if not res_data:
        return False
    text = str(res_data).lower()
    indicators = [
        "already", "repeat", "has checked", "have checked",
        "already_checked", "success_repeat_checkin", "repeat_checkin",
        "haschecked", "ischecked", "checked_in_today", "already_signed"
    ]
    return any(ind in text for ind in indicators)


def is_mtop_token_expired(res_data: Any) -> bool:
    """
    Визначає, чи повернув MTOP шлюз помилку застарілого H5 токена (FAIL_SYS_TOKEN_EXPIRED).
    Це не означає прострочення сесії акаунта, а лише потребує отримання нового _m_h5_tk та повторного підпису.
    """
    if not res_data:
        return False
    text = str(res_data).lower()
    indicators = ["fail_sys_token_expired", "token_expired", "token_invalid", "token expired"]
    return any(ind in text for ind in indicators)


def is_session_expired(res_data: Any) -> bool:
    """
    Визначає, чи є сесія акаунта неавторизованою або простроченою (потребує логіну).
    Примітка: MTOP FAIL_SYS_TOKEN_EXPIRED не входить сюди, оскільки токен шлюзу оновлюється автоматично.
    """
    if not res_data:
        return False
    text = str(res_data).lower()
    # Якщо це тимчасова помилка токена MTOP, це не є смертю сесії користувача
    if is_mtop_token_expired(res_data):
        return False

    indicators = [
        "not_login", "need_login", "session_expired", "illegal_access",
        "login_required", "fail_sys_user_not_login", "user_not_login",
        "login.aliexpress.com", "passport.aliexpress.com"
    ]
    return any(ind in text for ind in indicators)


def is_checkin_success(res_data: Any) -> bool:
    """
    Безпечно перевіряє успішність виконання нового чекіну в JSON-відповіді.
    Запобігає IndexError, TypeError та хибним спрацьовуванням від шлюзів MTOP.
    """
    if not isinstance(res_data, dict):
        return False

    # Повторний чекін, застарілий токен MTOP або прострочена сесія не є новим успішним чекіном
    if is_already_checked_in(res_data) or is_session_expired(res_data) or is_mtop_token_expired(res_data):
        return False

    # Перевірка на явний прапорець невдачі
    if res_data.get("success") is False:
        return False

    data_block = res_data.get("data")
    if isinstance(data_block, dict) and data_block.get("success") is False:
        return False

    result_block = res_data.get("result")
    if isinstance(result_block, dict) and result_block.get("success") is False:
        return False

    # Явний прапорець успіху на рівні відповіді або блоків data/result
    if res_data.get("success") is True:
        return True
    if isinstance(data_block, dict) and data_block.get("success") is True:
        return True
    if isinstance(result_block, dict) and result_block.get("success") is True:
        return True

    # Перевірка статусу шлюзу MTOP
    ret = res_data.get("ret")
    if isinstance(ret, list) and len(ret) > 0 and isinstance(ret[0], str):
        ret_val = ret[0]
        if ret_val.startswith("SUCCESS::") or ret_val == "SUCCESS":
            return True
    elif isinstance(ret, str) and (ret.startswith("SUCCESS::") or ret == "SUCCESS"):
        return True

    # Успішне нарахування монет у тілі відповіді
    if isinstance(data_block, dict) and (data_block.get("coinCount") or data_block.get("awardCoins")):
        return True

    return False


class CheckinManager:
    """
    Керує щоденним чекіном монет AliExpress з підтримкою:
    - MTOP-підпису з автоматичним оновленням (Self-Healing при FAIL_SYS_TOKEN_EXPIRED),
    - аналізу сторінки монетного центру (виявлення вже виконаного чекіну, стріку, балансу),
    - збору телеметрії стріку та балансу монет,
    - захисту від хибних спрацьовувань.
    """

    def __init__(self, client: AliClient):
        self.client = client
        self.account_name = client.account_name
        self.status = "FAILED"  # SUCCESS, ALREADY_DONE, SESSION_EXPIRED, FAILED
        self.streak_days = 0
        self.coins_earned = 0
        self.total_coins: Optional[int] = None
        self.session_valid = True

    def _prepare_mtop_params(
        self,
        api: str = "mtop.aliexpress.growth.coins.checkin",
        data: str = '{"channel":"COIN"}',
        app_key: str = "12574478"
    ) -> Optional[Dict[str, str]]:
        """
        Формує параметри MTOP з обов'язковим хеш-підписом sign та актуальним timestamp.
        """
        cookie_val = self.client.get_cookie("_m_h5_tk")
        if not cookie_val:
            return None

        token = cookie_val.split("_")[0].strip()
        if not token:
            return None
        t = str(int(time.time() * 1000))
        sign = generate_mtop_sign(token, t, app_key, data)

        return {
            "jsv": "2.7.2",
            "appKey": app_key,
            "t": t,
            "sign": sign,
            "api": api,
            "v": "1.0",
            "type": "originaljson",
            "dataType": "json",
            "data": data
        }

    def _parse_coin_page_html(self, html: str) -> bool:
        """
        Аналізує HTML сторінки монет на наявність балансу, стріку та статусу чекіну.
        Повертає True, якщо зафіксовано, що користувач сьогодні вже зачекінився.
        """
        if not html or not isinstance(html, str):
            return False

        # Витяг днів стріку
        streak_patterns = [
            r'["\'](?:continuousDays|signDays|continuousCheckinDays|streakDays|dayNum)["\']\s*:\s*(\d+)',
            r'data-continuous-days=["\'](\d+)["\']'
        ]
        for p in streak_patterns:
            m = re.search(p, html, re.IGNORECASE)
            if m:
                try:
                    self.streak_days = max(self.streak_days, int(m.group(1)))
                    break
                except ValueError:
                    pass

        # Витяг загального балансу монет
        balance_patterns = [
            r'["\'](?:totalCoins|coinBalance|currentCoins|coinsTotal)["\']\s*:\s*(\d+)',
            r'data-coin-balance=["\'](\d+)["\']'
        ]
        for p in balance_patterns:
            m = re.search(p, html, re.IGNORECASE)
            if m:
                try:
                    self.total_coins = int(m.group(1))
                    break
                except ValueError:
                    pass

        # Перевірка чи чекін уже виконаний
        already_patterns = [
            r'["\'](?:isSignInToday|hasSignIn|isSignIn|alreadySign|signInToday|hasChecked)["\']\s*:\s*true',
            r'["\']signInStatus["\']\s*:\s*(?:1|"1"|"DONE"|"SUCCESS"|"ALREADY")',
            r'class="[^"]*signed-in[^"]*"'
        ]
        for p in already_patterns:
            if re.search(p, html, re.IGNORECASE):
                return True

        return False

    def _extract_telemetry(self, res_data: Dict[str, Any]):
        """Витягує дані про дні стріку та нагородні монети із відповіді сервера."""
        if not isinstance(res_data, dict):
            return

        data_block = res_data.get("data")
        target_dict = data_block if isinstance(data_block, dict) else res_data

        # Дні стріку
        for key in ["continuousCheckinDays", "continuousDays", "signDays", "streak", "dayNum"]:
            val = target_dict.get(key)
            if val is not None and isinstance(val, (int, str)):
                try:
                    days = int(val)
                    if days > 0:
                        self.streak_days = max(self.streak_days, days)
                except ValueError:
                    pass

        # Зароблені монети
        for key in ["awardCoins", "coinCount", "points", "awardPoints", "coin"]:
            val = target_dict.get(key)
            if val is not None and isinstance(val, (int, str)):
                try:
                    coins = int(val)
                    if coins > 0:
                        self.coins_earned = coins
                except ValueError:
                    pass

        # Загальний баланс
        for key in ["totalCoins", "currentCoins", "balance", "totalPoints"]:
            val = target_dict.get(key)
            if val is not None and isinstance(val, (int, str)):
                try:
                    self.total_coins = int(val)
                except ValueError:
                    pass

    def run_checkin(self) -> bool:
        """
        Виконує спробу щоденного чекіну через актуальні endpoints AliExpress
        з автоматичним retry при оновленні токену MTOP.
        """
        logger.info(f"[{self.account_name}] 🔄 Початок щоденного чекіну...")

        # 1. Відвідуємо сторінки монетного центру (включаючи новий PC Index та Mobile H5)
        coin_urls = [
            "https://www.aliexpress.com/p/coin-pc-index/index.html",
            "https://m.aliexpress.com/p/coin-index/index.html",
            "https://m.aliexpress.com/app/coins.html",
            "https://coins.aliexpress.com/"
        ]
        already_checked_from_html = False
        for c_url in coin_urls:
            home_resp = self.client.get(c_url)
            if home_resp and home_resp.status_code == 200:
                if self._parse_coin_page_html(home_resp.text):
                    already_checked_from_html = True
                    break

        if self.total_coins is not None or self.streak_days > 0:
            logger.info(f"[{self.account_name}] 📊 Дані з кабінету монет: баланс {self.total_coins or 0} 🪙, стрік {self.streak_days} дн.")

        if already_checked_from_html:
            self.status = "ALREADY_DONE"
            streak_info = f", Стрік: {self.streak_days} дн." if self.streak_days > 0 else ""
            balance_info = f", Баланс: {self.total_coins} 🪙" if self.total_coins is not None else ""
            logger.info(f"[{self.account_name}] ℹ️ З монетного центру виявлено: чекін на сьогодні вже зараховано!{streak_info}{balance_info}")
            return True

        # 2. Формування ендпоінтів для чекіну
        checkin_endpoints = []

        # Актуальні MTOP API методи чекіну (H5 та Mobile)
        mtop_apis = [
            ("mtop.aliexpress.growth.bonus.daily.signin", '{"channel":"COIN"}', "12574478"),
            ("mtop.aliexpress.growth.coin.sign.in", '{"channel":"COIN"}', "12574478"),
            ("mtop.aliexpress.growth.user.signin", '{"channel":"COIN"}', "12574478"),
            ("mtop.aliexpress.growth.coin.signin", '{"channel":"COIN"}', "12574478"),
            ("mtop.aliexpress.coin.signIn", '{"channel":"COIN"}', "12574478"),
            ("mtop.aliexpress.user.grow.task.finish", '{"channel":"COIN","taskType":"SIGN"}', "12574478"),
            ("mtop.aliexpress.growth.bonus.daily.signin", '{"channel":"COIN"}', "21407387"),
            ("mtop.aliexpress.growth.coin.sign.in", '{"channel":"COIN"}', "21407387"),
        ]

        for api_name, api_data, app_key in mtop_apis:
            mtop_params = self._prepare_mtop_params(api=api_name, data=api_data, app_key=app_key)
            if mtop_params:
                checkin_endpoints.append({
                    "url": f"https://acs.aliexpress.com/h5/{api_name}/1.0/",
                    "method": "GET",
                    "params": mtop_params,
                    "is_mtop": True,
                    "api_name": api_name,
                    "api_data": api_data,
                    "app_key": app_key
                })

        # Додаткові HTTP POST ендпоінти
        checkin_endpoints.extend([
            {
                "url": "https://coins.aliexpress.com/api/sign/signIn",
                "method": "POST",
                "json": {"channel": "COIN"}
            },
            {
                "url": "https://coins.aliexpress.com/api/checkin",
                "method": "POST",
                "json": {}
            }
        ])

        success = False
        for ep in checkin_endpoints:
            url = ep["url"]
            method = ep["method"]
            is_mtop = ep.get("is_mtop", False)

            for attempt in range(2 if is_mtop else 1):
                try:
                    if method == "POST":
                        resp = self.client.post(url, json=ep.get("json", {}))
                    else:
                        resp = self.client.get(url, params=ep.get("params"))

                    if not resp or resp.status_code != 200:
                        continue

                    try:
                        res_data = resp.json()
                        logger.info(f"[{self.account_name}] Відповідь від {url}: {res_data}")

                        # Перевірка Self-Healing для MTOP токена
                        if is_mtop and is_mtop_token_expired(res_data) and attempt == 0:
                            logger.info(f"[{self.account_name}] 🔄 Отримано FAIL_SYS_TOKEN_EXPIRED. Оновлюємо MTOP-токен та повторюємо спробу...")
                            new_params = self._prepare_mtop_params(
                                api=ep.get("api_name", "mtop.aliexpress.growth.bonus.daily.signin"),
                                data=ep.get("api_data", '{"channel":"COIN"}'),
                                app_key=ep.get("app_key", "12574478")
                            )
                            if new_params:
                                ep["params"] = new_params
                                continue

                        if is_session_expired(res_data):
                            self.session_valid = False
                            self.status = "SESSION_EXPIRED"
                            logger.error(f"[{self.account_name}] ❌ Сесія AliExpress недійсна або прострочена для {url}!")
                        elif is_already_checked_in(res_data):
                            self._extract_telemetry(res_data)
                            self.status = "ALREADY_DONE"
                            logger.info(f"[{self.account_name}] ℹ️ Чекін на сьогодні вже був виконаний раніше.")
                            success = True
                            break
                        elif is_checkin_success(res_data):
                            self._extract_telemetry(res_data)
                            self.status = "SUCCESS"
                            logger.info(f"[{self.account_name}] ✅ Чекін успішно виконано! (Стрік: {self.streak_days} дн.)")
                            success = True
                            break
                    except Exception as parse_err:
                        logger.debug(f"[{self.account_name}] Відповідь від {url} не є JSON: {parse_err}")

                except Exception as ex:
                    logger.debug(f"[{self.account_name}] Endpoint {url} повернув виняток: {ex}")

            if success:
                break

        if not success and self.status != "SESSION_EXPIRED":
            self.status = "FAILED"
            logger.warning(f"[{self.account_name}] ⚠️ Чекін не підтверджено автоматично (можливо, вже зараховано або потрібне оновлення cookies).")

        return success
