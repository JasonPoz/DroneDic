import os
import sys

PROJECT_ROOT = "/home/c/co811774/public_html"
VENV_SITE_PACKAGES = "/home/c/co811774/public_html/venv/lib/python3.10/site-packages"

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if VENV_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, VENV_SITE_PACKAGES)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dronedic.settings")

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()