import logging
import random
import time
import json
import re
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional, Set, Tuple
from src.client import AliClient
from src.checkin import CheckinManager, _collect_nested_dicts, _clean_int
from src.mtop import is_captcha_required
from src.config import (
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
    TASK_PLATFORM_CANDIDATES,
    TASK_UMID_TOKEN,
    MAX_SAFE_TASKS_PER_RUN
)

logger = logging.getLogger(__name__)

# Небезпечні ключові слова — завдання з цими діями КАТЕГОРИЧНО ігноруються
UNSAFE_KEYWORDS = (
    "order", "buy", "pay", "cart", "purchase",
    "follow", "share", "deal_amount", "bind", "install",
    "покупка", "покупки", "покупку", "купити", "купіть",
    "замовлення", "замовлень", "замовити",
    "кошик", "кошика", "кошику", "кошиком",
    "підписка", "підписки", "підписку"
)
UNSAFE_REGEX = re.compile(r'(?i)\b(' + '|'.join(re.escape(w) for w in UNSAFE_KEYWORDS) + r')\b')

# Безпечні типи завдань (тільки пасивний перегляд / очікування 15 секунд)
SAFE_TASK_TYPES = {0, "0", 1, "1", 3, "3", "browse", "browse_item", "search", "view"}
SAFE_KEYWORDS = (
    "browse", "view", "explore", "visit", "find",
    "15s", "30s", "15 sec", "секунд", "перегляд", "пошук",
    "search", "keyword", "recently", "promoted", "recommend",
    "знайд", "улюблен", "пошук", "переглянут", "рекламован", "нещодавн",
    "ключов", "огляд"
)

# Ключові слова для ідентифікації типів завдань без обов'язкового URL
# Такі завдання мають власну логіку виконання (пошук, перегляд переглянутих тощо)
SEARCH_TASK_KEYWORDS = (
    "search", "keyword", "find", "favorite", "discover",
    "знайд", "улюблен", "пошук", "ключов"
)
RECENTLY_VIEWED_TASK_KEYWORDS = (
    "recently viewed", "recently browsed", "viewed items", "viewed products",
    "переглянут", "нещодавн", "недавн", "огляд"
)
PROMOTED_TASK_KEYWORDS = (
    "promoted", "recommended", "sponsored", "advertisement", "ads",
    "рекламован", "рекомендован", "спонсор"
)

# Резервні URL для завдань без валідного actionUrl
SEARCH_FALLBACK_KEYWORDS = [
    "phone case", "earphones", "usb cable", "screen protector",
    "led lights", "watch band", "laptop stand", "car holder",
    "wireless charger", "bluetooth speaker"
]
SEARCH_URL_TEMPLATE = "https://www.aliexpress.com/w/wholesale-{query}.html"
RECENTLY_VIEWED_URL = "https://www.aliexpress.com/p/coin-index/index.html"
PROMOTED_BROWSE_URL = "https://www.aliexpress.com/p/coin-index/index.html"
def _classify_special_task(task: Dict[str, Any]) -> Optional[str]:
    """
    Визначає спеціальний тип завдання за назвою та конфігурацією.
    Повертає:
    - 'search' для завдань пошуку за ключовими словами
    - 'recently_viewed' для огляду переглянутих товарів
    - 'promoted' для перегляду рекламованих товарів
    - None якщо завдання не відноситься до спеціальних типів
    """
    title = str(task.get("taskTitle", "")).lower()
    action_url = str(task.get("actionUrl", "")).lower()
    combined = f"{title} {action_url}"

    if any(kw in combined for kw in SEARCH_TASK_KEYWORDS):
        return "search"
    if any(kw in combined for kw in RECENTLY_VIEWED_TASK_KEYWORDS):
        return "recently_viewed"
    if any(kw in combined for kw in PROMOTED_TASK_KEYWORDS):
        return "promoted"
    return None


def _generate_fallback_url(special_type: str) -> str:
    """Генерує резервний URL для спеціальних завдань без валідного actionUrl."""
    if special_type == "search":
        keyword = random.choice(SEARCH_FALLBACK_KEYWORDS)
        return SEARCH_URL_TEMPLATE.format(query=keyword.replace(" ", "-"))
    elif special_type == "recently_viewed":
        return RECENTLY_VIEWED_URL
    elif special_type == "promoted":
        return PROMOTED_BROWSE_URL
    return ""


def _normalize_task_url(raw_url: Any) -> Optional[str]:
    """
    Нормалізує та валідує URL завдання:
    - Автоматично підвищує http:// та // до https://
    - Вимагає обов'язкової HTTPS-схеми
    - Дозволяє лише хости aliexpress.com або *.aliexpress.com
    """
    if not raw_url or not isinstance(raw_url, str):
        return None
    u = raw_url.strip()
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    if not u.startswith("https://"):
        return None
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    if host == "aliexpress.com" or host.endswith(".aliexpress.com"):
        return u
    return None


def _is_task_already_done(data: Any) -> bool:
    if not data:
        return False
    if isinstance(data, dict):
        if data.get("success") is False:
            return False
        if data.get("hasHandle") is False:
            return False
        err_msg = str(data.get("errorMessage", "") or data.get("msg", "")).lower()
        if any(f in err_msg for f in ["can't fund", "cannot find", "not found", "not exist", "no such", "error"]):
            return False
    text = str(data).lower()
    return any(w in text for w in [
        "already_done", "task_finished", "has finished", "already completed",
        "exceed_limit", "max_limit", "already_completed", "finish_limit"
    ])


def _is_task_success(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if _is_task_already_done(data):
        return False
    if data.get("errorCode"):
        return False
    data_block = data.get("data")
    if isinstance(data_block, dict):
        if data_block.get("errorCode"):
            return False
        if data_block.get("success") is False:
            return False
        res_in_data = data_block.get("result")
        if isinstance(res_in_data, dict):
            if res_in_data.get("errorCode"):
                return False
            if res_in_data.get("success") is False:
                return False
    result_block = data.get("result")
    if isinstance(result_block, dict):
        if result_block.get("errorCode"):
            return False
        if result_block.get("success") is False:
            return False
    if data.get("success") is False:
        return False
    if data.get("success") is True or data.get("isSuccess") is True:
        return True
    if isinstance(data_block, dict):
        if data_block.get("success") is True or data_block.get("isSuccess") is True:
            return True
        if data_block.get("completeSuccess") is True:
            return True
        res_in_data = data_block.get("result")
        if isinstance(res_in_data, dict):
            if res_in_data.get("success") is True or res_in_data.get("isSuccess") is True:
                return True
            if res_in_data.get("completeSuccess") is True:
                return True
    if isinstance(result_block, dict):
        if result_block.get("success") is True or result_block.get("isSuccess") is True:
            return True
        if result_block.get("completeSuccess") is True:
            return True
    ret = data.get("ret")
    if isinstance(ret, list) and len(ret) > 0 and isinstance(ret[0], str):
        return ret[0].startswith("SUCCESS::") or ret[0] == "SUCCESS"
    return False


def _is_no_group_match(res: Any) -> bool:
    """
    Визначає, чи повернув MTOP помилку таргетингу 'no group match' (E2-14-3).
    """
    if not isinstance(res, dict):
        return False
    ret = res.get("ret")
    ret_str = ret[0] if (isinstance(ret, list) and ret) else str(ret or "")
    if "E2-14-3" in ret_str or "no group match" in ret_str.lower():
        return True
    data_field = res.get("data")
    if isinstance(data_field, dict):
        if str(data_field.get("errorCode", "")).strip() == "E2-14-3":
            return True
        if "no group match" in str(data_field.get("errorMsg", "")).lower():
            return True
    return False


def _extract_user_locale_params(client: Optional[AliClient] = None) -> Tuple[str, str, str]:
    """
    Визначає (locale, currency, lang) для запитів завдань.
    За замовчуванням для України обов'язково використовує locale="UA", _currency="UAH", _lang="en_US"
    (з конфігурації TASK_LOCALE, TASK_CURRENCY, TASK_LANG).
    """
    return TASK_LOCALE, TASK_CURRENCY, TASK_LANG


def _extract_coins_from_complete_result(res: Dict[str, Any], task: Optional[Dict[str, Any]] = None) -> int:
    """
    Витягує нараховані монети з відповіді mtop.aliexpress.interactive.task.process.complete.
    Підтримує:
    1. interestIssueResultMap: map об'єктів з interestNum та JSON issueObject (transferPointCount).
    2. Fallback на поля awardCoins, coinCount, transferPointCount, rewardCoinNum у nested dicts.
    """
    if not isinstance(res, dict):
        return 0

    total_coins = 0
    data_dict = res.get("data")
    if not isinstance(data_dict, dict):
        data_dict = {}

    # 1. Основний формат AliExpress MTOP: interestIssueResultMap
    res_dict = data_dict.get("result") if isinstance(data_dict.get("result"), dict) else data_dict
    issue_map = res_dict.get("interestIssueResultMap") if isinstance(res_dict, dict) else None
    if not issue_map and isinstance(data_dict, dict):
        issue_map = data_dict.get("interestIssueResultMap")
    if isinstance(issue_map, dict):
        for interest_id, item in issue_map.items():
            if not isinstance(item, dict):
                continue
            item_coins = 0
            num_val = _clean_int(item.get("interestNum"))
            if num_val > 0:
                item_coins = num_val

            obj_raw = item.get("issueObject")
            if obj_raw:
                obj_data = {}
                if isinstance(obj_raw, str):
                    try:
                        obj_data = json.loads(obj_raw)
                    except Exception:
                        obj_data = {}
                elif isinstance(obj_raw, dict):
                    obj_data = obj_raw

                if isinstance(obj_data, dict):
                    t_val = _clean_int(
                        obj_data.get("transferPointCount")
                        or obj_data.get("pointCount")
                        or obj_data.get("amount")
                        or obj_data.get("coins")
                    )
                    if t_val > 0:
                        item_coins = max(item_coins, t_val)

            if item_coins > 0:
                total_coins += item_coins

    if total_coins > 0:
        return total_coins

    # 2. Fallback для альтернативних структур відповідей
    for block in _collect_nested_dicts(data_dict or res):
        for k in ("awardCoins", "coinCount", "rewardCoinNum", "prizeAmount", "transferPointCount", "interestNum"):
            val = _clean_int(block.get(k))
            if val > 0:
                total_coins = max(total_coins, val)

    return total_coins


class CoinsFarmManager:
    """
    Керує безпечним фармом монет за завдання (Coin Tasks):
    - запит списку активних завдань через MTOP (TASK_DELIVERY_QUERY_API),
    - жорстка фільтрація: ТІЛЬКИ безпечні пасивні завдання перегляду (~15 сек),
    - виконання завдання (перегляд сторінки) та фіксація через TASK_PROCESS_COMPLETE_API,
    - забирання (клейм) зароблених монет через TASK_PROCESS_ISSUE_API,
    - безпечний fallback на симуляцію перегляду каталогу при відсутності активних завдань.
    """
    _dead_urls: Set[str] = set()

    def __init__(self, client: AliClient, checkin_mgr: Optional[CheckinManager] = None):
        self.client = client
        self.account_name = client.account_name
        self.checkin_mgr = checkin_mgr
        self.task_coins_earned = 0
        self.completed_tasks = 0
        self.status = "INIT"
        self.error_message: Optional[str] = None
        self.working_platform: str = TASK_PLATFORM
        self.tasks_completed: int = 0
        self.tasks_already_done: int = 0
        self.tasks_skipped: int = 0
        self.tasks_failed: int = 0
        self.last_complete_coins: int = 0
        self.last_complete_data: Dict[str, Any] = {}
        self.task_records: List[Dict[str, Any]] = []

    @classmethod
    def reset_dead_urls(cls):
        """Очищує кеш неактивних URL (для ізоляції тестів)."""
        cls._dead_urls.clear()

    def fetch_active_tasks(self) -> List[Dict[str, Any]]:
        """
        Отримує список доступних завдань користувача з MTOP платформи завдань
        (із fallback до legacy REST-ендпоінтів для сумісності).
        """
        # 1. Основний шлях: MTOP interactive.task.delivery.query (тільки якщо checkin_mgr доступний)
        if self.checkin_mgr is not None:
            try:
                candidate_platforms: List[str] = []
                for p in [TASK_PLATFORM] + TASK_PLATFORM_CANDIDATES:
                    if p and p not in candidate_platforms:
                        candidate_platforms.append(p)

                mtop_res: Optional[Dict[str, Any]] = None
                working_platform: Optional[str] = None
                all_no_group_match = True

                locale, currency, lang = _extract_user_locale_params(self.client)
                ua_val = f"defaultUA2_load_failed with timeout@@https://m.aliexpress.com/p/coin-index/index.html@@{int(time.time() * 1000)}"

                for platform_candidate in candidate_platforms:
                    if getattr(self.checkin_mgr, "waf_blocked", False) is True:
                        self.status = "CAPTCHA_REQUIRED"
                        self.error_message = "Alibaba WAF captcha blocked"
                        logger.warning(f"[{self.account_name}] 🧩 Зупинка опитування платформ завдань через блокування WAF.")
                        break

                    res = self.checkin_mgr._call_mtop(
                        TASK_DELIVERY_QUERY_API,
                        data={
                            "ua": ua_val,
                            "umidToken": TASK_UMID_TOKEN,
                            "umidTokenType": "TOKEN",
                            "_lang": lang,
                            "_currency": currency,
                            "lang": lang,
                            "currency": currency,
                            "locale": locale,
                            "ttid": TASK_TTID,
                            "platform": platform_candidate,
                            "sceneId": TASK_SCENE_ID,
                            "taskSceneId": TASK_SCENE_ID,
                            "projectAppKey": PROJECT_APP_KEY,
                            "taskAppKey": TASK_APP_KEY,
                            "asac": ASAC_TASK_CAMPAIGN,
                            "fromNew": True
                        }
                    )
                    if getattr(self.checkin_mgr, "waf_blocked", False) is True or is_captcha_required(res):
                        self.status = "CAPTCHA_REQUIRED"
                        self.error_message = "Alibaba WAF captcha blocked"
                        if self.checkin_mgr:
                            self.checkin_mgr.waf_blocked = True
                        logger.warning(f"[{self.account_name}] 🧩 Виявлено WAF / Captcha при запиті завдань. Перериваємо опитування платформ.")
                        break

                    if not res or not isinstance(res, dict):
                        all_no_group_match = False
                        continue

                    if _is_no_group_match(res):
                        logger.info(
                            f"[{self.account_name}] ℹ️ Платформа '{platform_candidate}' повернула E2-14-3 (no group match). Пробуємо наступний варіант..."
                        )
                        continue

                    # Якщо повернулася відповідь не з помилкою no group match
                    all_no_group_match = False
                    ret = res.get("ret")
                    ret_str = ret[0] if (isinstance(ret, list) and ret) else str(ret or "")

                    is_success = False
                    if isinstance(ret, list) and ret:
                        if "SUCCESS" in ret_str.upper():
                            is_success = True
                    elif res.get("success") is True or (isinstance(res.get("data"), dict) and res["data"].get("success") is not False):
                        is_success = True
                    elif "data" in res and ret is None:
                        is_success = True

                    if is_success:
                        logger.info(f"[{self.account_name}] ✅ Знайдено робочу платформу для завдань: '{platform_candidate}'")
                        mtop_res = res
                        working_platform = platform_candidate
                        self.working_platform = platform_candidate
                        break
                    else:
                        logger.error(f"[{self.account_name}] ❌ Помилка запиту завдань ({TASK_DELIVERY_QUERY_API}): {ret_str}")
                        self.status = "FAILED"
                        self.error_message = ret_str
                        return []

                if working_platform is None:
                    if all_no_group_match:
                        logger.warning(
                            f"[{self.account_name}] ⚠️ Для поточної сцени/регіону немає активної групи завдань "
                            f"(E2-14-3: no group match). Можливо, потрібні актуальні taskSceneId та projectAppKey з додатку."
                        )
                        self.status = "SKIPPED"
                        self.error_message = "no group match (E2-14-3)"
                        return []
                    elif self.status != "FAILED":
                        logger.warning(f"[{self.account_name}] ⚠️ MTOP {TASK_DELIVERY_QUERY_API} не повернув відповіді.")
                        self.status = "FAILED"
                        self.error_message = "No response from MTOP"
                        return []
                    else:
                        return []

                data_field = mtop_res.get("data")
                raw_items = []
                candidate_dicts = [mtop_res]
                if isinstance(data_field, dict):
                    candidate_dicts.append(data_field)
                    if isinstance(data_field.get("data"), dict):
                        candidate_dicts.append(data_field["data"])

                for d in candidate_dicts:
                    for k in ("result", "taskList", "tasks", "taskDeliveryList", "deliveryList"):
                        val = d.get(k)
                        if isinstance(val, list) and val and isinstance(val[0], dict):
                            raw_items = val
                            break
                    if raw_items:
                        break

                if not raw_items and isinstance(data_field, list) and data_field and isinstance(data_field[0], dict):
                    raw_items = data_field
                elif not raw_items and isinstance(data_field, dict) and isinstance(data_field.get("data"), list):
                    sub_list = data_field["data"]
                    if sub_list and isinstance(sub_list[0], dict):
                        raw_items = sub_list

                if raw_items:
                    # Логуємо кожен елемент масиву materials окремим рядком без скорочення
                    logger.info(f"[{self.account_name}] 📋 MTOP отримано елементів завдань: {len(raw_items)}")
                    for idx, it in enumerate(raw_items, 1):
                        if not isinstance(it, dict):
                            continue
                        mats = it.get("materials")
                        if isinstance(mats, list) and mats:
                            for m_idx, m in enumerate(mats, 1):
                                if isinstance(m, dict):
                                    try:
                                        m_str = json.dumps(m, ensure_ascii=False)
                                    except Exception:
                                        m_str = str(m)
                                    logger.debug(f"[{self.account_name}]   [Завдання #{idx}.{m_idx}] materials: {m_str}")
                        else:
                            try:
                                it_str = json.dumps(it, ensure_ascii=False)
                            except Exception:
                                it_str = str(it)
                            logger.debug(f"[{self.account_name}]   [Завдання #{idx}] data: {it_str}")

                    normalized = []
                    # Збираємо пари (it, mat) для обробки
                    task_pairs = []
                    for it in raw_items:
                        if not isinstance(it, dict):
                            continue
                        mats = it.get("materials")
                        if isinstance(mats, list) and mats and isinstance(mats[0], dict) and (len(mats) > 1 or not (it.get("taskInstanceId") or it.get("taskId") or it.get("id"))):
                            for m in mats:
                                if isinstance(m, dict):
                                    task_pairs.append((it, m))
                        else:
                            mat = mats[0] if (isinstance(mats, list) and mats and isinstance(mats[0], dict)) else {}
                            task_pairs.append((it, mat))

                    for it, mat in task_pairs:
                        # Розпаршуємо behaviorConfig (якщо це JSON-рядок)
                        cfg = {}
                        behavior_raw = mat.get("behaviorConfig") or it.get("behaviorConfig")
                        if isinstance(behavior_raw, str) and behavior_raw.strip():
                            try:
                                cfg = json.loads(behavior_raw)
                            except Exception:
                                cfg = {}
                        elif isinstance(behavior_raw, dict):
                            cfg = behavior_raw

                        task_id = (
                            it.get("taskInstanceId")
                            or it.get("taskId")
                            or it.get("id")
                            or mat.get("taskInstanceId")
                            or mat.get("taskId")
                            or mat.get("id")
                            or mat.get("materialId")
                        )
                        status = (
                            mat.get("newTaskStatus")
                            or mat.get("status")
                            or it.get("status")
                            or ("completed" if it.get("completed") else "todo")
                        )
                        task_type = (
                            mat.get("taskType")
                            if mat.get("taskType") is not None
                            else (
                                it.get("taskType")
                                if it.get("taskType") is not None
                                else (
                                    mat.get("type")
                                    if mat.get("type") is not None
                                    else (
                                        it.get("type")
                                        if it.get("type") is not None
                                        else cfg.get("type")
                                    )
                                )
                            )
                        )

                        # Назва завдання: зчитуємо mainTitle, subTitle, title, taskName
                        raw_title = (
                            mat.get("mainTitle")
                            or mat.get("subTitle")
                            or mat.get("taskTitle")
                            or mat.get("title")
                            or mat.get("taskName")
                            or it.get("taskTitle")
                            or it.get("title")
                            or it.get("taskName")
                            or f"Task_{task_id}"
                        )
                        title = str(raw_title).replace("\r", " ").replace("\n", " ").strip()

                        # URL завдання: шукаємо реальний URL, НЕ підставляємо дефолтний women-clothing!
                        action_url = (
                            mat.get("actionUrl")
                            or mat.get("materialUrl")
                            or mat.get("targetUrl")
                            or mat.get("jumpUrl")
                            or mat.get("url")
                            or mat.get("taskAction")
                            or cfg.get("url")
                            or cfg.get("actionUrl")
                            or cfg.get("targetUrl")
                            or it.get("actionUrl")
                            or it.get("materialUrl")
                            or it.get("targetUrl")
                            or it.get("jumpUrl")
                            or it.get("url")
                            or ""
                        )
                        action_url = str(action_url).strip()

                        # Тривалість перегляду (staySeconds / time)
                        stay_seconds = (
                            _clean_int(cfg.get("time"))
                            or _clean_int(cfg.get("duration"))
                            or _clean_int(cfg.get("staySeconds"))
                            or _clean_int(mat.get("staySeconds"))
                            or _clean_int(it.get("staySeconds"))
                            or 15
                        )

                        un_issue = mat.get("unIssueRecord") or it.get("unIssueRecord")
                        instance_status = mat.get("instanceStatus")
                        if instance_status is None:
                            instance_status = it.get("instanceStatus", 0)

                        group_id = it.get("groupId") or mat.get("groupId")
                        interest_code = mat.get("interestCode") or it.get("interestCode")

                        times_limit = _clean_int(mat.get("timesLimit") or it.get("timesLimit") or 1)
                        if times_limit <= 0:
                            times_limit = 1
                        times_joined = _clean_int(mat.get("timesJoined") or it.get("timesJoined") or 0)
                        raw_issue_handle = mat.get("issueHandle") if mat.get("issueHandle") is not None else it.get("issueHandle", 0)
                        issue_handle = _clean_int(raw_issue_handle)

                        raw_type = (
                            mat.get("type")
                            if mat.get("type") is not None
                            else (
                                it.get("type")
                                if it.get("type") is not None
                                else cfg.get("type")
                            )
                        )
                        browse_zero_flag = bool(
                            mat.get("browseZeroFlag")
                            or it.get("browseZeroFlag")
                            or cfg.get("browseZeroFlag")
                        )
                        task_tag_id = (
                            cfg.get("taskTagId")
                            or mat.get("taskTagId")
                            or it.get("taskTagId")
                        )
                        component_type = (
                            cfg.get("componentType")
                            or mat.get("componentType")
                            or it.get("componentType")
                        )

                        normalized.append({
                            "taskId": task_id,
                            "taskInstanceId": task_id,
                            "taskAppKey": it.get("taskAppKey") or TASK_APP_KEY,
                            "projectAppKey": it.get("projectAppKey") or PROJECT_APP_KEY,
                            "taskSceneId": it.get("taskSceneId") or TASK_SCENE_ID,
                            "sceneId": it.get("sceneId") or TASK_SCENE_ID,
                            "groupId": group_id,
                            "interestCode": interest_code,
                            "instanceStatus": instance_status,
                            "timesLimit": times_limit,
                            "timesJoined": times_joined,
                            "issueHandle": issue_handle,
                            "taskType": task_type,
                            "rawType": raw_type,
                            "browseZeroFlag": browse_zero_flag,
                            "taskTagId": task_tag_id,
                            "componentType": component_type,
                            "taskTitle": title,
                            "status": str(status).lower(),
                            "unIssueRecord": un_issue,
                            "staySeconds": stay_seconds,
                            "actionUrl": action_url,
                            "behaviorConfig": cfg,
                            "locale": it.get("locale") or locale,
                            "_lang": it.get("_lang") or lang,
                            "_currency": it.get("_currency") or currency,
                            "lang": it.get("lang") or it.get("_lang") or lang,
                            "currency": it.get("currency") or it.get("_currency") or currency,
                            "ttid": it.get("ttid") or TASK_TTID,
                            "platform": it.get("platform") or working_platform or TASK_PLATFORM,
                            "raw": it
                        })

                    if normalized:
                        self.status = "SUCCESS"
                        logger.info(f"[{self.account_name}] 📋 MTOP нормалізовано завдань платформи: {len(normalized)}")
                        for idx, t in enumerate(normalized, 1):
                            logger.info(f"[{self.account_name}]   [{idx}] ID: {t.get('taskId')} | '{t.get('taskTitle')}' | Статус: {t.get('status')} | Тип: {t.get('taskType')} | Час: {t.get('staySeconds')}с | URL: {t.get('actionUrl') or '-'}")
                        return normalized

                # Якщо відповідь SUCCESS, але список завдань порожній
                self.status = "SUCCESS"
                logger.info(f"[{self.account_name}] ℹ️ Активних завдань платформи на сьогодні не знайдено.")
                return []
            except Exception as e:
                logger.error(f"[{self.account_name}] ❌ Виняток під час MTOP task delivery query: {e}")
                self.status = "FAILED"
                self.error_message = str(e)
                return []

        # 2. Legacy fallback для тестових моків та старих ендпоінтів
        task_query_urls = [
            "https://coins.aliexpress.com/api/tasks",
            "https://coins.aliexpress.com/api/task/list"
        ]
        if all(query_url in self._dead_urls for query_url in task_query_urls):
            logger.debug(f"[{self.account_name}] Усі ендпоінти списку завдань неактивні, пропускаємо.")
            return []

        for query_url in task_query_urls:
            if query_url in self._dead_urls:
                continue
            try:
                resp = self.client.get(query_url, params={"channel": "COIN"})
                if not resp or resp.status_code != 200:
                    if resp is None or resp.status_code in (404, 405, 410, 500, 502, 503, 504):
                        self._dead_urls.add(query_url)
                    continue
                raw_ct = ""
                if hasattr(resp, "headers") and hasattr(resp.headers, "get"):
                    ct_val = resp.headers.get("content-type", "")
                    if isinstance(ct_val, str):
                        raw_ct = ct_val.lower()

                if raw_ct and "json" not in raw_ct:
                    self._dead_urls.add(query_url)
                    continue
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        task_list = (
                            data.get("data", {}).get("taskList")
                            or data.get("result", {}).get("taskList")
                            or []
                        )
                        if isinstance(task_list, list) and task_list:
                            logger.info(f"[{self.account_name}] Знайдено завдань через legacy API: {len(task_list)}")
                            return task_list
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"[{self.account_name}] Не вдалося отримати список завдань: {e}")

        return []

    def classify_task(self, task: Dict[str, Any]) -> Tuple[bool, str, str]:
        """
        Класифікує завдання за безпекою та доступністю для виконання.
        Повертає кортеж (is_safe, status, reason).
        """
        if not isinstance(task, dict):
            return False, "SKIPPED", "Некоректні дані завдання"

        status = str(task.get("status", "")).lower()
        if status in ("completed", "finished", "done"):
            return False, "ALREADY_DONE", "Вже виконано сьогодні раніше"

        # Безпечний парсинг behaviorConfig
        cfg = {}
        raw_cfg = task.get("behaviorConfig")
        if isinstance(raw_cfg, str) and raw_cfg.strip().startswith("{"):
            try:
                cfg = json.loads(raw_cfg)
            except Exception:
                cfg = {}
        elif isinstance(raw_cfg, dict):
            cfg = raw_cfg

        task_tag_id = task.get("taskTagId") or cfg.get("taskTagId")
        raw_type = task.get("rawType") if task.get("rawType") is not None else task.get("type")

        # Відсіюємо зовнішні дії та міні-ігри: taskTagId або raw_type == 30
        if task_tag_id or str(raw_type) == "30" or _clean_int(raw_type) == 30:
            logger.debug(f"[{self.account_name}] 🎮 Пропущено зовнішню дію / міні-гру (tag: {task_tag_id}, type: {raw_type}): {task.get('taskTitle')}")
            return False, "SKIPPED", "Зовнішня дія / міні-гра"

        is_claim_only = status in ("waitforreceive", "wait_for_receive") or bool(task.get("unIssueRecord"))

        instance_status = task.get("instanceStatus")
        clean_instance_status = _clean_int(instance_status) if instance_status is not None else None
        if clean_instance_status == 2:
            return False, "ALREADY_DONE", "Вже виконано сьогодні раніше"

        if clean_instance_status not in (0, None) and not is_claim_only:
            return False, "SKIPPED", f"Неактивний статус (instanceStatus={instance_status})"

        times_limit = _clean_int(task.get("timesLimit", 1))
        times_joined = _clean_int(task.get("timesJoined", 0))
        if times_limit > 0 and times_joined >= times_limit and not is_claim_only:
            return False, "SKIPPED", f"Всі спроби вичерпано ({times_joined}/{times_limit})"

        # Якщо це готова нагорода для забирання монет
        if is_claim_only:
            return True, "CLAIM", "Готово до отримання нагороди"

        # Визначаємо спеціальний тип завдання (пошук, огляд переглянутих, рекламовані)
        special_type = _classify_special_task(task)
        task_title_str = str(task.get("taskTitle", ""))
        task_title_lower = task_title_str.lower()

        # Якщо завдання вимагає реальної дії (додати в кошик, купити, замовити тощо)
        is_explicit_action = any(act in task_title_lower for act in (
            "додай", "додати", "add to", "купи", "купіть", "купити", "замов", "order", "pay", "оплат", "follow", "підпиш", "підпис"
        ))

        # Перевірка на небезпечні ключові слова через word boundaries (\b)
        # Нормалізуємо роздільники (_, -, /, ?, &, =, .) у пробіли, щоб add_to_cart та follow_shop відповідали cart та follow
        raw_text = " ".join([
            task_title_str,
            str(task.get("taskType", "")),
            str(task.get("actionUrl", "")),
            str(task.get("actionType", ""))
        ])
        normalized_text = re.sub(r'[\_\-\/\?\&=\.]', ' ', raw_text)

        # Спеціальні завдання пасивного перегляду / пошуку ігнорують слово cart в URL, якщо в заголовку немає прямої дії купівлі чи додавання
        is_safe_special_browse = bool(special_type and not is_explicit_action)
        if not is_safe_special_browse and (is_explicit_action or UNSAFE_REGEX.search(normalized_text)):
            logger.debug(f"[{self.account_name}] 🚫 Пропущено небезпечне завдання: {task.get('taskTitle')}")
            return False, "SKIPPED", "Небезпечна дія (покупка/підписка/кошик)"

        # Відсіюємо щоденний чекін без URL
        if "daily check" in task_title_lower and not task.get("actionUrl"):
            logger.debug(f"[{self.account_name}] ℹ️ Пропущено 'Daily check-in' без URL перегляду: {task.get('taskTitle')}")
            return False, "SKIPPED", "Щоденний чекін без URL для перегляду"

        # Для завдань на перегляд обов'язкова наявність валідного HTTPS actionUrl
        # АЛЕ спеціальні завдання можуть використовувати резервний URL
        raw_action_url = task.get("actionUrl")
        action_url = _normalize_task_url(raw_action_url)
        if not action_url:
            if special_type:
                # Генеруємо резервний URL для спеціальних завдань
                fallback_url = _generate_fallback_url(special_type)
                if fallback_url:
                    task["_fallback_url"] = fallback_url
                    task["_special_type"] = special_type
                    logger.info(f"[{self.account_name}] 🔄 Завдання '{task.get('taskTitle')}' ({special_type}): використовуємо резервний URL")
                    if special_type == "search":
                        return True, "SAFE_SEARCH", "Безпечне завдання пошуку (ключові слова)"
                    elif special_type == "recently_viewed":
                        return True, "SAFE_BROWSE", "Безпечне завдання перегляду переглянутих товарів"
                    else:
                        return True, "SAFE_BROWSE", "Безпечне завдання перегляду рекламованих товарів"
            return False, "SKIPPED", "Відсутній або невалідний HTTPS URL для перегляду"

        # Спеціальні завдання з валідним URL — безумовно безпечні
        if special_type:
            task["_special_type"] = special_type
            if special_type == "search":
                return True, "SAFE_SEARCH", "Безпечне завдання пошуку (ключові слова)"
            return True, "SAFE_BROWSE", f"Безпечне завдання ({special_type})"

        # Справжні завдання перегляду: browseZeroFlag is True або raw_type == 0
        browse_zero = bool(task.get("browseZeroFlag") or cfg.get("browseZeroFlag"))
        raw_type_val = task.get("rawType") if task.get("rawType") is not None else task.get("type")
        task_type = task.get("taskType")
        action_type = task.get("actionType")

        # Визначаємо ефективний тип завдання
        effective_type = task_type if task_type is not None else (raw_type_val if raw_type_val is not None else action_type)
        effective_str = str(effective_type).strip().lower() if effective_type is not None else ""

        # Тип 0 або рядкові типи перегляду є безумовно безпечними
        if browse_zero or effective_type in (0, "0", "browse", "browse_item", "search", "view") or effective_str in ("0", "browse", "browse_item", "search", "view"):
            return True, "SAFE_BROWSE", "Безпечне завдання перегляду"

        # Структурні типи 1, 3 або None (коли тип відсутній у відповіді materials):
        # Дозволяються ТІЛЬКИ якщо мають ключові слова перегляду у назві або прапорець browseZeroFlag
        if effective_type in (1, "1", 3, "3", None) or effective_str in ("1", "3"):
            title_lower = str(task.get("taskTitle", "")).lower()
            if browse_zero or any(kw in title_lower for kw in SAFE_KEYWORDS):
                return True, "SAFE_BROWSE", "Безпечне завдання перегляду"
            return False, "SKIPPED", "Фільтр безпеки: непідтримуваний тип"

        # Будь-які невідомі типи (наприклад 99, 10, 30 тощо) НЕ підвищуються за заголовком
        return False, "SKIPPED", "Фільтр безпеки: непідтримуваний тип"

    def is_safe_task(self, task: Dict[str, Any]) -> bool:
        """
        Жорсткий фільтр безпеки: повертає True для безпечних пасивних завдань
        (перегляд сторінок/товарів ~15 секунд або забирання вже готової нагороди).
        Всі покупки, додавання в кошик, підписки чи зовнішні дії/міні-ігри відхиляються.
        """
        is_safe, _, _ = self.classify_task(task)
        return is_safe

    def farm_browse_tasks(self, count: int = 15) -> int:
        """
        Виконує безпечні завдання із затримкою та нарахуванням монет (підтримка timesLimit).
        """
        self.completed_tasks = 0
        self.task_coins_earned = 0
        self.tasks_completed = 0
        self.tasks_already_done = 0
        self.tasks_skipped = 0
        self.tasks_failed = 0
        self.task_records = []

        # 1. Якщо доступний MTOP шлюз, виконуємо реальні безпечні завдання
        if self.checkin_mgr is not None:
            if (
                getattr(self.checkin_mgr, "waf_blocked", False)
                or getattr(self.checkin_mgr, "status", None) == "CAPTCHA_REQUIRED"
                or getattr(self.checkin_mgr, "mobile_status", None) == "CAPTCHA_REQUIRED"
            ):
                logger.warning(f"[{self.account_name}] 🧩 Виявлено WAF капчу, пропускаємо виконання завдань.")
                self.status = "SKIPPED"
                self.tasks_skipped += 1
                self.task_records.append({
                    "task_id": "WAF_CAPTCHA",
                    "title": "Завдання платформи",
                    "status": "SKIPPED",
                    "coins": 0,
                    "reason": "Пропущено через блокування капчею WAF"
                })
                return 0
            if getattr(self.checkin_mgr, "session_valid", True) is False or getattr(self.checkin_mgr, "status", None) == "SESSION_EXPIRED":
                logger.warning(f"[{self.account_name}] ❌ Сесія недійсна, пропускаємо виконання завдань.")
                self.status = "SKIPPED"
                self.tasks_skipped += 1
                self.task_records.append({
                    "task_id": "SESSION_EXPIRED",
                    "title": "Завдання платформи",
                    "status": "SKIPPED",
                    "coins": 0,
                    "reason": "Пропущено через недійсну сесію акаунта"
                })
                return 0

            logger.info(f"[{self.account_name}] 🛍️ Початок виконання завдань (безпечний перегляд, сесійний ліміт: {count})...")
            active_tasks = self.fetch_active_tasks()
            if self.status == "FAILED":
                logger.error(f"[{self.account_name}] ❌ Етап виконання завдань позначено як FAILED через помилку MTOP API.")
                self.tasks_failed += 1
                self.task_records.append({
                    "task_id": "DELIVERY_QUERY",
                    "title": "Опитування платформи завдань",
                    "status": "FAILED",
                    "coins": 0,
                    "reason": self.error_message or "Помилка MTOP API"
                })
                return 0
            if self.status in ("SKIPPED", "NO_TASKS"):
                logger.info(f"[{self.account_name}] ℹ️ Виконання завдань пропущено ({self.status}).")
                self.tasks_skipped += 1
                self.task_records.append({
                    "task_id": "DELIVERY_QUERY",
                    "title": "Опитування платформи завдань",
                    "status": "SKIPPED",
                    "coins": 0,
                    "reason": self.error_message or "Немає активних груп (E2-14-3)"
                })
                return 0

            safe_tasks = []
            for t in active_tasks:
                is_safe, rec_status, reason = self.classify_task(t)
                if is_safe:
                    safe_tasks.append(t)
                else:
                    if rec_status in ("ALREADY_DONE", "DONE"):
                        self.tasks_already_done += 1
                    else:
                        self.tasks_skipped += 1
                    t_title = t.get("taskTitle") or f"Завдання {t.get('taskId')}"
                    self.task_records.append({
                        "task_id": str(t.get("taskId") or t.get("taskInstanceId") or "UNKNOWN"),
                        "title": t_title,
                        "status": rec_status,
                        "coins": 0,
                        "reason": reason
                    })

            if safe_tasks:
                safe_tasks.sort(key=lambda t: 0 if (t.get("status") in ("waitforreceive", "wait_for_receive") or t.get("unIssueRecord")) else 1)

                for idx, task in enumerate(safe_tasks, 1):
                    if getattr(self.checkin_mgr, "waf_blocked", False) is True or self.status == "CAPTCHA_REQUIRED":
                        self.status = "CAPTCHA_REQUIRED"
                        logger.warning(f"[{self.account_name}] 🧩 Зупинка виконання завдань через блокування WAF/Captcha.")
                        break

                    attempts = self.tasks_completed + self.tasks_failed
                    if attempts >= count:
                        self.tasks_skipped += 1
                        t_title = task.get("taskTitle") or f"Завдання {task.get('taskId')}"
                        self.task_records.append({
                            "task_id": str(task.get("taskId") or task.get("taskInstanceId") or "UNKNOWN"),
                            "title": t_title,
                            "status": "SKIPPED",
                            "coins": 0,
                            "reason": "Перевищено ліміт завдань на сесію"
                        })
                        continue

                    title = task.get("taskTitle", f"Завдання #{idx}")
                    status = task.get("status", "")
                    task_id = str(task.get("taskId") or task.get("taskInstanceId") or f"TASK_{idx}")
                    is_claim_only = status in ("waitforreceive", "wait_for_receive") or bool(task.get("unIssueRecord"))

                    if is_claim_only:
                        logger.info(f"[{self.account_name}] 🎁 [{idx}/{len(safe_tasks)}] Забирання нагороди за виконане завдання: '{title}'...")
                        coins = self._claim_task_reward(task)
                        if getattr(self.checkin_mgr, "waf_blocked", False) is True or self.status == "CAPTCHA_REQUIRED":
                            self.status = "CAPTCHA_REQUIRED"
                            break

                        if coins >= 0:
                            self.completed_tasks += 1
                            self.tasks_completed += 1
                            self.task_coins_earned += coins
                            self.task_records.append({
                                "task_id": task_id,
                                "title": title,
                                "status": "COMPLETED",
                                "coins": coins,
                                "reason": "Забрано невиплачену нагороду"
                            })
                        else:
                            self.tasks_failed += 1
                            self.task_records.append({
                                "task_id": task_id,
                                "title": title,
                                "status": "FAILED",
                                "coins": 0,
                                "reason": "Не вдалося отримати нагороду"
                            })
                        continue

                    # Розрахунок доступних повторів завдання (timesLimit - timesJoined)
                    times_limit = _clean_int(task.get("timesLimit", 1))
                    times_joined = _clean_int(task.get("timesJoined", 0))
                    target_runs = max(1, times_limit - times_joined)

                    target_url = task.get("actionUrl") or task.get("_fallback_url", "")
                    special_type = task.get("_special_type")
                    req_seconds = _clean_int(task.get("staySeconds", 15))
                    if req_seconds <= 0:
                        req_seconds = 15
                    if req_seconds > 30:
                        self.tasks_skipped += 1
                        self.task_records.append({
                            "task_id": task_id,
                            "title": title,
                            "status": "SKIPPED",
                            "coins": 0,
                            "reason": f"Тривалість перегляду ({req_seconds}с) перевищує безпечний ліміт 30с"
                        })
                        logger.info(f"[{self.account_name}] ⏭️ Пропущено завдання '{title}': тривалість {req_seconds}с > 30с.")
                        continue
                    stay_seconds = req_seconds

                    for run_idx in range(1, target_runs + 1):
                        if getattr(self.checkin_mgr, "waf_blocked", False) is True or self.status == "CAPTCHA_REQUIRED":
                            self.status = "CAPTCHA_REQUIRED"
                            break

                        attempts = self.tasks_completed + self.tasks_failed
                        if attempts >= count:
                            logger.info(f"[{self.account_name}] Досягнуто ліміт завдань на сесію ({count}).")
                            break

                        run_label = f" ({run_idx}/{target_runs})" if target_runs > 1 else ""
                        run_task_id = f"{task_id}_{run_idx}" if target_runs > 1 else task_id

                        # Для пошукових завдань генеруємо різні ключові слова для кожного повтору
                        effective_url = target_url
                        if special_type == "search" and run_idx > 1:
                            keyword = random.choice(SEARCH_FALLBACK_KEYWORDS)
                            effective_url = SEARCH_URL_TEMPLATE.format(query=keyword.replace(" ", "-"))

                        nav_url = _normalize_task_url(effective_url)
                        if nav_url:
                            type_label = f" [{special_type}]" if special_type else ""
                            logger.info(f"[{self.account_name}] ⏳ [{idx}/{len(safe_tasks)}]{run_label} [Безпечне завдання{type_label}] '{title}': перегляд сторінки ({stay_seconds} сек) -> {nav_url}...")
                            nav_resp = None
                            try:
                                nav_resp = self.client.get(nav_url)
                            except Exception as e:
                                logger.debug(f"[{self.account_name}] Помилка відкриття URL завдання: {e}")

                            if nav_resp is not None and (getattr(nav_resp, "status_code", None) == 403 or is_captcha_required(nav_resp)):
                                if self.checkin_mgr:
                                    self.checkin_mgr.waf_blocked = True
                                self.status = "CAPTCHA_REQUIRED"
                                logger.warning(f"[{self.account_name}] 🧩 Блокування WAF / 403 при переході на {nav_url}!")
                                break
                        else:
                            logger.info(f"[{self.account_name}] ⏳ [{idx}/{len(safe_tasks)}]{run_label} [Безпечне завдання] '{title}': пасивне очікування ({stay_seconds} сек)...")

                        # Реальна пауза для зарахування перегляду на сервері AliExpress
                        self.client.sleep_random(stay_seconds + 0.2, stay_seconds + 1.5)

                        # Фіксація завершення завдання
                        complete_ok = self._complete_task(task)
                        if getattr(self.checkin_mgr, "waf_blocked", False) is True or self.status == "CAPTCHA_REQUIRED":
                            self.status = "CAPTCHA_REQUIRED"
                            break

                        earned = task.get("earnedCoins", 0)

                        # Клейм монет викликаємо лише якщо complete_ok успішний і завдання потребує ручного клейму
                        # (issueHandle == 1 або unIssueRecord).
                        coins_claimed = -1
                        needs_manual_claim = bool(task.get("unIssueRecord")) or _clean_int(task.get("issueHandle", 0)) == 1
                        if complete_ok and earned <= 0 and needs_manual_claim and not task.get("illegalCompletion"):
                            coins_claimed = self._claim_task_reward(task)
                            if getattr(self.checkin_mgr, "waf_blocked", False) is True or self.status == "CAPTCHA_REQUIRED":
                                self.status = "CAPTCHA_REQUIRED"
                                break
                            if coins_claimed > 0:
                                earned = coins_claimed

                        if complete_ok and (earned > 0 or coins_claimed >= 0 or task.get("completeSuccess")):
                            self.completed_tasks += 1
                            self.tasks_completed += 1
                            if earned > 0:
                                self.task_coins_earned += earned
                            status_reason = f"Успішно зараховано (+{earned} 🪙)" if earned > 0 else "Переглянуто та зараховано"
                            self.task_records.append({
                                "task_id": run_task_id,
                                "title": f"{title}{run_label}",
                                "status": "COMPLETED",
                                "coins": earned,
                                "reason": status_reason
                            })
                            logger.info(f"[{self.account_name}] ✅ Завдання '{title}'{run_label} успішно зараховано! (+{earned} 🪙)")
                        else:
                            self.tasks_failed += 1
                            self.task_records.append({
                                "task_id": run_task_id,
                                "title": f"{title}{run_label}",
                                "status": "FAILED",
                                "coins": 0,
                                "reason": "Сервер не підтвердив завершення перегляду"
                            })
                            logger.warning(f"[{self.account_name}] ⚠️ Не вдалося завершити завдання '{title}'{run_label}.")
                            break

                        # Перевіряємо, чи сервер повернув фініш завдання
                        comp_res = task.get("completeResult", {})
                        comp_data = comp_res.get("data", {}) if isinstance(comp_res, dict) else {}
                        comp_result = comp_data.get("result", {}) if isinstance(comp_data, dict) and isinstance(comp_data.get("result"), dict) else (comp_data if isinstance(comp_data, dict) else {})
                        server_status = _clean_int(comp_result.get("instanceStatus"))
                        server_joined = _clean_int(comp_result.get("timesJoined"))
                        server_limit = _clean_int(comp_result.get("timesLimit"))

                        if server_status == 2 or (server_limit > 0 and server_joined >= server_limit):
                            logger.info(f"[{self.account_name}] ℹ️ Завдання '{title}' завершило всі доступні повтори ({server_joined}/{server_limit}).")
                            break

                        if run_idx < target_runs:
                            self.client.sleep_random(1.5, 3.0)

                    if getattr(self.checkin_mgr, "waf_blocked", False) is True or self.status == "CAPTCHA_REQUIRED":
                        self.status = "CAPTCHA_REQUIRED"
                        break

                if self.status != "CAPTCHA_REQUIRED":
                    if self.tasks_failed > 0:
                        self.status = "PARTIAL" if self.tasks_completed > 0 else "FAILED"
                    elif self.tasks_completed > 0:
                        self.status = "SUCCESS"
                    elif self.tasks_already_done > 0:
                        self.status = "ALREADY_DONE"
                    else:
                        self.status = "SUCCESS"
                return self.completed_tasks
            else:
                logger.info(f"[{self.account_name}] ℹ️ Немає доступних безпечних завдань для виконання.")
                if self.status not in ("FAILED", "CAPTCHA_REQUIRED"):
                    self.status = "SKIPPED" if not active_tasks else "SUCCESS"
                if not active_tasks:
                    self.tasks_skipped += 1
                    self.task_records.append({
                        "task_id": "EMPTY",
                        "title": "Завдання платформи",
                        "status": "SKIPPED",
                        "coins": 0,
                        "reason": "Список завдань на сьогодні порожній"
                    })
                return 0
        else:
            logger.warning(f"[{self.account_name}] ⚠️ checkin_mgr відсутній, MTOP завдання недоступні.")
            self.status = "SKIPPED"
            return 0

    def _complete_task(self, task: Dict[str, Any]) -> bool:
        """
        Відправляє запит завершення безпечного завдання до MTOP шлюзу.
        """
        task["earnedCoins"] = 0
        task["completeSuccess"] = False
        task.pop("illegalCompletion", None)
        task.pop("errorCode", None)
        task.pop("completeResult", None)

        task_id = task.get("taskInstanceId") or task.get("taskId")
        if not task_id:
            return False

        locale, currency, lang = _extract_user_locale_params(self.client)
        task_platform = task.get("platform") or getattr(self, "working_platform", TASK_PLATFORM)
        ua_val = f"defaultUA2_load_failed with timeout@@https://m.aliexpress.com/p/coin-index/index.html@@{int(time.time() * 1000)}"
        payload = {
            "ua": ua_val,
            "umidToken": TASK_UMID_TOKEN,
            "umidTokenType": "TOKEN",
            "taskInstanceId": task_id,
            "taskAppKey": task.get("taskAppKey", TASK_APP_KEY),
            "projectAppKey": task.get("projectAppKey", PROJECT_APP_KEY),
            "taskSceneId": task.get("taskSceneId", TASK_SCENE_ID),
            "sceneId": task.get("sceneId", TASK_SCENE_ID),
            "ttid": task.get("ttid", TASK_TTID),
            "platform": task_platform,
            "fromNew": True,
            "asac": ASAC_TASK_CAMPAIGN,
            "idemId": str(int(time.time() * 1000)),
            "locale": task.get("locale", locale),
            "_lang": task.get("_lang", lang),
            "_currency": task.get("_currency", currency),
            "lang": task.get("lang", task.get("_lang", lang)),
            "currency": task.get("currency", task.get("_currency", currency))
        }
        if task.get("groupId"):
            payload["groupId"] = task["groupId"]
        if task.get("interestCode"):
            payload["interestCode"] = task["interestCode"]

        try:
            res = self.checkin_mgr._call_mtop(TASK_PROCESS_COMPLETE_API, data=payload)
            try:
                res_str = json.dumps(res, ensure_ascii=False)
            except Exception:
                res_str = str(res)
            logger.debug(f"[{self.account_name}] 📥 Відповідь MTOP _complete_task ({task_id}): {res_str}")
            if res and isinstance(res, dict):
                res_data = res.get("data") if isinstance(res.get("data"), dict) else {}
                res_result = res_data.get("result") if isinstance(res_data.get("result"), dict) else {}

                # Помилка, якщо в data або data.result (чи в корені) є errorCode
                has_error = bool(
                    res.get("errorCode")
                    or res_data.get("errorCode")
                    or res_result.get("errorCode")
                )

                # Завершення успішне ТІЛЬКИ якщо немає помилок і completeSuccess is True
                complete_success = bool(
                    res_result.get("completeSuccess") is True
                    or res_data.get("completeSuccess") is True
                    or (res_data.get("success") is True and not has_error)
                    or (res_result.get("success") is True and not has_error)
                )

                if not has_error and complete_success and _is_task_success(res):
                    earned_coins = _extract_coins_from_complete_result(res, task)
                    self.last_complete_coins = earned_coins
                    self.last_complete_data = res_data
                    task["earnedCoins"] = earned_coins
                    task["completeResult"] = res
                    task["completeSuccess"] = True
                    return True

                err_code = res_data.get("errorCode") or res_result.get("errorCode") or res.get("errorCode")
                if err_code:
                    task["errorCode"] = err_code
                    if err_code == "ILLEGAL_COMPLETION":
                        task["illegalCompletion"] = True
                logger.warning(f"[{self.account_name}] ⚠️ _complete_task для {task_id} не підтверджено: {res.get('ret') or res}")
        except Exception as e:
            logger.warning(f"[{self.account_name}] ⚠️ Помилка у _complete_task: {e}")

        task["earnedCoins"] = 0
        self.last_complete_coins = 0
        return False

    def _claim_task_reward(self, task: Dict[str, Any]) -> int:
        """
        Забирає нагороду (монети) за виконане завдання через MTOP process.issue.
        Повертає кількість отриманих монет (>= 0) або -1 у разі помилки.
        """
        task_id = task.get("taskInstanceId") or task.get("taskId")
        if not task_id:
            return -1

        # Ніколи не викликаємо claim при ILLEGAL_COMPLETION
        if task.get("illegalCompletion") or task.get("errorCode") == "ILLEGAL_COMPLETION":
            logger.warning(f"[{self.account_name}] 🚫 Пропуск _claim_task_reward для {task_id}: завдання має ILLEGAL_COMPLETION")
            return -1

        un_issue = task.get("unIssueRecord")
        detail_ids_str = json.dumps(un_issue) if isinstance(un_issue, (list, dict)) else str(un_issue or "")

        locale, currency, lang = _extract_user_locale_params(self.client)
        task_platform = task.get("platform") or getattr(self, "working_platform", TASK_PLATFORM)
        ua_val = f"defaultUA2_load_failed with timeout@@https://m.aliexpress.com/p/coin-index/index.html@@{int(time.time() * 1000)}"
        payload = {
            "ua": ua_val,
            "umidToken": TASK_UMID_TOKEN,
            "umidTokenType": "TOKEN",
            "taskInstanceId": task_id,
            "taskAppKey": task.get("taskAppKey", TASK_APP_KEY),
            "projectAppKey": task.get("projectAppKey", PROJECT_APP_KEY),
            "taskSceneId": task.get("taskSceneId", TASK_SCENE_ID),
            "sceneId": task.get("sceneId", TASK_SCENE_ID),
            "detailIds": "",
            "detailIdsString": detail_ids_str,
            "ttid": task.get("ttid", TASK_TTID),
            "platform": task_platform,
            "fromNew": True,
            "asac": ASAC_TASK_CAMPAIGN,
            "idemId": str(int(time.time() * 1000)),
            "locale": task.get("locale", locale),
            "_lang": task.get("_lang", lang),
            "_currency": task.get("_currency", currency),
            "lang": task.get("lang", task.get("_lang", lang)),
            "currency": task.get("currency", task.get("_currency", currency))
        }
        if task.get("groupId"):
            payload["groupId"] = task["groupId"]
        if task.get("interestCode"):
            payload["interestCode"] = task["interestCode"]

        try:
            res = self.checkin_mgr._call_mtop(TASK_PROCESS_ISSUE_API, data=payload)
            try:
                res_str = json.dumps(res, ensure_ascii=False)
            except Exception:
                res_str = str(res)
            logger.debug(f"[{self.account_name}] 📥 Відповідь MTOP _claim_task_reward ({task_id}): {res_str}")
            if res and isinstance(res, dict):
                res_data = res.get("data") if isinstance(res.get("data"), dict) else {}
                res_result = res_data.get("result") if isinstance(res_data.get("result"), dict) else {}
                has_error = bool(
                    res.get("errorCode")
                    or res_data.get("errorCode")
                    or res_result.get("errorCode")
                )
                if not has_error and _is_task_success(res):
                    coins = 0
                    for b in _collect_nested_dicts(res):
                        for k in ("coinCount", "awardCoins", "amount", "coins", "prizeAmount", "rewardCoinNum"):
                            val = _clean_int(b.get(k))
                            if val > 0:
                                coins = max(coins, val)
                    return coins
                logger.warning(f"[{self.account_name}] ⚠️ _claim_task_reward для {task_id} не підтверджено: {res.get('ret') or res}")
        except Exception as e:
            logger.error(f"[{self.account_name}] ❌ Виняток під час _claim_task_reward: {e}")

        return -1
