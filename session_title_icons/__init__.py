"""Session-title emoji plugin entry point. No import-time actions."""


def register(ctx):
    from .plugin import register as install
    return install(ctx)
