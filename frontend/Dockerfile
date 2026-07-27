FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
# The nginx entrypoint runs envsubst over /etc/nginx/templates/*.template at
# container start. Nothing is substituted today, but keeping the template path
# means adding a secret later doesn't require changing the image layout.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 8081
