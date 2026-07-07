# Build stage
FROM node:20-alpine AS builder
WORKDIR /app

# Use npmmirror (faster from China intranet)
RUN npm config set registry https://registry.npmmirror.com

COPY package*.json ./
RUN npm install --no-audit --no-fund

COPY . .
RUN npm run build

# Serve stage
FROM nginx:1.27-alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 5173

CMD ["nginx", "-g", "daemon off;"]
