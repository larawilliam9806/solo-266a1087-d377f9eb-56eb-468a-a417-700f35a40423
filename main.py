"""Root-level ASGI shim.

Allows both ``uvicorn main:app`` (from /workspace) and
``uvicorn app.main:app`` to start the same application.
"""

from app.main import app  # noqa: F401

if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )
