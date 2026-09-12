FROM node:22-bookworm-slim AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
# Never reuse a host-generated Vinext bundle: it can contain stale manifests
# and absolute paths that do not exist inside this Linux image.
RUN rm -rf .vinext && npm run build

FROM node:22-bookworm-slim

WORKDIR /app
COPY --from=build /app /app
EXPOSE 8083
# The Sites/Vinext build emits a Cloudflare Worker plus its static assets.
# Wrangler serves both consistently; `vinext start` serves SSR but misses the
# generated `audit/_next` assets in this build.
CMD ["./node_modules/.bin/wrangler", "dev", "--config", "dist/server/wrangler.json", "--ip", "0.0.0.0", "--port", "8083", "--local"]
