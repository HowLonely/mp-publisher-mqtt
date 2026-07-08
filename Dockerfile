# Se usa imagen ligera de Python compatible con Raspberry Pi
FROM python:3.11-slim

# Evita que Python genere archivos .pyc y fuerza la salida de logs en tiempo real
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Instalar dependencias
COPY requeriments.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el script al contenedor
COPY . .

# Ejecutar el script
CMD ["python", "main"]
