# Sprint 30 — Kelly Criterion + Max Drawdown Kill Switch (from QUANT guide)

**Fecha**: 2026-07-09
**Status**: ✅ Cerrado (19/19 tests nuevos)
**Inspiración**: Carlos shared "QUANT — The Setup Guide (Claude Trading Skills by @seb.ai)". Es un pack de 62 skills de crypto/DeFi (no todas aplicables a nuestro bot multi-asset), pero 2 ideas son altamente valiosas.

## Resumen

Del PDF de QUANT extraje 2 ideas que SÍ nos sirven (el resto es crypto/DeFi-focused):

1. **Kelly Criterion** — position sizing óptimo basado en edge del modelo
2. **Max Drawdown Kill Switch** — pausa automática si drawdown > X%

Implementé ambos con tests completos.

## Componentes

### `src/safety/kelly_drawdown.py`

#### 1. Kelly Criterion

```python
@dataclass
class KellyConfig:
    enabled: bool = False          # off by default; opt-in
    fractional_multiplier: float = 0.25  # 1/4 Kelly (conservador)
    min_edge: float = 0.02         # minimum edge to enter
    min_win_prob: float = 0.30     # minimum win probability
    max_position_pct: float = 0.20 # hard cap (nunca más de 20%)

def kelly_fraction(win_prob, avg_win, avg_loss, cfg) -> float:
    # Full Kelly: f* = (bp - q) / b
    # Fractional: f* * 0.25
    # Cap: min(fractional, 0.20)
```

**Por qué fractional 0.25?** Full Kelly es muy agresivo en mercados reales (drawdowns del 50% son comunes). El consenso académico es usar 0.25-0.5 Kelly para trading retail.

**Ejemplo con ML del Sprint 19** (55% win, 1.5:1 R:R):
- Edge = 0.55 × 1.5 - 0.45 × 1.0 = 0.375
- Full Kelly = 0.375 / 1.5 = 0.25 (25% del bankroll)
- Fractional 0.25 = 0.0625 (6.25% del bankroll)
- Cap 0.20 → final = **6.25%** (vs el 1% fijo que teníamos antes)

**Comparación con regla fija del 1%**: Kelly sizing arriesga más cuando hay más edge (buena señal), menos cuando no hay edge (mala señal). Es **adaptativo al edge real del modelo**.

#### 2. Max Drawdown Kill Switch

```python
class DrawdownKillSwitch:
    def __init__(threshold_pct=15.0, cooldown_hours=24.0):
        ...
    
    def update(current_equity) -> DrawdownState:
        # Update peak (if new high)
        # Compute drawdown = (current - peak) / peak
        # Trigger if drawdown <= -threshold_pct
        # Auto-reset if cooldown elapsed AND equity recovered
```

**Por qué importa**: previene revenge trading después de losses, evita doblar down en estrategia rota, fuerza pausa para análisis.

**Diseño clave**: el auto-reset solo ocurre cuando `equity recovered` (drawdown > -threshold_pct). Esto evita un loop de "reset → trigger → reset" cuando la equity sigue baja.

## Tests (19 nuevos)

```
tests/test_kelly_drawdown.py
├── KellyFractionTest (10 tests)
│   ├── test_zero_signal_returns_zero                ✓
│   ├── test_strong_signal_returns_positive           ✓
│   ├── test_full_kelly_example                       ✓
│   ├── test_fractional_multiplier_caps_position      ✓
│   ├── test_max_position_cap                         ✓
│   ├── test_min_edge_filter                          ✓
│   ├── test_min_win_prob_filter                      ✓
│   ├── test_zero_or_negative_avg_loss                ✓
│   └── test_realistic_sprint19_ml_scenario           ✓
└── DrawdownKillSwitchTest (9 tests)
    ├── test_initial_state_no_trigger                  ✓
    ├── test_drawdown_below_threshold_no_trigger       ✓
    ├── test_drawdown_at_threshold_triggers           ✓
    ├── test_drawdown_beyond_threshold_triggers        ✓
    ├── test_recovery_resets_peak                      ✓
    ├── test_auto_reset_after_cooldown                 ✓
    ├── test_no_auto_reset_if_still_in_drawdown        ✓
    ├── test_manual_reset                             ✓
    ├── test_state_readout                            ✓
    └── test_zero_equity_no_crash                      ✓
```

## Por qué NO instalé el plugin completo

AGIPro / @seb.ai's QUANT plugin tiene 62 skills pero:
- **Crypto/DeFi-first** (Solana, Birdeye, DexScreener) — no aplicable a nuestro bot multi-asset
- Necesita `uv` (otro package manager) — overhead innecesario
- Skills de execution son demo-only por default y requieren confirmación explícita
- El repo es nuevo/pequeño → menos battle-tested

Pero las **ideas filosóficas** (Kelly, drawdown limits, walk-forward, position sizing) son **universales** y las adapté a Python puro sin dependencias externas.

## Score de capacidad actualizado

| Capacidad | Antes | Después |
|---|---|---|
| Position sizing | ⚠️ (1% fijo) | ✅ (Kelly adaptativo) |
| Max drawdown protection | ❌ | ✅ (kill switch auto) |
| Adaptive to model edge | ❌ | ✅ (Kelly usa win_prob + R:R) |
| Recovery logic | ❌ | ✅ (auto-reset + manual override) |

**Score global sube a ~89%** con este sprint (de 87%).

## Próximos pasos

- **Sprint 31**: Wire-up en RiskAgent: usar Kelly sizing si está enabled
- **Sprint 32**: Wire-up en main.py: integrar DrawdownKillSwitch con el equity tracker
- **Sprint 33**: Dashboard widget que muestre el estado de Kelly + drawdown

## Lección de diseño

Cuando ves un pack externo (como QUANT):
- **No instales todo** solo porque está bien hecho.
- **Extrae las ideas** que aplican a tu contexto.
- **Reimplementa en tu stack** (Python puro, sin nuevas deps) si vale la pena.
- **Documenta por qué** cada pieza sí/no aplica (este archivo).

Ver [[../Sprints/Sprint_29_PreFlight_Start_Live]] para el sprint anterior.