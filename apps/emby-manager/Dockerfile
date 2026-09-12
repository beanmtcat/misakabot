FROM node:22-alpine AS frontend
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app
ENV EMBY_WEB_ROOT=/app/web/dist
COPY pyproject.toml ./
COPY src ./src
COPY --from=frontend /web/dist ./web/dist
RUN pip install --no-cache-dir .

USER 10001:10001
EXPOSE 8084
CMD ["emby-manager-api"]
