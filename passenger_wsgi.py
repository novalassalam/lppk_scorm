"""
Penghubung untuk cPanel "Setup Python App" (Passenger, WSGI).
  Application startup file : passenger_wsgi.py
  Application Entry point  : application
"""

import os, sys, threading

sys.path.insert(0, os.path.dirname(__file__))

from a2wsgi import ASGIMiddleware

import app as web

# Passenger tidak menjalankan lifespan FastAPI, jadi jalankan manual di sini
web.recover_jobs()
threading.Thread(target=web.cleanup_loop, daemon=True).start()

application = ASGIMiddleware(web.app)
