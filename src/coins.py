import logging
import random
import time
import json
import re
from typing import List, Dict, Any, Optional, Set, Tuple
from src.client import AliClient
from src.checkin import CheckinManager, _collect_nested_dicts, _clean_int
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

# Список популярних категорій та товарів для симуляції перегляду
BROWSE_TARGETS = [
    "https://www.aliexpress.com/category/100003109/women-clothing.html",
    "https://www.aliexpress.com/category/100003070/men-clothing.html",
    "https://www.aliexpress.com/category/509/cellphones-telecommunications.html",
    "https://www.aliexpress.com/category/44/consumer-electronics.html",
    "https://www.aliexpress.com/category/7/computer-office.html",
    "https://www.aliexpress.com/category/1509/jewelry-accessories.html",
    "https://www.aliexpress.com/p/coin-pc-index/index.html",
]

# Legacy URL для зворотної сумісності з тестами
REPORT_TASK_URLS = [
    "https://coins.aliexpress.com/api/finishTask",
    "https://coins.aliexpress.com/api/task/finish"
]

# Небезпечні ключові слова — завдання з цими діями КАТЕГОРИЧНО ігноруються
UNSAFE_KEYWORDS = (
    "order", "buy", "pay", "cart", "purchase",
    "follow", "share", "deal_amount", "bind", "install",
    "покупка", "купити", "замовлення", "кошик", "підписка"
)
UNSAFE_REGEX = re.compile(r'(?i)\b(' + '|'.join(re.escape(w) for w in UNSAFE_KEYWORDS) + r')\b')

# Безпечні типи завдань (тільки пасивний перегляд / очікування 15 секунд)
SAFE_TASK_TYPES = {0, "0", "browse", "browse_item", "search", "view"}
SAFE_KEYWORDS = (
    "browse", "view", "explore", "visit", "find",
    "15s", "30s", "15 sec", "секунд", "перегляд", "пошук"
)


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
        self.tasks_skipped: int = 0
        self.tasks_failed: int = 0
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
                                    logger.info(f"[{self.account_name}]   [Завдання #{idx}.{m_idx}] materials: {m_str}")
                        else:
                            try:
                                it_str = json.dumps(it, ensure_ascii=False)
                            except Exception:
                                it_str = str(it)
                            logger.info(f"[{self.account_name}]   [Завдання #{idx}] data: {it_str}")

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
                                        else cfg.get("type", 0)
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

        # Перевірка на небезпечні ключові слова через word boundaries (\b)
        text_repr = " ".join([
            str(task.get("taskTitle", "")),
            str(task.get("taskType", "")),
            str(task.get("actionUrl", "")),
            str(task.get("actionType", ""))
        ])
        if UNSAFE_REGEX.search(text_repr):
            logger.debug(f"[{self.account_name}] 🚫 Пропущено небезпечне завдання: {task.get('taskTitle')}")
            return False, "SKIPPED", "Небезпечна дія (покупка/підписка/кошик)"

        # Відсіюємо щоденний чекін без URL
        task_title_lower = str(task.get("taskTitle", "")).lower()
        if "daily check" in task_title_lower and not task.get("actionUrl"):
            logger.debug(f"[{self.account_name}] ℹ️ Пропущено 'Daily check-in' без URL перегляду: {task.get('taskTitle')}")
            return False, "SKIPPED", "Щоденний чекін без URL для перегляду"

        # Якщо це готова нагорода для забирання монет
        if is_claim_only:
            return True, "CLAIM", "Готово до отримання нагороди"

        # Для завдань на перегляд обов'язкова наявність actionUrl
        if not task.get("actionUrl"):
            return False, "SKIPPED", "Відсутній URL для перегляду"

        # Справжні завдання перегляду: browseZeroFlag is True або raw_type == 0
        browse_zero = bool(task.get("browseZeroFlag") or cfg.get("browseZeroFlag"))
        if browse_zero or raw_type in (0, "0") or (raw_type is not None and str(raw_type).strip() == "0"):
            return True, "SAFE_BROWSE", "Безпечне завдання перегляду"

        task_type = task.get("taskType")
        if task_type in SAFE_TASK_TYPES or (task_type is not None and str(task_type).strip().lower() in SAFE_TASK_TYPES):
            return True, "SAFE_BROWSE", "Безпечне завдання перегляду"

        text_repr_lower = text_repr.lower()
        if any(safe_word in text_repr_lower for safe_word in SAFE_KEYWORDS):
            return True, "SAFE_BROWSE", "Безпечне завдання перегляду"

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
        self.tasks_skipped = 0
        self.tasks_failed = 0
        self.task_records = []

        # 1. Якщо доступний MTOP шлюз, виконуємо реальні безпечні завдання
        if self.checkin_mgr is not None:
            if getattr(self.checkin_mgr, "status", None) == "CAPTCHA_REQUIRED" or getattr(self.checkin_mgr, "mobile_status", None) == "CAPTCHA_REQUIRED":
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
                    if self.completed_tasks >= count:
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

                    target_url = task.get("actionUrl")
                    req_seconds = _clean_int(task.get("staySeconds", 15))
                    if req_seconds <= 0:
                        req_seconds = 15
                    stay_seconds = max(1, min(req_seconds, 30))

                    for run_idx in range(1, target_runs + 1):
                        if self.completed_tasks >= count:
                            logger.info(f"[{self.account_name}] Досягнуто ліміт завдань на сесію ({count}).")
                            break

                        run_label = f" ({run_idx}/{target_runs})" if target_runs > 1 else ""
                        run_task_id = f"{task_id}_{run_idx}" if target_runs > 1 else task_id

                        if target_url:
                            logger.info(f"[{self.account_name}] ⏳ [{idx}/{len(safe_tasks)}]{run_label} [Безпечне завдання] '{title}': перегляд сторінки ({stay_seconds} сек) -> {target_url}...")
                            try:
                                self.client.get(target_url)
                            except Exception as e:
                                logger.debug(f"[{self.account_name}] Помилка відкриття URL завдання: {e}")
                        else:
                            logger.info(f"[{self.account_name}] ⏳ [{idx}/{len(safe_tasks)}]{run_label} [Безпечне завдання] '{title}': пасивне очікування ({stay_seconds} сек)...")

                        # Реальна пауза для зарахування перегляду на сервері AliExpress
                        self.client.sleep_random(stay_seconds + 0.2, stay_seconds + 1.5)

                        # Фіксація завершення завдання
                        complete_ok = self._complete_task(task)
                        earned = task.get("earnedCoins", 0)

                        # Клейм монет викликаємо лише якщо complete_ok успішний і завдання потребує ручного клейму
                        # (issueHandle == 1 або unIssueRecord).
                        # При ILLEGAL_COMPLETION або неуспішному complete_ok клейм НІКОЛИ не викликається!
                        coins_claimed = -1
                        needs_manual_claim = bool(task.get("unIssueRecord")) or _clean_int(task.get("issueHandle", 0)) == 1
                        if complete_ok and earned <= 0 and needs_manual_claim and not task.get("illegalCompletion"):
                            coins_claimed = self._claim_task_reward(task)
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

                self.status = "SUCCESS"
                return self.completed_tasks
            else:
                logger.info(f"[{self.account_name}] ℹ️ Немає доступних безпечних завдань для виконання.")
                self.status = "SUCCESS"
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

        # 2. Якщо checkin_mgr відсутній (legacy fallback для ізольованих тестів)
        if all(u in self._dead_urls for u in REPORT_TASK_URLS):
            logger.debug(f"[{self.account_name}] Усі ендпоінти завершення завдань неактивні, пропускаємо перегляд сторінок.")
            return 0

        logger.info(f"[{self.account_name}] 🛍️ Фонова симуляція перегляду каталогу (к-ть: {count})...")
        active_tasks = self.fetch_active_tasks()
        browse_task_ids = [
            t.get("taskId") for t in active_tasks
            if isinstance(t, dict) and t.get("taskId") and "browse" in str(t.get("taskType", "")).lower() and not t.get("completed", False)
        ]

        targets = random.sample(BROWSE_TARGETS, min(count, len(BROWSE_TARGETS)))
        completed_or_acknowledged = 0

        for idx, url in enumerate(targets, 1):
            if all(u in self._dead_urls for u in REPORT_TASK_URLS):
                logger.debug(f"[{self.account_name}] Усі ендпоінти завершення завдань стали неактивними, зупиняємо перегляд сторінок.")
                break

            logger.info(f"[{self.account_name}] [{idx}/{len(targets)}] Перегляд: {url}")
            resp = self.client.get(url)
            if resp and resp.status_code == 200:
                logger.debug(f"[{self.account_name}] Сторінку успішно відвідано.")
            else:
                status = resp.status_code if resp else "Error"
                logger.warning(f"[{self.account_name}] Не вдалося завантажити {url} (статус: {status})")

            current_task_id = browse_task_ids.pop(0) if browse_task_ids else f"BROWSE_{idx}"
            reported = self._report_task_complete(
                task_type="BROWSE_ITEM",
                task_id=current_task_id,
                scene_id="browse_channel_coin"
            )
            if reported:
                completed_or_acknowledged += 1
                self.tasks_completed += 1
                self.task_records.append({
                    "task_id": current_task_id,
                    "title": f"Перегляд: {url}",
                    "status": "COMPLETED",
                    "coins": 0,
                    "reason": "Legacy симуляція зарахована"
                })
            else:
                self.tasks_failed += 1
                self.task_records.append({
                    "task_id": current_task_id,
                    "title": f"Перегляд: {url}",
                    "status": "FAILED",
                    "coins": 0,
                    "reason": "Legacy ендпоінт відхилив"
                })

        self.completed_tasks = completed_or_acknowledged
        if completed_or_acknowledged > 0:
            self.status = "SUCCESS"
        return completed_or_acknowledged

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
            logger.info(f"[{self.account_name}] 📥 Відповідь MTOP _complete_task ({task_id}): {res_str}")
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
            logger.info(f"[{self.account_name}] 📥 Відповідь MTOP _claim_task_reward ({task_id}): {res_str}")
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

    def _report_task_complete(
        self,
        task_type: str = "BROWSE_ITEM",
        task_id: Optional[str] = None,
        scene_id: Optional[str] = "browse_channel_coin"
    ) -> bool:
        """Legacy метод репорту для зворотної сумісності."""
        report_urls = REPORT_TASK_URLS
        if all(u in self._dead_urls for u in report_urls):
            return False

        payload: Dict[str, Any] = {
            "taskType": task_type,
            "channel": "COIN"
        }
        if task_id:
            payload["taskId"] = task_id
        if scene_id:
            payload["sceneId"] = scene_id

        for url in report_urls:
            if url in self._dead_urls:
                continue
            try:
                resp = self.client.post(url, json=payload)
                if not resp or resp.status_code != 200:
                    if resp is None or resp.status_code in (404, 405, 410, 500, 502, 503, 504):
                        self._dead_urls.add(url)
                    continue

                raw_ct = ""
                if hasattr(resp, "headers") and hasattr(resp.headers, "get"):
                    ct_val = resp.headers.get("content-type", "")
                    if isinstance(ct_val, str):
                        raw_ct = ct_val.lower()

                if raw_ct and "json" not in raw_ct:
                    self._dead_urls.add(url)
                    continue

                try:
                    data = resp.json()
                    if not isinstance(data, dict):
                        continue
                    if data.get("hasHandle") is False or "can't fund" in str(data.get("errorMessage", "")).lower():
                        continue

                    if _is_task_already_done(data) or _is_task_success(data):
                        return True
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"[{self.account_name}] Помилка legacy репорту таски: {e}")

        return False
