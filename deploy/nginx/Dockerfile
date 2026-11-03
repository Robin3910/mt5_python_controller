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
# npm run build 末尾会跑 verify-build.mjs：逐个 chunk 做语法解析 + 校验引用完整性。
# 这一步能挡住「源码/依赖层面」的坏产物。
RUN npm run build \
 && cd /app/dist \
 && find . -type f -exec sha256sum {} + | sort -k2 > /app/dist.sha256 \
 && echo "[verify] 已记录 $(wc -l < /app/dist.sha256) 个产物的校验和"

# Stage 2: nginx serves dist + reverse proxy to backend
FROM nginx:stable-alpine

COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /app/dist /usr/share/nginx/html
COPY --from=frontend-build /app/dist.sha256 /tmp/dist.sha256

# 落盘后再验一遍。
# 2026-11-03 事故：dist/assets/installCanvasRenderer-*.js 的第 18218 字节在写入
# 镜像 layer 时被改成 '9'（原为 ')'），线上直接抛
#   SyntaxError: Unexpected identifier 'n'
# 页面全白，而 build 阶段毫无感知——所以校验必须发生在文件真正落到 layer 之后。
# 这里逐文件比对 build 阶段记录的 sha256，任何字节损坏都会让构建失败而不是带病上线。
# 注意：busybox 的 sha256sum -c 在校验失败时返回 rc=1，可直接用于判断。
RUN set -e; \
    cd /usr/share/nginx/html; \
    if ! sha256sum -c /tmp/dist.sha256; then \
      echo "[verify] 静态资源校验失败：产物在写入镜像 layer 时被损坏，请用 --no-cache 重建。" >&2; \
      exit 1; \
    fi; \
    rm -f /tmp/dist.sha256; \
    echo "[verify] 静态资源校验通过"
