"""Official Cloudflare Python Workers WSGI entrypoint."""

from workers import wsgi

from contacts.app import create_app

app = create_app({"APP_ENV": "production"})
Default = wsgi.entrypoint(app)
