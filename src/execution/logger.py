import logging
import os

def setup_logger(name: str = "trading_bot", log_file: str = "trading_bot.log") -> logging.Logger:
    """
    Configura el sistema de logging para el bot de trading.
    
    Args:
        name: Nombre del logger
        log_file: Archivo de destino
        
    Returns:
        logging.Logger: Instancia del logger
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    # Crear un formatter
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Handler para consola
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    # Handler para archivo
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    
    # Evitar handlers duplicados si se llama varias veces
    if not logger.handlers:
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)
        
    return logger

# Instancia global
logger = setup_logger()
