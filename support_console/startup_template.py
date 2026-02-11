"""Template for IPython kernel startup code."""

# Rendered via string concatenation, NOT str.format(), because custom_code
# may contain arbitrary Python with brace literals (dicts, f-strings, sets).
_STARTUP_HEADER = """\
# Support Console: auto-injected startup
print("Initializing Support Console kernel...")

"""

_STARTUP_FOOTER = """
print("Support Console ready.")
"""


def render_startup(custom_code: str = "") -> str:
    """Wrap custom code in the startup template.

    Uses string concatenation to avoid any template-substitution bugs
    with brace characters in user-provided Python code.
    """
    return _STARTUP_HEADER + custom_code + _STARTUP_FOOTER


# Keep DEFAULT_STARTUP for backward compatibility with tests that reference it.
DEFAULT_STARTUP = _STARTUP_HEADER + "{custom_startup}" + _STARTUP_FOOTER

FLASK_STARTUP = """\
from {app_factory_module} import {app_factory_func}
from {db_module} import {db_var}

app = {app_factory_func}()
ctx = app.app_context()
ctx.push()

from {models_module} import *

print(f"DB: {{{db_var}.engine.url.database}}")
try:
    subclasses = {db_var}.Model.__subclasses__()
    names = ', '.join(m.__name__ for m in subclasses[:10])
    suffix = '...' if len(subclasses) > 10 else ''
    print(f"Models: {{names}}{{suffix}}")
except Exception:
    pass
"""
