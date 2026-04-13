"""
Shared model helper utilities.

Small helpers used across management commands and utility modules.
"""


def get_optional_model(model_name):
    """
    Lazy import for optional models.

    Returns (Model, True) or (None, False). Avoids import-time errors
    for models that may not exist in the current schema.

    Args:
        model_name: Name of the model class to look up in animal.models.

    Returns:
        Tuple of (model class or None, bool indicating existence).
    """
    try:
        from animal import models
        return getattr(models, model_name, None), hasattr(models, model_name)
    except Exception:
        return None, False
