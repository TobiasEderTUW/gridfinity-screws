# Build context is the repo root (see docker-compose.yml), so the label font can come
# straight from generator/assets instead of a second copy in web/public.
FROM nginx:1.27-alpine
COPY web/nginx.conf /etc/nginx/templates/default.conf.template
ENV API_PORT=8000
COPY web/public/ /usr/share/nginx/html/
COPY generator/assets/fonts/ /usr/share/nginx/html/fonts/
EXPOSE 80
