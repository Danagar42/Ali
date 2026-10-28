import os
import sys
import re
import html
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import requests

logger = logging.getLogger(__name__)


def _format_coins(val: Any) -> str:
    """Безпечно форматує кількість монет, підтримуючи int, float, str та тестові моки."""
    if val is None or isinstance(val, bool):
        return "-"
    if isinstance(val, (int, float)):
        return f"{int(val):,}"
    try:
        if isinstance(val, str):
            clean = val.strip().replace(",", "")
            f_val = float(clean)
            return f"{int(f_val):,}"
    except Exception:
        pass
    return str(val)


_USD_TO_UAH_CACHE: Optional[float] = None


def get_usd_to_uah_rate(force_refresh: bool = False) -> float:
    """
    Отримує актуальний офіційний курс USD до UAH від НБУ.
    Підтримує перевизначення через змінну середовища USD_TO_UAH_RATE.
    Має fallback на 42.0 при недоступності мережі або таймауті.
    Результат кешується для уникнення повторних запитів під час одного запуску.
    """
    global _USD_TO_UAH_CACHE
    env_val = os.getenv("USD_TO_UAH_RATE", "").strip()
    if env_val:
        try:
            val = float(env_val)
            if val > 0:
                return val
        except ValueError:
            pass

    if not force_refresh and _USD_TO_UAH_CACHE is not None:
        return _USD_TO_UAH_CACHE

    url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                rate = data[0].get("rate")
                if isinstance(rate, (int, float)) and rate > 0:
                    _USD_TO_UAH_CACHE = float(rate)
                    return _USD_TO_UAH_CACHE
    except Exception as e:
        logger.debug(f"Не вдалося отримати курс НБУ ({e}), використовується базовий курс 42.0")

    _USD_TO_UAH_CACHE = 42.0
    return _USD_TO_UAH_CACHE


def _format_fiat_equiv(coins: Any, rate: Optional[float] = None, show_sign: bool = False) -> str:
    """
    Форматує еквівалент монет AliExpress у USD ($) та UAH (грн).
    Правило AliExpress: 100 монет = $1.00 USD (1 монета = $0.01 USD).
    """
    if rate is None or rate <= 0:
        rate = get_usd_to_uah_rate()

    try:
        c_val = int(coins) if isinstance(coins, (int, float)) else int(str(coins).replace(",", "").strip())
    except Exception:
        c_val = 0

    usd = c_val * 0.01
    uah = usd * rate

    if show_sign and c_val != 0:
        sign = "+" if c_val > 0 else "-"
        return f"{sign}${abs(usd):.2f} ≈ {abs(uah):.1f} грн"
    else:
        return f"${usd:.2f} ≈ {uah:.1f} грн"


def _safe_truncate_html(text: str, max_length: int = 4000) -> str:
    """
    Безпечно скорочує повідомлення з HTML-розміткою для Telegram API:
    - Обрізає на межі рядка (щоб не ламати слова та теги)
    - Закриває всі відкриті HTML-теги у зворотному порядку вкладеності
    - Додає попередження про скорочення
    - Гарантує синтаксичну валідність HTML без HTTP 400 у Telegram API
    - Суворо гарантує len(result) <= max_length
    """
    if len(text) <= max_length:
        return text
    if max_length <= 0:
        return ""

    long_suffix = "\n\n<i>...[текст скорочено через ліміт Telegram]...</i>"
    if max_length >= 80:
        suffix = long_suffix
    else:
        suffix = ""

    budget = max(0, max_length - len(suffix))
    truncated = text[:budget]

    if max_length >= 200 and budget > 60:
        last_nl = truncated.rfind("\n")
        if last_nl > budget // 2:
            truncated = truncated[:last_nl]

    tag_pattern = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9]+)(?:\s+[^>]*)?>")
    void_tags = {"br", "hr", "img", "meta", "input"}

    while True:
        last_open = truncated.rfind("<")
        last_close = truncated.rfind(">")
        if last_open > last_close:
            truncated = truncated[:last_open]

        last_amp = truncated.rfind("&")
        last_semi = truncated.rfind(";")
        if last_amp > last_semi and (len(truncated) - last_amp) < 10:
            truncated = truncated[:last_amp]

        stack = []
        for match in tag_pattern.finditer(truncated):
            is_closing = match.group(1) == "/"
            tag = match.group(2).lower()
            if tag in void_tags or match.group(0).endswith("/>"):
                continue
            if is_closing:
                if stack and stack[-1] == tag:
                    stack.pop()
                elif tag in stack:
                    for i in range(len(stack) - 1, -1, -1):
                        if stack[i] == tag:
                            stack.pop(i)
                            break
            else:
                stack.append(tag)

        close_tags = "".join(f"</{tag}>" for tag in reversed(stack))
        total_len = len(truncated) + len(close_tags) + len(suffix)

        if total_len <= max_length:
            break

        excess = total_len - max_length
        if excess >= len(truncated):
            truncated = ""
            close_tags = ""
            if len(suffix) > max_length:
                suffix = suffix[:max_length]
            break
        truncated = truncated[:-excess]

    res = truncated + close_tags + suffix
    # Очищуємо порожні парні теги типу <b></b>, <i></i>, <code></code> на межі обрізання
    empty_tag_pattern = re.compile(r"<([a-zA-Z0-9]+)(?:\s+[^>]*)?>\s*</\1>")
    while True:
        cleaned, n = empty_tag_pattern.subn("", res)
        if n == 0:
            break
        res = cleaned

    return res


@dataclass
class AccountSummary:
    """Підсумкові дані обробки одного акаунта."""
    name: str
    checkin_status: str  # "SUCCESS", "ALREADY_DONE", "CAPTCHA_REQUIRED", "FAILED", "SESSION_EXPIRED"
    coins_earned: int = 0
    coins_earned_this_run: Optional[int] = None
    prior_rewards_today: int = 0
    streak_days: int = 0
    total_coins: Optional[int] = None
    balance_verified: bool = True
    tasks_done: int = 0
    session_valid: bool = True
    error_message: Optional[str] = None
    initial_coins: Optional[int] = None
    pc_status: Optional[str] = None
    pc_coins: int = 0
    pc_streak: int = 0
    mobile_status: Optional[str] = None
    mobile_coins: int = 0
    mobile_streak: int = 0
    checkin_coins: int = 0
    tasks_status: Optional[str] = None
    tasks_coins: int = 0
    tasks_already_done: int = 0
    tasks_skipped: int = 0
    tasks_failed: int = 0
    task_details: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        # Повторний чекін не є новою нагородою; свіжими можуть бути лише таски.
        if self.coins_earned_this_run is None:
            if self.checkin_status == "ALREADY_DONE" or self.prior_rewards_today > 0:
                self.coins_earned_this_run = self.tasks_coins
            else:
                self.coins_earned_this_run = self.coins_earned
        if self.tasks_already_done == 0 and self.task_details:
            self.tasks_already_done = sum(
                1 for t in self.task_details
                if isinstance(t, dict) and t.get("status") in ("ALREADY_DONE", "DONE")
            )
        if self.checkin_coins == 0:
            pc_c = int(self.pc_coins) if isinstance(self.pc_coins, (int, float)) else 0
            mob_c = int(self.mobile_coins) if isinstance(self.mobile_coins, (int, float)) else 0
            earned = int(self.coins_earned) if isinstance(self.coins_earned, (int, float)) else 0
            tc = int(self.tasks_coins) if isinstance(self.tasks_coins, (int, float)) else 0
            if earned > 0 or tc > 0:
                self.checkin_coins = max(0, earned - tc)
            elif pc_c > 0 or mob_c > 0:
                self.checkin_coins = pc_c + mob_c

    @property
    def aggregate_status(self) -> str:
        """
        Агрегований статус акаунта:
        - ACTION_REQUIRED: потрібна дія користувача (прострочена сесія або капча)
        - FAILED: збій усіх етапів або відсутність успішних результатів при наявності помилок
        - PARTIAL: частковий успіх (деякі етапи успішні, деякі зазнали збою)
        - OK: усі запущені етапи успішно завершені без збоїв
        """
        if (
            not self.session_valid
            or self.checkin_status in ("SESSION_EXPIRED", "CAPTCHA_REQUIRED")
            or self.pc_status in ("SESSION_EXPIRED", "CAPTCHA_REQUIRED")
            or self.mobile_status in ("SESSION_EXPIRED", "CAPTCHA_REQUIRED")
            or self.tasks_status in ("SESSION_EXPIRED", "CAPTCHA_REQUIRED")
        ):
            return "ACTION_REQUIRED"

        has_failures = (
            self.checkin_status not in ("SUCCESS", "ALREADY_DONE")
            or self.mobile_status == "FAILED"
            or self.pc_status == "FAILED"
            or self.tasks_status == "FAILED"
            or self.tasks_failed > 0
            or bool(self.error_message)
        )
        has_successes = (
            self.checkin_status in ("SUCCESS", "ALREADY_DONE")
            or self.pc_status in ("SUCCESS", "ALREADY_DONE")
            or self.mobile_status in ("SUCCESS", "ALREADY_DONE")
            or self.tasks_status in ("SUCCESS", "ALREADY_DONE")
            or self.coins_earned > 0
            or self.tasks_done > 0
        )

        if has_successes and not has_failures:
            return "OK"
        elif has_successes and has_failures:
            return "PARTIAL"
        else:
            return "FAILED"

    @property
    def has_problem(self) -> bool:
        """Повертає True, якщо обробка акаунта завершилася зі збоєм, частково або потребує дії."""
        return self.aggregate_status in ("PARTIAL", "FAILED", "ACTION_REQUIRED")


class NotificationDispatcher:
    """
    Керує відправкою сповіщень:
    1. Генерація точного та деталізованого Markdown-звіту у $GITHUB_STEP_SUMMARY для GitHub Actions.
    2. Відправка структурованого інформативного повідомлення у Telegram через бота.
    """

    def __init__(self, state_dir: str = ".state"):
        self.summaries: List[AccountSummary] = []
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.notification_state_path = os.path.join(state_dir, "telegram_notification.json")

    def add_summary(self, summary: AccountSummary):
        self.summaries.append(summary)

    def _checkin_icon(self, status: Optional[str]) -> str:
        if status == "SUCCESS":
            return "✅ Зараховано"
        if status == "ALREADY_DONE":
            return "ℹ️ Вже був"
        if status == "CAPTCHA_REQUIRED":
            return "🧩 Капча"
        if status == "SESSION_EXPIRED":
            return "❌ Прострочено"
        if status == "SKIPPED":
            return "⏭️ Пропущено"
        return "⚠️ Збій"

    def _session_icon(self, valid: bool) -> str:
        return "🟢 Активна" if valid else "🔴 Недійсна"

    def format_step_summary(self) -> str:
        """
        Форматує багатий структурований Markdown-звіт для GitHub Step Summary.
        Містить:
        - Загальний статус та статистику нагород (чекін + таски)
        - Основну зведену таблицю результатів по акаунтах
        - Окрему таблицю Dual-Channel чекіну (PC + Mobile)
        - Деталізацію виконаних, пропущених та збійних інтерактивних завдань MTOP
        - Деталізацію зібраних купонів
        """
        total_accounts = len(self.summaries)
        successful_accounts = sum(
            1 for s in self.summaries
            if s.checkin_status in ("SUCCESS", "ALREADY_DONE") and s.session_valid
        )
        total_coins_earned = sum(s.coins_earned_this_run for s in self.summaries)
        total_checkin_coins = sum(max(0, s.coins_earned_this_run - s.tasks_coins) for s in self.summaries)
        total_tasks_coins = sum(int(s.tasks_coins) if isinstance(s.tasks_coins, (int, float)) else 0 for s in self.summaries)
        total_tasks_done = sum(int(s.tasks_done) if isinstance(s.tasks_done, (int, float)) else 0 for s in self.summaries)
        has_expired = any(
            not s.session_valid
            or s.checkin_status == "SESSION_EXPIRED"
            or s.pc_status == "SESSION_EXPIRED"
            or s.mobile_status == "SESSION_EXPIRED"
            for s in self.summaries
        )
        has_captcha = any(
            s.checkin_status == "CAPTCHA_REQUIRED"
            or s.pc_status == "CAPTCHA_REQUIRED"
            or s.mobile_status == "CAPTCHA_REQUIRED"
            for s in self.summaries
        )

        status_text = (
            '⚪ Немає акаунтів' if total_accounts == 0
            else ('🟢 Успішно' if successful_accounts == total_accounts
            else ('🔴 Потрібна увага' if has_expired
            else ('🧩 Капча' if has_captcha
            else ('🔴 Збій' if successful_accounts == 0
            else '🟡 Частково'))))
        )

        lines = [
            "# 🪙 AliExpress Farm Summary Report",
            "",
            f"> **Статус обробки:** {status_text} | "
            f"**Акаунтів:** {successful_accounts}/{total_accounts} | "
            f"**Зароблено монет за запуск:** +{total_coins_earned} 🪙",
            f"> **Деталізація нагород:** 🪙 Чекін: +{total_checkin_coins} 🪙 | 🛍️ Завдання: +{total_tasks_coins} 🪙 ({total_tasks_done} вик.)",
            ""
        ]

        if has_expired:
            lines.extend([
                "> [!CAUTION]",
                "> Виявлено акаунти з недійсною або простроченою сесією! Необхідно оновити секрети в репозиторії, щоб уникнути втрати щоденного стріку.",
                ""
            ])
        if has_captcha:
            lines.extend([
                "> [!WARNING]",
                "> 🧩 **Виявлено захист від роботів (Alibaba WAF Captcha)!**",
                "> Необхідно відкрити сайт AliExpress у звичайному браузері з тієї ж IP/проксі та пройти перевірку слайдера для зняття блокування.",
                ""
            ])

        lines.extend([
            "### 📊 Результати по акаунтах",
            "",
            "| Акаунт | Чекін | Стрік | За запуск | Всього монет | Таски | Сесія |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
        ])

        for s in self.summaries:
            streak_val = int(s.streak_days) if isinstance(s.streak_days, (int, float)) else 0
            streak_str = f"🔥 {streak_val} дн." if streak_val > 0 else "-"
            earned_val = s.coins_earned_this_run
            earned_str = f"+{earned_val} 🪙" if earned_val > 0 else "0"
            if s.total_coins is not None:
                if s.initial_coins is not None:
                    actual_diff = int(s.total_coins) - int(s.initial_coins)
                    diff_s = f"+{actual_diff}" if actual_diff > 0 else str(actual_diff)
                    if actual_diff != earned_val:
                        total_str = f"{_format_coins(s.initial_coins)} ➔ {_format_coins(s.total_coins)} 🪙 ({diff_s})"
                    elif earned_val > 0 or actual_diff != 0:
                        total_str = f"{_format_coins(s.initial_coins)} ➔ {_format_coins(s.total_coins)} 🪙"
                    else:
                        total_str = f"{_format_coins(s.total_coins)} 🪙"
                else:
                    total_str = f"{_format_coins(s.total_coins)} 🪙"

                if not getattr(s, "balance_verified", True) and s.tasks_coins > 0:
                    total_str += f" (+{s.tasks_coins} оцінка)"
            else:
                total_str = "-"
            tasks_str = str(s.tasks_done)
            safe_name = html.escape(str(s.name or "Account")).replace("|", "\\|")
            lines.append(
                f"| **{safe_name}** | {self._checkin_icon(s.checkin_status)} | {streak_str} | {earned_str} | {total_str} | {tasks_str} | {self._session_icon(s.session_valid)} |"
            )

        lines.append("")

        if any(not getattr(s, "balance_verified", True) and s.tasks_coins > 0 for s in self.summaries):
            lines.append("> ⚠️ **Увага:** Пост-баланс не верифіковано сервером через захист або таймаут, бонуси за завдання вказані як попередня оцінка.")
            lines.append("")
        errors = [s for s in self.summaries if s.error_message]
        if errors:
            lines.extend([
                "### ⚠️ Помилки та попередження",
                "",
                "| Акаунт | Помилка / Причина |",
                "| :--- | :--- |"
            ])
            for s in errors:
                safe_name = html.escape(str(s.name or "Account")).replace("|", "\\|")
                err_text = html.escape(str(s.error_message)).replace("|", "\\|").replace("\n", " ")
                lines.append(f"| **{safe_name}** | ❌ {err_text} |")
            lines.append("")

        # 1. Деталізація чекіну по каналах
        lines.extend([
            "### 📱 Деталізація чекіну по каналах (Dual-Channel)",
            "",
            "| Акаунт | PC Статус | PC Монети | Mobile Статус | Mobile Монети | Стрік | Загальний чекін |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
        ])
        for s in self.summaries:
            safe_name = html.escape(str(s.name or "Account")).replace("|", "\\|")
            pc_st = self._checkin_icon(s.pc_status or s.checkin_status)
            if isinstance(s.pc_coins, (int, float)) and s.pc_coins > 0:
                pc_c = f"+{int(s.pc_coins)} 🪙"
                if (s.pc_status or s.checkin_status) == "ALREADY_DONE":
                    pc_c += " (раніше)"
            else:
                pc_c = "0"

            mob_st = self._checkin_icon(s.mobile_status or s.checkin_status)
            if isinstance(s.mobile_coins, (int, float)) and s.mobile_coins > 0:
                mob_c = f"+{int(s.mobile_coins)} 🪙"
                if (s.mobile_status or s.checkin_status) == "ALREADY_DONE":
                    mob_c += " (раніше)"
            else:
                mob_c = "0"

            strk_v = int(s.streak_days) if isinstance(s.streak_days, (int, float)) else 0
            strk = f"🔥 {strk_v} дн." if strk_v > 0 else "-"
            checkin_this_run = max(0, s.coins_earned_this_run - s.tasks_coins)
            chk_c = f"+{checkin_this_run} 🪙" if checkin_this_run > 0 else "0"
            lines.append(
                f"| **{safe_name}** | {pc_st} | {pc_c} | {mob_st} | {mob_c} | {strk} | {chk_c} |"
            )
        lines.append("")

        # 2. Деталізація завдань MTOP
        has_task_records = any(bool(s.task_details) for s in self.summaries)
        has_tasks_done = any(isinstance(s.tasks_done, (int, float)) and s.tasks_done > 0 for s in self.summaries)
        has_tasks_skipped = any(isinstance(s.tasks_skipped, (int, float)) and s.tasks_skipped > 0 for s in self.summaries)
        has_tasks_failed = any(isinstance(s.tasks_failed, (int, float)) and s.tasks_failed > 0 for s in self.summaries)
        if has_task_records or has_tasks_done or has_tasks_skipped or has_tasks_failed:
            lines.extend([
                "### 🛍️ Деталізація інтерактивних завдань (MTOP)",
                "",
                "| Акаунт | Завдання | Статус | Монети | Коментар / Причина |",
                "| :--- | :--- | :---: | :---: | :--- |"
            ])
            for s in self.summaries:
                safe_name = html.escape(str(s.name or "Account")).replace("|", "\\|")
                if s.task_details and isinstance(s.task_details, list):
                    for t in s.task_details:
                        if not isinstance(t, dict):
                            continue
                        t_title = html.escape(str(t.get("title") or t.get("task_id") or "Завдання")).replace("|", "\\|")
                        t_st_raw = t.get("status", "COMPLETED")
                        if t_st_raw == "COMPLETED":
                            t_icon = "✅ Виконано"
                        elif t_st_raw in ("ALREADY_DONE", "DONE"):
                            t_icon = "ℹ️ Вже виконано"
                        elif t_st_raw == "SKIPPED":
                            t_icon = "⏭️ Пропущено"
                        else:
                            t_icon = "❌ Збій"
                        t_c_val = t.get("coins", 0)
                        t_coins = f"+{t_c_val} 🪙" if isinstance(t_c_val, (int, float)) and t_c_val > 0 else "-"
                        t_reason = html.escape(str(t.get("reason", "-"))).replace("|", "\\|")
                        lines.append(f"| **{safe_name}** | {t_title} | {t_icon} | {t_coins} | {t_reason} |")
                elif isinstance(s.tasks_done, (int, float)) and s.tasks_done > 0:
                    t_c = int(s.tasks_coins) if isinstance(s.tasks_coins, (int, float)) else 0
                    lines.append(f"| **{safe_name}** | Виконані безпечні завдання ({s.tasks_done}) | ✅ Виконано | +{t_c} 🪙 | Зараховано |")
                elif s.checkin_status == "CAPTCHA_REQUIRED":
                    lines.append(f"| **{safe_name}** | Завдання платформи | ⏭️ Пропущено | - | Пропущено через блокування капчею WAF |")
                elif not s.session_valid or s.checkin_status == "SESSION_EXPIRED":
                    lines.append(f"| **{safe_name}** | Завдання платформи | ❌ Пропущено | - | Пропущено через недійсну сесію акаунта |")
                elif isinstance(s.tasks_skipped, (int, float)) and s.tasks_skipped > 0:
                    lines.append(f"| **{safe_name}** | Завдання платформи | ⏭️ Пропущено | - | Пропущено {s.tasks_skipped} завдань за фільтрами безпеки |")
                else:
                    lines.append(f"| **{safe_name}** | Завдання платформи | ℹ️ Немає | - | Завдання не виконувалися |")
            lines.append("")

        return "\n".join(lines)

    def generate_step_summary(self) -> Optional[str]:
        """
        Генерує багатий структурований Markdown-звіт та записує його у $GITHUB_STEP_SUMMARY.
        Повертає згенерований текст або None, якщо змінна оточення відсутня.
        """
        summary_file = os.getenv("GITHUB_STEP_SUMMARY")
        if not summary_file:
            logger.debug("GITHUB_STEP_SUMMARY не визначено (не в GitHub Actions runner).")
            return None

        content = self.format_step_summary()
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(content + "\n")
            logger.info("📄 Звіт успішно записано у GITHUB_STEP_SUMMARY.")
        except Exception as e:
            logger.warning(f"Не вдалося записати звіт у GITHUB_STEP_SUMMARY: {e}")

        return content

    def send_telegram_notification(self, detailed: Optional[bool] = None) -> bool:
        """
        Надсилає структуроване та детальне сповіщення у Telegram (якщо вказано токен та chat_id).
        За замовчуванням генерує стислий звіт без переліку рутинних тасок та невдалих купонів,
        із відображенням фіатного еквівалента зароблених та накопичених монет (USD + UAH).
        Детальний перелік тасок і купонів доступний через параметр detailed=True або
        змінну середовища TELEGRAM_DETAILED_TASKS=1.
        """
        if not self.telegram_bot_token or not self.telegram_chat_id:
            logger.debug("Telegram credentials не налаштовані, сповіщення пропущено.")
            return False

        if detailed is None:
            env_val = os.getenv("TELEGRAM_DETAILED_TASKS", "").strip().lower()
            is_detailed = env_val in ("1", "true", "yes")
        else:
            is_detailed = bool(detailed)

        total_accounts = len(self.summaries)
        successful_accounts = sum(
            1 for s in self.summaries
            if s.checkin_status in ("SUCCESS", "ALREADY_DONE") and s.session_valid
        )
        total_coins = sum(s.coins_earned_this_run for s in self.summaries)
        total_checkin_coins = sum(max(0, s.coins_earned_this_run - s.tasks_coins) for s in self.summaries)
        total_tasks_coins = sum(int(s.tasks_coins) if isinstance(s.tasks_coins, (int, float)) else 0 for s in self.summaries)
        has_expired = any(
            not s.session_valid
            or s.checkin_status == "SESSION_EXPIRED"
            or s.pc_status == "SESSION_EXPIRED"
            or s.mobile_status == "SESSION_EXPIRED"
            for s in self.summaries
        )
        has_captcha = any(
            s.checkin_status == "CAPTCHA_REQUIRED"
            or s.pc_status == "CAPTCHA_REQUIRED"
            or s.mobile_status == "CAPTCHA_REQUIRED"
            for s in self.summaries
        )

        # Перевірка на свіжі дії або проблеми для уникнення повторного спаму в Telegram при рознесеному розкладі
        fresh_action = any(
            s.checkin_status == "SUCCESS"
            or s.pc_status == "SUCCESS"
            or s.mobile_status == "SUCCESS"
            or (isinstance(s.tasks_done, (int, float)) and s.tasks_done > 0)
            or (isinstance(s.tasks_coins, (int, float)) and s.tasks_coins > 0)
            or s.coins_earned_this_run > 0
            for s in self.summaries
        )
        has_problem = (
            has_expired
            or has_captcha
            or (total_accounts == 0)
            or (successful_accounts < total_accounts)
            or any(s.aggregate_status in ("PARTIAL", "FAILED", "ACTION_REQUIRED") for s in self.summaries)
            or any(bool(s.error_message) for s in self.summaries)
            or any(s.checkin_status in ("FAILED", "SESSION_EXPIRED", "CAPTCHA_REQUIRED") for s in self.summaries)
            or any(s.mobile_status == "FAILED" for s in self.summaries)
            or any(s.tasks_failed > 0 for s in self.summaries)
        )

        is_ci = os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("CI") == "true"
        event_name = os.getenv("GITHUB_EVENT_NAME", "").strip().lower()
        skip_sleep = os.getenv("SKIP_RANDOM_SLEEP", "").strip().lower() in ("1", "true", "yes")
        is_manual = event_name in ("workflow_dispatch", "push", "pull_request") or (
            event_name != "schedule" and (skip_sleep or not is_ci)
        )
        notify_always = os.getenv("TELEGRAM_NOTIFY_ALWAYS", "").strip().lower() in ("1", "true", "yes")

        automatic = (event_name == "schedule") and not is_manual and not notify_always
        notification_state = {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "chat_id": self.telegram_chat_id,
            "total_accounts": total_accounts,
            "problems": [
                {
                    "name": s.name,
                    "statuses": [
                        status if status in ("FAILED", "SESSION_EXPIRED", "CAPTCHA_REQUIRED") else None
                        for status in (s.checkin_status, s.pc_status, s.mobile_status, s.tasks_status)
                    ],
                    "session_valid": s.session_valid,
                    "tasks_failed": s.tasks_failed,
                    "failed_tasks": sorted(
                        str(t.get("task_id") or t.get("title") or "")
                        for t in s.task_details
                        if isinstance(t, dict) and t.get("status") == "FAILED"
                    ),
                    "error_message": s.error_message,
                }
                for s in sorted(self.summaries, key=lambda s: s.name) if s.has_problem
            ],
        }
        if automatic:
            if total_accounts > 0 and not fresh_action and not has_problem:
                logger.info("ℹ️ Усі акаунти вже були успішно оброблені сьогодні (ALREADY_DONE), повторне сповіщення в Telegram пропущено.")
                return False
            if not fresh_action:
                try:
                    with open(self.notification_state_path, encoding="utf-8") as f:
                        if json.load(f) == notification_state:
                            logger.info("ℹ️ Ці проблеми вже повідомлено сьогодні; повторне сповіщення пропущено.")
                            return False
                except (OSError, ValueError):
                    pass

        if has_expired:
            status_header = "🚨 <b>AliExpress Farmer: Потрібна увага!</b>"
        elif has_captcha:
            status_header = "🧩 <b>AliExpress Farmer: Потрібна капча!</b>"
        elif total_accounts == 0:
            status_header = "ℹ️ <b>AliExpress Daily Farm: Немає акаунтів</b>"
        elif successful_accounts == 0 and total_accounts > 0:
            status_header = "❌ <b>AliExpress Daily Farm: Збій!</b>"
        elif successful_accounts < total_accounts or any(s.aggregate_status == "PARTIAL" for s in self.summaries):
            status_header = "⚠️ <b>AliExpress Daily Farm: Частковий успіх</b>"
        else:
            status_header = "🚀 <b>AliExpress Daily Farm: Звіт</b>"

        rate = get_usd_to_uah_rate()
        fiat_today = _format_fiat_equiv(total_coins, rate=rate, show_sign=True)

        msg_lines = [
            status_header,
            f"👤 Акаунтів: <b>{successful_accounts}/{total_accounts}</b>",
        ]
        if total_coins > 0:
            coins_line = f"🪙 Зароблено за запуск: <b>+{total_coins} 🪙</b> (<b>{fiat_today}</b>)"
        else:
            coins_line = f"🪙 Зароблено за запуск: <b>0 🪙</b> (<b>{fiat_today}</b>)"

        if total_checkin_coins > 0 or total_tasks_coins > 0:
            coins_line += f" <i>(чекін: +{total_checkin_coins}, таски: +{total_tasks_coins})</i>"
        msg_lines.append(coins_line)
        msg_lines.append("")

        # Розміщуємо критичні попередження про сесію та капчу нагорі повідомлення перед списком акаунтів
        if has_expired or has_captcha:
            if has_expired:
                expired_names = [html.escape(s.name) for s in self.summaries if not s.session_valid or s.checkin_status == "SESSION_EXPIRED" or s.pc_status == "SESSION_EXPIRED" or s.mobile_status == "SESSION_EXPIRED"]
                msg_lines.append(f"⚠️ <b>УВАГА: Сесію прострочено:</b> {', '.join(expired_names)}. Оновіть cookies у секретах!")
            if has_captcha:
                captcha_names = [html.escape(s.name) for s in self.summaries if s.checkin_status == "CAPTCHA_REQUIRED" or s.pc_status == "CAPTCHA_REQUIRED" or s.mobile_status == "CAPTCHA_REQUIRED"]
                msg_lines.append(f"🧩 <b>УВАГА: Виявлено капчу WAF:</b> {', '.join(captcha_names)}. Пройдіть перевірку в браузері!")
            msg_lines.append("")

        for s in self.summaries:
            agg_st = s.aggregate_status
            if agg_st == "OK":
                status_badge = "✅"
            elif agg_st == "PARTIAL":
                status_badge = "⚠️"
            elif agg_st == "ACTION_REQUIRED":
                status_badge = "🧩" if (s.checkin_status == "CAPTCHA_REQUIRED" or s.pc_status == "CAPTCHA_REQUIRED" or s.mobile_status == "CAPTCHA_REQUIRED") else "🚨"
            else:
                status_badge = "❌"

            safe_name = html.escape(str(s.name or "Account"))
            detail = f"{self._checkin_icon(s.checkin_status)}"
            streak_v = int(s.streak_days) if isinstance(s.streak_days, (int, float)) else 0
            if streak_v > 0:
                detail += f", стрік {streak_v} дн."
            earned_v = s.coins_earned_this_run
            if earned_v > 0:
                detail += f", +{earned_v} монет"
            tasks_v = int(s.tasks_done) if isinstance(s.tasks_done, (int, float)) else 0
            if tasks_v > 0:
                detail += f", тасок: {tasks_v}"
            if not s.session_valid:
                detail += " (СЕСІЯ ПРОСТРОЧЕНА!)"
            if s.error_message:
                detail += f" ({s.error_message})"

            msg_lines.append(f"{status_badge} <b>{safe_name}</b>: {html.escape(detail)}")

            # Розширені деталі для акаунту
            sub_lines = []

            # 1. Початковий та фінальний баланс монет + фіатний еквівалент
            if s.total_coins is not None:
                fiat_total = _format_fiat_equiv(s.total_coins, rate=rate, show_sign=False)
                verified_note = ""
                if not getattr(s, "balance_verified", True) and s.tasks_coins > 0:
                    verified_note = f" (бонус ~+{s.tasks_coins} 🪙 очікує підтвердження)"

                if s.initial_coins is not None:
                    actual_diff = int(s.total_coins) - int(s.initial_coins)
                    diff_str = f"+{actual_diff}" if actual_diff > 0 else str(actual_diff)
                    reward_str = f"+{earned_v}" if earned_v > 0 else str(earned_v)
                    if actual_diff != earned_v:
                        sub_lines.append(
                            f"   💰 Баланс: <code>{_format_coins(s.initial_coins)}</code> ➔ <b>{_format_coins(s.total_coins)} 🪙</b> "
                            f"(зміна балансу: <b>{diff_str}</b>, нагороди за запуск: <b>{reward_str}</b>){verified_note} • <b>{fiat_total}</b>"
                        )
                    elif earned_v > 0 or actual_diff != 0:
                        sub_lines.append(
                            f"   💰 Баланс: <code>{_format_coins(s.initial_coins)}</code> ➔ <b>{_format_coins(s.total_coins)} 🪙</b> "
                            f"(<b>{diff_str}</b>){verified_note} • <b>{fiat_total}</b>"
                        )
                    else:
                        sub_lines.append(f"   💰 Баланс: <b>{_format_coins(s.total_coins)} 🪙</b>{verified_note} • <b>{fiat_total}</b>")
                else:
                    sub_lines.append(f"   💰 Баланс: <b>{_format_coins(s.total_coins)} 🪙</b>{verified_note} • <b>{fiat_total}</b>")
            elif earned_v > 0:
                fiat_earned = _format_fiat_equiv(earned_v, rate=rate, show_sign=True)
                sub_lines.append(f"   💰 Зароблено за запуск: <b>+{earned_v} 🪙</b> (<b>{fiat_earned}</b>) (загальний баланс не отримано)")

            # 2. Двоканальний чекін (PC + Mobile)
            if s.pc_status or s.mobile_status:
                pc_txt = f"🖥️ PC: {self._checkin_icon(s.pc_status or s.checkin_status)}"
                pc_c = int(s.pc_coins) if isinstance(s.pc_coins, (int, float)) else 0
                if pc_c > 0:
                    if (s.pc_status or s.checkin_status) == "ALREADY_DONE":
                        pc_txt += f" (+{pc_c} 🪙 раніше)"
                    else:
                        pc_txt += f" (+{pc_c} 🪙)"
                mob_txt = f"📱 App: {self._checkin_icon(s.mobile_status or s.checkin_status)}"
                mob_c = int(s.mobile_coins) if isinstance(s.mobile_coins, (int, float)) else 0
                if mob_c > 0:
                    if (s.mobile_status or s.checkin_status) == "ALREADY_DONE":
                        mob_txt += f" (+{mob_c} 🪙 раніше)"
                    else:
                        mob_txt += f" (+{mob_c} 🪙)"
                sub_lines.append(f"   ├ {pc_txt}\n   └ {mob_txt}")

            # 3. Інтерактивні таски MTOP
            tasks_c_v = int(s.tasks_coins) if isinstance(s.tasks_coins, (int, float)) else 0
            tasks_sk_v = int(s.tasks_skipped) if isinstance(s.tasks_skipped, (int, float)) else 0
            tasks_f_v = int(s.tasks_failed) if isinstance(s.tasks_failed, (int, float)) else 0

            tasks_already_done = int(s.tasks_already_done) if isinstance(s.tasks_already_done, (int, float)) else 0
            if s.task_details and isinstance(s.task_details, list):
                tasks_already_done = sum(
                    1 for t in s.task_details
                    if isinstance(t, dict) and t.get("status") in ("ALREADY_DONE", "DONE")
                )
                tasks_sk_v = sum(
                    1 for t in s.task_details
                    if isinstance(t, dict) and t.get("status") == "SKIPPED"
                )
            elif tasks_already_done > 0:
                tasks_sk_v = max(0, tasks_sk_v - tasks_already_done)

            if tasks_v > 0 or tasks_c_v > 0 or tasks_already_done > 0 or tasks_sk_v > 0 or tasks_f_v > 0 or s.task_details:
                task_parts = [f"<b>{tasks_v}</b> вик."]
                if tasks_c_v > 0:
                    task_parts[0] += f" (+{tasks_c_v} 🪙)"
                if tasks_already_done > 0:
                    task_parts.append(f"{tasks_already_done} вже вик.")
                if tasks_sk_v > 0:
                    task_parts.append(f"{tasks_sk_v} пропущ.")
                if tasks_f_v > 0:
                    task_parts.append(f"{tasks_f_v} збій")
                sub_lines.append(f"   🛍️ Таски: {', '.join(task_parts)}")

                # Відображення деталей завдань ТІЛЬКИ в детальному режимі
                if is_detailed and s.task_details and isinstance(s.task_details, list):
                    # Групуємо багаторазові виконані таски (timesLimit > 1) за базовою назвою
                    completed_grouped: Dict[str, Dict[str, Any]] = {}
                    other_tasks: List[Dict[str, Any]] = []

                    for t in s.task_details:
                        if not isinstance(t, dict):
                            continue
                        t_st = t.get("status")
                        if t_st == "COMPLETED":
                            raw_title = str(t.get("title") or t.get("task_id") or "Завдання")
                            base_title = re.sub(r"\s*\(\d+/\d+\)$", "", raw_title).strip()
                            c_val = t.get("coins", 0)
                            c_int = int(c_val) if isinstance(c_val, (int, float)) else 0
                            if base_title not in completed_grouped:
                                completed_grouped[base_title] = {
                                    "title": base_title,
                                    "status": "COMPLETED",
                                    "count": 1,
                                    "coins": c_int,
                                    "reason": t.get("reason", "")
                                }
                            else:
                                completed_grouped[base_title]["count"] += 1
                                completed_grouped[base_title]["coins"] += c_int
                        else:
                            other_tasks.append(t)

                    processed_tasks: List[Dict[str, Any]] = []
                    for base_title, grp in completed_grouped.items():
                        cnt = grp["count"]
                        disp_title = f"{base_title} (×{cnt})" if cnt > 1 else base_title
                        processed_tasks.append({
                            "title": disp_title,
                            "status": "COMPLETED",
                            "coins": grp["coins"],
                            "reason": grp["reason"]
                        })
                    processed_tasks.extend(other_tasks)

                    sorted_details = sorted(
                        processed_tasks,
                        key=lambda x: 0 if isinstance(x, dict) and x.get("status") == "COMPLETED"
                        else (1 if isinstance(x, dict) and x.get("status") in ("ALREADY_DONE", "DONE")
                        else (2 if isinstance(x, dict) and x.get("status") == "FAILED" else 3))
                    )
                    shown_tasks = sorted_details[:10]
                    for t in shown_tasks:
                        if not isinstance(t, dict):
                            continue
                        t_title = html.escape(str(t.get("title", "Завдання")))
                        t_st = t.get("status")
                        t_c = t.get("coins", 0)
                        t_r = html.escape(str(t.get("reason", "")))
                        if t_st == "COMPLETED":
                            if isinstance(t_c, (int, float)) and t_c > 0:
                                sub_lines.append(f"     • ✅ <i>{t_title}</i> (+{t_c} 🪙)")
                            else:
                                t_note = f" ({t_r})" if t_r and t_r != "-" else ""
                                sub_lines.append(f"     • ✅ <i>{t_title}</i>{t_note}")
                        elif t_st in ("ALREADY_DONE", "DONE"):
                            sub_lines.append(f"     • ℹ️ <i>{t_title}</i> ({t_r})")
                        elif t_st == "SKIPPED":
                            sub_lines.append(f"     • ⏭️ <i>{t_title}</i> ({t_r})")
                        else:
                            sub_lines.append(f"     • ⚠️ <i>{t_title}</i> ({t_r})")
                    if len(sorted_details) > len(shown_tasks):
                        sub_lines.append(f"     • <i>...ще {len(sorted_details) - len(shown_tasks)} завдань</i>")

            if sub_lines:
                msg_lines.extend(sub_lines)

        if has_expired:
            msg_lines.extend([
                "",
                "⚠️ <i>Будь ласка, оновіть cookies у GitHub Secrets, щоб не втратити стрік щоденних нагород!</i>"
            ])
        if has_captcha:
            msg_lines.extend([
                "",
                "🧩 <i>Виявлено захист Alibaba WAF! Відкрийте AliExpress у браузері та пройдіть слайдер перевірки.</i>"
            ])
        if any(not getattr(s, "balance_verified", True) and s.tasks_coins > 0 for s in self.summaries):
            msg_lines.extend([
                "",
                "⚠️ <i>Пост-баланс не верифіковано сервером (бонуси за завдання вказані орієнтовно).</i>"
            ])

        text = "\n".join(msg_lines)

        # Безпечне скорочення для Telegram API з гарантією закриття HTML-тегів
        text = _safe_truncate_html(text, max_length=4000)

        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                logger.info("📬 Сповіщення у Telegram успішно надіслано!")
                if automatic:
                    try:
                        os.makedirs(os.path.dirname(self.notification_state_path), exist_ok=True)
                        with open(self.notification_state_path, "w", encoding="utf-8") as f:
                            json.dump(notification_state, f, ensure_ascii=False)
                    except OSError as e:
                        logger.warning(f"Не вдалося зберегти стан Telegram-сповіщення: {e}")
                return True
            else:
                logger.warning(f"Telegram API повернув помилку: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            err_msg = str(e)
            if self.telegram_bot_token:
                err_msg = err_msg.replace(self.telegram_bot_token, "***")
            logger.warning(f"Помилка при відправці сповіщення у Telegram: {err_msg}")
            return False

    def dispatch(self, detailed: Optional[bool] = None):
        """Виконує відправку у всі налаштовані канали."""
        self.generate_step_summary()
        self.send_telegram_notification(detailed=detailed)
