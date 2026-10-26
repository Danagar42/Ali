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

# Мобільний канал чекіну монет AliExpress
COIN_MOBILE_REFERER = "https://m.aliexpress.com/p/coin-index/index.html"
MOBILE_ORIGIN = "https://m.aliexpress.com"

MOBILE_SIGN_LIST_API = "mtop.aliexpress.coin.channel.sign.list"
MOBILE_SIGN_EXECUTE_API = "mtop.aliexpress.coin.channel.sign.execute"
MOBILE_SIGN_QUERY_LIST_API = "mtop.aliexpress.social.interactive.sign.model.query.list"
MOBILE_RECEIVE_PRIZE_API = "mtop.aliexpress.social.interactive.sign.user.receive.prize"
MOBILE_CHECKIN_APIS = [
    MOBILE_SIGN_EXECUTE_API,
    MOBILE_RECEIVE_PRIZE_API
]

QUERY_APIS = {
    "mtop.aliexpress.coin.pc.sign.list",
    "mtop.aliexpress.growth.channel.sign.list",
    "mtop.aliexpress.growth.channel.sign.querySignList",
    "mtop.aliexpress.interactive.task.delivery.query",
}

MUTATION_APIS = {
    "mtop.aliexpress.coin.pc.sign",
    "mtop.aliexpress.coin.pc.sign.execute",
    MOBILE_SIGN_EXECUTE_API,
    MOBILE_RECEIVE_PRIZE_API,
    TASK_PROCESS_COMPLETE_API,
    TASK_PROCESS_ISSUE_API,
}


def is_mutation_api(api: str, data: Optional[Union[Dict[str, Any], str]] = None) -> bool:
    """
    Визначає, чи є MTOP endpoint мутацією стану (чекін, завершення завдання, отримання нагороди),
    чи безпечним/ідемпотентним читанням (список завдань, перевірка статусу, баланс).
    """
    if not api or not isinstance(api, str):
        return False
    if api in MUTATION_APIS:
        return True
    if api in QUERY_APIS:
        return False
    if api == "mtop.aliexpress.coin.execute":
        data_str = str(data) if data is not None else ""
        if data is None or data == "{}" or "UserCoinNum" in data_str:
            return False
        return True
    api_lower = api.lower()
    if any(q in api_lower for q in ("list", "query")):
        return False
    return any(m in api_lower for m in (".execute", ".complete", ".issue", ".receiveprize", "receive_prize", "sign"))


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


class MtopTransport:
    """
    Централізований транспорт для підписання MTOP запитів, обробки відповідей,
    відновлення токенів (Self-Healing) та блокування при WAF/Captcha.
    """

    def __init__(self, client: AliClient, account_name: Optional[str] = None):
        self.client = client
        self.account_name = account_name or client.account_name
        self.waf_blocked: bool = False

    def refresh_token(self):
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

    def prepare_params(
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

    def call(
        self,
        api: str,
        data: Optional[Union[Dict[str, Any], str]] = None,
        max_retries: int = 2
    ) -> Optional[Dict[str, Any]]:
        """
        Виконує підписаний POST-запит до MTOP шлюзу AliExpress
        із перевіркою WAF блокування та автоматичним оновленням токена.
        """
        if self.waf_blocked:
            logger.warning(f"[{self.account_name}] 🚫 MTOP {api} пропущено через активне блокування WAF/Captcha.")
            return None

        is_mutation = is_mutation_api(api, data)
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

        if not self.client.get_cookie("_m_h5_tk"):
            self.refresh_token()

        for attempt in range(max_retries):
            if self.waf_blocked:
                return None

            token_before = self.client.get_cookie("_m_h5_tk")
            params = self.prepare_params(api=api, data=data)
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
                    logger.warning(f"[{self.account_name}] 🧩 Виявлено вимогу проходження капчі (WAF) у тексті відповіді!")
                    return None

                if not resp or resp.status_code != 200:
                    status = resp.status_code if resp else "None"
                    body = resp.text[:120] if resp else ""
                    if is_mutation:
                        logger.warning(
                            f"[{self.account_name}] ⚠️ MTOP мутація {api} зазнала невдачі на рівні транспорту (HTTP {status}: {body}). "
                            f"Повторний запит скасовано для запобігання дублюванню операції."
                        )
                        return None
                    logger.warning(f"[{self.account_name}] ⚠️ MTOP {api} повернув HTTP {status}: {body}")
                    continue

                res_data = resp.json()
                if isinstance(res_data, dict) and "api" not in res_data:
                    res_data["api"] = api

                logger.debug(f"[{self.account_name}] Відповідь від {url}: {res_data}")

                if is_captcha_required(res_data):
                    self.waf_blocked = True
                    logger.warning(f"[{self.account_name}] 🧩 Виявлено вимогу проходження капчі (WAF) у JSON відповіді!")
                    return res_data

                if is_mtop_token_expired(res_data) and attempt < max_retries - 1:
                    logger.info(f"[{self.account_name}] 🔄 Отримано FAIL_SYS_TOKEN_EXPIRED для {api}. Оновлюємо токен та повторюємо спробу...")
                    if self.client.get_cookie("_m_h5_tk") == token_before:
                        self.refresh_token()
                    continue

                return res_data
            except Exception as ex:
                logger.debug(f"[{self.account_name}] MTOP помилка для {api}: {ex}")
                if is_mutation:
                    logger.warning(
                        f"[{self.account_name}] ⚠️ Збій виклику MTOP мутації {api}: {ex}. "
                        f"Повторний запит скасовано для безпеки."
                    )
                    return None

        return None
