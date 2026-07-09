# MandateGate

`src/safety/mandate_gate.py`

## Responsabilidad

Validar cada trade propuesta **contra los límites del usuario**
(universe, exposure, daily cap). Inspirado en Vibe-Trading "mandate gate":

> "user-committed mandate (symbol universe / order size / exposure /
> leverage / daily cap), filesystem kill switch, fail-closed pre-trade
> gate, full audit ledger. The AI can't quietly go rogue with your money."

## 4 chequeos

1. **Universe de símbolos permitidos**
2. **Max position USD por trade**
3. **Daily loss rolling 24h** (suma de risks_usd de trades aprobadas en últimas 24h)
4. **Total exposure USD** (notional abierto + este trade)

## MandateConfig

```python
@dataclass
class MandateConfig:
    enabled: bool = False
    allowed_symbols: Set[str] = field(default_factory=set)
    max_position_usd: float = 20.0
    max_daily_loss_usd: float = 5.0
    max_total_exposure_usd: float = 100.0
```

## MandateVerdict

```python
@dataclass
class MandateVerdict:
    ok: bool
    reason: str = ""
    daily_loss_so_far_usd: float = 0.0
    open_exposure_usd: float = 0.0
```

## API

```python
config = MandateConfig(enabled=True, allowed_symbols={"BTC-USD", "ETH-USD"},
                      max_position_usd=20, max_daily_loss_usd=5, max_total_exposure_usd=100)
gate = MandateGate(config, audit_ledger=audit)

verdict = gate.validate({
    "asset": "BTC-USD",
    "notional_usd": 15,
    "risk_usd": 1.0,
})

if verdict.ok:
    approved = True
else:
    print(f"Bloqueado: {verdict.reason}")
```

## Activar

Por **default OFF** para no romper paper trading. En config.yaml:

```yaml
mandate:
  enabled: true
  allowed_symbols: ["BTC-USD", "BTCUSDT", "ETH-USD", "GLD", "USO", "SPY", "QQQ"]
  max_position_usd: 20
  max_daily_loss_usd: 5
  kill_switch_file: "/tmp/GUARITRADBOT_KILL"
```

## Test verificado

```
[OK] OK BTC 15usd                                            | all_checks_passed
[BLOCK] BLOQUEADO: GME no en universe                         | symbol_not_allowed:GME
[BLOCK] BLOQUEADO: notional > max_position_usd               | notional_exceeds_max:$50.00>$20.00
```

El cuarto test (rolling daily_loss) requiere que la trade
anterior haya sido **aprobada** (no rechazada) para acumular riesgo.
Los rechazos no consumen daily_loss (legítimo — no gastas capital en
trades que no se ejecutan).

## Daily loss calculation

Lee del audit ledger:
```python
cutoff = time.time() - 24 * 3600
rows = self.audit.read_since(cutoff)
return sum(r.get("risk_usd", 0.0) for r in rows
           if r.get("event_type") == "TRADE_APPROVED")
```

## Conecta con

- [[Modules/AuditLedger]] — para calcular daily loss
- [[Modules/RiskManagerAgent]] — consulta antes de aprobar
- [[Sprints/Sprint_1_Safety_Layer]]
