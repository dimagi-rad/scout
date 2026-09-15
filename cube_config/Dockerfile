FROM cubejs/cube:v1.6.39

COPY package.json /cube/conf/package.json
COPY cube.js /cube/conf/cube.js
COPY validator-core.js /cube/conf/validator-core.js
COPY validator-server.js /cube/conf/validator-server.js
COPY validator-worker.js /cube/conf/validator-worker.js
COPY bounded-local-cache.js /cube/conf/bounded-local-cache.js
COPY patch-local-cache.js /cube/conf/patch-local-cache.js
COPY start-cube-with-validator.sh /start-cube-with-validator.sh

WORKDIR /cube/conf
RUN npm install --omit=dev \
    && node /cube/conf/patch-local-cache.js \
    && chmod +x /start-cube-with-validator.sh

CMD ["/bin/bash", "-c", "node /cube/conf/patch-local-cache.js --verify && exec /start-cube-with-validator.sh"]
