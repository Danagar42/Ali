import logging
import random
import time
import json
from typing import List, Dict, Any, Optional, Set
from src.client import AliClient
from src.checkin import CheckinManager, _collect_nested_dicts, _clean_int
from src.config import (
    TASK_SCENE_ID,
    TASK_APP_KEY,
    ASAC_TASK_CAMPAIGN,
    TASK_DELIVERY_QUERY_API,
    TASK_PROCESS_COMPLETE_API,
    TASK_PROCESS_ISSUE_API,
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

# Безпечні типи завдань (тільки пасивний перегляд / очікування 15 секунд)
SAFE_TASK_TYPES = {0, "0", "browse", "browse_item", "search", "view"}
SAFE_KEYWORDS = (
    "browse", "view", "explore", "visit", "check", "find",
    "15s", "30s", "15 sec", "секунд", "перегляд", "пошук", "coins"
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
    if data.get("success") is False:
        return False
    data_block = data.get("data")
    if isinstance(data_block, dict) and data_block.get("success") is False:
        return False
    result_block = data.get("result")
    if isinstance(result_block, dict) and result_block.get("success") is False:
        return False
    if data.get("success") is True or data.get("isSuccess") is True:
        return True
    if isinstance(data_block, dict) and (data_block.get("success") is True or data_block.get("isSuccess") is True):
        return True
    if isinstance(result_block, dict) and (result_block.get("success") is True or result_block.get("isSuccess") is True):
        return True
    ret = data.get("ret")
    if isinstance(ret, list) and len(ret) > 0 and isinstance(ret[0], str):
        return ret[0].startswith("SUCCESS::") or ret[0] == "SUCCESS"
    return False


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
                mtop_res = self.checkin_mgr._call_mtop(
                    TASK_DELIVERY_QUERY_API,
                    data={
                        "taskSceneId": TASK_SCENE_ID,
                        "taskAppKey": TASK_APP_KEY,
                        "asac": ASAC_TASK_CAMPAIGN,
                        "fromNew": True,
                        "_lang": "en_KR",
                        "_currency": "USD"
                    }
                )
                if mtop_res and isinstance(mtop_res, dict):
                    raw_items = []
                    data_field = mtop_res.get("data")
                    if isinstance(data_field, dict):
                        result_field = data_field.get("result")
                        if isinstance(result_field, list):
                            raw_items = result_field
                        elif isinstance(data_field.get("taskList"), list):
                            raw_items = data_field["taskList"]
                    elif isinstance(mtop_res.get("result"), list):
                        raw_items = mtop_res["result"]

                    if raw_items:
                        normalized = []
                        for it in raw_items:
                            if not isinstance(it, dict):
                                continue
                            materials = it.get("materials") or []
                            mat = materials[0] if (isinstance(materials, list) and materials and isinstance(materials[0], dict)) else {}

                            task_id = it.get("taskInstanceId") or it.get("taskId") or it.get("id") or mat.get("taskInstanceId")
                            status = mat.get("newTaskStatus") or it.get("status") or ("completed" if it.get("completed") else "todo")
                            task_type = mat.get("taskType") if mat.get("taskType") is not None else it.get("taskType", 0)
                            title = mat.get("taskTitle") or mat.get("title") or it.get("taskTitle") or it.get("title") or f"Task_{task_id}"
                            action_url = mat.get("actionUrl") or mat.get("taskAction") or it.get("actionUrl") or BROWSE_TARGETS[0]
                            un_issue = mat.get("unIssueRecord") or it.get("unIssueRecord")

                            normalized.append({
                                "taskId": task_id,
                                "taskInstanceId": task_id,
                                "taskAppKey": it.get("taskAppKey") or TASK_APP_KEY,
                                "taskSceneId": it.get("taskSceneId") or TASK_SCENE_ID,
                                "taskType": task_type,
                                "taskTitle": title,
                                "status": str(status).lower(),
                                "unIssueRecord": un_issue,
                                "staySeconds": _clean_int(mat.get("staySeconds")) or 15,
                                "actionUrl": action_url,
                                "raw": it
                            })

                        if normalized:
                            logger.info(f"[{self.account_name}] 📋 MTOP знайдено завдань платформи: {len(normalized)}")
                            return normalized
            except Exception as e:
                logger.debug(f"[{self.account_name}] MTOP task delivery query: {e}")

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
                content_type = resp.headers.get("content-type", "").lower()
                if "json" not in content_type:
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

    def is_safe_task(self, task: Dict[str, Any]) -> bool:
        """
        Жорсткий фільтр безпеки: повертає True ТІЛЬКИ для повністю безпечних пасивних завдань
        (перегляд сторінок/товарів ~15 секунд або забирання вже готової нагороди).
        Всі покупки, додавання в кошик, підписки чи зовнішні шери відхиляються.
        """
        if not isinstance(task, dict):
            return False

        status = str(task.get("status", "")).lower()
        if status in ("completed", "finished", "done"):
            return False

        # Об'єднуємо всі текстові поля для перевірки на заборонені дії
        text_repr = " ".join([
            str(task.get("taskTitle", "")),
            str(task.get("taskType", "")),
            str(task.get("actionUrl", "")),
            str(task.get("actionType", ""))
        ]).lower()

        # Якщо є хоча б одне ризиковане ключове слово — завдання НЕ виконується
        if any(bad_word in text_repr for bad_word in UNSAFE_KEYWORDS):
            logger.debug(f"[{self.account_name}] 🚫 Пропущено небезпечне завдання: {task.get('taskTitle')}")
            return False

        # Якщо завдання вже виконане і очікує лише клейму (waitForReceive) — безпечно забрати монети!
        if status in ("waitforreceive", "wait_for_receive") or bool(task.get("unIssueRecord")):
            return True

        # Перевірка на безпечний тип завдання перегляду
        raw_type = task.get("taskType")
        if raw_type in SAFE_TASK_TYPES or str(raw_type).lower() in SAFE_TASK_TYPES:
            return True

        # Перевірка за безпечними ключовими словами
        if any(safe_word in text_repr for safe_word in SAFE_KEYWORDS):
            return True

        return False

    def farm_browse_tasks(self, count: int = 4) -> int:
        """
        Виконує безпечні завдання із затримкою ~15 секунд та нарахуванням монет.
        Якщо активних завдань платформи немає, виконує безпечний фоновий перегляд сторінок.
        """
        self.completed_tasks = 0
        self.task_coins_earned = 0

        # 1. Якщо доступний MTOP шлюз, виконуємо реальні безпечні завдання
        if self.checkin_mgr is not None:
            logger.info(f"[{self.account_name}] 🛍️ Початок виконання завдань (безпечний перегляд ~15 сек, ліміт: {count})...")
            active_tasks = self.fetch_active_tasks()
            safe_tasks = [t for t in active_tasks if self.is_safe_task(t)]

            if safe_tasks:
                safe_tasks.sort(key=lambda t: 0 if (t.get("status") in ("waitforreceive", "wait_for_receive") or t.get("unIssueRecord")) else 1)
                tasks_to_run = safe_tasks[:count]

                for idx, task in enumerate(tasks_to_run, 1):
                    title = task.get("taskTitle", f"Завдання #{idx}")
                    status = task.get("status", "")
                    is_claim_only = status in ("waitforreceive", "wait_for_receive") or bool(task.get("unIssueRecord"))

                    if is_claim_only:
                        logger.info(f"[{self.account_name}] 🎁 [{idx}/{len(tasks_to_run)}] Забирання нагороди за виконане завдання: '{title}'...")
                        coins = self._claim_task_reward(task)
                        if coins >= 0:
                            self.completed_tasks += 1
                            self.task_coins_earned += coins
                        continue

                    # Виконання завдання з очікуванням 15-18 секунд
                    target_url = task.get("actionUrl") or random.choice(BROWSE_TARGETS)
                    stay_seconds = max(15, min(task.get("staySeconds", 15), 30))
                    logger.info(f"[{self.account_name}] ⏳ [{idx}/{len(tasks_to_run)}] [Безпечне завдання] '{title}': перегляд сторінки ({stay_seconds} сек)...")

                    try:
                        self.client.get(target_url)
                    except Exception as e:
                        logger.debug(f"[{self.account_name}] Помилка відкриття URL завдання: {e}")

                    # Реальна пауза 15-18 секунд для зарахування перегляду на сервері AliExpress
                    self.client.sleep_random(stay_seconds + 0.2, stay_seconds + 2.5)

                    # Фіксація завершення завдання
                    complete_ok = self._complete_task(task)

                    # Забирання нагороди (клейм монет)
                    coins = self._claim_task_reward(task)
                    if complete_ok or coins >= 0:
                        self.completed_tasks += 1
                        if coins > 0:
                            self.task_coins_earned += coins
                        logger.info(f"[{self.account_name}] ✅ Завдання '{title}' успішно зараховано! (+{coins if coins > 0 else 0} 🪙)")

                return self.completed_tasks

        # 2. Якщо всі ендпоінти репорту вже відомі як мертві — пропускаємо перегляд без запитів
        if all(u in self._dead_urls for u in REPORT_TASK_URLS):
            logger.debug(f"[{self.account_name}] Усі ендпоінти завершення завдань неактивні, пропускаємо перегляд сторінок.")
            return 0

        # 3. Fallback: безпечний фоновий перегляд каталогу
        logger.info(f"[{self.account_name}] 🛍️ Фонова симуляція перегляду каталогу (к-ть: {count})...")
        active_tasks = self.fetch_active_tasks() if not self.checkin_mgr else []
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

        self.completed_tasks = completed_or_acknowledged
        return completed_or_acknowledged

    def _complete_task(self, task: Dict[str, Any]) -> bool:
        """
        Відправляє запит завершення безпечного завдання до MTOP шлюзу.
        """
        task_id = task.get("taskInstanceId") or task.get("taskId")
        if not task_id:
            return False

        payload = {
            "taskInstanceId": task_id,
            "taskAppKey": task.get("taskAppKey", TASK_APP_KEY),
            "taskSceneId": task.get("taskSceneId", TASK_SCENE_ID),
            "fromNew": True,
            "asac": ASAC_TASK_CAMPAIGN,
            "idemId": str(int(time.time() * 1000)),
            "_lang": "en_KR",
            "_currency": "USD"
        }

        try:
            res = self.checkin_mgr._call_mtop(TASK_PROCESS_COMPLETE_API, data=payload)
            if res and isinstance(res, dict):
                return _is_task_success(res)
        except Exception as e:
            logger.debug(f"[{self.account_name}] Помилка _complete_task: {e}")

        return False

    def _claim_task_reward(self, task: Dict[str, Any]) -> int:
        """
        Забирає нагороду (монети) за виконане завдання через MTOP process.issue.
        Повертає кількість отриманих монет (>= 0) або -1 у разі помилки.
        """
        task_id = task.get("taskInstanceId") or task.get("taskId")
        if not task_id:
            return -1

        un_issue = task.get("unIssueRecord")
        detail_ids_str = json.dumps(un_issue) if isinstance(un_issue, (list, dict)) else str(un_issue or "")

        payload = {
            "taskInstanceId": task_id,
            "detailIds": "",
            "detailIdsString": detail_ids_str,
            "fromNew": True,
            "asac": ASAC_TASK_CAMPAIGN,
            "idemId": str(int(time.time() * 1000)),
            "_lang": "en_KR",
            "_currency": "USD"
        }

        try:
            res = self.checkin_mgr._call_mtop(TASK_PROCESS_ISSUE_API, data=payload)
            if res and isinstance(res, dict):
                if _is_task_success(res):
                    coins = 0
                    for b in _collect_nested_dicts(res):
                        for k in ("coinCount", "awardCoins", "amount", "coins", "prizeAmount", "rewardCoinNum"):
                            val = _clean_int(b.get(k))
                            if val > 0:
                                coins = max(coins, val)
                    return coins
        except Exception as e:
            logger.debug(f"[{self.account_name}] Помилка _claim_task_reward: {e}")

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

                content_type = resp.headers.get("content-type", "").lower()
                if "json" not in content_type:
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
