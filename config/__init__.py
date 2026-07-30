# Side-effecting import so the procrastinate app is registered when Django starts
from .procrastinate import app as procrastinate_app

__all__ = ("procrastinate_app",)
