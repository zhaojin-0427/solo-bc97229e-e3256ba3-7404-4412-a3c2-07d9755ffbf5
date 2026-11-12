"""Tiny stdlib stand-ins for fastapi/starlette/pydantic.

The real service uses FastAPI + Pydantic (see requirements.txt and the Docker
image). This offline sandbox has no package index, so tests run against these
minimal, deliberately narrow shims instead. They live outside ``app/`` and are
never shipped in the image.
"""
