# syntax=docker/dockerfile:1.7
# Atlas AI dashboard: a production build served by nginx. No development server, no Node in the final image.
#
#   docker build -t atlas-ai-frontend:local frontend
ARG NODE_IMAGE=node:22.22.0-alpine
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.29-alpine3.22

FROM ${NODE_IMAGE} AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html vite.config.js ./
COPY src ./src
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime
ARG VERSION=dev
# Security updates newer than the base image's snapshot (Trivy flagged dozens of HIGH findings in the unpatched one).
# The image's own user is restored right after.
USER root
RUN apk upgrade --no-cache
USER 101
LABEL org.opencontainers.image.title="atlas-ai-frontend" org.opencontainers.image.version="${VERSION}"
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]
