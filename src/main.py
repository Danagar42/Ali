import os
import sys
import time
import random
import logging

def setup_logging():
    """Забезпечує UTF-8 кодування та налаштовує логування на рівні INFO для всіх модулів."""
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger("AliExpressFarmer")

from src.config import SKIP_RANDOM_SLEEP, MAX_START_SLEEP_SECONDS
from src.auth import load_accounts
from src.client import AliClient
from src.checkin import CheckinManager
from src.coins import CoinsFarmManager
from src.coupons import CouponCollector
from src.notifications import NotificationDispatcher, AccountSummary


def initial_random_jitter():
    """Випадковий зсув запуску для запобігання детекту однакового часу."""
    if SKIP_RANDOM_SLEEP:
        logger.info("⚡ SKIP_RANDOM_SLEEP активовано — пропускаємо випадковий зсув запуску.")
        return

    if MAX_START_SLEEP_SECONDS <= 0:
        return

    min_sleep = min(10, MAX_START_SLEEP_SECONDS)
    delay = random.randint(min_sleep, MAX_START_SLEEP_SECONDS)
    logger.info(f"⏳ Анти-детект: випадковий зсув запуску на {delay} секунд ({delay / 60:.1f} хв)...")
    time.sleep(delay)


def main():
    setup_logging()
    logger.info("🚀 Запуск AliExpress Coin & Coupon Farmer")

    # 1. Початковий зсув часу
    initial_random_jitter()

    # 2. Завантаження акаунтів
    accounts = load_accounts()
    if not accounts:
        logger.error("❌ Не знайдено жодного валідного акаунту! Додайте ALI_COOKIES або ALI_COOKIES_1..N у змінні оточення.")
        sys.exit(1)

    logger.info(f"📋 Знайдено акаунтів для обробки: {len(accounts)}")

    dispatcher = NotificationDispatcher()
    success_count = 0
    failed_count = 0

    # 3. Обробка кожного акаунту з ізоляцією помилок
    for idx, account_item in enumerate(accounts, 1):
        account_name, cookies = account_item
        proxy = getattr(account_item, "proxy", None)

        logger.info(f"\n==========================================")
        logger.info(f"👤 [{idx}/{len(accounts)}] Обробка {account_name}")
        logger.info(f"==========================================")

        try:
            client = AliClient(cookies=cookies, account_name=account_name, proxy=proxy)

            # 3.1 Щоденний чекін (з підтримкою self-healing MTOP)
            checkin_mgr = CheckinManager(client)
            checkin_ok = checkin_mgr.run_checkin()

            # 3.2 Фарм монет за перегляд
            coins_mgr = CoinsFarmManager(client)
            coins_done = coins_mgr.farm_browse_tasks(count=4)

            # 3.3 Збір купонів
            coupon_mgr = CouponCollector(client)
            coupons_done = coupon_mgr.collect_available_coupons()

            # Фіксація звіту по акаунту для сповіщень
            summary = AccountSummary(
                name=account_name,
                checkin_status=checkin_mgr.status,
                coins_earned=checkin_mgr.coins_earned,
                streak_days=checkin_mgr.streak_days,
                total_coins=checkin_mgr.total_coins,
                tasks_done=coins_done,
                coupons_claimed=coupons_done,
                session_valid=checkin_mgr.session_valid
            )
            dispatcher.add_summary(summary)

            # Якщо чекін зараховано/вже був виконаний або зараховані таски перегляду
            if (checkin_ok or coins_done > 0) and checkin_mgr.session_valid:
                success_count += 1
                logger.info(f"✅ [{account_name}] Обробку успішно завершено.")
            else:
                failed_count += 1
                logger.warning(f"⚠️ [{account_name}] Обробку завершено, але жодної активності не підтверджено (перевірте валідність cookies).")
        except Exception as e:
            failed_count += 1
            dispatcher.add_summary(AccountSummary(
                name=account_name,
                checkin_status="FAILED",
                session_valid=False,
                error_message=str(e)
            ))
            logger.error(f"❌ [{account_name}] Критична помилка при обробці: {e}", exc_info=True)

        # Пауза між акаунтами, якщо їх декілька
        if idx < len(accounts):
            account_delay = random.uniform(10, 25)
            logger.info(f"⏸️ Пауза перед наступним акаунтом: {account_delay:.1f} сек...\n")
            time.sleep(account_delay)

    logger.info(f"\n📊 Підсумок обробки: успішно {success_count}, з помилками {failed_count} (всього {len(accounts)})")

    # 4. Відправка сповіщень у GitHub Actions Step Summary та Telegram
    dispatcher.dispatch()

    # 5. Якщо всі акаунти зазнали збою, повертаємо код помилки для алерту в CI
    if failed_count > 0 and success_count == 0:
        logger.error("❌ Усі акаунти завершилися з помилками!")
        sys.exit(1)

    logger.info("🎉 Виконання завдань завершено!")


if __name__ == "__main__":
    main()
