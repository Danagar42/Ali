import os
import sys
import time
import json
import random
import signal
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

def _sigterm_handler(signum, frame):
    """Обробник SIGTERM для коректного переривання та збереження результатів."""
    logging.getLogger("AliExpressFarmer").warning("⚠️ Отримано сигнал SIGTERM. Завершення процесу...")
    raise SystemExit(143)

try:
    signal.signal(signal.SIGTERM, _sigterm_handler)
except Exception:
    pass

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

from src.config import SKIP_RANDOM_SLEEP, MAX_START_SLEEP_SECONDS, MAX_SAFE_TASKS_PER_RUN
from src.auth import load_accounts
from src.client import AliClient
from src.checkin import CheckinManager
from src.coins import CoinsFarmManager
from src.notifications import NotificationDispatcher, AccountSummary


def save_history(
    summaries: list,
    state_dir: str = ".state",
    run_id: Optional[str] = None
) -> Optional[str]:
    """
    Зберігає підсумки виконання за поточний день у .state/history.json (історія останніх 30 днів).
    Підтримує накопичувальну історію декількох запусків на день:
    - Зберігає детальний знімок кожного окремого запуску у списку 'runs' без затирання попередніх запусків.
    - Агрегує денний заробіток та оновлює актуальний баланс на основі останнього підтвердженого результату.
    """
    try:
        os.makedirs(state_dir, exist_ok=True)
        file_path = os.path.join(state_dir, "history.json")
        history: List[Dict[str, Any]] = []
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        history = [entry for entry in data if isinstance(entry, dict)]
            except Exception as e:
                logger.debug(f"Не вдалося зчитати історію зі {file_path}: {e}")

        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        now_ts = int(time.time())
        now_iso = now.isoformat()

        # Знаходимо існуючий запис за сьогодні або створюємо новий
        today_entry = next((entry for entry in history if entry.get("date") == today_str), None)
        if today_entry is None:
            today_entry = {
                "date": today_str,
                "timestamp": now_ts,
                "updated_at": now_iso,
                "runs_count": 0,
                "accounts": {},
                "runs": []
            }
            history.append(today_entry)
        else:
            today_entry["timestamp"] = now_ts
            today_entry["updated_at"] = now_iso
            if "runs" not in today_entry or not isinstance(today_entry["runs"], list):
                today_entry["runs"] = []
            if "accounts" not in today_entry or not isinstance(today_entry["accounts"], dict):
                today_entry["accounts"] = {}

        # Знімок поточного запуску для акаунтів
        current_run_accounts: Dict[str, Any] = {}
        for s in summaries:
            earned_this = getattr(s, "coins_earned_this_run", None)
            if earned_this is None:
                earned_this = s.coins_earned or 0

            current_run_accounts[s.name] = {
                "checkin_status": s.checkin_status,
                "streak_days": s.streak_days,
                "coins_earned": s.coins_earned,
                "coins_earned_this_run": earned_this,
                "prior_rewards_today": getattr(s, "prior_rewards_today", 0),
                "total_coins": s.total_coins,
                "balance_verified": getattr(s, "balance_verified", True),
                "tasks_done": s.tasks_done,
                "tasks_coins": s.tasks_coins,
                "session_valid": s.session_valid,
                "error_message": s.error_message
            }

        effective_run_id = run_id or f"run_{now_ts}_{len(today_entry['runs']) + 1}"
        existing_run_record = next(
            (r for r in today_entry["runs"] if isinstance(r, dict) and r.get("run_id") == effective_run_id),
            None
        )
        if existing_run_record is not None:
            existing_run_record["timestamp"] = now_ts
            existing_run_record["run_time"] = now_iso
            existing_run_record["accounts"] = current_run_accounts
        else:
            run_record = {
                "run_id": effective_run_id,
                "timestamp": now_ts,
                "run_time": now_iso,
                "accounts": current_run_accounts
            }
            today_entry["runs"].append(run_record)

        today_entry["runs_count"] = len(today_entry["runs"])

        # Оновлення агрегованого стану акаунтів за день
        for s in summaries:
            acc_name = s.name
            earned_this = getattr(s, "coins_earned_this_run", None)
            if earned_this is None:
                earned_this = s.coins_earned or 0

            prev_acc = today_entry["accounts"].get(acc_name)
            if prev_acc is None:
                today_entry["accounts"][acc_name] = {
                    "checkin_status": s.checkin_status,
                    "streak_days": s.streak_days,
                    "coins_earned": earned_this,
                    "coins_earned_this_run": earned_this,
                    "prior_rewards_today": getattr(s, "prior_rewards_today", 0),
                    "total_coins": s.total_coins,
                    "balance_verified": getattr(s, "balance_verified", True),
                    "tasks_done": s.tasks_done,
                    "tasks_coins": s.tasks_coins,
                    "session_valid": s.session_valid,
                    "error_message": s.error_message,
                    "runs_count": 1
                }
            else:
                accumulated_coins = sum(
                    r.get("accounts", {}).get(acc_name, {}).get("coins_earned_this_run", 0)
                    for r in today_entry["runs"]
                    if isinstance(r, dict)
                )
                accumulated_tasks_done = sum(
                    r.get("accounts", {}).get(acc_name, {}).get("tasks_done", 0)
                    for r in today_entry["runs"]
                    if isinstance(r, dict)
                )
                accumulated_tasks_coins = sum(
                    r.get("accounts", {}).get(acc_name, {}).get("tasks_coins", 0)
                    for r in today_entry["runs"]
                    if isinstance(r, dict)
                )

                resolved_total = s.total_coins if s.total_coins is not None else prev_acc.get("total_coins")
                resolved_verified = getattr(s, "balance_verified", False) or (prev_acc.get("balance_verified", False) and s.total_coins is None)

                resolved_status = s.checkin_status
                if prev_acc.get("checkin_status") == "SUCCESS" and resolved_status == "ALREADY_DONE":
                    resolved_status = "SUCCESS"

                today_entry["accounts"][acc_name] = {
                    "checkin_status": resolved_status,
                    "streak_days": max(s.streak_days or 0, prev_acc.get("streak_days", 0)),
                    "coins_earned": accumulated_coins,
                    "coins_earned_this_run": earned_this,
                    "prior_rewards_today": getattr(s, "prior_rewards_today", 0),
                    "total_coins": resolved_total,
                    "balance_verified": resolved_verified,
                    "tasks_done": accumulated_tasks_done,
                    "tasks_coins": accumulated_tasks_coins,
                    "session_valid": s.session_valid,
                    "error_message": s.error_message or prev_acc.get("error_message"),
                    "runs_count": len([
                        r for r in today_entry["runs"]
                        if isinstance(r, dict) and acc_name in r.get("accounts", {})
                    ])
                }

        if len(history) > 30:
            history = history[-30:]

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        logger.info(f"💾 Історію виконання збережено у {file_path} (запусків сьогодні: {today_entry['runs_count']})")
        return file_path
    except Exception as e:
        logger.warning(f"⚠️ Не вдалося зберегти стан історії: {e}")
        return None


def run_doctor() -> int:
    """Діагностика оточення, ключів, підключення до мережі та стану перед запуском."""
    print("=" * 60)
    print("🩺 AliExpress Coin Farmer Doctor: Діагностика системи")
    print("=" * 60)
    critical_errors = 0
    warnings = 0

    # 1. Python version
    py_ver = sys.version_info
    if py_ver >= (3, 9):
        print(f"✅ Python: {py_ver.major}.{py_ver.minor}.{py_ver.micro} (підтримується)")
    else:
        print(f"❌ Python: {py_ver.major}.{py_ver.minor}.{py_ver.micro} (потрібно >= 3.9)")
        critical_errors += 1

    # 2. Перевірка секретів акаунтів
    accounts = load_accounts()
    if accounts:
        print(f"✅ Акаунти: знайдено {len(accounts)} валідних конфігурацій")
        for idx, acc in enumerate(accounts, 1):
            name, c = acc
            keys = list(c.keys()) if isinstance(c, dict) else []
            has_token = "_m_h5_tk" in c if isinstance(c, dict) else "_m_h5_tk" in str(c)
            token_status = "🟢 token OK" if has_token else "🟡 no _m_h5_tk (буде оновлено через probe)"
            proxy = getattr(acc, "proxy", None)
            proxy_info = f", proxy: {proxy.split('@')[-1]}" if proxy else ""
            print(f"   [{idx}] {name} ({len(keys)} cookies, {token_status}{proxy_info})")
    else:
        print("❌ Акаунти: не знайдено жодного акаунту (ALI_COOKIES або ALI_COOKIES_1..N)")
        critical_errors += 1

    # 3. Перевірка Telegram сповіщень
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        masked = tg_token[:5] + "..." + tg_token[-4:] if len(tg_token) > 10 else "***"
        print(f"✅ Telegram: налаштовано (Bot: {masked}, Chat: {tg_chat})")
    else:
        print("🟡 Telegram: сповіщення не налаштовано (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID відсутні)")
        warnings += 1

    # 4. Перевірка мережевої доступності AliExpress
    try:
        import requests
        resp = requests.get("https://www.aliexpress.com", timeout=5.0)
        print(f"✅ Мережа AliExpress: доступно (HTTP {resp.status_code})")
    except Exception as e:
        print(f"❌ Мережа AliExpress: недоступно ({e})")
        critical_errors += 1

    # 5. Перевірка курсу валют НБУ
    try:
        from src.notifications import get_usd_to_uah_rate
        rate = get_usd_to_uah_rate(force_refresh=True)
        print(f"✅ Курс НБУ: успішно отримано (1 USD = {rate:.2f} UAH)")
    except Exception as e:
        print(f"🟡 Курс НБУ: помилка ({e}), буде використано базовий курс 42.0")
        warnings += 1

    # 6. Перевірка прав на запис у .state/
    try:
        os.makedirs(".state", exist_ok=True)
        test_file = os.path.join(".state", ".doctor_test")
        with open(test_file, "w") as f:
            f.write("ok")
        if os.path.exists(test_file):
            os.remove(test_file)
        print("✅ Сховище .state/: доступне для читання та запису")
    except Exception as e:
        print(f"❌ Сховище .state/: помилка доступу ({e})")
        critical_errors += 1

    print("=" * 60)
    if critical_errors == 0:
        print(f"🎉 Результат діагностики: ВСЕ ГОТОВО ДО РОБОТИ ({warnings} зауважень)")
        return 0
    else:
        print(f"❌ Результат діагностики: ВИЯВЛЕНО {critical_errors} КРИТИЧНИХ ПОМИЛОК!")
        return 1


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
    logger.info("🚀 Запуск AliExpress Coin Farmer")

    # 1. Завантаження акаунтів (перевірка наявності акаунтів ДО випадкового зсуву часу)
    accounts = load_accounts()
    if not accounts:
        logger.error("❌ Не знайдено жодного валідного акаунту! Додайте ALI_COOKIES або ALI_COOKIES_1..N у змінні оточення.")
        sys.exit(1)

    logger.info(f"📋 Знайдено акаунтів для обробки: {len(accounts)}")

    # 2. Початковий зсув часу
    initial_random_jitter()

    dispatcher = NotificationDispatcher()
    current_run_id = f"run_{int(time.time())}_{os.getpid()}"
    checkin_success_count = 0
    success_count = 0
    partial_count = 0
    failed_count = 0
    action_required_count = 0

    # 3. Обробка кожного акаунту з ізоляцією помилок
    try:
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

                # Фіксуємо баланс після чекіну для порівняння
                pre_task_coins = checkin_mgr.total_coins if isinstance(checkin_mgr.total_coins, (int, float)) else None

                # 3.2 Фарм монет за перегляд безпечних завдань (~15 сек)
                coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)
                coins_done = coins_mgr.farm_browse_tasks(count=MAX_SAFE_TASKS_PER_RUN)

                # 3.3 Оновлюємо актуальний баланс монет після виконання всіх завдань
                fresh_post_balance = None
                try:
                    fresh_post_balance = checkin_mgr._fetch_coin_balance()
                except Exception as e:
                    logger.debug(f"[{account_name}] Не вдалося оновити баланс монет після завдань: {e}")

                # Облік зароблених монет (чекін + безпечні завдання)
                task_coins = getattr(coins_mgr, "task_coins_earned", 0)
                if not isinstance(task_coins, (int, float)):
                    task_coins = 0
                earned_from_checkin = checkin_mgr.coins_earned if isinstance(checkin_mgr.coins_earned, (int, float)) else 0
                total_earned_today = earned_from_checkin + int(task_coins)

                if isinstance(fresh_post_balance, (int, float)):
                    account_total_coins = int(fresh_post_balance)
                    balance_verified = True
                elif isinstance(getattr(checkin_mgr, "total_coins", None), (int, float)):
                    account_total_coins = int(checkin_mgr.total_coins)
                    balance_verified = (int(task_coins) == 0)
                elif isinstance(pre_task_coins, (int, float)):
                    account_total_coins = int(pre_task_coins)
                    balance_verified = (int(task_coins) == 0)
                else:
                    account_total_coins = None
                    balance_verified = False

                if account_total_coins is not None:
                    checkin_mgr.total_coins = account_total_coins

                # Початковий баланс беремо безпосередньо з checkin_mgr без синтетичних віднімань
                init_c = getattr(checkin_mgr, "initial_coins", None)
                initial_balance = int(init_c) if isinstance(init_c, (int, float)) else None

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
                tasks_already_done = int(getattr(coins_mgr, "tasks_already_done", 0)) if isinstance(getattr(coins_mgr, "tasks_already_done", 0), (int, float)) else 0
                if tasks_already_done == 0 and task_records:
                    tasks_already_done = sum(1 for t in task_records if isinstance(t, dict) and t.get("status") in ("ALREADY_DONE", "DONE"))

                chk_status = checkin_mgr.status if isinstance(getattr(checkin_mgr, "status", None), str) else ("SUCCESS" if checkin_ok else "FAILED")
                t_status = coins_mgr.status if isinstance(getattr(coins_mgr, "status", None), str) else "NONE"

                # Фіксація звіту по акаунту для сповіщень
                summary = AccountSummary(
                    name=account_name,
                    checkin_status=chk_status,
                    coins_earned=total_earned_today,
                    coins_earned_this_run=int(getattr(checkin_mgr, "coins_earned_this_run", 0)) + int(task_coins),
                    prior_rewards_today=int(getattr(checkin_mgr, "prior_rewards_today", 0)),
                    streak_days=checkin_mgr.streak_days,
                    total_coins=account_total_coins,
                    balance_verified=balance_verified,
                    tasks_done=coins_done,
                    session_valid=checkin_mgr.session_valid,
                    initial_coins=initial_balance,
                    pc_status=pc_status,
                    pc_coins=pc_coins_earned,
                    pc_streak=int(getattr(checkin_mgr, "pc_streak", 0)) if isinstance(getattr(checkin_mgr, "pc_streak", 0), (int, float)) else 0,
                    mobile_status=mobile_status,
                    mobile_coins=mobile_coins_earned,
                    mobile_streak=int(getattr(checkin_mgr, "mobile_streak", 0)) if isinstance(getattr(checkin_mgr, "mobile_streak", 0), (int, float)) else 0,
                    checkin_coins=earned_from_checkin,
                    tasks_status=t_status,
                    tasks_coins=int(task_coins),
                    tasks_already_done=tasks_already_done,
                    tasks_skipped=tasks_skipped,
                    tasks_failed=tasks_failed,
                    task_details=task_records
                )
                dispatcher.add_summary(summary)
                save_history(dispatcher.summaries, run_id=current_run_id)

                # Фіксація підтвердження чекіну для перевірки CI
                if checkin_ok and getattr(checkin_mgr, "session_valid", True):
                    checkin_success_count += 1

                # Облік статусу на основі aggregate_status
                agg_status = summary.aggregate_status
                if agg_status == "OK":
                    success_count += 1
                    logger.info(f"✅ [{account_name}] Обробка акаунту успішна (OK).")
                elif agg_status == "PARTIAL":
                    partial_count += 1
                    logger.warning(f"⚠️ [{account_name}] Обробка акаунту часткова (PARTIAL).")
                elif agg_status == "ACTION_REQUIRED":
                    action_required_count += 1
                    if checkin_mgr.status == "CAPTCHA_REQUIRED":
                        logger.warning(f"🧩 [{account_name}] Потрібна дія: чекін заблоковано капчею WAF.")
                    else:
                        logger.warning(f"🚨 [{account_name}] Потрібна дія: сесія недійсна, оновіть cookies.")
                else:
                    failed_count += 1
                    logger.warning(f"❌ [{account_name}] Обробка акаунту завершилася зі збоєм (FAILED).")
            except Exception as e:
                failed_count += 1
                dispatcher.add_summary(AccountSummary(
                    name=account_name,
                    checkin_status="FAILED",
                    session_valid=False,
                    error_message=str(e)
                ))
                save_history(dispatcher.summaries, run_id=current_run_id)
                logger.error(f"❌ [{account_name}] Критична помилка при обробці: {e}", exc_info=True)

            # Пауза між акаунтами, якщо їх декілька
            if idx < len(accounts):
                account_delay = random.uniform(10, 25)
                logger.info(f"⏸️ Пауза перед наступним акаунтом: {account_delay:.1f} сек...\n")
                time.sleep(account_delay)
    finally:
        if dispatcher.summaries:
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
                already_done_s = f", {s.tasks_already_done} вже вик." if getattr(s, "tasks_already_done", 0) > 0 else ""
                logger.info(f"   ├ Інтерактивні таски: {s.tasks_done} вик. (+{tasks_c} 🪙){already_done_s}, {s.tasks_skipped} пропущ., {s.tasks_failed} збій")
                if s.task_details:
                    sorted_td = sorted(
                        s.task_details,
                        key=lambda x: 0 if isinstance(x, dict) and x.get("status") == "COMPLETED"
                        else (1 if isinstance(x, dict) and x.get("status") in ("ALREADY_DONE", "DONE")
                        else (2 if isinstance(x, dict) and x.get("status") == "FAILED" else 3))
                    )
                    for td in sorted_td[:10]:
                        st_icon = (
                            "✅" if td.get("status") == "COMPLETED"
                            else ("ℹ️" if td.get("status") in ("ALREADY_DONE", "DONE")
                            else ("⏭️" if td.get("status") == "SKIPPED" else "❌"))
                        )
                        coins_s = f" (+{td.get('coins')} 🪙)" if td.get("coins", 0) > 0 else ""
                        reason_s = f" ({td.get('reason')})" if td.get("reason") else ""
                        logger.info(f"   │   {st_icon} {td.get('title')}{coins_s}{reason_s}")
                    if len(sorted_td) > 10:
                        logger.info(f"   │   ... ще {len(sorted_td) - 10} завдань")
                logger.info(f"   └ 💰 Баланс: {init_str} ➔ {total_str} 🪙 (Сьогодні: +{earned_str} 🪙)")
            logger.info(f"-------------------------------------------------------")
            total_earned_all = sum(int(s.coins_earned) if isinstance(s.coins_earned, (int, float)) else 0 for s in dispatcher.summaries)
            total_checkin_all = sum(int(s.checkin_coins) if isinstance(s.checkin_coins, (int, float)) else 0 for s in dispatcher.summaries)
            total_tasks_all = sum(int(s.tasks_coins) if isinstance(s.tasks_coins, (int, float)) else 0 for s in dispatcher.summaries)
            logger.info(f"Успішно (OK): {success_count} | Частково (PARTIAL): {partial_count} | З помилками (FAILED): {failed_count} | Потрібна дія: {action_required_count} | Всього монет: +{total_earned_all} 🪙 (Чекін: +{total_checkin_all}, Таски: +{total_tasks_all})")
            logger.info(f"=======================================================\n")

            # 4. Відправка сповіщень у GitHub Actions Step Summary та Telegram
            dispatcher.dispatch()

            # 5. Збереження щоденного стану історії для персистентності між запусками
            save_history(dispatcher.summaries, run_id=current_run_id)

    # 6. Якщо жоден чекін не підтверджено або всі акаунти зазнали збою, повертаємо код помилки для алерту в CI
    if checkin_success_count == 0 or ((failed_count > 0 or action_required_count > 0) and success_count == 0 and partial_count == 0):
        logger.error("❌ Усі акаунти завершилися з помилками або жоден чекін не підтверджено!")
        sys.exit(1)

    logger.info("🎉 Виконання завдань завершено!")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("doctor", "--doctor", "-d"):
        sys.exit(run_doctor())
    main()
