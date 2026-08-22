FROM node:22.23.2-bookworm-slim AS build

WORKDIR /app
COPY package.json package-lock.json ./
# openapi-typescript 7 declares a peer of typescript ^5.x and the project is on 6.
# It is only used by the generate-types script, never by the build.
RUN npm ci --legacy-peer-deps

COPY . .
# Baked into the bundle at build time. It points at the nginx origin below, not at the
# API container, so the browser makes same-origin requests and the API needs no CORS.
ARG VITE_API_BASE_URL=http://localhost:8080
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.29-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
