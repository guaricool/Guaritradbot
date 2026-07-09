# B015 — venv sin dependencias

**Severidad**: 🟠 medio (operacional — portabilidad rota)

## Síntomas

`pip install -r requirements.txt` desde una máquina nueva fallaba
con `ModuleNotFoundError: yaml` (o `schedule`, `streamlit`, `dotenv`)
al ejecutar el bot.

## Causa

`requirements.txt` declara los paquetes, pero el venv `venv/`
creado por `python -m venv venv` solo tenía `pandas`, `yfinance`,
`numpy`, etc. Faltaban las deps declaradas.

El bot funcionaba localmente porque el **system Python** de Carlos
tenía todo instalado globalmente (heredado de otro proyecto).

## Fix (Sprint 0)

**Trabajo**: documentar el workaround en `MEMORY.md` y en este
vault. El usuario debe correr:

```bash
pip install -r requirements.txt
```

O alternativamente usar system Python directamente (workaround que
estamos usando).

**Fix permanente** sería un `setup.py` o `pyproject.toml` con todas
las deps, y forzar la activación del venv en un script de
`entrypoint.sh`. Esto sería un sprint futuro.

## Por qué no hice más

Es operacional, no del scope de Sprint 0. Lo importante era
**arreglar los bugs que rompían runtime**; las dependencias eran
una inconveniencia, no un crash.

## Ver también

- [[Deployment]] — cómo configurar el entorno
- [[Project_History]]
- [[Bugs_Index]]
