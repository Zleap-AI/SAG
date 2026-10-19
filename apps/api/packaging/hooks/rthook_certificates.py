"""Load portable trust roots before HTTP clients cache their SSL contexts."""

import os

import certifi

# Explicit trust settings, including empty or invalid ones, belong to the user.
if "SSL_CERT_FILE" not in os.environ and "SSL_CERT_DIR" not in os.environ:
    os.environ["SSL_CERT_FILE"] = certifi.where()
