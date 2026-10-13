import logging
import random
from typing import List
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

class CoinsFarmManager:
    """
    Керує фармом монет: перегляд товарів, виконання активностей росту.
    """
    def __init__(self, client: AliClient):
        self.client = client
        self.account_name = client.account_name

    def farm_browse_tasks(self, count: int = 5):
        """
        Імітує перегляд сторінок товарів/категорій для отримання бонусів перегляду.
        """
        logger.info(f"[{self.account_name}] 🛍️ Початок виконання завдань з перегляду товарів (к-ть: {count})...")
        targets = random.sample(BROWSE_TARGETS, min(count, len(BROWSE_TARGETS)))

        for idx, url in enumerate(targets, 1):
            logger.info(f"[{self.account_name}] [{idx}/{len(targets)}] Перегляд: {url}")
            resp = self.client.get(url)
            if resp and resp.status_code == 200:
                logger.debug(f"[{self.account_name}] Сторінку успішно відвідано.")
            else:
                status = resp.status_code if resp else 'Error'
                logger.warning(f"[{self.account_name}] Не вдалося завантажити {url} (статус: {status})")

            # Спроба відправити підтвердження виконання таски на зростання (grow-task)
            self._report_task_complete(task_type="BROWSE_ITEM")

    def _report_task_complete(self, task_type: str = "BROWSE_ITEM"):
        """
        Відправляє подію виконання завдання на бекенд AliExpress.
        """
        report_url = "https://www.aliexpress.com/fn/user-grow-task/finishTask"
        payload = {
            "taskType": task_type,
            "channel": "COIN"
        }
        try:
            resp = self.client.post(report_url, json=payload)
            if resp and resp.status_code == 200:
                logger.debug(f"[{self.account_name}] Task report response: {resp.text[:120]}")
        except Exception as e:
            logger.debug(f"[{self.account_name}] Помилка репорту таски: {e}")
