"""Container layer directory extractor.

``app.engine`` is dependency-free (standard library only). The FastAPI
application is exposed lazily so that importing the engine does not pull in
the web stack::

    uvicorn app.main:app
    uvicorn app:app        # lazy attribute below
"""

__version__ = "1.0.0"


def __getattr__(name: str):
    if name == "app":
        from .main import app as fastapi_app

        return fastapi_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["__version__"]
