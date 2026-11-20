FROM node:24-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY server.js validation.js ./
USER node
ENV NODE_OPTIONS=--max-old-space-size=160
CMD ["node", "server.js"]
