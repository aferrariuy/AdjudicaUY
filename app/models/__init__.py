"""ORM models package.

Importing the model modules here registers them with
:class:`app.database.Base` so Alembic's autogenerate (and the test
suite's ``Base.metadata.create_all``) see the full schema.
"""

from __future__ import annotations

from app.models.adjudicacion import Adjudicacion  # noqa: F401
from app.models.compra import Compra  # noqa: F401
from app.models.oferente import Oferente  # noqa: F401
