import pandas as pd
import numpy as np
from typing import Dict, Any, List, Callable
from itertools import product
from src.optimization.backtester import VectorizedBacktester

class HyperoptManager:
    """
    Optimizador Grid Search que utiliza VectorizedBacktester para encontrar
    los mejores parámetros maximizando el Ratio de Sharpe.
    """
    def __init__(self):
        self.best_params = {}
        self.backtester = VectorizedBacktester()

    def optimize(
        self, 
        strategy_name: str, 
        historical_data: pd.DataFrame, 
        parameter_space: Dict[str, List], 
        signal_generator: Callable,
        metric: str = "sharpe_ratio"
    ) -> Dict[str, Any]:
        """
        Ejecuta el Grid Search sobre todas las combinaciones del parameter_space.
        
        signal_generator: Una función que recibe (df, **params) y devuelve una Serie de Pandas con señales (-1, 0, 1)
        """
        print(f"[{self.__class__.__name__}] Optimizando {strategy_name}...")
        
        if historical_data.empty:
            print(f"[{self.__class__.__name__}] ⚠️ No hay datos históricos para {strategy_name}.")
            return {}

        best_metric = -np.inf
        best_found = {}
        
        param_names = list(parameter_space.keys())
        param_values = list(parameter_space.values())
        
        # Generamos todas las combinaciones (Grid Search)
        combinations = list(product(*param_values))
        print(f"[{self.__class__.__name__}] Probando {len(combinations)} combinaciones...")
        
        for values in combinations:
            params = dict(zip(param_names, values))
            
            # 1. Generar la señal inyectando los parámetros actuales (e.g. rsi_oversold=30)
            # Para el backtester vectorizado, le pasamos una lambda que aplica estos parámetros
            def current_signal_func(df):
                return signal_generator(df, **params)
                
            # 2. Correr el backtest
            results = self.backtester.run(historical_data, current_signal_func)
            current_metric = results["metrics"].get(metric, 0)
            
            # 3. Guardar si es el mejor
            if current_metric > best_metric:
                best_metric = current_metric
                best_found = params
                
        self.best_params[strategy_name] = best_found
        print(f"[{self.__class__.__name__}] -> Mejores parámetros ({metric}: {best_metric:.2f}): {best_found}")
        return best_found

    def get_best_params(self, strategy_name: str) -> Dict[str, Any]:
        return self.best_params.get(strategy_name, {})
