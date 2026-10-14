import os
import sys
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
import requests

logger = logging.getLogger(__name__)


@dataclass
class AccountSummary:
    """Підсумкові дані обробки одного акаунта."""
    name: str
    checkin_status: str  # "SUCCESS", "ALREADY_DONE", "FAILED", "SESSION_EXPIRED"
    coins_earned: int = 0
    streak_days: int = 0
    total_coins: Optional[int] = None
    tasks_done: int = 0
    coupons_claimed: int = 0
    session_valid: bool = True
    error_message: Optional[str] = None


class NotificationDispatcher:
    """
    Керує відправкою сповіщень:
    1. Генерація багатого Markdown-звіту у $GITHUB_STEP_SUMMARY для таби Actions.
    2. Відправка повідомлень у Telegram через бота (за наявності токену та chat_id).
    """

    def __init__(self):
        self.summaries: List[AccountSummary] = []
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    def add_summary(self, summary: AccountSummary):
        self.summaries.append(summary)

    def _checkin_icon(self, status: str) -> str:
        if status == "SUCCESS":
            return "✅ Зараховано"
        if status == "ALREADY_DONE":
            return "ℹ️ Вже був"
        if status == "SESSION_EXPIRED":
            return "❌ Прострочено"
        return "⚠️ Збій"

    def _session_icon(self, valid: bool) -> str:
        return "🟢 Активна" if valid else "🔴 Недійсна"

    def generate_step_summary(self) -> Optional[str]:
        """
        Генерує Markdown-таблицю та записує її у $GITHUB_STEP_SUMMARY.
        """
        summary_file = os.getenv("GITHUB_STEP_SUMMARY")
        if not summary_file:
            logger.debug("GITHUB_STEP_SUMMARY не визначено (не в GitHub Actions runner).")
            return None

        total_accounts = len(self.summaries)
        successful_accounts = sum(
            1 for s in self.summaries
            if (s.checkin_status in ("SUCCESS", "ALREADY_DONE") or s.tasks_done > 0) and s.session_valid
        )
        total_coins_earned = sum(s.coins_earned for s in self.summaries)
        total_coupons = sum(s.coupons_claimed for s in self.summaries)
        has_expired = any(not s.session_valid or s.checkin_status == "SESSION_EXPIRED" for s in self.summaries)

        lines = [
            "# 🪙 AliExpress Farm Summary Report",
            "",
            f"> **Статус обробки:** {'🟢 Успішно' if successful_accounts == total_accounts and total_accounts > 0 else ('🔴 Потрібна увага' if has_expired else '🟡 Частково')} | "
            f"**Акаунтів:** {successful_accounts}/{total_accounts} | "
            f"**Зароблено монет сьогодні:** +{total_coins_earned} 🪙 | "
            f"**Купонів зібрано:** {total_coupons} 🎟️",
            ""
        ]

        if has_expired:
            lines.extend([
                "> [!CRITICAL]",
                "> Виявлено акаунти з недійсною або простроченою сесією! Необхідно оновити секрети в репозиторії, щоб уникнути втрати щоденного стріку.",
                ""
            ])

        lines.extend([
            "### 📊 Результати по акаунтах",
            "",
            "| Акаунт | Чекін | Стрік | Сьогодні | Всього монет | Таски | Купони | Сесія |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |"
        ])

        for s in self.summaries:
            streak_str = f"🔥 {s.streak_days} дн." if s.streak_days > 0 else "-"
            earned_str = f"+{s.coins_earned} 🪙" if s.coins_earned > 0 else "0"
            total_str = f"{s.total_coins:,} 🪙" if s.total_coins is not None else "-"
            tasks_str = f"{s.tasks_done}"
            coupons_str = f"{s.coupons_claimed}"
            lines.append(
                f"| **{s.name}** | {self._checkin_icon(s.checkin_status)} | {streak_str} | {earned_str} | {total_str} | {tasks_str} | {coupons_str} | {self._session_icon(s.session_valid)} |"
            )

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
        Надсилає сповіщення у Telegram (якщо вказано TELEGRAM_BOT_TOKEN та TELEGRAM_CHAT_ID).
        """
        if not self.telegram_bot_token or not self.telegram_chat_id:
            logger.debug("Telegram credentials не налаштовані, сповіщення пропущено.")
            return False

        total_accounts = len(self.summaries)
        successful_accounts = sum(
            1 for s in self.summaries
            if (s.checkin_status in ("SUCCESS", "ALREADY_DONE") or s.tasks_done > 0) and s.session_valid
        )
        total_coins = sum(s.coins_earned for s in self.summaries)
        has_expired = any(not s.session_valid or s.checkin_status == "SESSION_EXPIRED" for s in self.summaries)

        status_header = "🚨 <b>AliExpress Farmer: Потрібна увага!</b>" if has_expired else "🚀 <b>AliExpress Daily Farm: Звіт</b>"

        msg_lines = [
            status_header,
            f"👤 Акаунтів: <b>{successful_accounts}/{total_accounts}</b>",
            f"🪙 Зароблено монет: <b>+{total_coins}</b>",
            ""
        ]

        for s in self.summaries:
            status_badge = "✅" if s.checkin_status in ("SUCCESS", "ALREADY_DONE") and s.session_valid else "❌"
            detail = f"{self._checkin_icon(s.checkin_status)}"
            if s.streak_days > 0:
                detail += f", стрік {s.streak_days} дн."
            if s.coins_earned > 0:
                detail += f", +{s.coins_earned} монет"
            if not s.session_valid:
                detail += " (СЕСІЯ ПРОСТРОЧЕНА!)"

            msg_lines.append(f"{status_badge} <b>{s.name}</b>: {detail}")

        if has_expired:
            msg_lines.extend([
                "",
                "⚠️ <i>Будь ласка, оновіть cookies у GitHub Secrets, щоб не втратити стрік щоденних нагород!</i>"
            ])

        text = "\n".join(msg_lines)
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
