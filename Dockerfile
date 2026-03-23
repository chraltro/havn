# --- Stage 1: Build frontend ---
FROM node:20-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ .
RUN npm run build

# --- Stage 2: Python runtime ---
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
COPY --from=frontend /build/frontend/dist /app/frontend/dist

RUN pip install --no-cache-dir .

WORKDIR /project
EXPOSE 3000

ENTRYPOINT ["havn"]
CMD ["serve", "--host", "0.0.0.0"]
