# --- dev stage: used by docker-compose.override.yml for hot reload ---
FROM node:22-alpine AS dev
WORKDIR /srv
COPY package.json ./
RUN npm install
COPY . .
EXPOSE 80
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "80"]

# --- build stage ---
FROM node:22-alpine AS build
WORKDIR /srv
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

# --- production: static files behind nginx, /api proxied to the API service ---
FROM nginx:alpine AS prod
COPY --from=build /srv/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
