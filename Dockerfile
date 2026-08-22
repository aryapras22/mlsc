# One image for all three Python processes (API, worker, beat); the command differs.
#
# environment.yml is the only declaration of dependencies (pyproject.toml deliberately
# declares none), so the image is built by solving that file with conda rather than by
# a separate requirements list that could drift from it.

FROM continuumio/miniconda3:25.3.1-1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/conda/envs/mlsc/bin:$PATH \
    HF_HOME=/opt/model-cache

COPY environment.yml /tmp/environment.yml
RUN conda env create -f /tmp/environment.yml && conda clean -afy

# Sentence-transformers downloads its embedding model on first use. A writable,
# volume-mountable location keeps that download out of the image and off every restart.
RUN useradd --create-home --uid 10001 mlsc \
    && mkdir -p /opt/model-cache \
    && chown mlsc:mlsc /opt/model-cache

WORKDIR /app
COPY alembic.ini pyproject.toml ./
COPY mlsc ./mlsc

USER mlsc
EXPOSE 8000
CMD ["uvicorn", "mlsc.main:app", "--host", "0.0.0.0", "--port", "8000"]
