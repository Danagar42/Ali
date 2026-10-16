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

            # 3.2 Фарм монет за перегляд безпечних завдань (~15 сек)
            coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)
            coins_done = coins_mgr.farm_browse_tasks(count=4)

            # 3.3 Збір купонів
            coupon_mgr = CouponCollector(client)
            coupons_done = coupon_mgr.collect_available_coupons()

            # Облік зароблених монет (чекін + безпечні завдання)
            task_coins = getattr(coins_mgr, "task_coins_earned", 0)
            if not isinstance(task_coins, (int, float)):
                task_coins = 0
            total_earned_today = (checkin_mgr.coins_earned or 0) + int(task_coins)

            account_total_coins = checkin_mgr.total_coins
            if account_total_coins is not None and task_coins > 0:
                account_total_coins += int(task_coins)

            # Фіксація звіту по акаунту для сповіщень
            summary = AccountSummary(
                name=account_name,
                checkin_status=checkin_mgr.status,
                coins_earned=total_earned_today,
                streak_days=checkin_mgr.streak_days,
                total_coins=account_total_coins,
                tasks_done=coins_done,
                coupons_claimed=coupons_done,
                session_valid=checkin_mgr.session_valid
            )
            dispatcher.add_summary(summary)

            # Зараховуємо успіх тільки якщо підтверджено чекін (новий або вже виконаний)
            if checkin_ok and checkin_mgr.session_valid:
                success_count += 1
                logger.info(f"✅ [{account_name}] Чекін успішно підтверджено.")
            else:
                failed_count += 1
                if checkin_mgr.status == "CAPTCHA_REQUIRED":
                    logger.warning(f"⚠️ [{account_name}] Чекін заблоковано капчею WAF (потрібно пройти перевірку в браузері).")
                else:
                    logger.warning(f"⚠️ [{account_name}] Чекін не підтверджено (перевірте валідність cookies або наявність капчі).")
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
