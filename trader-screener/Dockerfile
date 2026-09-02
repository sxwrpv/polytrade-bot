FROM node:20-alpine

ARG GIT_REVISION=unknown
ARG BUILD_TIME=""
ENV GIT_REVISION=${GIT_REVISION} \
    BUILD_TIME=${BUILD_TIME}
LABEL org.opencontainers.image.revision=${GIT_REVISION} \
      org.opencontainers.image.created=${BUILD_TIME} \
      org.opencontainers.image.source="https://github.com/sxwrpv/polytrade-bot"

WORKDIR /app
COPY --chown=node:node package.json server.mjs ./
COPY --chown=node:node lib/ lib/
COPY --chown=node:node public/ public/
COPY --chown=node:node data/ data/

USER node
EXPOSE 4310
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:4310/api/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]

CMD ["node", "server.mjs"]
