import random
from typing import Dict, Any

class MLSignalFilter:
    """
    Inspirado en intelligent-trading-bot.
    Una capa de validación extra para filtrar falsas señales usando ML (simulado aquí).
    """
    def __init__(self, confidence_threshold: float = 0.65):
        self.confidence_threshold = confidence_threshold

    def validate_signal(self, asset: str, signal: str, data: Any) -> Dict[str, Any]:
        """
        Evalúa una señal generada y devuelve un nivel de confianza.
        Si la confianza supera el umbral, la señal es válida.
        """
        if signal == "NEUTRAL":
            return {"is_valid": False, "confidence": 0.0, "reason": "Neutral signal"}

        # Simular una predicción de modelo de ML (ej. Random Forest, XGBoost)
        # En una implementación real, esto usaría características (features) del 'data' para predecir la probabilidad de éxito.
        confidence = random.uniform(0.4, 0.95)
        
        is_valid = confidence >= self.confidence_threshold
        
        return {
            "is_valid": is_valid,
            "confidence": round(confidence, 4),
            "reason": f"ML model confidence {confidence:.2%} " + ("passed" if is_valid else "rejected") + f" threshold {self.confidence_threshold:.2%}"
        }
