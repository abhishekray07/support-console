"""Template for IPython kernel startup code."""

DEFAULT_STARTUP = """\
# Support Console: auto-injected startup
print("Initializing Support Console kernel...")

{custom_startup}

print("Support Console ready.")
"""

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
