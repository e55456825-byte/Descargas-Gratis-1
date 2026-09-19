#!/usr/bin/env bash
# start.sh — Lanzar Bit Uploader v2
set -e

cd "$(dirname "$0")"

# Crear venv si no existe
if [ ! -d "venv" ]; then
    echo "📦 Creando entorno virtual..."
    python3 -m venv venv
fi

source venv/bin/activate

# Instalar dependencias
echo "📥 Instalando dependencias..."
pip install -r requirements.txt --quiet

# Cargar .env si existe
if [ -f ".env" ]; then
    echo "📄 Cargando .env..."
    set -a
    source .env
    set +a
fi

# Lanzar bot
echo "🚀 Iniciando bot..."
python3 main.py
