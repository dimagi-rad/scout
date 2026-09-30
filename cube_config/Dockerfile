FROM cubejs/cube:v1.6.39

COPY package.json package-lock.json /cube/conf/
COPY cube.js /cube/conf/cube.js
COPY connection-slots.js /cube/conf/connection-slots.js
COPY validator-core.js /cube/conf/validator-core.js
COPY validator-server.js /cube/conf/validator-server.js
COPY validator-compiler.js /cube/conf/validator-compiler.js
COPY validator-worker.js /cube/conf/validator-worker.js
COPY bounded-local-cache.js /cube/conf/bounded-local-cache.js
COPY pinned-patch.js /cube/conf/pinned-patch.js
COPY patch-local-cache.js /cube/conf/patch-local-cache.js
COPY idle-orchestrator-storage.js /cube/conf/idle-orchestrator-storage.js
COPY patch-orchestrator-storage.js /cube/conf/patch-orchestrator-storage.js
COPY start-cube-with-validator.sh /start-cube-with-validator.sh

WORKDIR /cube/conf
RUN npm ci --omit=dev \
    && node /cube/conf/patch-local-cache.js \
    && node /cube/conf/patch-orchestrator-storage.js \
    && chmod +x /start-cube-with-validator.sh

CMD ["/bin/bash", "-c", "node /cube/conf/patch-orchestrator-storage.js --verify && node /cube/conf/patch-local-cache.js --verify && exec /start-cube-with-validator.sh"]
