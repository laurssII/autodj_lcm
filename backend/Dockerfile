# Dos imágenes desde el mismo código:
#   api     FastAPI. Reparte el trabajo pesado y sirve audio. Ligera.
#   worker  Análisis, letras, renderizado de mezclas y stems. Lleva Essentia, Whisper y Demucs.
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    AUTODJ_DATA_DIR=/data HF_HOME=/data/models TORCH_HOME=/data/models/torch

# El ffmpeg de Debian incluye el filtro rubberband (cambio de tempo y de tono de calidad)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && (ffmpeg -hide_banner -filters | grep -q rubberband || echo "AVISO: este ffmpeg no trae rubberband; no habrá mezclas adaptativas")

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY autodj ./autodj
COPY tests ./tests
VOLUME ["/data"]


FROM base AS api
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "autodj.api:app", "--host", "0.0.0.0", "--port", "8000"]


FROM base AS worker
COPY requirements-analysis.txt requirements-stems.txt ./
RUN pip install essentia || echo "Essentia no disponible: se usará el análisis en numpy"
RUN pip install faster-whisper || echo "faster-whisper no disponible: wordplay desactivado"
# torch para CPU (sin CUDA) y Demucs; si no se pueden instalar, las mezclas siguen funcionando sin stems
RUN (pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu \
     && pip install -r requirements-stems.txt) \
 || echo "Demucs no disponible: las mezclas usarán EQ en vez de stems"
CMD ["python", "-m", "autodj.worker"]
