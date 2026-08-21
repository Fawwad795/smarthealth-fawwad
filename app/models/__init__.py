"""Registry of every ORM model in the application.

SQLAlchemy registers a table in Base.metadata as a side effect of the model
class being defined, and Python only executes a class statement when
something imports its module. A model file that nothing imports is therefore
invisible to SQLAlchemy, and so invisible to Alembic.

migrations/env.py imports this package for exactly that reason. A model
missing from the list below will not be seen by
`alembic revision --autogenerate` -- and if its table already exists in the
database, the diff reads as "this model was deleted" and the generated
migration will contain op.drop_table(). So: every new model gets a line
here, in the same commit that adds the model.

Import order does not matter. Models refer to one another by string --
ForeignKey("providers.id"), relationship("Provider") -- which SQLAlchemy
resolves once every class has loaded, so no model module imports another and
no circular import can form. Keep the list alphabetical.

The names are re-exported through __all__ rather than silenced with
`# noqa: F401`. Both stop the linter flagging an import that is never used,
but __all__ also states the package's public surface and gives one list to
check against.
"""

__all__: list[str] = []
