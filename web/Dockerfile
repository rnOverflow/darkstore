# Both React apps (customer + ops) are built inside Docker, so the host needs no Node.
# Build context is the repo root (it needs infra/nginx/nginx.conf too).
FROM node:22-alpine AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY infra/nginx/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /web/dist /usr/share/nginx/html
