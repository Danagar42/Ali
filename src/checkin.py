import logging
from typing import Dict, Any, Optional
from src.client import AliClient

logger = logging.getLogger(__name__)

class CheckinManager:
    """
    Керує щоденним чекіном монет AliExpress.
    """
    def __init__(self, client: AliClient):
        self.client = client
        self.account_name = client.account_name

    def run_checkin(self) -> bool:
        """
        Виконує спробу щоденного чекіну через відомі endpoints AliExpress.
        """
        logger.info(f"[{self.account_name}] 🔄 Початок щоденного чекіну...")

        # 1. Спроба відвідати головну сторінку монет (оновлює сесію та перевіряє доступність)
        coin_home_url = "https://coins.aliexpress.com/"
        home_resp = self.client.get(coin_home_url)
        if not home_resp or home_resp.status_code != 200:
            logger.warning(f"[{self.account_name}] Не вдалося завантажити сторінку монет (статус: {home_resp.status_code if home_resp else 'None'})")

        # 2. Виклик основного чекін API
        # AliExpress використовує ендпоінти росту користувача (growth/coins/checkin)
        checkin_endpoints = [
            {
                "url": "https://www.aliexpress.com/fn/user-grow-task/checkin",
                "method": "POST",
                "json": {"channel": "COIN"}
            },
            {
                "url": "https://coins.aliexpress.com/api/checkin",
                "method": "POST",
                "json": {}
            },
            {
                "url": "https://acs.aliexpress.com/h5/mtop.aliexpress.growth.coins.checkin/1.0/",
                "method": "GET",
                "params": {"api": "mtop.aliexpress.growth.coins.checkin"}
            }
        ]

        success = False
        for ep in checkin_endpoints:
            url = ep["url"]
            method = ep["method"]
            try:
                if method == "POST":
                    resp = self.client.post(url, json=ep.get("json", {}))
                else:
                    resp = self.client.get(url, params=ep.get("params"))

                if resp and resp.status_code == 200:
                    try:
                        res_data = resp.json()
                        logger.info(f"[{self.account_name}] Відповідь від {url}: {res_data}")
                        # Перевіряємо типові коди успіху
                        if res_data.get("success") is True or res_data.get("ret", [""])[0].startswith("SUCCESS"):
                            logger.info(f"[{self.account_name}] ✅ Чекін успішно виконано!")
                            success = True
                            break
                        elif "already" in str(res_data).lower():
                            logger.info(f"[{self.account_name}] ℹ️ Чекін на сьогодні вже був виконаний раніше.")
                            success = True
                            break
                    except Exception:
                        # Якщо відповідь не JSON
                        if "success" in resp.text.lower():
                            logger.info(f"[{self.account_name}] ✅ Чекін успішно зараховано (текстова відповідь)!")
                            success = True
                            break
            except Exception as ex:
                logger.debug(f"[{self.account_name}] Endpoint {url} повернув помилку: {ex}")

        if not success:
            logger.warning(f"[{self.account_name}] ⚠️ Чекін не підтверджено автоматично (можливо, вже зараховано або потрібне оновлення cookies).")

        return success
