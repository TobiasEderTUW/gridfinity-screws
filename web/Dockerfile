FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/templates/default.conf.template
ENV API_PORT=8000
COPY public/ /usr/share/nginx/html/
EXPOSE 80
