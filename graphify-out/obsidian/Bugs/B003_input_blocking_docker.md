# B003 — input() bloqueante en Docker

**Severidad**: 🔴 crítico (rompía el contenedor en Coolify)

## Síntomas

Modo `execution_mode: "human_in_the_loop"` colgaba indefinidamente.
En Docker (`stdin` = null):
```
input("¿Aprobar? (Y/N): ")
```
espera para siempre. El contenedor agota el health check y Coolify
lo mata.

## Causa

`src/execution/execution_node.py` línea 20:
```python
decision = input("¿Aprobar? (Y/N, default=N en 30s): ")
```

Sin try/except para el caso `stdin=null`.

## Fix (Sprint 0, commit `10d144c`)

```python
try:
    decision = input("¿Aprobar? (Y/N, default=N en 30s): ").strip().upper()
    if decision != "Y":
        # rechazado
        return
except (EOFError, KeyboardInterrupt):
    # No hay TTY — SKIP seguro
    print("[ExecutionNode] ⚠️ No hay TTY. SKIP seguro")
    if self.audit:
        self.audit.append("TRADE_SKIPPED_NO_TTY", ...)
    return
```

Además, publicamos `ORDER_PENDING_APPROVAL` al EventBus ANTES del
input, para que [[Modules/NotificationAgent]] lo mande a Telegram.

## Lección

Cualquier `input()` en un daemon debe tener fallback graceful. En
producción, los daemons suelen correr en contenedores sin stdin.

## Modo recomendado

Para producción usar `execution_mode: "auto"` y dejar que
[[Modules/KillSwitch]] sea el único override humano (touch file).

## Ver también

- [[Modules/ExecutionNode]]
- [[Modules/KillSwitch]] — override human-safe
- [[Bugs_Index]]
