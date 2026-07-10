import ccxt
import os
try:
    from dotenv import load_dotenv
except ImportError:
    # Sprint 31 hardening: dotenv missing shouldn't crash the bot at startup.
    # Fall back to a no-op loader so the bot can still try to read env vars
    # already exported in the container (Coolify env_file).
    def load_dotenv(*args, **kwargs):
        return False

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
        Obtiene el balance disponible. binance global usa USDT,
        binance.us usa USD. Aceptamos ambos.

        Sprint 43 H1 fix: on any error, RAISE the exception instead
        of silently returning 100.0. The audit flagged the old
        "fallback to 100" behavior as a fail-open vulnerability:
        if the broker call fails (network timeout, wrong API keys,
        exchange down, etc.), the caller would size orders based
        on imaginary money. A user with a $0 real balance could
        see orders sized as if they had $100.

        Callers that want a simulated fallback (e.g. for paper
        mode or local dev) should catch the exception and decide
        based on `GUARICO_ALLOW_SIMULATED_BALANCE`:
          - True  → return a simulated value (caller's choice)
          - False → re-raise or return None (production safe)

        A genuine balance of $0 is returned as `0.0` (not raised)
        — that's a valid state, not an error.

        Returns:
            float: the free USD-equivalent balance. May be 0.0 if
                the account has no USD/USDT/BUSD/USDC free.
        Raises:
            ccxt.NetworkError, ccxt.ExchangeError, or any
            underlying broker error.
        """
        balance = self.exchange.fetch_balance()
        for sym in ("USD", "USDT", "BUSD", "USDC"):
            if sym in balance:
                info = balance[sym]
                free = info.get("free") if isinstance(info, dict) else None
                if free is not None and float(free) > 0:
                    return float(free)
                total = info.get("total") if isinstance(info, dict) else None
                if total is not None and float(total) > 0:
                    return float(total)
        # Try raw structure (some exchanges nest balances differently)
        raw = balance.get("info", {}).get("balances", []) if isinstance(balance.get("info"), dict) else []
        for entry in raw:
            asset = entry.get("asset", "").upper()
            if asset in ("USD", "USDT", "BUSD", "USDC"):
                free = float(entry.get("free", 0) or 0)
                if free > 0:
                    return free
        return 0.0
            
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
