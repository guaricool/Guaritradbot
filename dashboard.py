import streamlit as st
import yaml
import json
import os
import pandas as pd
from datetime import datetime

st.set_page_config(page_title="Guaritradbot Epic Dashboard", layout="wide", page_icon="📈")

st.title("📈 Guaritradbot: Multi-Agent Epic Dashboard")
st.markdown("Monitor de operaciones, agentes y estado del sistema en vivo.")

# Leer Configuración
config_path = "config.yaml"
if os.path.exists(config_path):
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
else:
    config = {}

# Leer Estado Reciente
state_path = "latest_state.json"
last_state = {}
last_update = "Nunca"

if os.path.exists(state_path):
    try:
        with open(state_path, "r") as f:
            data = json.load(f)
            last_state = data.get("state", {})
            last_update = data.get("timestamp", "Desconocido")
    except Exception as e:
        st.error(f"Error leyendo estado: {e}")

# Layout principal
col1, col2, col3 = st.columns(3)

with col1:
    st.info("⚙️ **Configuración Actual**")
    st.write(f"**Modo:** `{config.get('execution_mode', 'N/A')}`")
    st.write(f"**Exchange:** `{config.get('exchange', {}).get('name', 'N/A').upper()}`")
    st.write(f"**Testnet:** `{config.get('exchange', {}).get('use_testnet', True)}`")

with col2:
    st.success("💰 **Capital y Riesgo**")
    st.write(f"**Riesgo por Trade:** `{config.get('trading', {}).get('risk_per_trade_pct', 0)}%`")
    st.write(f"**Cap. Máx por Trade:** `{config.get('exchange', {}).get('max_capital_per_trade_pct', 0)}%`")

with col3:
    st.warning("⏱️ **Épocas y Scheduler**")
    st.write(f"**Intervalo (horas):** `{config.get('schedule', {}).get('run_interval_hours', 1)}`")
    st.write(f"**Duración de Época:** `{config.get('schedule', {}).get('epoch_duration_days', 7)} días`")
    st.write(f"**Última Act.:** `{last_update}`")

st.divider()

st.subheader("🤖 Últimas Operaciones Aprobadas (Risk Manager)")

execute_data = last_state.get("execute_trades", {})
executed_trades = execute_data.get("executed_trades", [])

if not executed_trades:
    st.write("No hay operaciones recientes en memoria.")
else:
    df_trades = pd.DataFrame(executed_trades)
    st.dataframe(df_trades, use_container_width=True)

st.subheader("🧠 Hipótesis Generadas (Strategy Agent)")
hypotheses = last_state.get("generate_hypotheses", {}).get("hypotheses", [])

if not hypotheses:
    st.write("No hay hipótesis recientes en memoria.")
else:
    df_hyp = pd.DataFrame(hypotheses)
    st.dataframe(df_hyp, use_container_width=True)
