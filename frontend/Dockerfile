FROM node:20-alpine

WORKDIR /app

# Install dependencies first so the layer is cached across source changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev"]
