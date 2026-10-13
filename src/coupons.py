import logging
from src.client import AliClient

logger = logging.getLogger(__name__)

COUPON_CENTER_URL = "https://sale.aliexpress.com/country@UA/coupon_center.htm"

class CouponCollector:
    """
    Керує пошуком та збором доступних купонів продавців та AliExpress.
    """
    def __init__(self, client: AliClient):
        self.client = client
        self.account_name = client.account_name

    def collect_available_coupons(self):
        """
        Відвідує Coupon Center та відправляє запити на збір активних купонів.
        """
        logger.info(f"[{self.account_name}] 🎟️ Перевірка доступних купонів у Coupon Center...")
        resp = self.client.get(COUPON_CENTER_URL)
        if resp and resp.status_code == 200:
            logger.info(f"[{self.account_name}] Успішно відвідано Coupon Center.")
        else:
            logger.warning(f"[{self.account_name}] Coupon Center повернув статус: {resp.status_code if resp else 'None'}")

        # Спроба забрати загальнодоступні купони платформи
        claim_endpoints = [
            "https://campaign.aliexpress.com/coupon/claim",
            "https://coupon.aliexpress.com/buyer/coupon/applyCoupon.htm"
        ]

        for url in claim_endpoints:
            try:
                res = self.client.post(url, json={"channel": "web_coupon_center"})
                if res and res.status_code == 200:
                    logger.debug(f"[{self.account_name}] Відповідь збору купонів: {res.text[:100]}")
            except Exception as e:
                logger.debug(f"[{self.account_name}] Помилка збору купона: {e}")
