import os
import sys
import time
import random
import logging
from src.config import SKIP_RANDOM_SLEEP, MAX_START_SLEEP_SECONDS
from src.auth import load_accounts
from src.client import AliClient
from src.checkin import CheckinManager
from src.coins import CoinsFarmManager
from src.coupons import CouponCollector

# Налаштування виводу логів з підтримкою UTF-8
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

logger = logging.getLogger("AliExpressFarmer")
logger.setLevel(logging.INFO)
logger.addHandler(handler)


def initial_random_jitter():
    """Випадковий зсув запуску (0..30 хв) для запобігання детекту однакового часу."""
    if SKIP_RANDOM_SLEEP:
        logger.info("⚡ SKIP_RANDOM_SLEEP активовано — пропускаємо випадковий зсув запуску.")
        return

    delay = random.randint(30, MAX_START_SLEEP_SECONDS)
    logger.info(f"⏳ Анти-детект: випадковий зсув запуску на {delay} секунд ({delay / 60:.1f} хв)...")
    time.sleep(delay)

def main():
    logger.info("🚀 Запуск AliExpress Coin & Coupon Farmer")

    # 1. Початковий зсув часу
    initial_random_jitter()

    # 2. Завантаження акаунтів
    accounts = load_accounts()
    if not accounts:
        logger.error("❌ Не знайдено жодного акаунту! Додайте ALI_COOKIES або ALI_COOKIES_1, ALI_COOKIES_2 у змінні оточення.")
        sys.exit(1)

    logger.info(f"📋 Знайдено акаунтів для обробки: {len(accounts)}")

    # 3. Обробка кожного акаунту
    for idx, (account_name, cookies) in enumerate(accounts, 1):
        logger.info(f"\n==========================================")
        logger.info(f"👤 [{idx}/{len(accounts)}] Обробка {account_name}")
        logger.info(f"==========================================")

        client = AliClient(cookies=cookies, account_name=account_name)

        # 3.1 Щоденний чекін
        checkin_mgr = CheckinManager(client)
        checkin_mgr.run_checkin()

        # 3.2 Фарм монет за перегляд
        coins_mgr = CoinsFarmManager(client)
        coins_mgr.farm_browse_tasks(count=4)

        # 3.3 Збір купонів
        coupon_mgr = CouponCollector(client)
        coupon_mgr.collect_available_coupons()

        # Пауза між акаунтами, якщо їх декілька
        if idx < len(accounts):
            account_delay = random.uniform(10, 25)
            logger.info(f"⏸️ Пауза перед наступним акаунтом: {account_delay:.1f} сек...\n")
            time.sleep(account_delay)

    logger.info("\n🎉 Усі завдання успішно завершено!")

if __name__ == "__main__":
    main()
