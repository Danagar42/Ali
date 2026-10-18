import os
import sys
import re
import html
import logging
from dataclasses import dataclass, field
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
    streak_days: int = 0
    total_coins: Optional[int] = None
    tasks_done: int = 0
    coupons_claimed: int = 0
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
    tasks_coins: int = 0
    tasks_already_done: int = 0
    tasks_skipped: int = 0
    tasks_failed: int = 0
    task_details: List[Dict[str, Any]] = field(default_factory=list)
    coupon_details: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        # Автоматично вираховуємо initial_coins, якщо він не переданий явно, але відомі total_coins
        if self.initial_coins is None and isinstance(self.total_coins, (int, float)):
            earned = int(self.coins_earned) if isinstance(self.coins_earned, (int, float)) else 0
            self.initial_coins = max(0, int(self.total_coins) - earned)
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


class NotificationDispatcher:
    """
    Керує відправкою сповіщень:
    1. Генерація точного та деталізованого Markdown-звіту у $GITHUB_STEP_SUMMARY для GitHub Actions.
    2. Відправка структурованого інформативного повідомлення у Telegram через бота.
    """

    def __init__(self):
        self.summaries: List[AccountSummary] = []
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

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

    def generate_step_summary(self) -> Optional[str]:
        """
        Генерує багатий структурований Markdown-звіт та записує його у $GITHUB_STEP_SUMMARY.
        Містить:
        - Загальний статус та статистику нагород (чекін + таски)
        - Основну зведену таблицю результатів по акаунтах
        - Окрему таблицю Dual-Channel чекіну (PC + Mobile)
        - Деталізацію виконаних, пропущених та збійних інтерактивних завдань MTOP
        - Деталізацію зібраних купонів
        """
        summary_file = os.getenv("GITHUB_STEP_SUMMARY")
        if not summary_file:
            logger.debug("GITHUB_STEP_SUMMARY не визначено (не в GitHub Actions runner).")
            return None

        total_accounts = len(self.summaries)
        successful_accounts = sum(
            1 for s in self.summaries
            if s.checkin_status in ("SUCCESS", "ALREADY_DONE") and s.session_valid
        )
        total_coins_earned = sum(int(s.coins_earned) if isinstance(s.coins_earned, (int, float)) else 0 for s in self.summaries)
        total_checkin_coins = sum(int(s.checkin_coins) if isinstance(s.checkin_coins, (int, float)) else 0 for s in self.summaries)
        total_tasks_coins = sum(int(s.tasks_coins) if isinstance(s.tasks_coins, (int, float)) else 0 for s in self.summaries)
        total_coupons = sum(int(s.coupons_claimed) if isinstance(s.coupons_claimed, (int, float)) else 0 for s in self.summaries)
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
            f"**Зароблено монет сьогодні:** +{total_coins_earned} 🪙 | "
            f"**Купонів зібрано:** {total_coupons} 🎟️",
            f"> **Деталізація нагород:** 🪙 Чекін: +{total_checkin_coins} 🪙 | 🛍️ Завдання: +{total_tasks_coins} 🪙 ({total_tasks_done} вик.)",
            ""
        ]

        if has_expired:
            lines.extend([
                "> [!CRITICAL]",
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
            "| Акаунт | Чекін | Стрік | Сьогодні | Всього монет | Таски | Купони | Сесія |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |"
        ])

        for s in self.summaries:
            streak_val = int(s.streak_days) if isinstance(s.streak_days, (int, float)) else 0
            streak_str = f"🔥 {streak_val} дн." if streak_val > 0 else "-"
            earned_val = int(s.coins_earned) if isinstance(s.coins_earned, (int, float)) else 0
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
            else:
                total_str = "-"
            tasks_str = str(s.tasks_done)
            coupons_str = str(s.coupons_claimed)
            safe_name = html.escape(str(s.name or "Account")).replace("|", "\\|")
            lines.append(
                f"| **{safe_name}** | {self._checkin_icon(s.checkin_status)} | {streak_str} | {earned_str} | {total_str} | {tasks_str} | {coupons_str} | {self._session_icon(s.session_valid)} |"
            )

        lines.append("")

        # Таблиця помилок, якщо такі виникали
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
            chk_c = f"+{int(s.checkin_coins)} 🪙" if isinstance(s.checkin_coins, (int, float)) and s.checkin_coins > 0 else "0"
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

        # 3. Деталізація купонів (якщо є спроби/збір)
        has_coupon_records = any(bool(s.coupon_details) for s in self.summaries)
        has_coupons_done = any(isinstance(s.coupons_claimed, (int, float)) and s.coupons_claimed > 0 for s in self.summaries)
        if has_coupon_records or has_coupons_done:
            lines.extend([
                "### 🎟️ Деталізація зібраних купонів",
                "",
                "| Акаунт | Кампанія / Купон | Статус | Результат |",
                "| :--- | :--- | :---: | :--- |"
            ])
            for s in self.summaries:
                safe_name = html.escape(str(s.name or "Account")).replace("|", "\\|")
                if s.coupon_details and isinstance(s.coupon_details, list):
                    for c in s.coupon_details:
                        if not isinstance(c, dict):
                            continue
                        c_id = html.escape(str(c.get("id", "Купон"))).replace("|", "\\|")
                        c_status = "✅ Зібрано" if c.get("status") == "SUCCESS" else ("ℹ️ Вже зібрано" if c.get("status") == "ALREADY_DONE" else "⚠️ Пропущено/Збій")
                        c_reason = html.escape(str(c.get("reason", "-"))).replace("|", "\\|")
                        lines.append(f"| **{safe_name}** | {c_id} | {c_status} | {c_reason} |")
                elif isinstance(s.coupons_claimed, (int, float)) and s.coupons_claimed > 0:
                    lines.append(f"| **{safe_name}** | Промо-купони ({s.coupons_claimed}) | ✅ Зібрано | Успішно отримано в Coupon Center |")
            lines.append("")

        content = "\n".join(lines)

        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(content + "\n")
            logger.info("📄 Звіт успішно записано у GITHUB_STEP_SUMMARY.")
        except Exception as e:
            logger.warning(f"Не вдалося записати звіт у GITHUB_STEP_SUMMARY: {e}")

        return content

    def send_telegram_notification(self) -> bool:
        """
        Надсилає структуроване та детальне сповіщення у Telegram (якщо вказано токен та chat_id).
        Включає:
        - Загальний статус та розподіл монет (чекін + таски)
        - Початковий та фінальний баланс монет по кожному акаунту
        - Розбивку чекіну по каналах PC та Mobile
        - Список виконаних, пропущених та збійних завдань
        - Зібрані купони
        - Попередження при капчі або прострочених сесіях
        """
        if not self.telegram_bot_token or not self.telegram_chat_id:
            logger.debug("Telegram credentials не налаштовані, сповіщення пропущено.")
            return False

        total_accounts = len(self.summaries)
        successful_accounts = sum(
            1 for s in self.summaries
            if s.checkin_status in ("SUCCESS", "ALREADY_DONE") and s.session_valid
        )
        total_coins = sum(int(s.coins_earned) if isinstance(s.coins_earned, (int, float)) else 0 for s in self.summaries)
        total_checkin_coins = sum(int(s.checkin_coins) if isinstance(s.checkin_coins, (int, float)) else 0 for s in self.summaries)
        total_tasks_coins = sum(int(s.tasks_coins) if isinstance(s.tasks_coins, (int, float)) else 0 for s in self.summaries)
        total_coupons = sum(int(s.coupons_claimed) if isinstance(s.coupons_claimed, (int, float)) else 0 for s in self.summaries)
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

        if has_expired:
            status_header = "🚨 <b>AliExpress Farmer: Потрібна увага!</b>"
        elif has_captcha:
            status_header = "🧩 <b>AliExpress Farmer: Потрібна капча!</b>"
        elif total_accounts == 0:
            status_header = "ℹ️ <b>AliExpress Daily Farm: Немає акаунтів</b>"
        elif successful_accounts == 0 and total_accounts > 0:
            status_header = "❌ <b>AliExpress Daily Farm: Збій!</b>"
        elif successful_accounts < total_accounts:
            status_header = "⚠️ <b>AliExpress Daily Farm: Частковий успіх</b>"
        else:
            status_header = "🚀 <b>AliExpress Daily Farm: Звіт</b>"

        msg_lines = [
            status_header,
            f"👤 Акаунтів: <b>{successful_accounts}/{total_accounts}</b>",
            f"🪙 Зароблено монет: <b>+{total_coins}</b> (чекін: +{total_checkin_coins}, таски: +{total_tasks_coins})",
        ]
        if total_coupons > 0:
            msg_lines.append(f"🎟️ Купонів зібрано: <b>{total_coupons}</b>")
        msg_lines.append("")

        for s in self.summaries:
            if s.checkin_status in ("SUCCESS", "ALREADY_DONE") and s.session_valid:
                status_badge = "✅"
            elif s.checkin_status == "CAPTCHA_REQUIRED" or s.pc_status == "CAPTCHA_REQUIRED" or s.mobile_status == "CAPTCHA_REQUIRED":
                status_badge = "🧩"
            else:
                status_badge = "❌"

            safe_name = html.escape(str(s.name or "Account"))
            detail = f"{self._checkin_icon(s.checkin_status)}"
            streak_v = int(s.streak_days) if isinstance(s.streak_days, (int, float)) else 0
            if streak_v > 0:
                detail += f", стрік {streak_v} дн."
            earned_v = int(s.coins_earned) if isinstance(s.coins_earned, (int, float)) else 0
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

            # 1. Початковий та фінальний баланс монет
            if s.total_coins is not None:
                if s.initial_coins is not None:
                    actual_diff = int(s.total_coins) - int(s.initial_coins)
                    diff_str = f"+{actual_diff}" if actual_diff > 0 else str(actual_diff)
                    day_str = f"+{earned_v}" if earned_v > 0 else str(earned_v)
                    if actual_diff != earned_v:
                        sub_lines.append(
                            f"   💰 Баланс: <code>{_format_coins(s.initial_coins)}</code> ➔ <b>{_format_coins(s.total_coins)} 🪙</b> "
                            f"(за запуск: <b>{diff_str}</b>, за день: <b>{day_str}</b>)"
                        )
                    elif earned_v > 0 or actual_diff != 0:
                        sub_lines.append(
                            f"   💰 Баланс: <code>{_format_coins(s.initial_coins)}</code> ➔ <b>{_format_coins(s.total_coins)} 🪙</b> "
                            f"(<b>{diff_str}</b>)"
                        )
                    else:
                        sub_lines.append(f"   💰 Баланс: <b>{_format_coins(s.total_coins)} 🪙</b>")
                else:
                    sub_lines.append(f"   💰 Баланс: <b>{_format_coins(s.total_coins)} 🪙</b>")
            elif earned_v > 0:
                sub_lines.append(f"   💰 Зароблено сьогодні: <b>+{earned_v} 🪙</b> (загальний баланс не отримано)")

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

                # Відображення деталей завдань (до 10 елементів, пріоритет виконаних)
                if s.task_details and isinstance(s.task_details, list):
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

            # 4. Купони
            coupons_v = int(s.coupons_claimed) if isinstance(s.coupons_claimed, (int, float)) else 0
            if coupons_v > 0:
                sub_lines.append(f"   🎟️ Купони: <b>{coupons_v}</b> зібрано")
                if s.coupon_details and isinstance(s.coupon_details, list):
                    for c in s.coupon_details[:3]:
                        if isinstance(c, dict) and c.get("status") == "SUCCESS":
                            c_name = html.escape(str(c.get("id", "Купон")))
                            sub_lines.append(f"     • 🎟️ <i>{c_name}</i> (✅ отримано)")
            elif s.coupon_details and isinstance(s.coupon_details, list):
                valid_coupons = [c for c in s.coupon_details if isinstance(c, dict)]
                if any(c.get("status") == "FAILED" for c in valid_coupons):
                    f_reason = next((c.get("reason") for c in valid_coupons if c.get("status") == "FAILED" and c.get("reason")), "Збій")
                    sub_lines.append(f"   🎟️ Купони: ⚠️ Не отримано ({html.escape(str(f_reason))})")
                elif valid_coupons and all(c.get("status") == "ALREADY_DONE" for c in valid_coupons):
                    sub_lines.append("   🎟️ Купони: ℹ️ Всі доступні купони вже отримано")
                elif any(c.get("status") == "ALREADY_DONE" for c in valid_coupons):
                    sub_lines.append("   🎟️ Купони: ℹ️ Доступні купони вже отримано раніше")
                elif any(c.get("status") == "SKIPPED" for c in valid_coupons):
                    sk_reason = next((c.get("reason") for c in valid_coupons if c.get("reason")), "Пропущено")
                    sub_lines.append(f"   🎟️ Купони: ⏭️ Пропущено ({html.escape(str(sk_reason))})")

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
                return True
            else:
                logger.warning(f"Telegram API повернув помилку: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            logger.warning(f"Помилка при відправці сповіщення у Telegram: {e}")
            return False

    def dispatch(self):
        """Виконує відправку у всі налаштовані канали."""
        self.generate_step_summary()
        self.send_telegram_notification()
