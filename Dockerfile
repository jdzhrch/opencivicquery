FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    OPENCIVICQUERY_HOST=0.0.0.0

WORKDIR /app

RUN addgroup --system opencivicquery && adduser --system --ingroup opencivicquery opencivicquery

COPY ["pyproject.toml", "README.md", "LICENSE", "./"]
COPY requirements.txt ./
COPY src ./src
COPY deployments ./deployments
COPY packs ./packs

RUN pip install --no-cache-dir --no-deps --require-hashes -r requirements.txt \
 && pip install --no-cache-dir --no-deps . \
 && chown -R opencivicquery:opencivicquery /app

USER opencivicquery

CMD ["opencivicquery-server"]
