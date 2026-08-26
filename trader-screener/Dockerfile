FROM node:20-alpine

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
