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


def _normalize_res(res_data: Any):
    """
    Нормалізує об'єкт відповіді (requests.Response, dict, str тощо) без ризику TypeError або
    хибного короткого замикання bool(Response) на статус-кодах 4xx/5xx.
    Повертає кортеж (текстовий_контент, словник_json_або_None).
    """
    if res_data is None:
        return "", None

    # Обробка об'єктів requests.Response (або duck-typed об'єктів)
    if hasattr(res_data, "text") or hasattr(res_data, "status_code"):
        parts = []
        try:
            parts.append(str(getattr(res_data, "text", "")))
        except Exception:
            pass
        try:
            url_val = str(getattr(res_data, "url", ""))
            if url_val:
                parts.append(url_val)
        except Exception:
            pass
        try:
            headers_val = str(getattr(res_data, "headers", ""))
            if headers_val:
                parts.append(headers_val)
        except Exception:
            pass

        full_text = " ".join(parts)
        json_obj = None
        if hasattr(res_data, "json"):
            try:
                parsed = res_data.json()
                if isinstance(parsed, dict):
                    json_obj = parsed
            except Exception:
                pass
        return full_text, json_obj

    if isinstance(res_data, dict):
        return str(res_data), res_data

    if isinstance(res_data, str):
        json_obj = None
        try:
            parsed = json.loads(res_data)
            if isinstance(parsed, dict):
                json_obj = parsed
        except Exception:
            pass
        return res_data, json_obj

    return str(res_data), None


def is_captcha_required(res_data: Any) -> bool:
    """
    Визначає, чи вимагає Alibaba WAF проходження верифікації (слайдер капчі).
    Сигнатури:
    - RGV587_RUN_SCRIPT: стандартний скрипт захисту Alibaba WAF
    - FAIL_SYS_USER_VALIDATE: MTOP код необхідності валідації користувача
    - _____tmd_____/punish: шлях редиректу на сторінку покарання / розгадування слайдера
    """
    if res_data is None:
        return False
    text, json_data = _normalize_res(res_data)
    if not text and not json_data:
        return False

    text_lower = text.lower()
    indicators = [
        "rgv587_run_script",
        "rgv587",
        "fail_sys_user_validate",
        "_____tmd_____/punish",
        "punish?x="
    ]
    if any(ind in text_lower for ind in indicators):
        return True

    if json_data:
        ret = json_data.get("ret")
        if isinstance(ret, list):
            for r in ret:
                r_lower = str(r).lower()
                if any(ind in r_lower for ind in indicators):
                    return True
        data_block = json_data.get("data")
        if isinstance(data_block, dict):
            url_field = str(data_block.get("url", "")).lower()
            if "_____tmd_____/punish" in url_field or "rgv587" in url_field:
                return True

    return False


def is_already_checked_in(res_data: Any) -> bool:
    """
    Визначає, чи було чекін уже зараховано раніше сьогодні.
    """
    if res_data is None:
        return False
    if is_captcha_required(res_data) or is_mtop_token_expired(res_data) or is_session_expired(res_data):
        return False

    text, json_data = _normalize_res(res_data)
    if not text and not json_data:
        return False

    if json_data:
        data_block = json_data.get("data")
        if isinstance(data_block, dict):
            inner = data_block.get("data")
            if isinstance(inner, dict):
                if inner.get("signToday") is False:
                    return False
                if inner.get("signToday") is True:
                    return True
            if data_block.get("signToday") is False:
                return False
            if data_block.get("signToday") is True:
                return True

        ret = json_data.get("ret")
        if isinstance(ret, list):
            for r in ret:
                r_lower = str(r).lower()
                if any(ind in r_lower for ind in [
                    "success_repeat_checkin", "repeat_checkin", "already_checked",
                    "already_signed", "has_checked", "has_signed", "haschecked", "ischecked"
                ]):
                    return True

    text_lower = text.lower()
    # Запобігаємо хибному спрацьовуванню на помилки типу "Token already expired" або "Please repeat later"
    if "token" in text_lower and "expired" in text_lower:
        return False
    if "repeat later" in text_lower or "flow_limit" in text_lower or "rate limit" in text_lower:
        return False

    indicators = [
        "has checked in", "have checked in", "already checked in", "already signed in",
        "already_checked", "success_repeat_checkin", "repeat_checkin",
        "haschecked", "ischecked", "checked_in_today", "already_signed",
        "has signed today", "already checked",
        'signtoday": true', 'signtoday":true'
    ]
    return any(ind in text_lower for ind in indicators)


def is_mtop_token_expired(res_data: Any) -> bool:
    """
    Визначає, чи повернув MTOP шлюз помилку застарілого/порожнього H5 токена.
    Це не означає прострочення сесії акаунта, а лише потребує отримання нового _m_h5_tk та повторного підпису.
    """
    if res_data is None:
        return False
    text, _ = _normalize_res(res_data)
    if not text:
        return False
    text_lower = text.lower()
    indicators = [
        "fail_sys_token_expired", "token_expired", "token_invalid", "token expired",
        "fail_sys_token_empty", "token_empty"
    ]
    return any(ind in text_lower for ind in indicators)


def is_session_expired(res_data: Any) -> bool:
    """
    Визначає, чи є сесія акаунта неавторизованою або простроченою (потребує оновлення cookies / повторного логіну).
    Примітка: MTOP FAIL_SYS_TOKEN_EXPIRED та капча WAF не входять сюди.
    """
    if res_data is None:
        return False
    if is_captcha_required(res_data) or is_mtop_token_expired(res_data):
        return False

    text, _ = _normalize_res(res_data)
    if not text:
        return False
    text_lower = text.lower()
    indicators = [
        "not_login", "need_login", "session_expired", "illegal_access",
        "login_required", "fail_sys_user_not_login", "user_not_login",
        "fail_sys_session_expired", "session_timeout",
        "login.aliexpress.com", "passport.aliexpress.com"
    ]
    return any(ind in text_lower for ind in indicators)


def is_checkin_success(res_data: Any) -> bool:
    """
    Безпечно перевіряє успішність виконання нового чекіну в JSON-відповіді або Response.
    Запобігає IndexError, TypeError та хибним спрацьовуванням від шлюзів MTOP при бізнес-помилках.
    """
    if res_data is None:
        return False
    _, json_data = _normalize_res(res_data)
    if not isinstance(json_data, dict):
        return False

    # Капча, повторний чекін, застарілий токен MTOP або прострочена сесія не є новим успішним чекіном
    if is_captcha_required(res_data) or is_already_checked_in(res_data) or is_session_expired(res_data) or is_mtop_token_expired(res_data):
        return False

    res_data = json_data

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
                if resp is not None and is_captcha_required(resp):
                    self.status = "CAPTCHA_REQUIRED"
                    logger.warning(f"[{self.account_name}] 🧩 Виявлено вимогу проходження капчі (WAF) у тексті відповіді!")
                    return None

                if not resp or resp.status_code != 200:
                    status = resp.status_code if resp else "None"
                    body = resp.text[:120] if resp else ""
                    logger.warning(f"[{self.account_name}] ⚠️ MTOP {api} повернув HTTP {status}: {body}")
                    continue

                res_data = resp.json()
                logger.info(f"[{self.account_name}] Відповідь від {url}: {res_data}")

                if is_captcha_required(res_data):
                    self.status = "CAPTCHA_REQUIRED"
                    logger.warning(f"[{self.account_name}] 🧩 Виявлено вимогу проходження капчі (WAF) у JSON відповіді!")
                    return res_data

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

    def _extract_coins_from_sign_list(self, res_data: Any):
        """Витягує зароблені монети із signList відповідно до поточного стріку."""
        if res_data is None or self.streak_days <= 0:
            return
        if not isinstance(res_data, dict):
            _, res_data = _normalize_res(res_data)
            if not isinstance(res_data, dict):
                return

        data_block = res_data.get("data")
        target_dict = data_block if isinstance(data_block, dict) else res_data
        inner_dict = target_dict.get("data") if isinstance(target_dict, dict) and isinstance(target_dict.get("data"), dict) else {}

        day_keys = ["days", "day", "dayNum", "signDays", "continuousCheckinDays", "streak", "index"]
        coin_keys = ["coins", "coin", "awardCoins", "coinCount", "points", "awardPoints", "rewardCoins", "amount"]

        for d in (inner_dict, target_dict, res_data):
            sign_list = d.get("signList")
            if isinstance(sign_list, list) and len(sign_list) > 0:
                parsed_items = []
                today_coins = None

                for item in sign_list:
                    if not isinstance(item, dict):
                        continue

                    # Кількість монет
                    item_coins = None
                    for ck in coin_keys:
                        val = item.get(ck)
                        if val is not None and isinstance(val, (int, str, float)):
                            try:
                                c = int(float(str(val).replace(",", "").strip()))
                                if c > 0:
                                    item_coins = c
                                    break
                            except (ValueError, TypeError):
                                pass

                    if item_coins is None:
                        continue

                    # Маркер сьогоднішнього дня
                    if any(item.get(k) is True for k in ("isToday", "today", "current", "signToday")):
                        today_coins = item_coins

                    # Номер дня
                    day_num = None
                    for dk in day_keys:
                        val = item.get(dk)
                        if val is not None and isinstance(val, (int, str, float)):
                            try:
                                day_num = int(float(str(val).replace(",", "").strip()))
                                break
                            except (ValueError, TypeError):
                                pass

                    parsed_items.append((day_num, item_coins))

                # 1. Точне співпадіння за номером дня (1-based: day == streak_days)
                for day_num, coins in parsed_items:
                    if day_num == self.streak_days:
                        self.coins_earned = coins
                        return

                # 2. Якщо стрік перевищує довжину циклу календаря (наприклад, день 8 у 7-денному циклі)
                cycle_day = ((self.streak_days - 1) % len(sign_list)) + 1
                for day_num, coins in parsed_items:
                    if day_num == cycle_day:
                        self.coins_earned = coins
                        return

                # 3. 0-індексовані дні (дні від 0 до N-1)
                for day_num, coins in parsed_items:
                    if day_num is not None and (day_num == self.streak_days - 1 or day_num == cycle_day - 1):
                        self.coins_earned = coins
                        return

                # 4. Якщо номер дня не збігся, але є елемент з позначкою сьогодні
                if today_coins is not None:
                    self.coins_earned = today_coins
                    return

    def _has_explicit_streak_days(self, res_data: Any) -> bool:
        """Перевіряє, чи містить відповідь явне значення стріку (signDays/streak тощо)."""
        if not isinstance(res_data, dict):
            _, res_data = _normalize_res(res_data)
            if not isinstance(res_data, dict):
                return False
        data_block = res_data.get("data")
        target_dict = data_block if isinstance(data_block, dict) else res_data
        inner_dict = target_dict.get("data") if isinstance(target_dict, dict) and isinstance(target_dict.get("data"), dict) else {}

        for d in (inner_dict, target_dict, res_data):
            if isinstance(d, dict):
                for key in ["continuousCheckinDays", "continuousDays", "signDays", "streak", "dayNum"]:
                    val = d.get(key)
                    if val is not None and isinstance(val, (int, str, float)):
                        try:
                            if int(float(str(val).replace(",", "").strip())) > 0:
                                return True
                        except (ValueError, TypeError):
                            pass
        return False

    def _extract_telemetry(self, res_data: Dict[str, Any], extract_coins: bool = True):
        """Витягує дані про дні стріку та нагородні монети із відповіді сервера."""
        if not isinstance(res_data, dict):
            _, res_data = _normalize_res(res_data)
            if not isinstance(res_data, dict):
                return

        data_block = res_data.get("data")
        target_dict = data_block if isinstance(data_block, dict) else res_data
        inner_dict = target_dict.get("data") if isinstance(target_dict, dict) and isinstance(target_dict.get("data"), dict) else {}

        # Дні стріку
        extracted_days = None
        for d in (inner_dict, target_dict, res_data):
            for key in ["continuousCheckinDays", "continuousDays", "signDays", "streak", "dayNum"]:
                val = d.get(key)
                if val is not None and isinstance(val, (int, str, float)):
                    try:
                        days = int(float(str(val).replace(",", "").strip()))
                        if days > 0:
                            extracted_days = days if extracted_days is None else max(extracted_days, days)
                    except (ValueError, TypeError):
                        pass

        if extracted_days is not None:
            self.streak_days = extracted_days

        # Якщо signToday явним чином False, це відповідь до чекіну, і монети є вчорашніми нагородами — їх не можна зараховувати!
        has_sign_today_false = any(
            isinstance(d, dict) and d.get("signToday") is False
            for d in (inner_dict, target_dict, res_data)
        )
        if has_sign_today_false:
            extract_coins = False

        # Зароблені монети (екстрагуються лише за підтвердженого успіху чекіну або signToday == True)
        if extract_coins:
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
                self._extract_coins_from_sign_list(res_data)

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
            if home_resp and is_captcha_required(home_resp):
                self.status = "CAPTCHA_REQUIRED"
                logger.warning(f"[{self.account_name}] 🧩 Сторінка PC Index вимагає капчі WAF!")
                return False
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

        if self.status == "CAPTCHA_REQUIRED":
            return False

        sign_today = None
        if status_res:
            if is_captcha_required(status_res):
                self.status = "CAPTCHA_REQUIRED"
                logger.warning(f"[{self.account_name}] 🧩 Статус чекіну повернув вимогу капчі (WAF)!")
                return False

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
                    self.streak_days = int(float(str(sign_days).replace(",", "").strip()))
                except (ValueError, TypeError):
                    pass

            is_already_done = sign_today is True or is_already_checked_in(status_res)
            self._extract_telemetry(status_res, extract_coins=is_already_done)

        # 3. Якщо вже зараховано сьогодні
        if sign_today is True or (status_res and is_already_checked_in(status_res)):
            self.status = "ALREADY_DONE"
            if self.coins_earned == 0 and status_res:
                self._extract_telemetry(status_res, extract_coins=True)
            logger.info(f"[{self.account_name}] ℹ️ Чекін на сьогодні вже зараховано (стрік: {self.streak_days} дн.)")
        else:
            # 4. Виконання чекіну через mtop.aliexpress.coin.pc.sign
            sign_res = self._call_mtop("mtop.aliexpress.coin.pc.sign", data=sign_payload)
            if self.status == "CAPTCHA_REQUIRED":
                logger.warning(f"[{self.account_name}] 🧩 Виконання чекіну повернуло вимогу капчі (WAF)!")
                return False

            if sign_res:
                if is_captcha_required(sign_res):
                    self.status = "CAPTCHA_REQUIRED"
                    logger.warning(f"[{self.account_name}] 🧩 Виконання чекіну повернуло вимогу капчі (WAF)!")
                    return False
                elif is_session_expired(sign_res):
                    self.session_valid = False
                    self.status = "SESSION_EXPIRED"
                    logger.error(f"[{self.account_name}] ❌ Сесія AliExpress недійсна або прострочена!")
                    return False
                elif is_already_checked_in(sign_res):
                    self.status = "ALREADY_DONE"
                    self._extract_telemetry(sign_res, extract_coins=True)
                    if self.coins_earned == 0 and status_res:
                        self._extract_coins_from_sign_list(status_res)
                    logger.info(f"[{self.account_name}] ℹ️ Чекін на сьогодні вже був виконаний раніше.")
                elif is_checkin_success(sign_res):
                    self.status = "SUCCESS"
                    has_explicit_days = self._has_explicit_streak_days(sign_res)
                    if not has_explicit_days:
                        self.streak_days = (self.streak_days + 1) if self.streak_days > 0 else 1
                    self._extract_telemetry(sign_res, extract_coins=True)
                    if self.streak_days == 0:
                        self.streak_days = 1
                    if self.coins_earned == 0 and status_res:
                        self._extract_coins_from_sign_list(status_res)
                    logger.info(f"[{self.account_name}] ✅ Чекін успішно виконано! (Стрік: {self.streak_days} дн.)")
                else:
                    self.status = "FAILED"
                    logger.warning(f"[{self.account_name}] ⚠️ Чекін не підтверджено автоматично (можливо, вже зараховано або потрібне оновлення cookies).")
            else:
                if self.status != "CAPTCHA_REQUIRED":
                    self.status = "FAILED"
                logger.warning(f"[{self.account_name}] ⚠️ Не отримано відповіді від ендпоінту чекіну.")

            if self.status == "CAPTCHA_REQUIRED":
                return False

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
