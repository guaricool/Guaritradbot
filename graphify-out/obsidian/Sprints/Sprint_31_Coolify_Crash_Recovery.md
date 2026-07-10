# Sprint 31 — Crash loop fix B028v2 + Coolify redeploy recovery

**Fecha**: 2026-07-09/10
**Status**: ✅ Cerrado
**Trigger**: Carlos reportó "en el coolify esta Exited (14x restarts)"

## Resumen

El dashboard container estaba en crash loop. **No era el bot — era el dashboard.** El bot engine corría perfectamente. La causa raíz fue un bug **mío** del Sprint 28 que dejó el `.streamlit/config.toml` con 2 secciones `[browser]` inválidas.

## Investigación end-to-end (sin pedir logs a Carlos)

1. **Verificar pre-deploy**: 
   - `ssh root@13.140.181.29 "docker ps -a | grep guaritradbot"` → container vivo (Up 47s)
   - `docker logs --tail 80 <container-bot>` → workflow completo OK, orders fallan graceful
   
2. **Verificar dashboard**:
   - `docker ps -a | grep dashboard` → **`Restarting (1) 9 seconds ago`** ← ¡crash!
   - `docker logs --tail 60 <container-dashboard>` → `toml.decoder.TomlDecodeError: What? browser already exists?`
   
3. **Identificar root cause**: `.streamlit/config.toml` tenía 2 secciones `[browser]` (líneas 1 y 35).

4. **Verificar deploy pipeline**:
   - `psql coolify` → `application_deployment_queues` muestra **#334 failed** a las 03:11 con `network wyn2ah6rflg6ufwzpvzk436f declared as external, but could not be found`
   
5. **Fix**:
   - Consolidar `[browser]` section (3 keys en una)
   - Validar con `toml.loads()` local
   - Commit `6aee4ff` → push
   - `docker network create wyn2ah6rflg6ufwzpvzk436f` (Coolify no la creó)
   - Commit vacío `d73924d` para forzar redeploy
   
6. **Verificación post-fix**:
   - Deploy #335 finished 03:21:45
   - Ambos containers Up 49s, sin crashes
   - Bot workflow completo, dashboard en `http://0.0.0.0:8501`

## Diagnóstico

### Bug 1 — `.streamlit/config.toml` (mío, Sprint 28)

Mi fix original de B028 (`e185d61`) añadió `gatherUsageStats = false` en una sección `[browser]`. Después, al configurar para Coolify port binding, añadí un SEGUNDO `[browser]` con `serverAddress`/`serverPort` en vez de consolidarlo. **TOML no permite headers duplicados** → `TomlDecodeError`.

```toml
# MAL (mi fix original):
[browser]
gatherUsageStats = false

[browser]   # ← esto rompe el parser
serverAddress = "0.0.0.0"
serverPort = 8501

# BIEN (consolidado):
[browser]
gatherUsageStats = false
serverAddress = "0.0.0.0"
serverPort = 8501
```

### Bug 2 — Red Docker perdida (Coolify)

Después de varios crash-loop → containers eliminados → estado inconsistente en Coolify donde la red per-recurso `wyn2ah6rflg6ufwzpvzk436f` no se recreó automáticamente. El deploy `docker compose up` falla porque la red está declarada como `external: true` en el compose.

**Fix**: `docker network create --driver bridge <uuid>` y redeploy.

## Lección principal

**1. NO duplicar secciones TOML.** Aunque parezca "más seguro", es **inválido**. Siempre consolidar.

**2. Validar TOML antes de commitear** con el parser local:
```python
import toml
cfg = toml.loads(open('.streamlit/config.toml').read())
```

**3. Cuando un container está en crash loop, primero identificar CUÁL**:
- `docker ps -a | grep <name>` → todos los containers, no solo los running
- `docker logs --tail 80 <container-id>` → el traceback específico
- En mi caso: **bot estaba bien, dashboard estaba roto**

**4. Verificar el deploy queue de Coolify** directamente vía su DB Postgres si la UI no es accesible:
```sql
SELECT id, status, created_at, finished_at 
FROM application_deployment_queues 
ORDER BY created_at DESC LIMIT 5;
```

**5. Las redes Docker "external" en Coolify no siempre se recrean** después de crashes. Si un deploy falla con "network X declared as external, but could not be found", crear la red manualmente.

**6. Para forzar un redeploy sin cambiar código real**: `git commit --allow-empty -m "Trigger redeploy"` + push. Útil cuando el fix es operacional (red, permisos, estado Docker) y no requiere cambios de código.

## Commits del sprint

| SHA | Descripción |
|---|---|
| `6aee4ff` | fix(B028): consolidate duplicate [browser] section in config.toml |
| `d73924d` | Trigger Coolify redeploy after network fix (B028v2) — empty commit |

## Archivos tocados

- `.streamlit/config.toml` — consolidé `[browser]`
- `graphify-out/obsidian/Bugs_Index.md` — añadí B028v2 (🔴 crítico)

## Próximos pasos

- **Sprint 32**: Integrar Kelly Criterion + DrawdownKillSwitch al RiskAgent y al main loop (ya los tenemos en `src/safety/kelly_drawdown.py` pero no enchufados).
- **Sprint 33**: Dashboard widget que muestre estado del Kelly + drawdown en vivo.
- **Sprint 34**: Considerar upgrade Streamlit 1.36 → 1.40+ (eliminaría warnings restantes, pero riesgo de breaking changes).

## Verificación final

```
$ docker ps -a | grep -E 'guaritradbot|dashboard'
7d9008077569  wyn2ah6rflg6ufwzpvzk436f_guaritradbot:d73924d  Up 49 seconds  "python -u main.py"
efeefc4199f4  wyn2ah6rflg6ufwzpvzk436f_dashboard:d73924d      Up 49 seconds  "streamlit run..."  0.0.0.0:8501->8501/tcp
```

✅ Bot + dashboard back online.
✅ Deploy #335 finished sin errors.
✅ Workflow del bot completando ciclos.

Ver [[../Bugs/B028v2_coolify_dashboard_crashloop]] para el detalle del bug.