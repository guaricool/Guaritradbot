import os
import requests
import logging
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NotificationAgent")

class NotificationAgent:
    def __init__(self, event_bus, config):
        self.event_bus = event_bus
        self.enabled = config.get("notifications", {}).get("enabled", False)
        
        load_dotenv()
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        # Subscribe to execution events
        if self.event_bus:
            self.event_bus.subscribe("TRADES_EXECUTED", self.handle_trades_executed)
            self.event_bus.subscribe("SYSTEM_ERROR", self.handle_error)

    def send_telegram_message(self, text):
        if not self.enabled:
            return
            
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID). Skipping notification.")
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        
        try:
            response = requests.post(url, json=payload, timeout=5)
            if response.status_code != 200:
                logger.error(f"Failed to send Telegram message: {response.text}")
        except Exception as e:
            logger.error(f"Error connecting to Telegram API: {e}")

    def handle_trades_executed(self, event_data):
        trades = event_data.get("trades", [])
        if not trades:
            return
            
        msg = "🤖 <b>Guaritradbot Epic - Nueva Operación</b>\n\n"
        for t in trades:
            msg += f"🔹 <b>Par:</b> {t['asset']}\n"
            msg += f"🔹 <b>Estrategia:</b> {t['strategy']}\n"
            msg += f"🔹 <b>Dirección:</b> {t['direction'].upper()}\n"
            msg += f"🔹 <b>Precio:</b> {t['entry_price']:.2f}\n"
            msg += f"🔹 <b>Tamaño:</b> {t['position_size']:.4f}\n\n"
            
        self.send_telegram_message(msg)
        
    def handle_error(self, event_data):
        error_msg = event_data.get("error", "Error desconocido")
        msg = f"⚠️ <b>Alerta de Error Crítico</b>\n\n{error_msg}"
        self.send_telegram_message(msg)
