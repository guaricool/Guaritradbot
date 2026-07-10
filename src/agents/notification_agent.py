"""
Sprint 34 — NotificationAgent (refactored).

What changed vs Sprint 18 version:
  - Live gate: notifications only fire when `notifications.live_only=true`
    AND the bot is currently in live mode (mandate.enabled=true). Reads
    mode_override.json on every call so a dashboard toggle takes effect
    immediately (cheap file read).
  - New `TRADE_CLOSED` handler: when a position is closed (SL/TP hit,
    smart profit take, manual), sends a Telegram message with the
    realized P&L, duration, and close reason.
  - New `POSITION_UPDATE` handler: when the PositionUpdateScheduler emits
    a per-position update (default hourly), formats a P&L progress card
    with current price vs entry, $ and % P&L, distance to SL/TP, and
    duration.
  - Removed `TRADES_EXECUTED` handler — replaced with the per-trade
    `TRADE_OPENED` handler (called when the position repo actually adds
    a position, not when the ExecutionNode reports a fill — fills and
    position opens can diverge on dry-runs / partial fills).

The `TRADES_EXECUTED` event is still published by ExecutionNode for
backward compat with any other subscribers (audit log readers, etc).
"""
import json
import logging
import os
import platform
import socket
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NotificationAgent")


def _is_live_mode(override_path: str = "audit/mode_override.json") -> bool:
    """Read mode_override.json and return True if mandate_enabled is on.

    Falls back to False on any error (file missing, JSON malformed) so
    the default behavior is "no live notifications" — safer than the
    inverse (would spam Carlos with paper-trade pings in production).
    """
    try:
        if not os.path.exists(override_path):
            return False
        with open(override_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("mandate_enabled", False))
    except Exception:
        return False


class NotificationAgent:
    def __init__(self, event_bus, config, audit=None, mode_override_path="audit/mode_override.json"):
        self.event_bus = event_bus
        self.audit = audit
        notif_cfg = config.get("notifications", {}) if config else {}
        self.enabled = bool(notif_cfg.get("enabled", False))
        self.live_only = bool(notif_cfg.get("live_only", True))
        # Skip P&L updates below this USD threshold
        self.position_update_min_pnl_usd = float(
            notif_cfg.get("position_update_min_pnl_usd", 0.0)
        )
        self.mode_override_path = mode_override_path

        load_dotenv()
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if self.enabled and (not self.bot_token or not self.chat_id):
            logger.warning(
                "Telegram not configured (missing TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID). Notifications will be skipped."
            )

        # Subscribe to all the events we care about
        if self.event_bus:
            self.event_bus.subscribe("TRADE_OPENED", self.handle_trade_opened)
            self.event_bus.subscribe("TRADE_CLOSED", self.handle_trade_closed)
            self.event_bus.subscribe("POSITION_UPDATE", self.handle_position_update)
            # Keep SYSTEM_ERROR for critical alerts (always notify, even in paper)
            self.event_bus.subscribe("SYSTEM_ERROR", self.handle_error)

    # ------------------------------------------------------------------
    #  Gate
    # ------------------------------------------------------------------
    def _should_notify(self) -> bool:
        """Master gate: enabled + (not live_only OR is_live_mode)."""
        if not self.enabled:
            return False
        if self.live_only and not _is_live_mode(self.mode_override_path):
            return False
        return True

    # ------------------------------------------------------------------
    #  Telegram transport
    # ------------------------------------------------------------------
    def send_telegram_message(self, text: str) -> bool:
        """Send a message via Telegram Bot API. Returns True on success."""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured. Skipping notification.")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            response = requests.post(url, json=payload, timeout=5)
            if response.status_code != 200:
                logger.error(
                    "Telegram send failed [%d]: %s",
                    response.status_code, response.text[:200],
                )
                return False
            return True
        except Exception as e:
            logger.error("Telegram send error: %s", e)
            return False

    # ------------------------------------------------------------------
    #  Event handlers
    # ------------------------------------------------------------------
    def handle_trade_opened(self, event_data: dict):
        """Notify when a new position is opened (TRADE_OPENED event)."""
        if not self._should_notify():
            return

        asset = event_data.get("asset", "?")
        direction = event_data.get("direction", "?").upper()
        entry_price = float(event_data.get("entry_price", 0))
        qty = float(event_data.get("qty", 0))
        stop_loss = float(event_data.get("stop_loss", 0))
        take_profit = float(event_data.get("take_profit", 0))
        risk_usd = float(event_data.get("risk_usd", 0))
        notional = abs(entry_price * qty)
        strategy = event_data.get("strategy", "?")
        position_id = event_data.get("position_id", "?")

        # SL/TP distance in % for quick visual risk/reward
        if direction == "LONG" and entry_price > 0:
            sl_pct = ((entry_price - stop_loss) / entry_price) * 100
            tp_pct = ((take_profit - entry_price) / entry_price) * 100
        elif direction == "SHORT" and entry_price > 0:
            sl_pct = ((stop_loss - entry_price) / entry_price) * 100
            tp_pct = ((entry_price - take_profit) / entry_price) * 100
        else:
            sl_pct = tp_pct = 0.0

        msg = (
            "🟢 <b>NUEVA ENTRADA — LIVE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>Par:</b> {asset}\n"
            f"🔹 <b>Dirección:</b> {direction}\n"
            f"🔹 <b>Precio entrada:</b> ${entry_price:,.2f}\n"
            f"🔹 <b>Cantidad:</b> {qty:.6f}\n"
            f"🔹 <b>Notional:</b> ${notional:.2f}\n"
            f"🔹 <b>Stop Loss:</b> ${stop_loss:,.2f} (-{sl_pct:.2f}%)\n"
            f"🔹 <b>Take Profit:</b> ${take_profit:,.2f} (+{tp_pct:.2f}%)\n"
            f"🔹 <b>Riesgo:</b> ${risk_usd:.2f}\n"
            f"🔹 <b>Estrategia:</b> {strategy}\n"
            f"🔹 <b>ID:</b> <code>{position_id[:24]}</code>\n"
        )
        self.send_telegram_message(msg)

    def handle_trade_closed(self, event_data: dict):
        """Notify when a position is closed (TRADE_CLOSED event)."""
        if not self._should_notify():
            return

        asset = event_data.get("asset", "?")
        pnl_usd = float(event_data.get("pnl_usd") or 0.0)
        reason = event_data.get("reason", "?")
        entry_price = float(event_data.get("entry_price") or 0)
        close_price = float(event_data.get("close_price") or 0)
        direction = event_data.get("direction", "?").upper()
        duration_s = float(event_data.get("duration_s") or 0)
        duration_h = duration_s / 3600.0

        if entry_price > 0:
            pnl_pct = (pnl_usd / (entry_price * float(event_data.get("qty") or 1))) * 100
        else:
            pnl_pct = 0.0

        emoji = "✅" if pnl_usd >= 0 else "❌"
        sign = "+" if pnl_usd >= 0 else "-"
        pnl_str = f"{sign}${abs(pnl_usd):.2f}"
        pct_str = f"{sign}{abs(pnl_pct):.2f}%"

        msg = (
            f"{emoji} <b>POSICIÓN CERRADA — LIVE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>Par:</b> {asset}\n"
            f"🔹 <b>Dirección:</b> {direction}\n"
            f"🔹 <b>Precio entrada:</b> ${entry_price:,.2f}\n"
            f"🔹 <b>Precio cierre:</b> ${close_price:,.2f}\n"
            f"🔹 <b>P&L realizado:</b> <b>{pnl_str} ({pct_str})</b>\n"
            f"🔹 <b>Duración:</b> {duration_h:.1f}h\n"
            f"🔹 <b>Razón:</b> {reason}\n"
        )
        self.send_telegram_message(msg)

    def handle_position_update(self, event_data: dict):
        """Hourly P&L progress card (POSITION_UPDATE event)."""
        if not self._should_notify():
            return

        pnl_usd = float(event_data.get("unrealized_pnl_usd") or 0.0)
        # Skip dust updates
        if abs(pnl_usd) < self.position_update_min_pnl_usd:
            return

        asset = event_data.get("asset", "?")
        direction = event_data.get("direction", "?").upper()
        entry_price = float(event_data.get("entry_price") or 0)
        current_price = float(event_data.get("current_price") or 0)
        pnl_pct = float(event_data.get("unrealized_pnl_pct") or 0.0)
        duration_h = float(event_data.get("duration_hours") or 0.0)
        stop_loss = float(event_data.get("stop_loss") or 0)
        take_profit = float(event_data.get("take_profit") or 0)

        # Distance to SL/TP
        if current_price > 0:
            if direction == "LONG":
                dist_sl = ((current_price - stop_loss) / current_price) * 100
                dist_tp = ((take_profit - current_price) / current_price) * 100
            else:
                dist_sl = ((stop_loss - current_price) / current_price) * 100
                dist_tp = ((current_price - take_profit) / current_price) * 100
        else:
            dist_sl = dist_tp = 0.0

        # Color cue: 📈 winning, 📉 losing
        emoji = "📈" if pnl_usd >= 0 else "📉"
        sign = "+" if pnl_usd >= 0 else "-"
        pnl_str = f"{sign}${abs(pnl_usd):.2f}"
        pct_str = f"{sign}{abs(pnl_pct):.2f}%"
        progress_bar = self._progress_bar(pnl_pct)

        msg = (
            f"{emoji} <b>UPDATE ({duration_h:.1f}h) — LIVE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>Par:</b> {asset} ({direction})\n"
            f"🔹 <b>Entrada:</b> ${entry_price:,.2f}\n"
            f"🔹 <b>Actual:</b> ${current_price:,.2f}\n"
            f"🔹 <b>P&L:</b> <b>{pnl_str} ({pct_str})</b>\n"
            f"🔹 {progress_bar}\n"
            f"🔹 <b>A SL:</b> {dist_sl:.2f}%  |  <b>A TP:</b> {dist_tp:.2f}%\n"
        )
        self.send_telegram_message(msg)

    def handle_error(self, event_data: dict):
        """Critical error notification. Always fires, even in paper mode."""
        if not self.enabled:
            return

        error_msg = event_data.get("error", "Error desconocido")
        msg = f"⚠️ <b>Alerta de Error Crítico</b>\n\n{error_msg}"
        self.send_telegram_message(msg)

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _progress_bar(pct: float, width: int = 10) -> str:
        """ASCII bar showing P&L %. 0% = 5 empty, 100% = all filled, negative mirrors."""
        pct_clamped = max(min(pct, 100.0), -100.0)
        if pct_clamped >= 0:
            filled = int((pct_clamped / 100.0) * width)
            return "[" + "█" * filled + "░" * (width - filled) + "]"
        else:
            filled = int((abs(pct_clamped) / 100.0) * width)
            return "[" + "░" * (width - filled) + "█" * filled + "] (short)"

    # ------------------------------------------------------------------
    #  Smoke test (CLI: --test-telegram)
    # ------------------------------------------------------------------
    def send_test_message(self) -> bool:
        """Send a one-shot test message to verify Telegram wiring.

        Used by `python main.py --test-telegram` to confirm:
          - TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are present in .env
          - The bot can actually reach the Telegram API
          - The chat ID is the one Carlos expects (not a stale value)

        Bypasses the live gate (we WANT a message even in paper mode —
        that's the whole point of the test). Also includes diagnostic
        info (hostname, mode override, current time) so Carlos can tell
        at a glance which container sent the message.

        Returns True if Telegram accepted the message (HTTP 200).
        """
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "unknown-host"

        is_live = _is_live_mode(self.mode_override_path)
        mode_label = "🟢 LIVE" if is_live else "🟡 PAPER"

        token_preview = (
            f"***{self.bot_token[-6:]}" if self.bot_token and len(self.bot_token) > 6
            else "❌ MISSING"
        )
        chat_preview = (
            str(self.chat_id) if self.chat_id else "❌ MISSING"
        )

        msg = (
            "🤖 <b>Guaritradbot — Telegram Test</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>Host:</b> <code>{hostname}</code>\n"
            f"🔹 <b>Plataforma:</b> {platform.system()}\n"
            f"🔹 <b>Modo actual:</b> {mode_label}\n"
            f"🔹 <b>Hora:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🔹 <b>Notificaciones:</b> "
            f"{'✅ enabled' if self.enabled else '❌ disabled'}\n"
            f"🔹 <b>Live only:</b> {'sí' if self.live_only else 'no'}\n"
            f"🔹 <b>Token:</b> <code>{token_preview}</code>\n"
            f"🔹 <b>Chat ID:</b> <code>{chat_preview}</code>\n"
            "\n✅ <i>Si ves este mensaje, Telegram está bien configurado.</i>\n"
        )
        return self.send_telegram_message(msg)
