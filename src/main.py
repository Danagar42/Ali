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
            coupon_mgr = CouponCollector(client, checkin_mgr=checkin_mgr)
            coupons_done = coupon_mgr.collect_available_coupons()

            # Облік зароблених монет (чекін + безпечні завдання)
            task_coins = getattr(coins_mgr, "task_coins_earned", 0)
            if not isinstance(task_coins, (int, float)):
                task_coins = 0
            earned_from_checkin = checkin_mgr.coins_earned if isinstance(checkin_mgr.coins_earned, (int, float)) else 0
            total_earned_today = earned_from_checkin + int(task_coins)

            checkin_total = checkin_mgr.total_coins if isinstance(checkin_mgr.total_coins, (int, float)) else None
            account_total_coins = checkin_total
            if account_total_coins is not None and task_coins > 0:
                account_total_coins += int(task_coins)

            # Розрахунок початкового балансу до чекіну та завдань
            init_c = getattr(checkin_mgr, "initial_coins", None)
            initial_balance = int(init_c) if isinstance(init_c, (int, float)) else None
            if initial_balance is None and account_total_coins is not None:
                initial_balance = max(0, account_total_coins - total_earned_today)

            pc_coins_earned = int(getattr(checkin_mgr, "pc_coins", 0)) if isinstance(getattr(checkin_mgr, "pc_coins", 0), (int, float)) else 0
            mobile_coins_earned = int(getattr(checkin_mgr, "mobile_coins", 0)) if isinstance(getattr(checkin_mgr, "mobile_coins", 0), (int, float)) else 0
            pc_status = getattr(checkin_mgr, "pc_status", None)
            if not isinstance(pc_status, str):
                pc_status = None
            mobile_status = getattr(checkin_mgr, "mobile_status", None)
            if not isinstance(mobile_status, str):
                mobile_status = None
            tasks_skipped = int(getattr(coins_mgr, "tasks_skipped", 0)) if isinstance(getattr(coins_mgr, "tasks_skipped", 0), (int, float)) else 0
            tasks_failed = int(getattr(coins_mgr, "tasks_failed", 0)) if isinstance(getattr(coins_mgr, "tasks_failed", 0), (int, float)) else 0
            task_records = getattr(coins_mgr, "task_records", [])
            if not isinstance(task_records, list):
                task_records = []
            coupon_records = getattr(coupon_mgr, "coupon_records", [])
            if not isinstance(coupon_records, list):
                coupon_records = []

            # Фіксація звіту по акаунту для сповіщень
            summary = AccountSummary(
                name=account_name,
                checkin_status=checkin_mgr.status,
                coins_earned=total_earned_today,
                streak_days=checkin_mgr.streak_days,
                total_coins=account_total_coins,
                tasks_done=coins_done,
                coupons_claimed=coupons_done,
                session_valid=checkin_mgr.session_valid,
                initial_coins=initial_balance,
                pc_status=pc_status,
                pc_coins=pc_coins_earned,
                pc_streak=int(getattr(checkin_mgr, "pc_streak", 0)) if isinstance(getattr(checkin_mgr, "pc_streak", 0), (int, float)) else 0,
                mobile_status=mobile_status,
                mobile_coins=mobile_coins_earned,
                mobile_streak=int(getattr(checkin_mgr, "mobile_streak", 0)) if isinstance(getattr(checkin_mgr, "mobile_streak", 0), (int, float)) else 0,
                checkin_coins=earned_from_checkin,
                tasks_coins=int(task_coins),
                tasks_skipped=tasks_skipped,
                tasks_failed=tasks_failed,
                task_details=task_records,
                coupon_details=coupon_records
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

    logger.info(f"\n=======================================================")
    logger.info(f"📊 ПІДСУМКОВИЙ ЗВІТ ОБРОБКИ ({len(accounts)} акаунтів)")
    logger.info(f"=======================================================")
    for s in dispatcher.summaries:
        init_str = f"{int(s.initial_coins):,}" if isinstance(s.initial_coins, (int, float)) else "Н/Д"
        total_str = f"{int(s.total_coins):,}" if isinstance(s.total_coins, (int, float)) else "Н/Д"
        earned_str = f"{int(s.coins_earned):,}" if isinstance(s.coins_earned, (int, float)) else ("0" if s.coins_earned is None else str(s.coins_earned))
        streak_v = int(s.streak_days) if isinstance(s.streak_days, (int, float)) else 0
        streak_str = f"{streak_v} дн." if streak_v > 0 else "-"
        pc_c = int(s.pc_coins) if isinstance(s.pc_coins, (int, float)) else 0
        mob_c = int(s.mobile_coins) if isinstance(s.mobile_coins, (int, float)) else 0
        tasks_c = int(s.tasks_coins) if isinstance(s.tasks_coins, (int, float)) else 0
        pc_note = " (раніше)" if (s.pc_status or s.checkin_status) == "ALREADY_DONE" and pc_c > 0 else ""
        mob_note = " (раніше)" if (s.mobile_status or s.checkin_status) == "ALREADY_DONE" and mob_c > 0 else ""
        logger.info(f"👤 [{s.name}]")
        logger.info(f"   ├ Сесія: {'🟢 Валідна' if s.session_valid else '🔴 Недійсна'}")
        if s.error_message:
            logger.info(f"   ├ ⚠️ Помилка: {s.error_message}")
        logger.info(f"   ├ Чекін: {s.checkin_status} (🔥 стрік {streak_str})")
        logger.info(f"   ├ Канал PC: {s.pc_status or '-'} (+{pc_c} 🪙{pc_note})")
        logger.info(f"   ├ Канал Mobile: {s.mobile_status or '-'} (+{mob_c} 🪙{mob_note})")
        logger.info(f"   ├ Інтерактивні таски: {s.tasks_done} вик. (+{tasks_c} 🪙), {s.tasks_skipped} пропущ., {s.tasks_failed} збій")
        if s.task_details:
            for td in s.task_details[:6]:
                st_icon = "✅" if td.get("status") == "COMPLETED" else ("⏭️" if td.get("status") == "SKIPPED" else "❌")
                coins_s = f" (+{td.get('coins')} 🪙)" if td.get("coins", 0) > 0 else ""
                reason_s = f" ({td.get('reason')})" if td.get("reason") else ""
                logger.info(f"   │   {st_icon} {td.get('title')}{coins_s}{reason_s}")
            if len(s.task_details) > 6:
                logger.info(f"   │   ... ще {len(s.task_details) - 6} завдань")
        logger.info(f"   ├ Зібрано купонів: {s.coupons_claimed}")
        if s.coupon_details:
            for cd in s.coupon_details[:3]:
                c_icon = "✅" if cd.get("status") == "SUCCESS" else ("ℹ️" if cd.get("status") == "ALREADY_DONE" else "⚠️")
                logger.info(f"   │   {c_icon} {cd.get('id')}: {cd.get('reason')}")
        logger.info(f"   └ 💰 Баланс: {init_str} ➔ {total_str} 🪙 (Сьогодні: +{earned_str} 🪙)")
    logger.info(f"-------------------------------------------------------")
    total_earned_all = sum(int(s.coins_earned) if isinstance(s.coins_earned, (int, float)) else 0 for s in dispatcher.summaries)
    total_checkin_all = sum(int(s.checkin_coins) if isinstance(s.checkin_coins, (int, float)) else 0 for s in dispatcher.summaries)
    total_tasks_all = sum(int(s.tasks_coins) if isinstance(s.tasks_coins, (int, float)) else 0 for s in dispatcher.summaries)
    total_coupons_all = sum(int(s.coupons_claimed) if isinstance(s.coupons_claimed, (int, float)) else 0 for s in dispatcher.summaries)
    logger.info(f"Успішно: {success_count} | З помилками: {failed_count} | Всього монет: +{total_earned_all} 🪙 (Чекін: +{total_checkin_all}, Таски: +{total_tasks_all}) | Купонів: {total_coupons_all}")
    logger.info(f"=======================================================\n")

    # 4. Відправка сповіщень у GitHub Actions Step Summary та Telegram
    dispatcher.dispatch()

    # 5. Якщо всі акаунти зазнали збою, повертаємо код помилки для алерту в CI
    if failed_count > 0 and success_count == 0:
        logger.error("❌ Усі акаунти завершилися з помилками!")
        sys.exit(1)

    logger.info("🎉 Виконання завдань завершено!")


if __name__ == "__main__":
    main()
