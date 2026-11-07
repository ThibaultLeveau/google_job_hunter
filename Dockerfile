FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv
FROM python:3.14.10-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b AS python
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/backend

FROM python AS backend-build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src ./src
RUN uv sync --locked --no-dev --no-editable --no-cache

FROM python AS backend
COPY --from=backend-build /app/backend/.venv ./.venv
COPY backend/pyproject.toml ./
COPY backend/migrations ./migrations
COPY backend/scripts/runtime.py ./scripts/runtime.py
RUN mkdir -p /private/documents && chown 10001:10001 /private/documents && chmod 700 /private/documents
ENV PATH="/app/backend/.venv/bin:$PATH"
USER 10001:10001
ENTRYPOINT ["python", "-m", "scripts.runtime"]
CMD ["api"]

# Explicit test target; the external-service harness is absent from production.
FROM backend-build AS fixture-test
RUN uv sync --locked --no-editable --no-cache
COPY backend/migrations ./migrations
COPY backend/tests ./tests
ENV PATH="/app/backend/.venv/bin:$PATH"
USER 10001:10001
CMD ["python", "-m", "pytest", "tests/test_stack_harness.py", "--require-database", "-q", "-p", "no:cacheprovider"]

FROM node:26.13.0-bookworm-slim@sha256:add5c17c14752c54d2f3d519ec46a3815684d41c33f198bcb09fb998f7dcbf68 AS frontend-build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON=3.14.10 UV_LINK_MODE=copy
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./backend/
COPY backend/src ./backend/src
RUN uv sync --directory backend --locked --no-cache
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM nginx:1.30.6-alpine@sha256:583b7f2fce1cdb8da5ad5c39ebd3c33b98b87d493190e548bdee1fa8c619c311 AS web
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY --from=frontend-build /app/frontend/dist /usr/share/nginx/html
USER 101:101
EXPOSE 8080
ENTRYPOINT ["nginx", "-g", "daemon off;"]
