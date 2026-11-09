"""Fail closed until the hosted server has tested persistent storage."""

import os


def ensure_hosted_storage_supported(environ=None):
    env = os.environ if environ is None else environ
    if env.get("RENDER"):
        raise RuntimeError(
            "Render deployment is not ready: persistent PostgreSQL storage is not implemented"
        )
    if env.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is configured but PostgreSQL storage is not implemented")
