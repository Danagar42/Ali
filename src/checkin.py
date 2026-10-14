import re
import time
import json
import hashlib
import logging
from typing import Dict, Any, Optional, Union
from src.client import AliClient
from src.config import (
    MTOP_APP_KEY,
    MTOP_JSV,
    ASAC_CAMPAIGN,
    COIN_PC_REFERER,
    ORIGIN
)

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
    if isinstance(res_data, dict):
        if is_mtop_token_expired(res_data) or is_session_expired(res_data):
            return False

        data_block = res_data.get("data")
        if isinstance(data_block, dict):
            inner = data_block.get("data")
            if isinstance(inner, dict) and inner.get("signToday") is True:
                return True
            if data_block.get("signToday") is True:
                return True

        ret = res_data.get("ret")
        if isinstance(ret, list):
            for r in ret:
                r_lower = str(r).lower()
                if any(ind in r_lower for ind in [
                    "success_repeat_checkin", "repeat_checkin", "already_checked",
                    "already_signed", "has_checked", "has_signed", "haschecked", "ischecked"
                ]):
                    return True

    text = str(res_data).lower()
    # Запобігаємо хибному спрацьовуванню на помилки типу "Token already expired" або "Please repeat later"
    if "token" in text and "expired" in text:
        return False
    if "repeat later" in text or "flow_limit" in text or "rate limit" in text:
        return False

    indicators = [
        "has checked in", "have checked in", "already checked in", "already signed in",
        "already_checked", "success_repeat_checkin", "repeat_checkin",
        "haschecked", "ischecked", "checked_in_today", "already_signed",
        "has signed today", "already checked",
        'signtoday": true', 'signtoday":true'
    ]
    return any(ind in text for ind in indicators)


def is_mtop_token_expired(res_data: Any) -> bool:
    """
    Визначає, чи повернув MTOP шлюз помилку застарілого/порожнього H5 токена.
    Це не означає прострочення сесії акаунта, а лише потребує отримання нового _m_h5_tk та повторного підпису.
    """
    if not res_data:
        return False
    text = str(res_data).lower()
    indicators = [
        "fail_sys_token_expired", "token_expired", "token_invalid", "token expired",
        "fail_sys_token_empty", "token_empty"
    ]
    return any(ind in text for ind in indicators)


def is_session_expired(res_data: Any) -> bool:
    """
    Визначає, чи є сесія акаунта неавторизованою або простроченою (потребує оновлення cookies / повторного логіну).
    Примітка: MTOP FAIL_SYS_TOKEN_EXPIRED не входить сюди, оскільки токен шлюзу оновлюється автоматично.
    """
    if not res_data:
        return False
    if is_mtop_token_expired(res_data):
        return False

    text = str(res_data).lower()
    indicators = [
        "not_login", "need_login", "session_expired", "illegal_access",
        "login_required", "fail_sys_user_not_login", "user_not_login",
        "fail_sys_session_expired", "session_timeout",
        "login.aliexpress.com", "passport.aliexpress.com"
    ]
    return any(ind in text for ind in indicators)


def is_checkin_success(res_data: Any) -> bool:
    """
    Безпечно перевіряє успішність виконання нового чекіну в JSON-відповіді.
    Запобігає IndexError, TypeError та хибним спрацьовуванням від шлюзів MTOP при бізнес-помилках.
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
    inner_block = None
    if isinstance(data_block, dict):
        if data_block.get("success") is False:
            return False
        if data_block.get("errorCode") or data_block.get("errorMsg") or data_block.get("errorMessage"):
            return False
        inner = data_block.get("data")
        if isinstance(inner, dict):
            inner_block = inner
            if inner_block.get("success") is False:
                return False
            if inner_block.get("errorCode") or inner_block.get("errorMsg"):
                return False

    result_block = res_data.get("result")
    if isinstance(result_block, dict):
        if result_block.get("success") is False:
            return False
        if result_block.get("errorCode") or result_block.get("errorMsg") or result_block.get("errorMessage"):
            return False

    # Явний прапорець успіху на рівні відповіді або блоків data/result
    if res_data.get("success") is True:
        return True
    if isinstance(data_block, dict) and data_block.get("success") is True:
        return True
    if isinstance(inner_block, dict) and inner_block.get("success") is True:
        return True
    if isinstance(result_block, dict) and result_block.get("success") is True:
        return True

    # Перевірка статусу шлюзу MTOP при відсутності помилок
    ret = res_data.get("ret")
    if isinstance(ret, list) and len(ret) > 0 and isinstance(ret[0], str):
        if ret[0].startswith("SUCCESS::") or ret[0] == "SUCCESS":
            return True
    elif isinstance(ret, str) and (ret.startswith("SUCCESS::") or ret == "SUCCESS"):
        return True

    if isinstance(data_block, dict) and (data_block.get("coinCount") or data_block.get("awardCoins") or data_block.get("coins")):
        return True

    return False


class CheckinManager:
    """
    Керує щоденним чекіном монет AliExpress за актуальним протоколом PC Index:
    - mtop.aliexpress.coin.pc.sign.list (перевірка статусу та стріку)
    - mtop.aliexpress.coin.pc.sign (виконання щоденного чекіну)
    - mtop.aliexpress.coin.execute (отримання точного балансу монет)
    - MTOP-підпис з автоматичним оновленням (Self-Healing токена)
    - Регіональна сумісність (KR region)
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
        api: str = "mtop.aliexpress.coin.pc.sign.list",
        data: Optional[Union[Dict[str, Any], str]] = None,
        app_key: str = MTOP_APP_KEY
    ) -> Optional[Dict[str, str]]:
        """
        Формує параметри MTOP з обов'язковим хеш-підписом sign та актуальним timestamp.
        """
        if data is None:
            if api == "mtop.aliexpress.coin.execute":
                data_obj = {"scene": "UserCoinNum"}
            else:
                data_obj = {"asac": ASAC_CAMPAIGN, "_lang": "en_KR", "_currency": "USD"}
            data_str = json.dumps(data_obj, separators=(",", ":"))
        elif isinstance(data, dict):
            data_str = json.dumps(data, separators=(",", ":"))
        else:
            data_str = str(data)

        cookie_val = self.client.get_cookie("_m_h5_tk")
        if not cookie_val:
            return None

        token = cookie_val.split("_")[0].strip()
        if not token:
            return None

        t = str(int(time.time() * 1000))
        sign = generate_mtop_sign(token, t, app_key, data_str)

        return {
            "jsv": MTOP_JSV,
            "appKey": app_key,
            "t": t,
            "sign": sign,
            "api": api,
            "v": "1.0",
            "type": "originaljson",
            "dataType": "json",
            "asac": ASAC_CAMPAIGN,
            "data": data_str
        }

    def _call_mtop(
        self,
        api: str,
        data: Optional[Union[Dict[str, Any], str]] = None,
        max_retries: int = 2
    ) -> Optional[Dict[str, Any]]:
        """
        Виконує підписаний POST-запит до MTOP шлюзу AliExpress
        із підтримкою автоматичного відновлення (Self-Healing) токена.
        """
        url = f"https://acs.aliexpress.com/h5/{api}/1.0/"
        headers = {
            "Referer": COIN_PC_REFERER,
            "Origin": ORIGIN,
            "Content-Type": "application/x-www-form-urlencoded",
            "asac": ASAC_CAMPAIGN,
        }

        # Якщо токен ще відсутній у сесії, виконуємо probe-запит для отримання _m_h5_tk від шлюзу
        if not self.client.get_cookie("_m_h5_tk"):
            try:
                probe_url = "https://acs.aliexpress.com/h5/mtop.aliexpress.coin.execute/1.0/"
                self.client.session.get(
                    probe_url,
                    params={"api": "mtop.aliexpress.coin.execute", "v": "1.0", "data": "{}"},
                    headers={"Referer": COIN_PC_REFERER, "Origin": ORIGIN},
                    timeout=10
                )
            except Exception as e:
                logger.debug(f"[{self.account_name}] MTOP probe запит: {e}")

        for attempt in range(max_retries):
            params = self._prepare_mtop_params(api=api, data=data)
            if not params:
                logger.warning(f"[{self.account_name}] ⚠️ Не вдалося сформувати підпис MTOP для {api}: у cookies відсутній обов'язковий токен '_m_h5_tk'!")
                return None

            data_str = params.get("data", "{}")
            query_params = {k: v for k, v in params.items() if k != "data"}

            try:
                resp = self.client.post(
                    url,
                    params=query_params,
                    data={"data": data_str},
                    headers=headers
                )
                if not resp or resp.status_code != 200:
                    status = resp.status_code if resp else "None"
                    body = resp.text[:120] if resp else ""
                    logger.warning(f"[{self.account_name}] ⚠️ MTOP {api} повернув HTTP {status}: {body}")
                    continue

                res_data = resp.json()
                logger.info(f"[{self.account_name}] Відповідь від {url}: {res_data}")

                if is_mtop_token_expired(res_data) and attempt < max_retries - 1:
                    logger.info(f"[{self.account_name}] 🔄 Отримано FAIL_SYS_TOKEN_EXPIRED для {api}. Оновлюємо токен та повторюємо спробу...")
                    continue

                return res_data
            except Exception as ex:
                logger.debug(f"[{self.account_name}] MTOP помилка для {api}: {ex}")

        return None

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
        inner_dict = target_dict.get("data") if isinstance(target_dict, dict) and isinstance(target_dict.get("data"), dict) else {}

        # Дні стріку
        for d in (inner_dict, target_dict, res_data):
            for key in ["continuousCheckinDays", "continuousDays", "signDays", "streak", "dayNum"]:
                val = d.get(key)
                if val is not None and isinstance(val, (int, str, float)):
                    try:
                        days = int(float(str(val).replace(",", "").strip()))
                        if days > 0:
                            self.streak_days = max(self.streak_days, days)
                    except (ValueError, TypeError):
                        pass

        # Зароблені монети
        for d in (inner_dict, target_dict, res_data):
            for key in ["awardCoins", "coinCount", "points", "awardPoints", "coin", "coins"]:
                val = d.get(key)
                if val is not None and isinstance(val, (int, str, float)):
                    try:
                        coins = int(float(str(val).replace(",", "").strip()))
                        if coins > 0:
                            self.coins_earned = coins
                    except (ValueError, TypeError):
                        pass

        # Якщо coins_earned ще 0, перевіряємо signList за номером стріку
        if self.coins_earned == 0:
            for d in (inner_dict, target_dict, res_data):
                sign_list = d.get("signList")
                if isinstance(sign_list, list) and self.streak_days > 0:
                    for item in sign_list:
                        if isinstance(item, dict):
                            day_num = item.get("days") or item.get("day")
                            if day_num == self.streak_days and item.get("coins"):
                                try:
                                    self.coins_earned = int(float(str(item["coins"]).replace(",", "").strip()))
                                    break
                                except (ValueError, TypeError):
                                    pass

        # Загальний баланс
        for d in (inner_dict, target_dict, res_data):
            for key in ["totalCoins", "currentCoins", "balance", "totalPoints", "userCoinsNum"]:
                val = d.get(key)
                if val is not None and isinstance(val, (int, str, float)):
                    try:
                        self.total_coins = int(float(str(val).replace(",", "").strip()))
                    except (ValueError, TypeError):
                        pass

    def run_checkin(self) -> bool:
        """
        Виконує щоденний чекін за діючим протоколом AliExpress PC Index:
        1) Отримання статусу через mtop.aliexpress.coin.pc.sign.list. Якщо signToday == true -> ALREADY_DONE, стрік signDays.
        2) Якщо signToday == false -> виклик mtop.aliexpress.coin.pc.sign.
        3) Отримання точного балансу монет через mtop.aliexpress.coin.execute (scene: "UserCoinNum").
        """
        logger.info(f"[{self.account_name}] 🔄 Початок щоденного чекіну...")

        # 1. Початковий візит до сторінки PC Index для ініціалізації сесії
        try:
            home_resp = self.client.get(COIN_PC_REFERER)
            if home_resp and home_resp.status_code == 200:
                if self._parse_coin_page_html(home_resp.text):
                    self.status = "ALREADY_DONE"
                    streak_info = f", Стрік: {self.streak_days} дн." if self.streak_days > 0 else ""
                    balance_info = f", Баланс: {self.total_coins} 🪙" if self.total_coins is not None else ""
                    logger.info(f"[{self.account_name}] ℹ️ З монетного центру виявлено: чекін на сьогодні вже зараховано!{streak_info}{balance_info}")
                    self._fetch_coin_balance()
                    return True
        except Exception as e:
            logger.debug(f"[{self.account_name}] Warmup запит до {COIN_PC_REFERER}: {e}")

        # 2. Отримання статусу чекіну через mtop.aliexpress.coin.pc.sign.list
        sign_payload = {"asac": ASAC_CAMPAIGN, "_lang": "en_KR", "_currency": "USD"}
        status_res = self._call_mtop("mtop.aliexpress.coin.pc.sign.list", data=sign_payload)

        sign_today = None
        if status_res:
            if is_session_expired(status_res):
                self.session_valid = False
                self.status = "SESSION_EXPIRED"
                logger.error(f"[{self.account_name}] ❌ Сесія AliExpress недійсна або прострочена!")
                return False

            data_block = status_res.get("data", {})
            inner_data = data_block.get("data", {}) if isinstance(data_block, dict) and isinstance(data_block.get("data"), dict) else (data_block if isinstance(data_block, dict) else {})

            sign_today = inner_data.get("signToday") if isinstance(inner_data, dict) else None
            if sign_today is None and isinstance(data_block, dict):
                sign_today = data_block.get("signToday")

            sign_days = inner_data.get("signDays") if isinstance(inner_data, dict) else None
            if sign_days is None and isinstance(data_block, dict):
                sign_days = data_block.get("signDays")
            if sign_days is not None:
                try:
                    self.streak_days = max(self.streak_days, int(float(str(sign_days).replace(",", "").strip())))
                except (ValueError, TypeError):
                    pass

            self._extract_telemetry(status_res)

        # 3. Якщо вже зараховано сьогодні
        if sign_today is True or (status_res and is_already_checked_in(status_res)):
            self.status = "ALREADY_DONE"
            logger.info(f"[{self.account_name}] ℹ️ Чекін на сьогодні вже зараховано (стрік: {self.streak_days} дн.)")
        else:
            # 4. Виконання чекіну через mtop.aliexpress.coin.pc.sign
            sign_res = self._call_mtop("mtop.aliexpress.coin.pc.sign", data=sign_payload)
            if sign_res:
                if is_session_expired(sign_res):
                    self.session_valid = False
                    self.status = "SESSION_EXPIRED"
                    logger.error(f"[{self.account_name}] ❌ Сесія AliExpress недійсна або прострочена!")
                    return False
                elif is_already_checked_in(sign_res):
                    self.status = "ALREADY_DONE"
                    self._extract_telemetry(sign_res)
                    logger.info(f"[{self.account_name}] ℹ️ Чекін на сьогодні вже був виконаний раніше.")
                elif is_checkin_success(sign_res):
                    self.status = "SUCCESS"
                    self._extract_telemetry(sign_res)
                    if self.streak_days == 0:
                        self.streak_days = 1
                    logger.info(f"[{self.account_name}] ✅ Чекін успішно виконано! (Стрік: {self.streak_days} дн.)")
                else:
                    self.status = "FAILED"
                    logger.warning(f"[{self.account_name}] ⚠️ Чекін не підтверджено автоматично (можливо, вже зараховано або потрібне оновлення cookies).")
            else:
                self.status = "FAILED"
                logger.warning(f"[{self.account_name}] ⚠️ Не отримано відповіді від ендпоінту чекіну.")

        # 5. Отримання точного балансу монет через mtop.aliexpress.coin.execute
        self._fetch_coin_balance()

        return self.status in ("SUCCESS", "ALREADY_DONE")

    def _fetch_coin_balance(self):
        """Отримує точний баланс монет через mtop.aliexpress.coin.execute (scene: 'UserCoinNum')."""
        exec_payload = {"scene": "UserCoinNum"}
        exec_res = self._call_mtop("mtop.aliexpress.coin.execute", data=exec_payload)
        if exec_res and isinstance(exec_res, dict):
            data_block = exec_res.get("data")
            if isinstance(data_block, dict):
                inner_list = data_block.get("data")
                if isinstance(inner_list, list):
                    for item in inner_list:
                        if isinstance(item, dict) and item.get("name") in ("userCoinsNum", "totalCoins", "coinCount"):
                            try:
                                clean_val = str(item.get("value", "")).replace(",", "").strip()
                                self.total_coins = int(float(clean_val))
                                break
                            except (ValueError, TypeError):
                                pass
                elif isinstance(inner_list, dict):
                    for k in ("userCoinsNum", "totalCoins", "coinCount", "coins"):
                        if k in inner_list:
                            try:
                                clean_val = str(inner_list[k]).replace(",", "").strip()
                                self.total_coins = int(float(clean_val))
                                break
                            except (ValueError, TypeError):
                                pass
                if self.total_coins is None:
                    for k in ("userCoinsNum", "totalCoins", "coinCount", "coins"):
                        if k in data_block:
                            try:
                                clean_val = str(data_block[k]).replace(",", "").strip()
                                self.total_coins = int(float(clean_val))
                                break
                            except (ValueError, TypeError):
                                pass
            elif isinstance(data_block, list):
                for item in data_block:
                    if isinstance(item, dict) and item.get("name") in ("userCoinsNum", "totalCoins", "coinCount"):
                        try:
                            clean_val = str(item.get("value", "")).replace(",", "").strip()
                            self.total_coins = int(float(clean_val))
                            break
                        except (ValueError, TypeError):
                            pass

        if self.total_coins is not None:
            logger.info(f"[{self.account_name}] 🪙 Поточний баланс монет: {self.total_coins}")
