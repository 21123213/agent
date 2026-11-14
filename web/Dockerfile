# 前端：构建静态资源后用 nginx 托管，并把 /api 反代到 server
FROM node:20-alpine AS build

WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .
# 生产走同域相对路径 /api（由 nginx 反代），不要写死 localhost
ENV VITE_API_BASE_URL=
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
