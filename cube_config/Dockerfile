FROM cubejs/cube:v1.6.39

COPY package.json /cube/conf/package.json
COPY cube.js /cube/conf/cube.js
COPY validator-core.js /cube/conf/validator-core.js
COPY validator-server.js /cube/conf/validator-server.js
COPY validator-worker.js /cube/conf/validator-worker.js
COPY start-cube-with-validator.sh /start-cube-with-validator.sh

WORKDIR /cube/conf
RUN npm install --omit=dev && chmod +x /start-cube-with-validator.sh

CMD ["/start-cube-with-validator.sh"]
