import re
import time
import json
import hashlib
import logging
from typing import Dict, Any, Optional, Union, Tuple, List
from src.client import AliClient
from src.config import (
    MTOP_APP_KEY,
    MTOP_JSV,
    ASAC_CAMPAIGN,
    ASAC_MOBILE_CAMPAIGN,
    COIN_PC_REFERER,
    ORIGIN,
    TASK_SCENE_ID,
    TASK_APP_KEY,
    PROJECT_APP_KEY,
    ASAC_TASK_CAMPAIGN,
    TASK_DELIVERY_QUERY_API,
    TASK_PROCESS_COMPLETE_API,
    TASK_PROCESS_ISSUE_API,
    TASK_LOCALE,
    TASK_LANG,
    TASK_CURRENCY,
    TASK_TTID,
    TASK_PLATFORM,
    TASK_UMID_TOKEN,
    TASK_BX_UMIDTOKEN,
    TASK_MTOP_APP_KEY
)

logger = logging.getLogger(__name__)

# Мобільний канал чекіну монет AliExpress (Coin Channel MTOP ендпоінти)
COIN_MOBILE_REFERER = "https://m.aliexpress.com/p/coin-index/index.html"
MOBILE_ORIGIN = "https://m.aliexpress.com"

# Актуальні верифіковані ендпоінти мобільного чекіну (COIN_CHANNEL_2.0)
MOBILE_SIGN_LIST_API = "mtop.aliexpress.coin.channel.sign.list"
MOBILE_SIGN_EXECUTE_API = "mtop.aliexpress.coin.channel.sign.execute"
MOBILE_SIGN_QUERY_LIST_API = "mtop.aliexpress.social.interactive.sign.model.query.list"
MOBILE_RECEIVE_PRIZE_API = "mtop.aliexpress.social.interactive.sign.user.receive.prize"
MOBILE_CHECKIN_APIS = [
    MOBILE_SIGN_EXECUTE_API,
    MOBILE_RECEIVE_PRIZE_API
]


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


def _collect_nested_dicts(obj: Any, depth: int = 0, max_depth: int = 6) -> List[Dict[str, Any]]:
    """Рекурсивно збирає всі словники з вкладених структур JSON (включаючи списки та дочірні блоки)."""
    if depth > max_depth or obj is None:
        return []
    blocks: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        blocks.append(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                blocks.extend(_collect_nested_dicts(v, depth + 1, max_depth))
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                blocks.extend(_collect_nested_dicts(item, depth + 1, max_depth))
    return blocks


def _clean_int(val: Any) -> int:
    """Безпечно парсить цілочисельне значення з будь-якого типу (рядок з комами/пробілами/суфіксами, float), ігноруючи булеві прапорці."""
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, (int, float)):
        try:
            return int(val)
        except (ValueError, OverflowError):
            return 0
    # Видаляємо пробіли та коми між цифрами, наприклад "1 200" або "1,500"
    s = re.sub(r'(?<=\d)[\s,]+(?=\d)', '', str(val).strip())
    m = re.search(r'(\d+)', s)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, TypeError):
            pass
    return 0


def _find_mobile_daily_sign_node(res_data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Знаходить вузол сьогоднішнього дня (calendarDayDistance == 0 або calendarDayDistance відсутнє/None)
    та елемент signResultList у структурах signQuerySequenceNodeList / dailySignNodeList.
    Повертає кортеж: (today_node_dict, sign_result_dict).
    """
    if res_data is None:
        return None, None
    _, json_data = _normalize_res(res_data)
    if not isinstance(json_data, dict):
        return None, None

    all_blocks = _collect_nested_dicts(json_data)
    all_daily_nodes: List[Dict[str, Any]] = []
    seen_node_ids = set()

    for b in all_blocks:
        if "dailySignNodeList" in b and isinstance(b["dailySignNodeList"], list):
            for n in b["dailySignNodeList"]:
                if isinstance(n, dict) and id(n) not in seen_node_ids:
                    seen_node_ids.add(id(n))
                    all_daily_nodes.append(n)

    if not all_daily_nodes:
        return None, None

    # 1. Перший пріоритет: явне calendarDayDistance == 0
    today_node: Optional[Dict[str, Any]] = None
    has_explicit_distances = False

    for node in all_daily_nodes:
        dist = node.get("calendarDayDistance")
        if dist is not None and not isinstance(dist, bool):
            try:
                d_val = int(float(str(dist).strip()))
                has_explicit_distances = True
                if d_val == 0:
                    today_node = node
                    break
            except (ValueError, TypeError):
                pass

    # 2. Другий пріоритет: прапорці поточного дня (isToday / today / current / signToday / isCurrent)
    if today_node is None:
        for node in all_daily_nodes:
            if any(node.get(k) is True for k in ("isToday", "today", "current", "signToday", "isCurrent")):
                today_node = node
                break

    # 3. Якщо ані calendarDayDistance == 0, ані isToday не знайдено:
    # Якщо всі вузли мають явні ненульові індикатори дистанції (наприклад, +1 чи -1),
    # ми НЕ підставляємо чужий день! Повертаємо (None, None).
    if today_node is None:
        if has_explicit_distances:
            return None, None
        # Лише якщо індикатори дистанції повністю відсутні у всіх вузлах, беремо перший вузол
        for node in all_daily_nodes:
            if "calendarDayDistance" not in node or node.get("calendarDayDistance") is None:
                today_node = node
                break

    if not today_node or not isinstance(today_node, dict):
        return None, None

    sign_result_list = today_node.get("signResultList")
    sign_result: Optional[Dict[str, Any]] = None
    if isinstance(sign_result_list, list) and len(sign_result_list) > 0 and isinstance(sign_result_list[0], dict):
        sign_result = sign_result_list[0]
    elif isinstance(sign_result_list, dict):
        sign_result = sign_result_list
    elif isinstance(today_node.get("signResult"), dict):
        sign_result = today_node.get("signResult")

    return today_node, sign_result



def _extract_node_streak(source_dict: Optional[Dict[str, Any]]) -> int:
    """Витягує номер дня / стрік із sequenceNumber або подібних ключів."""
    if not source_dict or not isinstance(source_dict, dict):
        return 0
    for key in ("sequenceNumber", "continuousCheckinDays", "continuousDays", "signDays", "streak", "dayNum", "day", "days"):
        val = source_dict.get(key)
        if val is not None and not isinstance(val, bool):
            s_val = _clean_int(val)
            if s_val > 0:
                return s_val
    return 0


def _extract_prize_coins(source_dict: Optional[Dict[str, Any]]) -> int:
    """Витягує кількість монет із prizeInfoList або ключів нагороди."""
    if not source_dict or not isinstance(source_dict, dict):
        return 0

    prize_list = source_dict.get("prizeInfoList")
    if isinstance(prize_list, list):
        for prize in prize_list:
            if isinstance(prize, dict):
                ptype = str(prize.get("prizeType", "")).lower()
                if "coin" in ptype:
                    amt = prize.get("prizeAmount") if prize.get("prizeAmount") is not None else (prize.get("amount") if prize.get("amount") is not None else prize.get("coins"))
                    if amt is not None and not isinstance(amt, bool):
                        val = _clean_int(amt)
                        if val > 0:
                            return val
        for prize in prize_list:
            if isinstance(prize, dict):
                amt = prize.get("prizeAmount") if prize.get("prizeAmount") is not None else (prize.get("amount") if prize.get("amount") is not None else prize.get("coins"))
                if amt is not None and not isinstance(amt, bool):
                    val = _clean_int(amt)
                    if val > 0:
                        return val

    for key in ("prizeAmount", "awardCoins", "rewardCoins", "bonusAmount", "coins", "amount"):
        amt = source_dict.get(key)
        if amt is not None and not isinstance(amt, bool):
            val = _clean_int(amt)
            if val > 0:
                return val
    return 0


def is_already_checked_in(res_data: Any) -> bool:
    """
    Визначає, чи є відповідь свідченням того, що чекін на сьогодні вже був зарахований раніше.
    Враховує мобільні структури (signQuerySequenceNodeList / dailySignNodeList / signResultList / todayAlreadySign)
    та прапорці hasSignIn, isSignInToday, signToday і ret-коди.
    Розрізняє запити списку (sign.list / query.list) та підтвердження свіжого виконання (sign.execute).
    """
    if res_data is None:
        return False
    if is_captcha_required(res_data) or is_mtop_token_expired(res_data) or is_session_expired(res_data):
        return False

    text, json_data = _normalize_res(res_data)
    if not text:
        return False

    if isinstance(json_data, dict):
        blocks_to_check = _collect_nested_dicts(json_data)

        # 0. Мобільний прапорець todayAlreadySign: True у відповіді execute — безумовний пріоритет дубліката!
        for b in blocks_to_check:
            if b.get("todayAlreadySign") is True:
                return True

        # 0.1. Перевірка ret кодів на повторний чекін (SUCCESS_REPEAT_CHECKIN, FAIL_BIZ_REPEAT_CHECKIN тощо)
        ret = json_data.get("ret")
        if isinstance(ret, list):
            for r in ret:
                r_lower = str(r).lower()
                if any(ind in r_lower for ind in [
                    "success_repeat_checkin", "repeat_checkin", "already_checked",
                    "already_signed", "has_checked", "has_signed", "haschecked", "ischecked",
                    "already_signin", "repeat_signin", "has_signin", "signed_today", "already_claimed",
                    "fail_biz_repeat_checkin"
                ]):
                    return True

        # 0.2. Визначення свіжого виконання чекіну (execute confirmation)
        api_name = str(json_data.get("api", "")).lower()
        url_val = ""
        if hasattr(res_data, "url"):
            try:
                url_val = str(res_data.url).lower()
            except Exception:
                pass

        is_explicit_exec = any(ep in api_name or ep in url_val for ep in ("sign.execute", "receive.prize", "coin.pc.sign"))

        # Свіжі нагородні монети нового чекіну (awardCoins, rewardCoins, bonusAmount > 0)
        has_fresh_award = False
        for b in blocks_to_check:
            for k in ("awardCoins", "rewardCoins", "bonusAmount"):
                v = b.get(k)
                if v is not None and not isinstance(v, bool):
                    if _clean_int(v) > 0:
                        has_fresh_award = True
                        break
            if has_fresh_award:
                break

        has_top_sign_success = any(
            isinstance(d, dict) and d.get("signSuccess") is True
            for d in (
                json_data,
                json_data.get("data") if isinstance(json_data.get("data"), dict) else None,
                json_data.get("data", {}).get("data") if isinstance(json_data.get("data"), dict) and isinstance(json_data.get("data", {}).get("data"), dict) else None
            )
            if d is not None
        )

        today_node, sign_result = _find_mobile_daily_sign_node(json_data)
        node_has_sign_success = (
            (sign_result and isinstance(sign_result, dict) and sign_result.get("signSuccess") is True) or
            (today_node and isinstance(today_node, dict) and today_node.get("signSuccess") is True)
        )

        # Якщо це явна відповідь виконання (sign.execute або receive.prize):
        # signSuccess або свіжі нагороди свідчать про свіже виконання, а не про duplicate!
        if is_explicit_exec:
            if has_top_sign_success or has_fresh_award or node_has_sign_success:
                return False

        # Якщо є явний прапорець верхнього рівня signSuccess: True або свіжі нагороди (awardCoins):
        if has_top_sign_success or has_fresh_award:
            return False

        # 0.3. Перевірка вузла поточного дня у mobile sign list (signSuccess у signResultList або today_node)
        # У запитах списку signSuccess: True на сьогоднішньому вузлі свідчить, що чекін уже зроблено!
        if node_has_sign_success:
            return True

        # 1. Якщо є явний прапорець, що користувач сьогодні ще НЕ зачекінився
        for b in blocks_to_check:
            if any(b.get(k) is False for k in ("signToday", "hasSignIn", "isSignInToday", "hasSigned", "alreadySign", "hasCheckIn", "signed", "todayAlreadySign")):
                return False
        if sign_result and sign_result.get("signSuccess") is False:
            return False
        if today_node and today_node.get("signSuccess") is False:
            return False

        for b in blocks_to_check:
            if any(b.get(k) is True for k in ("signToday", "hasSignIn", "isSignInToday", "hasSigned", "alreadySign", "hasCheckIn", "signed")):
                return True

    text_lower = text.lower()
    # Запобігаємо хибному спрацьовуванню на помилки типу "Token already expired" або "Please repeat later"
    if "token" in text_lower and "expired" in text_lower:
        return False
    if "repeat later" in text_lower or "flow_limit" in text_lower or "rate limit" in text_lower:
        return False

    if any(neg in text_lower for neg in ('"signtoday":false', '"signtoday": false', '"hassignin":false', '"hassignin": false', '"issignintoday":false', '"issignintoday": false', '"todayalreadysign":false', '"todayalreadysign": false')):
        return False

    indicators = [
        "has checked in", "have checked in", "already checked in", "already signed in",
        "already_checked", "success_repeat_checkin", "repeat_checkin",
        "haschecked", "ischecked", "checked_in_today", "already_signed",
        "has signed today", "already checked", "already sign in", "repeat sign in",
        "already claimed"
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
    Підтримує прапорець signSuccess: True у відповідях мобільного виконання.
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

    ret = res_data.get("ret")
    ret_first = ret[0] if (isinstance(ret, list) and len(ret) > 0) else (ret if isinstance(ret, str) else "")
    if isinstance(ret_first, str) and ret_first and not (ret_first.startswith("SUCCESS::") or ret_first == "SUCCESS"):
        return False

    # Перевірка на явний прапорець невдачі на верхніх рівнях
    if res_data.get("success") is False or res_data.get("signSuccess") is False:
        return False

    data_block = res_data.get("data")
    if isinstance(data_block, dict):
        if data_block.get("success") is False or data_block.get("signSuccess") is False:
            return False
        inner = data_block.get("data")
        if isinstance(inner, dict):
            if inner.get("success") is False or inner.get("signSuccess") is False:
                return False

    # Збираємо всі словники на всіх рівнях вкладеності (data, result, signInResult, module, тощо)
    all_blocks = _collect_nested_dicts(res_data)

    for b in all_blocks:
        if b.get("success") is False:
            return False
        if b.get("errorCode") or b.get("errorMsg") or b.get("errorMessage"):
            return False
        if any(b.get(k) is False for k in ("signToday", "hasSignIn", "isSignInToday", "hasSigned", "alreadySign", "hasCheckIn", "signed")):
            return False

    # Якщо на сьогоднішньому вузлі мобільного списку чекін не виконано (signSuccess is False),
    # і немає явного прапорця успіху на рівні виконання — це не успішний чекін
    has_top_sign_success = any(
        isinstance(d, dict) and (d.get("signSuccess") is True or d.get("success") is True)
        for d in (res_data, data_block, (data_block.get("data") if isinstance(data_block, dict) else None))
        if d is not None
    )

    today_node, sign_result = _find_mobile_daily_sign_node(res_data)
    if not has_top_sign_success:
        if sign_result and sign_result.get("signSuccess") is False:
            return False
        elif today_node and today_node.get("signSuccess") is False:
            return False

    # Явний прапорець успіху на рівні відповіді або будь-якого вкладеного блоку (включаючи signSuccess: True)
    for b in all_blocks:
        if b.get("success") is True or b.get("signSuccess") is True:
            return True

    # Перевірка статусу шлюзу MTOP при відсутності помилок
    ret = res_data.get("ret")
    if isinstance(ret, list) and len(ret) > 0 and isinstance(ret[0], str):
        if ret[0].startswith("SUCCESS::") or ret[0] == "SUCCESS":
            return True
    elif isinstance(ret, str) and (ret.startswith("SUCCESS::") or ret == "SUCCESS"):
        return True

    for b in all_blocks:
        if (
            b.get("coinCount") or b.get("awardCoins") or
            b.get("coins") or b.get("amount") or b.get("rewardCoins")
        ):
            return True

    return False


class CheckinManager:
    """
    Керує щоденним чекіном монет AliExpress за двоканальним протоколом:
    1) PC Desktop Channel:
       - mtop.aliexpress.coin.pc.sign.list (перевірка статусу та стріку)
       - mtop.aliexpress.coin.pc.sign (виконання щоденного чекіну)
    2) Mobile App Channel:
       - mtop.aliexpress.growth.bonus.daily.signin / coin.sign.in (growth endpoints)
    3) Баланс та сервісні запити:
       - mtop.aliexpress.coin.execute (отримання точного балансу монет)
       - MTOP-підпис з автоматичним оновленням (Self-Healing токена _m_h5_tk)
    """

    MOBILE_APIS = MOBILE_CHECKIN_APIS

    def __init__(self, client: AliClient):
        self.client = client
        self.account_name = client.account_name
        self.status = "FAILED"  # SUCCESS, ALREADY_DONE, SESSION_EXPIRED, FAILED, CAPTCHA_REQUIRED
        self.waf_blocked: bool = False
        self.streak_days = 0
        self.coins_earned = 0
        self.total_coins: Optional[int] = None
        self.session_valid = True
        self.pc_status = "FAILED"
        self.mobile_status = "FAILED"
        self.pc_coins = 0
        self.mobile_coins = 0
        self.pc_streak = 0
        self.mobile_streak = 0
        self.initial_coins: Optional[int] = None
        self.final_coins: Optional[int] = None

    def _prepare_mtop_params(
        self,
        api: str = "mtop.aliexpress.coin.pc.sign.list",
        data: Optional[Union[Dict[str, Any], str]] = None,
        app_key: str = MTOP_APP_KEY
    ) -> Optional[Dict[str, str]]:
        """
        Формує параметри MTOP з обов'язковим хеш-підписом sign та актуальним timestamp.
        """
        is_task_api = (
            api in (TASK_DELIVERY_QUERY_API, TASK_PROCESS_COMPLETE_API, TASK_PROCESS_ISSUE_API)
            or "interactive.task" in api
        )
        is_mobile = (
            is_task_api
            or api in (MOBILE_SIGN_EXECUTE_API, MOBILE_SIGN_LIST_API, MOBILE_SIGN_QUERY_LIST_API, MOBILE_RECEIVE_PRIZE_API)
            or api in MOBILE_CHECKIN_APIS
            or "coin.channel." in api
            or "interactive.sign" in api
            or "growth." in api
        )
        if is_task_api:
            asac_val = ASAC_TASK_CAMPAIGN
        elif is_mobile:
            asac_val = ASAC_MOBILE_CAMPAIGN
        else:
            asac_val = ASAC_CAMPAIGN

        if is_task_api and app_key == MTOP_APP_KEY:
            app_key = TASK_MTOP_APP_KEY

        ttid_val = TASK_TTID if is_task_api else None
        platform_val = TASK_PLATFORM if is_task_api else None
        if isinstance(data, dict):
            if "ttid" in data:
                ttid_val = str(data["ttid"])
            if "platform" in data:
                platform_val = str(data["platform"])
        elif isinstance(data, str):
            try:
                parsed_check = json.loads(data)
                if isinstance(parsed_check, dict):
                    if "ttid" in parsed_check:
                        ttid_val = str(parsed_check["ttid"])
                    if "platform" in parsed_check:
                        platform_val = str(parsed_check["platform"])
            except Exception:
                pass

        ua_task_default = f"defaultUA2_load_failed with timeout@@https://m.aliexpress.com/p/coin-index/index.html@@{int(time.time() * 1000)}"

        if data is None or data == "" or data == {}:
            if api == "mtop.aliexpress.coin.execute":
                data_obj = {"scene": "UserCoinNum"}
            elif is_task_api:
                data_obj = {
                    "ua": ua_task_default,
                    "umidToken": TASK_UMID_TOKEN,
                    "umidTokenType": "TOKEN",
                    "taskSceneId": TASK_SCENE_ID,
                    "taskAppKey": TASK_APP_KEY,
                    "projectAppKey": PROJECT_APP_KEY,
                    "sceneId": TASK_SCENE_ID,
                    "asac": asac_val,
                    "ttid": ttid_val or TASK_TTID,
                    "platform": platform_val or TASK_PLATFORM,
                    "fromNew": True,
                    "locale": TASK_LOCALE,
                    "_lang": TASK_LANG,
                    "_currency": TASK_CURRENCY,
                    "lang": TASK_LANG,
                    "currency": TASK_CURRENCY
                }
            elif api == MOBILE_SIGN_EXECUTE_API:
                data_obj = {
                    "playCode": "COIN_CHANNEL_2.0",
                    "signTimeMills": int(time.time() * 1000),
                    "asac": asac_val,
                    "_lang": "en_KR",
                    "_currency": "USD"
                }
            elif api == MOBILE_SIGN_LIST_API:
                data_obj = {
                    "playCode": "COIN_CHANNEL_2.0",
                    "asac": asac_val,
                    "from": "",
                    "_lang": "en_KR",
                    "_currency": "USD"
                }
            elif api in (MOBILE_SIGN_QUERY_LIST_API, MOBILE_RECEIVE_PRIZE_API) or api in MOBILE_CHECKIN_APIS:
                data_obj = {
                    "channel": "COIN",
                    "asac": asac_val,
                    "_lang": "en_KR",
                    "_currency": "USD"
                }
            else:
                data_obj = {"asac": asac_val, "_lang": "en_KR", "_currency": "USD"}
            data_str = json.dumps(data_obj, separators=(",", ":"))
        elif isinstance(data, dict):
            data_copy = dict(data)
            if is_task_api:
                data_copy["asac"] = asac_val
                data_copy.setdefault("ua", ua_task_default)
                data_copy.setdefault("umidToken", TASK_UMID_TOKEN)
                data_copy.setdefault("umidTokenType", "TOKEN")
                data_copy.setdefault("ttid", ttid_val or TASK_TTID)
                data_copy.setdefault("platform", platform_val or TASK_PLATFORM)
                data_copy.setdefault("projectAppKey", PROJECT_APP_KEY)
                data_copy.setdefault("taskAppKey", TASK_APP_KEY)
                data_copy.setdefault("taskSceneId", TASK_SCENE_ID)
                data_copy.setdefault("sceneId", TASK_SCENE_ID)
                data_copy.setdefault("fromNew", True)
                data_copy.setdefault("locale", TASK_LOCALE)
                data_copy.setdefault("_lang", TASK_LANG)
                data_copy.setdefault("_currency", TASK_CURRENCY)
                data_copy.setdefault("lang", TASK_LANG)
                data_copy.setdefault("currency", TASK_CURRENCY)
            elif is_mobile:
                data_copy["asac"] = asac_val
                data_copy.setdefault("_lang", "en_KR")
                data_copy.setdefault("_currency", "USD")
            elif "asac" not in data_copy and api != "mtop.aliexpress.coin.execute":
                data_copy["asac"] = asac_val
            data_str = json.dumps(data_copy, separators=(",", ":"))
        elif isinstance(data, str):
            try:
                parsed_data = json.loads(data)
                if isinstance(parsed_data, dict):
                    if is_task_api:
                        parsed_data["asac"] = asac_val
                        parsed_data.setdefault("ua", ua_task_default)
                        parsed_data.setdefault("umidToken", TASK_UMID_TOKEN)
                        parsed_data.setdefault("umidTokenType", "TOKEN")
                        parsed_data.setdefault("ttid", ttid_val or TASK_TTID)
                        parsed_data.setdefault("platform", platform_val or TASK_PLATFORM)
                        parsed_data.setdefault("projectAppKey", PROJECT_APP_KEY)
                        parsed_data.setdefault("taskAppKey", TASK_APP_KEY)
                        parsed_data.setdefault("taskSceneId", TASK_SCENE_ID)
                        parsed_data.setdefault("sceneId", TASK_SCENE_ID)
                        parsed_data.setdefault("fromNew", True)
                        parsed_data.setdefault("locale", TASK_LOCALE)
                        parsed_data.setdefault("_lang", TASK_LANG)
                        parsed_data.setdefault("_currency", TASK_CURRENCY)
                        parsed_data.setdefault("lang", TASK_LANG)
                        parsed_data.setdefault("currency", TASK_CURRENCY)
                    elif is_mobile:
                        parsed_data["asac"] = asac_val
                        parsed_data.setdefault("_lang", "en_KR")
                        parsed_data.setdefault("_currency", "USD")
                    elif "asac" not in parsed_data and api != "mtop.aliexpress.coin.execute":
                        parsed_data["asac"] = asac_val
                    data_str = json.dumps(parsed_data, separators=(",", ":"))
                else:
                    data_str = data
            except Exception:
                data_str = data
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

        mtop_params = {
            "jsv": MTOP_JSV,
            "appKey": app_key,
            "t": t,
            "sign": sign,
            "api": api,
            "v": "1.0",
            "type": "originaljson",
            "dataType": "json",
            "asac": asac_val,
            "data": data_str
        }
        if is_task_api:
            mtop_params["isSec"] = "1"
            mtop_params["security"] = "true"
            mtop_params["securityParamsPosition"] = "data"
            mtop_params["H5Request"] = "true"
            mtop_params["asac"] = asac_val
        elif ttid_val:
            mtop_params["ttid"] = ttid_val
        if platform_val:
            mtop_params["platform"] = platform_val
        return mtop_params

    def _refresh_mtop_token(self):
        """Очищує старий _m_h5_tk (якщо треба) та виконує probe-запит для отримання свіжого токена."""
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
        is_task_api = (
            api in (TASK_DELIVERY_QUERY_API, TASK_PROCESS_COMPLETE_API, TASK_PROCESS_ISSUE_API)
            or "interactive.task" in api
        )
        is_mobile_api = (
            is_task_api or
            api in MOBILE_CHECKIN_APIS or
            api in (MOBILE_SIGN_LIST_API, MOBILE_SIGN_EXECUTE_API, MOBILE_SIGN_QUERY_LIST_API, MOBILE_RECEIVE_PRIZE_API) or
            "coin.channel." in api or
            "interactive.sign" in api or
            "growth." in api
        )
        if is_task_api:
            referer = "https://m.aliexpress.com/"
            origin = "https://m.aliexpress.com"
            asac_header = ASAC_TASK_CAMPAIGN
        elif is_mobile_api:
            referer = COIN_MOBILE_REFERER
            origin = MOBILE_ORIGIN
            asac_header = ASAC_MOBILE_CAMPAIGN
        else:
            referer = COIN_PC_REFERER
            origin = ORIGIN
            asac_header = ASAC_CAMPAIGN
        headers = {
            "Referer": referer,
            "Origin": origin,
            "Content-Type": "application/x-www-form-urlencoded",
            "asac": asac_header,
        }

        # Якщо токен ще відсутній у сесії, виконуємо probe-запит для отримання _m_h5_tk від шлюзу
        if not self.client.get_cookie("_m_h5_tk"):
            self._refresh_mtop_token()

        for attempt in range(max_retries):
            token_before = self.client.get_cookie("_m_h5_tk")
            params = self._prepare_mtop_params(api=api, data=data)
            if not params:
                logger.warning(f"[{self.account_name}] ⚠️ Не вдалося сформувати підпис MTOP для {api}: у cookies відсутній обов'язковий токен '_m_h5_tk'!")
                return None

            data_str = params.get("data", "{}")
            query_params = {k: v for k, v in params.items() if k != "data"}
            post_body = {"data": data_str}
            if is_task_api:
                post_body["bx-umidtoken"] = TASK_BX_UMIDTOKEN
                post_body["bx-sys"] = "ua-l:no__um-l:no__js:https://g.alicdn.com/"

            try:
                resp = self.client.post(
                    url,
                    params=query_params,
                    data=post_body,
                    headers=headers
                )
                if resp is not None and is_captcha_required(resp):
                    self.waf_blocked = True
                    if self.status not in ("SUCCESS", "ALREADY_DONE"):
                        self.status = "CAPTCHA_REQUIRED"
                    logger.warning(f"[{self.account_name}] 🧩 Виявлено вимогу проходження капчі (WAF) у тексті відповіді!")
                    return None

                if not resp or resp.status_code != 200:
                    status = resp.status_code if resp else "None"
                    body = resp.text[:120] if resp else ""
                    logger.warning(f"[{self.account_name}] ⚠️ MTOP {api} повернув HTTP {status}: {body}")
                    continue

                res_data = resp.json()
                if isinstance(res_data, dict) and "api" not in res_data:
                    res_data["api"] = api

                logger.debug(f"[{self.account_name}] Відповідь від {url}: {res_data}")

                if is_captcha_required(res_data):
                    self.waf_blocked = True
                    if self.status not in ("SUCCESS", "ALREADY_DONE"):
                        self.status = "CAPTCHA_REQUIRED"
                    logger.warning(f"[{self.account_name}] 🧩 Виявлено вимогу проходження капчі (WAF) у JSON відповіді!")
                    return res_data

                if is_mtop_token_expired(res_data) and attempt < max_retries - 1:
                    logger.info(f"[{self.account_name}] 🔄 Отримано FAIL_SYS_TOKEN_EXPIRED для {api}. Оновлюємо токен та повторюємо спробу...")
                    if self.client.get_cookie("_m_h5_tk") == token_before:
                        self._refresh_mtop_token()
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
                for key in [
                    "continuousCheckinDays", "continuousDays", "signDays", "streak",
                    "dayNum", "day", "days", "consecutiveDays", "signStreak", "currentDay"
                ]:
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

        # 0. Мобільні структури signQuerySequenceNodeList / dailySignNodeList / signResultList
        today_node, sign_result = _find_mobile_daily_sign_node(res_data)
        if today_node or sign_result:
            node_streak = _extract_node_streak(sign_result) or _extract_node_streak(today_node)
            if node_streak > 0:
                self.streak_days = max(self.streak_days, node_streak)
            if extract_coins:
                node_coins = _extract_prize_coins(sign_result) or _extract_prize_coins(today_node)
                if node_coins > 0:
                    self.coins_earned = max(self.coins_earned, node_coins)

        # Дні стріку
        extracted_days = None
        for d in (inner_dict, target_dict, res_data):
            for key in [
                "continuousCheckinDays", "continuousDays", "signDays", "streak",
                "dayNum", "day", "days", "consecutiveDays", "signStreak", "currentDay", "sequenceNumber"
            ]:
                val = d.get(key)
                if val is not None and isinstance(val, (int, str, float)):
                    try:
                        days = int(float(str(val).replace(",", "").strip()))
                        if days > 0:
                            extracted_days = days if extracted_days is None else max(extracted_days, days)
                    except (ValueError, TypeError):
                        pass

        # Також перевіряємо всі вкладені блоки на sequenceNumber
        for b in _collect_nested_dicts(res_data):
            seq = b.get("sequenceNumber")
            if seq is not None and isinstance(seq, (int, str, float)):
                try:
                    s_val = int(float(str(seq).replace(",", "").strip()))
                    if s_val > 0:
                        extracted_days = s_val if extracted_days is None else max(extracted_days, s_val)
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
                for key in [
                    "awardCoins", "coinCount", "points", "awardPoints",
                    "coin", "coins", "amount", "rewardCoins", "bonusAmount", "prizeAmount"
                ]:
                    val = d.get(key)
                    if val is not None and isinstance(val, (int, str, float)):
                        try:
                            coins = int(float(str(val).replace(",", "").strip()))
                            if coins > 0:
                                self.coins_earned = max(self.coins_earned, coins)
                        except (ValueError, TypeError):
                            pass

                p_coins = _extract_prize_coins(d)
                if p_coins > 0:
                    self.coins_earned = max(self.coins_earned, p_coins)

            # Також шукаємо prizeInfoList / prizeAmount у всіх вкладених блоках
            for b in _collect_nested_dicts(res_data):
                p_coins = _extract_prize_coins(b)
                if p_coins > 0:
                    self.coins_earned = max(self.coins_earned, p_coins)

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

    def _extract_mobile_data(self, res_data: Any) -> Tuple[int, int]:
        """
        Витягує зароблені монети та дні стріку з відповіді мобільного ендпоінту чекіну.
        Повертає кортеж (coins, streak).
        """
        if res_data is None:
            return 0, 0
        if not isinstance(res_data, dict):
            _, res_data = _normalize_res(res_data)
            if not isinstance(res_data, dict):
                return 0, 0

        streak = 0
        coins = 0

        # 0. Структури signQuerySequenceNodeList / dailySignNodeList / signResultList
        today_node, sign_result = _find_mobile_daily_sign_node(res_data)
        if today_node or sign_result:
            node_streak = _extract_node_streak(sign_result) or _extract_node_streak(today_node)
            if node_streak > 0:
                streak = max(streak, node_streak)
            node_coins = _extract_prize_coins(sign_result) or _extract_prize_coins(today_node)
            if node_coins > 0:
                coins = max(coins, node_coins)

        blocks = _collect_nested_dicts(res_data)

        streak_keys = [
            "sequenceNumber", "continuousCheckinDays", "continuousDays", "signDays", "streak",
            "dayNum", "day", "days", "consecutiveDays", "signStreak", "currentDay"
        ]
        # Специфічні ключі нагороди мають пріоритет над загальними лічильниками балансу
        primary_coin_keys = ["prizeAmount", "awardCoins", "rewardCoins", "bonusAmount", "amount"]
        secondary_coin_keys = ["coins", "coin", "points", "awardPoints", "coinCount"]

        # Перевірка чи є явний негативний прапорець (тоді монети в об'єкті — старі/попередні)
        has_negative_signin = any(
            isinstance(b, dict) and any(b.get(k) is False for k in ("signToday", "hasSignIn", "isSignInToday", "hasSigned", "alreadySign", "hasCheckIn", "signed"))
            for b in blocks
        )

        for b in blocks:
            for sk in streak_keys:
                val = b.get(sk)
                if val is not None and isinstance(val, (int, str, float)):
                    try:
                        s_val = int(float(str(val).replace(",", "").strip()))
                        if s_val > 0:
                            streak = max(streak, s_val)
                    except (ValueError, TypeError):
                        pass

        if not has_negative_signin:
            # 1. Шукаємо серед специфічних ключів винагороди та prizeInfoList
            for b in blocks:
                p_coins = _extract_prize_coins(b)
                if p_coins > 0:
                    coins = max(coins, p_coins)
                for ck in primary_coin_keys:
                    val = b.get(ck)
                    if val is not None and isinstance(val, (int, str, float)):
                        try:
                            c_val = int(float(str(val).replace(",", "").strip()))
                            if c_val > 0:
                                coins = max(coins, c_val)
                        except (ValueError, TypeError):
                            pass

            # 2. Якщо специфічних ключів не знайдено, перевіряємо вторинні ключі
            if coins == 0:
                for b in blocks:
                    for ck in secondary_coin_keys:
                        val = b.get(ck)
                        if val is not None and isinstance(val, (int, str, float)):
                            try:
                                c_val = int(float(str(val).replace(",", "").strip()))
                                if c_val > 0:
                                    coins = max(coins, c_val)
                            except (ValueError, TypeError):
                                pass

        return coins, streak

    def _run_pc_checkin(self) -> Dict[str, Any]:
        """
        Виконує чекін каналу PC Desktop (PC Index):
        1) Warmup GET до COIN_PC_REFERER.
        2) Отримання статусу через mtop.aliexpress.coin.pc.sign.list.
        3) Якщо не зараховано -> виконання через mtop.aliexpress.coin.pc.sign.
        Повертає словник: {"status": ..., "coins": ..., "streak": ...}
        """
        logger.info(f"[{self.account_name}] 🖥️ [Канал PC] Перевірка та чекін PC Desktop...")
        pc_result = {"status": "FAILED", "coins": 0, "streak": 0}

        # 1. Warmup до сторінки PC Index
        try:
            home_resp = self.client.get(COIN_PC_REFERER)
            if home_resp and is_captcha_required(home_resp):
                self.waf_blocked = True
                self.status = "CAPTCHA_REQUIRED"
                logger.warning(f"[{self.account_name}] 🧩 Сторінка PC Index вимагає капчі WAF!")
                pc_result["status"] = "CAPTCHA_REQUIRED"
                return pc_result
            if home_resp and home_resp.status_code == 200:
                if self._parse_coin_page_html(home_resp.text):
                    pc_result["status"] = "ALREADY_DONE"
                    pc_result["streak"] = self.streak_days
                    status_res = self._call_mtop("mtop.aliexpress.coin.pc.sign.list", data={"asac": ASAC_CAMPAIGN, "_lang": "en_KR", "_currency": "USD"})
                    if status_res:
                        self._extract_coins_from_sign_list(status_res)
                    pc_result["coins"] = self.coins_earned
                    logger.info(f"[{self.account_name}] ℹ️ [Канал PC] З монетного центру виявлено: чекін на сьогодні вже зараховано!")
                    return pc_result
        except Exception as e:
            logger.debug(f"[{self.account_name}] Warmup запит до {COIN_PC_REFERER}: {e}")

        # 2. Отримання статусу чекіну через mtop.aliexpress.coin.pc.sign.list
        sign_payload = {"asac": ASAC_CAMPAIGN, "_lang": "en_KR", "_currency": "USD"}
        status_res = self._call_mtop("mtop.aliexpress.coin.pc.sign.list", data=sign_payload)

        if self.status == "CAPTCHA_REQUIRED":
            pc_result["status"] = "CAPTCHA_REQUIRED"
            return pc_result

        sign_today = None
        pc_streak = 0
        pc_coins = 0

        if status_res:
            if is_captcha_required(status_res):
                self.status = "CAPTCHA_REQUIRED"
                logger.warning(f"[{self.account_name}] 🧩 Статус чекіну PC повернув вимогу капчі (WAF)!")
                pc_result["status"] = "CAPTCHA_REQUIRED"
                return pc_result

            if is_session_expired(status_res):
                self.session_valid = False
                self.status = "SESSION_EXPIRED"
                logger.error(f"[{self.account_name}] ❌ Сесія AliExpress недійсна або прострочена!")
                pc_result["status"] = "SESSION_EXPIRED"
                return pc_result

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

        # 3. Якщо вже зараховано сьогодні на PC
        if sign_today is True or (status_res and is_already_checked_in(status_res)):
            pc_result["status"] = "ALREADY_DONE"
            if self.coins_earned == 0 and status_res:
                self._extract_telemetry(status_res, extract_coins=True)
            pc_result["coins"] = self.coins_earned
            pc_result["streak"] = self.streak_days
            logger.info(f"[{self.account_name}] ℹ️ [Канал PC] Чекін на сьогодні вже зараховано (стрік: {pc_result['streak']} дн.)")
            return pc_result

        # 4. Виконання чекіну через mtop.aliexpress.coin.pc.sign
        sign_res = self._call_mtop("mtop.aliexpress.coin.pc.sign", data=sign_payload)
        if self.status == "CAPTCHA_REQUIRED":
            logger.warning(f"[{self.account_name}] 🧩 Виконання чекіну PC повернуло вимогу капчі (WAF)!")
            pc_result["status"] = "CAPTCHA_REQUIRED"
            return pc_result

        if sign_res:
            if is_captcha_required(sign_res):
                self.status = "CAPTCHA_REQUIRED"
                logger.warning(f"[{self.account_name}] 🧩 Виконання чекіну PC повернуло вимогу капчі (WAF)!")
                pc_result["status"] = "CAPTCHA_REQUIRED"
                return pc_result
            elif is_session_expired(sign_res):
                self.session_valid = False
                self.status = "SESSION_EXPIRED"
                logger.error(f"[{self.account_name}] ❌ Сесія AliExpress недійсна або прострочена!")
                pc_result["status"] = "SESSION_EXPIRED"
                return pc_result
            elif is_already_checked_in(sign_res):
                pc_result["status"] = "ALREADY_DONE"
                self._extract_telemetry(sign_res, extract_coins=True)
                if self.coins_earned == 0 and status_res:
                    self._extract_coins_from_sign_list(status_res)
                pc_result["coins"] = self.coins_earned
                pc_result["streak"] = self.streak_days
                logger.info(f"[{self.account_name}] ℹ️ [Канал PC] Чекін на сьогодні вже був виконаний раніше.")
                return pc_result
            elif is_checkin_success(sign_res):
                pc_result["status"] = "SUCCESS"
                has_explicit_days = self._has_explicit_streak_days(sign_res)
                if not has_explicit_days:
                    self.streak_days = (self.streak_days + 1) if self.streak_days > 0 else 1
                self._extract_telemetry(sign_res, extract_coins=True)
                if self.streak_days == 0:
                    self.streak_days = 1
                if self.coins_earned == 0 and status_res:
                    self._extract_coins_from_sign_list(status_res)
                pc_result["coins"] = self.coins_earned
                pc_result["streak"] = self.streak_days
                logger.info(f"[{self.account_name}] ✅ [Канал PC] Чекін успішно виконано! (Стрік: {pc_result['streak']} дн., Монет: +{pc_result['coins']})")
                return pc_result
            else:
                pc_result["status"] = "FAILED"
                pc_result["streak"] = self.streak_days
                logger.warning(f"[{self.account_name}] ⚠️ [Канал PC] Чекін не підтверджено.")
                return pc_result
        else:
            if self.status != "CAPTCHA_REQUIRED":
                pc_result["status"] = "FAILED"
            pc_result["streak"] = self.streak_days
            logger.warning(f"[{self.account_name}] ⚠️ [Канал PC] Не отримано відповіді від ендпоінту чекіну.")
            return pc_result

    def _checkin_mobile_channel(self) -> Dict[str, Any]:
        """
        Виконує повний потік мобільного чекіну:
        a) Запит до MOBILE_SIGN_LIST_API для перевірки статусу та стріку.
        b) Якщо не зачекінено -> виконання через MOBILE_SIGN_EXECUTE_API.
        c) За потреби fallback до допоміжних ендпоінтів з MOBILE_CHECKIN_APIS.
        Повертає словник: {"status": ..., "coins": ..., "streak": ...}
        """
        logger.info(f"[{self.account_name}] 📱 [Канал Mobile] Початок перевірки та чекіну мобільного додатку...")
        mobile_result = {"status": "FAILED", "coins": 0, "streak": 0}
        potential_streak = 0
        potential_coins = 0

        # a) Запит до MOBILE_SIGN_LIST_API з fallback до MOBILE_SIGN_QUERY_LIST_API
        logger.debug(f"[{self.account_name}] 📱 [Канал Mobile] Запит статусу чекіну до {MOBILE_SIGN_LIST_API}...")
        list_res = self._call_mtop(MOBILE_SIGN_LIST_API)

        if self.waf_blocked or self.status == "CAPTCHA_REQUIRED":
            mobile_result["status"] = "CAPTCHA_REQUIRED"
            return mobile_result

        # Перевірка на капчу та сесію ПЕРЕД fallback-запитом
        if list_res:
            if is_captcha_required(list_res):
                self.waf_blocked = True
                if self.status not in ("SUCCESS", "ALREADY_DONE"):
                    self.status = "CAPTCHA_REQUIRED"
                logger.warning(f"[{self.account_name}] 🧩 [Канал Mobile] {MOBILE_SIGN_LIST_API} вимагає капчі (WAF)!")
                mobile_result["status"] = "CAPTCHA_REQUIRED"
                return mobile_result

            if is_session_expired(list_res):
                self.session_valid = False
                self.status = "SESSION_EXPIRED"
                logger.error(f"[{self.account_name}] ❌ [Канал Mobile] Сесія недійсна або прострочена!")
                mobile_result["status"] = "SESSION_EXPIRED"
                return mobile_result

        # Fallback до MOBILE_SIGN_QUERY_LIST_API якщо sign.list повернув None або не містить валідних даних
        if not list_res or (not _find_mobile_daily_sign_node(list_res)[0] and not is_already_checked_in(list_res)):
            logger.debug(f"[{self.account_name}] 📱 [Канал Mobile] Спроба допоміжного запиту списку через {MOBILE_SIGN_QUERY_LIST_API}...")
            query_res = self._call_mtop(MOBILE_SIGN_QUERY_LIST_API)
            if self.waf_blocked or self.status == "CAPTCHA_REQUIRED":
                mobile_result["status"] = "CAPTCHA_REQUIRED"
                return mobile_result
            if query_res:
                if is_captcha_required(query_res):
                    self.waf_blocked = True
                    if self.status not in ("SUCCESS", "ALREADY_DONE"):
                        self.status = "CAPTCHA_REQUIRED"
                    mobile_result["status"] = "CAPTCHA_REQUIRED"
                    return mobile_result
                if is_session_expired(query_res):
                    self.session_valid = False
                    self.status = "SESSION_EXPIRED"
                    logger.error(f"[{self.account_name}] ❌ [Канал Mobile] Сесія недійсна або прострочена!")
                    mobile_result["status"] = "SESSION_EXPIRED"
                    return mobile_result
                list_res = query_res

        if list_res:
            today_node, sign_result = _find_mobile_daily_sign_node(list_res)
            if today_node or sign_result:
                node_streak = _extract_node_streak(sign_result) or _extract_node_streak(today_node)
                node_coins = _extract_prize_coins(sign_result) or _extract_prize_coins(today_node)
                if node_streak > 0:
                    potential_streak = node_streak
                if node_coins > 0:
                    potential_coins = node_coins

                sign_success = None
                if sign_result and "signSuccess" in sign_result:
                    sign_success = sign_result.get("signSuccess")
                elif today_node and "signSuccess" in today_node:
                    sign_success = today_node.get("signSuccess")

                if sign_success is True:
                    mobile_result["status"] = "ALREADY_DONE"
                    mobile_result["coins"] = potential_coins
                    mobile_result["streak"] = potential_streak
                    logger.info(
                        f"[{self.account_name}] ℹ️ [Канал Mobile] Чекін уже зараховано сьогодні за даними списку "
                        f"({MOBILE_SIGN_LIST_API}, стрік: {potential_streak} дн., монети: {potential_coins})"
                    )
                    return mobile_result
            elif is_already_checked_in(list_res):
                coins, streak = self._extract_mobile_data(list_res)
                mobile_result["status"] = "ALREADY_DONE"
                mobile_result["coins"] = coins or potential_coins
                mobile_result["streak"] = streak or potential_streak
                logger.info(f"[{self.account_name}] ℹ️ [Канал Mobile] Чекін уже зараховано сьогодні ({MOBILE_SIGN_LIST_API})")
                return mobile_result

        # b) Якщо не зараховано, викликаємо MOBILE_SIGN_EXECUTE_API
        logger.debug(f"[{self.account_name}] 📱 [Канал Mobile] Виконання чекіну через {MOBILE_SIGN_EXECUTE_API}...")
        exec_res = self._call_mtop(MOBILE_SIGN_EXECUTE_API)

        if self.waf_blocked or self.status == "CAPTCHA_REQUIRED":
            mobile_result["status"] = "CAPTCHA_REQUIRED"
            return mobile_result

        if exec_res:
            if is_captcha_required(exec_res):
                self.waf_blocked = True
                if self.status not in ("SUCCESS", "ALREADY_DONE"):
                    self.status = "CAPTCHA_REQUIRED"
                logger.warning(f"[{self.account_name}] 🧩 [Канал Mobile] {MOBILE_SIGN_EXECUTE_API} повернув вимогу капчі (WAF)!")
                mobile_result["status"] = "CAPTCHA_REQUIRED"
                return mobile_result

            if is_session_expired(exec_res):
                self.session_valid = False
                self.status = "SESSION_EXPIRED"
                logger.error(f"[{self.account_name}] ❌ [Канал Mobile] Сесія недійсна або прострочена!")
                mobile_result["status"] = "SESSION_EXPIRED"
                return mobile_result

            _, exec_dict = _normalize_res(exec_res)
            exec_dict = exec_dict if isinstance(exec_dict, dict) else {}

            exec_today_node, exec_sign_result = _find_mobile_daily_sign_node(exec_res)
            node_sign_success = (
                (exec_sign_result and exec_sign_result.get("signSuccess") is True) or
                (exec_today_node and exec_today_node.get("signSuccess") is True)
            )

            today_already = any(b.get("todayAlreadySign") is True for b in _collect_nested_dicts(exec_res))
            top_or_data_sign_success = any(
                isinstance(d, dict) and d.get("signSuccess") is True
                for d in (
                    exec_dict,
                    exec_dict.get("data") if isinstance(exec_dict.get("data"), dict) else None,
                    exec_dict.get("data", {}).get("data") if isinstance(exec_dict.get("data"), dict) and isinstance(exec_dict.get("data", {}).get("data"), dict) else None
                )
                if d is not None
            )

            # 1. Перевірка на свіжий успіх виконання ПЕРШОЮ чергою (Fresh execution FIRST)
            if not today_already and (is_checkin_success(exec_res) or top_or_data_sign_success or node_sign_success):
                coins, streak = self._extract_mobile_data(exec_res)
                mobile_result["status"] = "SUCCESS"
                mobile_result["coins"] = coins or potential_coins
                final_streak = streak or potential_streak
                mobile_result["streak"] = final_streak if final_streak > 0 else 1
                logger.info(f"[{self.account_name}] ✅ [Канал Mobile] Чекін успішно виконано через {MOBILE_SIGN_EXECUTE_API}! (Стрік: {mobile_result['streak']} дн., Монет: +{mobile_result['coins']})")
                return mobile_result

            # 2. Якщо не свіжий успіх, перевіряємо на повторний чекін
            if today_already or is_already_checked_in(exec_res):
                coins, streak = self._extract_mobile_data(exec_res)
                mobile_result["status"] = "ALREADY_DONE"
                mobile_result["coins"] = coins or potential_coins
                mobile_result["streak"] = streak or potential_streak
                logger.info(f"[{self.account_name}] ℹ️ [Канал Mobile] Чекін уже зараховано сьогодні ({MOBILE_SIGN_EXECUTE_API}, стрік: {mobile_result['streak']} дн.)")
                return mobile_result

        # c) Fallback до інших ендпоінтів у MOBILE_CHECKIN_APIS
        for api in MOBILE_CHECKIN_APIS:
            if api == MOBILE_SIGN_EXECUTE_API:
                continue
            logger.debug(f"[{self.account_name}] 📱 [Канал Mobile] Спроба fallback ендпоінту {api}...")
            res = self._call_mtop(api)
            if self.waf_blocked or self.status == "CAPTCHA_REQUIRED":
                mobile_result["status"] = "CAPTCHA_REQUIRED"
                return mobile_result
            if not res:
                continue
            if is_captcha_required(res):
                self.waf_blocked = True
                if self.status not in ("SUCCESS", "ALREADY_DONE"):
                    self.status = "CAPTCHA_REQUIRED"
                mobile_result["status"] = "CAPTCHA_REQUIRED"
                return mobile_result
            if is_session_expired(res):
                self.session_valid = False
                self.status = "SESSION_EXPIRED"
                mobile_result["status"] = "SESSION_EXPIRED"
                return mobile_result

            _, res_dict = _normalize_res(res)
            res_dict = res_dict if isinstance(res_dict, dict) else {}

            fb_today_node, fb_sign_result = _find_mobile_daily_sign_node(res)
            fb_node_sign_success = (
                (fb_sign_result and fb_sign_result.get("signSuccess") is True) or
                (fb_today_node and fb_today_node.get("signSuccess") is True)
            )

            today_already = any(b.get("todayAlreadySign") is True for b in _collect_nested_dicts(res))
            top_or_data_sign_success = any(
                isinstance(d, dict) and d.get("signSuccess") is True
                for d in (
                    res_dict,
                    res_dict.get("data") if isinstance(res_dict.get("data"), dict) else None,
                    res_dict.get("data", {}).get("data") if isinstance(res_dict.get("data"), dict) and isinstance(res_dict.get("data", {}).get("data"), dict) else None
                )
                if d is not None
            )

            # 1. Перевірка на свіжий успіх виконання ПЕРШОЮ чергою (Fresh execution FIRST)
            if not today_already and (is_checkin_success(res) or top_or_data_sign_success or fb_node_sign_success):
                coins, streak = self._extract_mobile_data(res)
                mobile_result["status"] = "SUCCESS"
                mobile_result["coins"] = coins or potential_coins
                mobile_result["streak"] = streak or potential_streak or 1
                return mobile_result

            # 2. Якщо не свіжий успіх, перевіряємо на повторний чекін
            if today_already or is_already_checked_in(res):
                coins, streak = self._extract_mobile_data(res)
                mobile_result["status"] = "ALREADY_DONE"
                mobile_result["coins"] = coins or potential_coins
                mobile_result["streak"] = streak or potential_streak
                return mobile_result

        logger.info(f"[{self.account_name}] ⚠️ [Канал Mobile] Усі мобільні ендпоінти вичерпано без підтвердження.")
        mobile_result["streak"] = potential_streak
        return mobile_result

    def _run_mobile_checkin(self) -> Dict[str, Any]:
        """Аліас для зворотної сумісності."""
        return self._checkin_mobile_channel()

    def run_checkin(self) -> bool:
        """
        Виконує Dual-Channel щоденний чекін:
        1) PC Desktop Channel (coin-pc-index / mtop.aliexpress.coin.pc.sign)
        2) Mobile App Channel (mtop.aliexpress.coin.channel.sign.list / sign.execute)
        3) Graceful orchestration & об'єднання результатів:
           - Якщо хоча б один канал SUCCESS -> загальний статус SUCCESS
           - Якщо обидва ALREADY_DONE -> ALREADY_DONE
           - Якщо один ALREADY_DONE, а інший FAILED -> ALREADY_DONE
           - Якщо обидва FAILED -> FAILED
           - Якщо капча -> CAPTCHA_REQUIRED (якщо PC не був SUCCESS/ALREADY_DONE)
           - Сумування монет виключно з каналів, де чекін виконано вперше (SUCCESS)
           - Встановлення максимального стріку між обома каналами
        4) Отримання актуального балансу через mtop.aliexpress.coin.execute
        """
        logger.info(f"[{self.account_name}] 🔄 Початок щоденного чекіну (Dual-Channel: PC + Mobile)...")
        self.coins_earned = 0
        self.waf_blocked = False
        self.status = "FAILED"
        self.pc_status = "FAILED"
        self.mobile_status = "FAILED"
        self.pc_coins = 0
        self.mobile_coins = 0
        self.pc_streak = 0
        self.mobile_streak = 0

        # 1. Запуск PC Desktop чекіну
        pc_res = self._run_pc_checkin()

        # Якщо на PC спрацювала капча або сесія прострочена — перериваємо негайно
        if pc_res.get("status") == "CAPTCHA_REQUIRED" or self.status == "CAPTCHA_REQUIRED":
            self.pc_status = "CAPTCHA_REQUIRED"
            self.mobile_status = "SKIPPED"
            self.status = "CAPTCHA_REQUIRED"
            return False
        if pc_res.get("status") == "SESSION_EXPIRED" or not self.session_valid:
            self.pc_status = "SESSION_EXPIRED"
            self.mobile_status = "SKIPPED"
            self.status = "SESSION_EXPIRED"
            return False

        # 2. Запуск Mobile App чекіну
        mobile_res = self._run_mobile_checkin()

        pc_status = pc_res.get("status", "FAILED")
        mobile_status = mobile_res.get("status", "FAILED")
        pc_coins = pc_res.get("coins", 0)
        mobile_coins = mobile_res.get("coins", 0)
        pc_streak = pc_res.get("streak", 0)
        mobile_streak = mobile_res.get("streak", 0)

        # Якщо на мобільному спрацювала капча
        if self.waf_blocked or mobile_res.get("status") == "CAPTCHA_REQUIRED" or self.status == "CAPTCHA_REQUIRED":
            self.pc_status = pc_status
            self.mobile_status = "CAPTCHA_REQUIRED"
            self.pc_coins = pc_coins if pc_status in ("SUCCESS", "ALREADY_DONE") else 0
            self.mobile_coins = 0
            self.pc_streak = pc_streak
            self.mobile_streak = 0
            if pc_status in ("SUCCESS", "ALREADY_DONE"):
                logger.warning(f"[{self.account_name}] 🧩 Канал Mobile вимагає капчі, але чекін на PC вже успішно зараховано ({pc_status})!")
                self.status = pc_status
            else:
                self.status = "CAPTCHA_REQUIRED"
                return False

        # Якщо на мобільному повернуто SESSION_EXPIRED
        if mobile_res.get("status") == "SESSION_EXPIRED" or not self.session_valid:
            self.pc_status = pc_status
            self.mobile_status = "SESSION_EXPIRED"
            self.pc_coins = pc_coins if pc_status in ("SUCCESS", "ALREADY_DONE") else 0
            self.mobile_coins = 0
            self.pc_streak = pc_streak
            self.mobile_streak = 0
            if pc_status in ("SUCCESS", "ALREADY_DONE"):
                logger.warning(f"[{self.account_name}] ⚠️ Канал Mobile повернув недійсну сесію, але чекін на PC вже успішно зараховано ({pc_status})!")
                self.session_valid = True
                self.status = pc_status
            else:
                self.status = "SESSION_EXPIRED"
                return False

        # 3. Визначення підсумкового статусу
        if pc_status == "SUCCESS" or mobile_status == "SUCCESS":
            self.status = "SUCCESS"
        elif pc_status == "ALREADY_DONE" or mobile_status == "ALREADY_DONE":
            self.status = "ALREADY_DONE"
        else:
            self.status = "FAILED"

        # 4. Об'єднання зароблених монет:
        # Монети, зароблені в цьому запуску, зараховуються лише від каналів зі свіжим SUCCESS.
        # Монети від ALREADY_DONE ніколи не додаються до свіжого SUCCESS!
        if pc_status == "SUCCESS" or mobile_status == "SUCCESS":
            fresh_coins = 0
            if pc_status == "SUCCESS":
                fresh_coins += pc_coins
            if mobile_status == "SUCCESS":
                fresh_coins += mobile_coins
            self.coins_earned = fresh_coins
        elif pc_status == "ALREADY_DONE" or mobile_status == "ALREADY_DONE":
            self.coins_earned = max(pc_coins, mobile_coins)
        else:
            self.coins_earned = 0

        # 5. Встановлення стріку (найвищий активний стрік по обох каналах)
        self.streak_days = max(pc_streak, mobile_streak, 0)

        # Збереження деталізованої інформації по каналах
        self.pc_status = pc_status
        self.mobile_status = mobile_status
        self.pc_coins = pc_coins if pc_status in ("SUCCESS", "ALREADY_DONE") else 0
        self.mobile_coins = mobile_coins if mobile_status in ("SUCCESS", "ALREADY_DONE") else 0
        self.pc_streak = pc_streak
        self.mobile_streak = mobile_streak

        logger.info(
            f"[{self.account_name}] 📊 Підсумок Dual-Channel чекіну: Статус={self.status}, "
            f"Стрік={self.streak_days} дн., Зароблено=+{self.coins_earned} 🪙 "
            f"(PC: {pc_status} +{pc_coins}🪙, Mobile: {mobile_status} +{mobile_coins}🪙)"
        )

        # 6. Отримання точного балансу монет через mtop.aliexpress.coin.execute
        if not self.waf_blocked:
            self._fetch_coin_balance()
        if self.total_coins is not None:
            self.final_coins = self.total_coins
            fresh_checkin_coins = 0
            if pc_status == "SUCCESS":
                fresh_checkin_coins += pc_coins
            if mobile_status == "SUCCESS":
                fresh_checkin_coins += mobile_coins
            self.initial_coins = max(0, self.total_coins - fresh_checkin_coins)

        return self.status in ("SUCCESS", "ALREADY_DONE")


    def _fetch_coin_balance(self):
        """Отримує точний баланс монет через mtop.aliexpress.coin.execute (scene: 'UserCoinNum')."""
        if self.waf_blocked:
            logger.debug(f"[{self.account_name}] WAF блокування активне — пропускаємо _fetch_coin_balance.")
            return
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
