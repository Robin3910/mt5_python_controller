# Stage 1: build Vue SPA
FROM node:20-alpine AS frontend-build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# 部署时由 build/.env 的 VITE_API_BASE 注入。留空走同源（nginx 反代 /api）。
# 已存在的环境变量优先于 frontend/.env*，所以这里能盖过镜像里拷进来的本地配置。
ARG VITE_API_BASE=
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build

# Stage 2: nginx serves dist + reverse proxy to backend
FROM nginx:stable-alpine

COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /app/dist /usr/share/nginx/html
