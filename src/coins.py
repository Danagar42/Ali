import logging
import random
from typing import List, Dict, Any, Optional
from src.client import AliClient

logger = logging.getLogger(__name__)

# Список популярних категорій та товарів для симуляції перегляду
BROWSE_TARGETS = [
    "https://www.aliexpress.com/category/100003109/women-clothing.html",
    "https://www.aliexpress.com/category/100003070/men-clothing.html",
    "https://www.aliexpress.com/category/509/cellphones-telecommunications.html",
    "https://www.aliexpress.com/category/44/consumer-electronics.html",
    "https://www.aliexpress.com/category/7/computer-office.html",
    "https://www.aliexpress.com/category/1509/jewelry-accessories.html",
    "https://coins.aliexpress.com/",
]

def _is_task_already_done(data: Any) -> bool:
    if not data:
        return False
    text = str(data).lower()
    return any(w in text for w in ["already", "limit", "finish", "repeat", "has finished", "already_done"])


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
    if data.get("success") is True:
        return True
    if isinstance(data_block, dict) and data_block.get("success") is True:
        return True
    if isinstance(result_block, dict) and result_block.get("success") is True:
        return True
    ret = data.get("ret")
    if isinstance(ret, list) and len(ret) > 0 and isinstance(ret[0], str):
        return ret[0].startswith("SUCCESS::") or ret[0] == "SUCCESS"
    return False


class CoinsFarmManager:
    """
    Керує фармом монет: перегляд товарів, виконання активностей росту,
    отримання завдань та відправка підтверджень із валідними ідентифікаторами.
    """
    def __init__(self, client: AliClient):
        self.client = client
        self.account_name = client.account_name

    def fetch_active_tasks(self) -> List[Dict[str, Any]]:
        """
        Запитує список доступних завдань росту користувача.
        """
        query_url = "https://www.aliexpress.com/fn/user-grow-task/queryTask"
        try:
            resp = self.client.get(query_url, params={"channel": "COIN"})
            if resp and resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    task_list = (
                        data.get("data", {}).get("taskList")
                        or data.get("result", {}).get("taskList")
                        or []
                    )
                    if isinstance(task_list, list) and task_list:
                        logger.info(f"[{self.account_name}] Знайдено активних завдань: {len(task_list)}")
                        return task_list
        except Exception as e:
            logger.debug(f"[{self.account_name}] Не вдалося отримати список завдань: {e}")
        return []

    def farm_browse_tasks(self, count: int = 4) -> int:
        """
        Імітує перегляд сторінок товарів/категорій для отримання бонусів перегляду.
        Повертає кількість успішно завершених або раніше виконаних завдань.
        """
        logger.info(f"[{self.account_name}] 🛍️ Початок виконання завдань з перегляду (к-ть: {count})...")
        active_tasks = self.fetch_active_tasks()
        browse_task_ids = [
            t.get("taskId") for t in active_tasks
            if isinstance(t, dict) and t.get("taskId") and "browse" in str(t.get("taskType", "")).lower() and not t.get("completed", False)
        ]

        targets = random.sample(BROWSE_TARGETS, min(count, len(BROWSE_TARGETS)))
        completed_or_acknowledged = 0

        for idx, url in enumerate(targets, 1):
            logger.info(f"[{self.account_name}] [{idx}/{len(targets)}] Перегляд: {url}")
            resp = self.client.get(url)
            if resp and resp.status_code == 200:
                logger.debug(f"[{self.account_name}] Сторінку успішно відвідано.")
            else:
                status = resp.status_code if resp else "Error"
                logger.warning(f"[{self.account_name}] Не вдалося завантажити {url} (статус: {status})")

            # Визначаємо taskId, якщо такий є зі списку завдань
            current_task_id = browse_task_ids.pop(0) if browse_task_ids else f"BROWSE_{idx}"
            if self._report_task_complete(
                task_type="BROWSE_ITEM",
                task_id=current_task_id,
                scene_id="browse_channel_coin"
            ):
                completed_or_acknowledged += 1

        return completed_or_acknowledged

    def _report_task_complete(
        self,
        task_type: str = "BROWSE_ITEM",
        task_id: Optional[str] = None,
        scene_id: Optional[str] = "browse_channel_coin"
    ) -> bool:
        """
        Відправляє подію виконання завдання на бекенд AliExpress із детальним логуванням відповіді.
        """
        report_urls = [
            "https://www.aliexpress.com/fn/user-grow-task/finishTask",
            "https://coins.aliexpress.com/api/finishTask"
        ]

        payload: Dict[str, Any] = {
            "taskType": task_type,
            "channel": "COIN"
        }
        if task_id:
            payload["taskId"] = task_id
        if scene_id:
            payload["sceneId"] = scene_id

        for url in report_urls:
            try:
                resp = self.client.post(url, json=payload)
                if not resp or resp.status_code != 200:
                    continue

                try:
                    data = resp.json()
                    ret_str = str(data.get("ret", [])) if isinstance(data, dict) else ""
                    if _is_task_already_done(data):
                        logger.info(f"[{self.account_name}] ℹ️ Завдання '{task_type}' вже виконано або вичерпано ліміт.")
                        return True
                    elif _is_task_success(data):
                        logger.info(f"[{self.account_name}] ✅ Завдання '{task_type}' зараховано!")
                        return True
                    else:
                        msg = data.get("msg") or data.get("message") or ret_str if isinstance(data, dict) else str(data)
                        logger.info(f"[{self.account_name}] ℹ️ Статус завдання '{task_type}': {msg}")
                        return False
                except Exception:
                    logger.debug(f"[{self.account_name}] Відповідь finishTask не JSON: {resp.text[:120]}")
            except Exception as e:
                logger.debug(f"[{self.account_name}] Помилка репорту таски через {url}: {e}")

        return False
