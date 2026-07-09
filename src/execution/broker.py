import ccxt
import os
from dotenv import load_dotenv

class BrokerClient:
    """
    Cliente genérico para conectarse a un exchange (Binance, Bybit, etc.)
    utilizando CCXT.
    """
    def __init__(self, exchange_name="binance", use_testnet=True):
        load_dotenv()
        
        # Sprint 0 fix: align with .env.example (which already declares BINANCE_*)
        api_key = os.getenv("BINANCE_API_KEY")
        secret = os.getenv("BINANCE_API_SECRET")
        
        # Instanciar el exchange dinámicamente desde ccxt
        exchange_class = getattr(ccxt, exchange_name)
        
        self.exchange = exchange_class({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
        })
        
        if use_testnet:
            self.exchange.set_sandbox_mode(True)
            print(f"[BrokerClient] Conectado a {exchange_name.upper()} en modo TESTNET (Sandbox).")
        else:
            print(f"[BrokerClient] ⚠️ Conectado a {exchange_name.upper()} en modo LIVE (Dinero Real).")
            
    def get_usdt_balance(self) -> float:
        """
        Obtiene el balance disponible en USDT. Si falla o estamos en modo offline, retorna 100 por defecto.
        """
        try:
            balance = self.exchange.fetch_balance()
            if 'USDT' in balance:
                return float(balance['USDT']['free'])
            return 0.0
        except Exception as e:
            print(f"[BrokerClient] -> Error obteniendo balance: {e}. Usando balance simulado de 100.00")
            return 100.0
            
    def create_market_order(self, symbol: str, side: str, amount: float):
        """
        Ejecuta una orden de mercado en el exchange.
        """
        try:
            print(f"[BrokerClient] Enviando orden {side.upper()} {amount} {symbol}...")
            # En un entorno de simulación sin API Keys válidas, esto fallará.
            order = self.exchange.create_market_order(symbol, side, amount)
            print(f"[BrokerClient] -> Orden ejecutada: {order['id']}")
            return order
        except Exception as e:
            print(f"[BrokerClient] -> Error ejecutando orden: {e}")
            return {"status": "failed", "error": str(e)}
