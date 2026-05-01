# Import procrastinate app so it's available when Django starts
from .procrastinate import app as procrastinate_app

__all__ = ("procrastinate_app",)
