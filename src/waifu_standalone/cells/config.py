from __future__ import annotations

import sys

from ..settings_admin import config_manager as _impl

sys.modules[__name__] = _impl
