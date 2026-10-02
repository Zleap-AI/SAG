# Cloud-owned web build overlay; stock sources and stock Dockerfile remain intact.
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
COPY scripts/require-npm.cjs ./scripts/require-npm.cjs
RUN npm ci

FROM python:3.12-alpine AS overlay
COPY . /web
COPY --from=chatbot web /overlay
RUN python /overlay/apply_overlay.py /web

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY --from=overlay /web .
ENV NEXT_TELEMETRY_DISABLED=1
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
ARG NEXT_PUBLIC_ENABLE_WINDOW_SCALING=true
ENV NEXT_PUBLIC_ENABLE_WINDOW_SCALING=$NEXT_PUBLIC_ENABLE_WINDOW_SCALING
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
