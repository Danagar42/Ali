import re
import logging
from typing import List, Dict, Any, Optional
from src.client import AliClient

logger = logging.getLogger(__name__)

COUPON_CENTER_URL = "https://sale.aliexpress.com/country@UA/coupon_center.htm"

def _is_coupon_already_claimed(data: Any) -> bool:
    if not data:
        return False
    text = str(data).lower()
    return any(w in text for w in ["already", "has claimed", "claimed", "limit", "already_claim"])


def _is_coupon_success(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if _is_coupon_already_claimed(data):
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


class CouponCollector:
    """
    Керує пошуком та збором доступних купонів платформи та магазинів.
    Знаходить актуальні activityId/couponId та надсилає валідні запити з обробкою відповідей.
    """
    def __init__(self, client: AliClient):
        self.client = client
        self.account_name = client.account_name

    def _extract_coupon_candidates(self, html_content: str) -> List[Dict[str, str]]:
        """
        Знаходить ідентифікатори купонів та акцій з HTML розмітки Coupon Center.
        """
        candidates: List[Dict[str, str]] = []
        if not html_content:
            return candidates

        # Пошук activityId (включаючи JSON та нелапковані JS-ключі)
        activity_matches = set(re.findall(r'["\']?activityId["\']?\s*[:=]\s*["\']([a-zA-Z0-9_\-]+)["\']', html_content, re.IGNORECASE))
        # Пошук couponId
        coupon_matches = set(re.findall(r'["\']?couponId["\']?\s*[:=]\s*["\']([a-zA-Z0-9_\-]+)["\']', html_content, re.IGNORECASE))

        for act_id in sorted(activity_matches):
            candidates.append({"activityId": act_id})
        for cpn_id in sorted(coupon_matches):
            candidates.append({"couponId": cpn_id})

        return candidates

    def collect_available_coupons(self):
        """
        Відвідує Coupon Center та відправляє запити на збір знайдених або промо-купонів.
        """
        logger.info(f"[{self.account_name}] 🎟️ Перевірка доступних купонів у Coupon Center...")
        resp = self.client.get(COUPON_CENTER_URL)
        candidates: List[Dict[str, str]] = []

        if resp and resp.status_code == 200:
            logger.info(f"[{self.account_name}] Успішно відвідано Coupon Center.")
            candidates = self._extract_coupon_candidates(resp.text)
            if candidates:
                logger.info(f"[{self.account_name}] Знайдено купонних кампаній у розмітці: {len(candidates)}")
        else:
            status = resp.status_code if resp else "None"
            logger.warning(f"[{self.account_name}] Coupon Center повернув статус: {status}")

        claim_endpoints = [
            "https://campaign.aliexpress.com/coupon/claim",
            "https://coupon.aliexpress.com/buyer/coupon/applyCoupon.htm"
        ]

        # Якщо специфічні кандидати не знайдені в HTML, використовуємо стандартні слоти промо-центру
        if not candidates:
            candidates = [
                {"activityId": "web_coupon_center_daily"},
                {"activityId": "platform_allowance"}
            ]

        claimed_count = 0
        for item in candidates[:5]:  # обмежуємо до 5 спроб, щоб не флудити
            act_id = item.get("activityId") or item.get("couponId", "")
            payload: Dict[str, Any] = {
                "channel": "web_coupon_center",
                "scene": "coupon_center",
            }
            if "activityId" in item:
                payload["activityId"] = item["activityId"]
            if "couponId" in item:
                payload["couponId"] = item["couponId"]

            for url in claim_endpoints:
                try:
                    res = self.client.post(url, json=payload)
                    if not res or res.status_code != 200:
                        continue

                    try:
                        data = res.json()
                        ret_str = str(data.get("ret", [])) if isinstance(data, dict) else ""
                        if _is_coupon_already_claimed(data):
                            logger.info(f"[{self.account_name}] ℹ️ Купон '{act_id}' уже отримано раніше або ліміт вичерпано.")
                            break
                        elif _is_coupon_success(data):
                            logger.info(f"[{self.account_name}] ✅ Купон '{act_id}' успішно отримано!")
                            claimed_count += 1
                            break
                        else:
                            msg = data.get("msg") or data.get("message") or ret_str if isinstance(data, dict) else str(data)
                            logger.debug(f"[{self.account_name}] Купон '{act_id}': {msg}")
                    except Exception:
                        logger.debug(f"[{self.account_name}] Відповідь купона {url} не JSON: {res.text[:80]}")
                except Exception as e:
                    logger.debug(f"[{self.account_name}] Помилка збору купона: {e}")

        return claimed_count
