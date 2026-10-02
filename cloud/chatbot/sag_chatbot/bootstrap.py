"""Optional chatbot launcher; install scoped hooks before stock routes import."""
from __future__ import annotations

import sys


def create_app():
    from .hooks import attach, install

    install()
    from sag_api.main import app

    return attach(app)


def main():
    import uvicorn

    sys.argv = ["uvicorn", "sag_chatbot.bootstrap:create_app", "--factory", *sys.argv[1:]]
    uvicorn.main()


if __name__ == "__main__":
    main()
