# syntax=docker/dockerfile:1
# Static React/Vite UI: build with Node, serve the compiled assets with nginx.
# Context is the repo root, so paths are prefixed with web/.
FROM node:20-slim AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /web/dist /usr/share/nginx/html
# Listen on 8080 and fall back to index.html so client-side routes resolve.
RUN printf 'server {\n    listen 8080;\n    root /usr/share/nginx/html;\n    location / { try_files $uri /index.html; }\n}\n' > /etc/nginx/conf.d/default.conf
EXPOSE 8080
