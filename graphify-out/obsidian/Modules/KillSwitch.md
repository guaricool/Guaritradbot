# KillSwitch

`src/safety/kill_switch.py`

## Responsabilidad

**Kill switch filesystem**. Si el archivo `/tmp/GUARITRADBOT_KILL`
existe, el bot PARA inmediatamente.

Inspirado en Vibe-Trading "filesystem kill switch" pattern.

> "filesystem-level instant kill switch, preemptive flatten"

## Uso

```bash
# ARMAR (parar el bot desde otra terminal)
touch /tmp/GUARITRADBOT_KILL

# DESARMAR (revivir)
rm /tmp/GUARITRADBOT_KILL
```

## API

```python
ks = KillSwitch("/tmp/GUARITRADBOT_KILL")

ks.arm()        # crea el archivo
ks.disarm()     # lo borra

if ks.is_triggered():
    # NO ejecutar nada, salir
    return
```

## Doble check (defensa en profundidad)

Se llama en **3 lugares**:

1. **main.py startup** — antes de cargar agents
   ```python
   if kill_switch.is_triggered():
       audit.append("BOT_START_BLOCKED_KILLSWITCH", ...)
       return  # bot NO arranca
   ```

2. **ExecutionNode.on_order_approved()** — antes de enviar al broker
   ```python
   if self.kill_switch.is_triggered():
       audit.append("TRADE_BLOCKED_KILLSWITCH", ...)
       return  # orden NO ejecutada
   ```

3. **ExecutionNode.execute_order()** — antes del create_market_order
   ```python
   if self.kill_switch.is_triggered():
       print("Kill switch ARMED — execute_order cancelado")
       return
   ```

Esto es **crash-only design**: aunque un check falle por race
condition, los otros dos detienen la ejecución.

## Configurable path

```yaml
mandate:
  kill_switch_file: "/tmp/GUARITRADBOT_KILL"
```

Por default `/tmp/GUARITRADBOT_KILL`. En Windows, el path se resuelve
a `C:\Users\...\AppData\Local\Temp\GUARITRADBOT_KILL` si se usa
`$temp`.

## Output al activarse

```
⛔ [KillSwitch] TRIGGERED — file found at /tmp/GUARITRADBOT_KILL
   rm /tmp/GUARITRADBOT_KILL  ← para revivir
```

## Conecta con

- [[Modules/AuditLedger]] — registra BOT_START_BLOCKED y TRADE_BLOCKED
- [[Modules/ExecutionNode]] — bloquea ejecución de órdenes
- [[Sprints/Sprint_1_Safety_Layer]]
