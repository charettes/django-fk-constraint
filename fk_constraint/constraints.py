from itertools import product
from typing import Iterable

from django.core.checks import CheckMessage, Error
from django.core.exceptions import FieldError, ValidationError
from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Deferrable
from django.db.models.fields.related import resolve_relation
from django.db.utils import DEFAULT_DB_ALIAS


class ForeignKeyConstraint(models.constraints.BaseConstraint):
    violation_error_code = "invalid"

    def __init__(
        self,
        to_model: type[models.Model] | str,
        *,
        from_fields: list[str],
        to_fields: list[str],
        name: str,
        deferrable: Deferrable | None = None,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
    ):
        self.to_model = to_model
        self.from_fields = from_fields
        self.to_fields = to_fields
        self.deferrable = deferrable
        super().__init__(
            name=name,
            violation_error_code=violation_error_code,
            violation_error_message=violation_error_message,
        )

    def _resolve_to_model(
        self, from_model: type[models.Model] | str
    ) -> type[models.Model]:
        to_model = resolve_relation(from_model, self.to_model)
        if isinstance(to_model, str):
            to_model = from_model._meta.apps.get_model(to_model)
        return to_model

    def _get_sql_params(
        self, model: type[models.Model], schema_editor: BaseDatabaseSchemaEditor
    ) -> dict[str, str]:
        opts = model._meta
        to_opts = self._resolve_to_model(model)._meta
        from_columns = (
            schema_editor.quote_name(opts.get_field(field_name).column)
            for field_name in self.from_fields
        )
        to_columns = (
            schema_editor.quote_name(to_opts.get_field(field_name).column)
            for field_name in self.to_fields
        )
        return {
            "table": schema_editor.quote_name(opts.db_table),
            "name": schema_editor.quote_name(self.name),
            "column": ", ".join(from_columns),
            "to_table": schema_editor.quote_name(to_opts.db_table),
            "to_column": ", ".join(to_columns),
            "deferrable": (
                # XXX: By default Django creates deferrable constraints on
                # backends that supports them as its the SQL standard but fall
                # back to immediate otherwise (e.g. MySQL InnoDB)
                schema_editor._deferrable_constraint_sql(self.deferrable)
                if self.deferrable is not None
                else schema_editor.connection.ops.deferrable_sql()
            ),
        }

    def _constraint_disallowed(self, schema_editor: BaseDatabaseSchemaEditor) -> bool:
        features = schema_editor.connection.features
        if not features.supports_foreign_keys or (
            self.deferrable is Deferrable.DEFERRED
            and not features.can_defer_constraint_checks
        ):
            return True
        return False

    def constraint_sql(
        self, model: type[models.Model], schema_editor: BaseDatabaseSchemaEditor
    ) -> str:
        if self._constraint_disallowed(schema_editor):
            return None
        return (
            "FOREIGN KEY (%(column)s) REFERENCES %(to_table)s (%(to_column)s)%(deferrable)s"
            % self._get_sql_params(model, schema_editor)
        )

    def create_sql(
        self, model: type[models.Model], schema_editor: BaseDatabaseSchemaEditor
    ) -> str:
        if self._constraint_disallowed(schema_editor):
            return None
        return schema_editor.sql_create_fk % self._get_sql_params(model, schema_editor)

    def remove_sql(
        self, model: type[models.Model], schema_editor: BaseDatabaseSchemaEditor
    ) -> str:
        if self._constraint_disallowed(schema_editor):
            return None
        return schema_editor._delete_fk_sql(model, self.name)

    @property
    def normalized_to_model(self) -> str:
        to_model = self.to_model
        if not isinstance(to_model, str):
            to_model = to_model._meta.label_lower
        else:
            to_model = to_model.lower()
        return to_model

    def deconstruct(self):
        path, (), kwargs = super().deconstruct()
        kwargs |= {
            "to_model": self.normalized_to_model,
            "from_fields": self.from_fields,
            "to_fields": self.to_fields,
        }
        if self.deferrable is not None:
            kwargs["deferrable"] = self.deferrable
        return path, (), kwargs

    def __eq__(self, other) -> bool:
        if isinstance(other, ForeignKeyConstraint):
            return (
                self.name == other.name
                and self.normalized_to_model == other.normalized_to_model
                and self.from_fields == other.from_fields
                and self.to_fields == other.to_fields
                and self.deferrable == other.deferrable
            )
        return super().__eq__(other)

    def get_violation_error_message(self) -> str | None:
        if self.violation_error_message == self.default_violation_error_message:
            # Reuse ForeignKey.default_error_messages["invalid"] to avoid shipping
            # translations.
            return models.ForeignKey.default_error_messages["invalid"].replace(
                "%(value)r", "%(value)s"
            )
        return super().get_violation_error_message()

    def validate(
        self,
        model: type[models.Model],
        instance: models.Model,
        exclude: Iterable[str] | None = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        if exclude and set(exclude).intersection(self.from_fields):
            return
        criteria = {}
        to_model = self._resolve_to_model(model)
        opts = model._meta
        to_opts = to_model._meta
        for from_field_name, to_field_name in zip(self.from_fields, self.to_fields):
            from_field = opts.get_field(from_field_name)
            # NULL can't match any remote value.
            if (value := getattr(instance, from_field.attname)) is None:
                return
            to_field = to_opts.get_field(to_field_name)
            criteria[to_field.name] = value
        if not to_model._base_manager.using(using).filter(**criteria).exists():
            violation_error_message = self.get_violation_error_message()
            raise ValidationError(
                violation_error_message,
                code=self.violation_error_code,
                params={
                    "model": to_opts.verbose_name,
                    "field": "(%s)" % ", ".join(criteria),
                    "fields": list(criteria),
                    "value": "(%s)" % ", ".join(map(repr, criteria.values())),
                    "values": list(criteria.values()),
                },
            )

    def check(
        self, model: type[models.Model], connection: BaseDatabaseWrapper
    ) -> list[CheckMessage]:
        errors = model._check_local_fields(self.from_fields, "constraints")
        required_db_features = model._meta.required_db_features
        if not (
            connection.features.supports_foreign_keys
            or "supports_foreign_keys" in required_db_features
        ):
            errors.append(
                Warning(
                    f"Database {connection.alias} does not support foreign keys.",
                    hint=(
                        "A constraint won't be created. Silence this warning if you "
                        "don't care about it."
                    ),
                    obj=self,
                    id="fk_constraint.W001",
                )
            )
        if self.deferrable is Deferrable.DEFERRED and not (
            # XXX: It'd be better to have a proper feature flag for this.
            connection.features.can_defer_constraint_checks
            or "can_defer_constraint_checks" in required_db_features
        ):
            errors.append(
                Warning(
                    f"Database {connection.alias} does not support deferrable foreign keys.",
                    hint=(
                        "A constraint won't be created. Silence this warning if you "
                        "don't care about it."
                    ),
                    obj=self,
                    id="fk_constraint.W002",
                )
            )
        try:
            to_model = self._resolve_to_model(model)
        except LookupError:
            errors.append(
                Error(
                    f"Referenced model {self.to_model} is not registered.",
                    obj=self,
                    id="fk_constraint.E001",
                )
            )
            return errors
        errors.extend(to_model._check_local_fields(self.to_fields, "constraints"))
        to_opts = to_model._meta
        to_fields = {}
        for field_name in self.to_fields:
            try:
                to_field = to_opts.get_field(field_name)
            except FieldError:
                # A field is missing, to_model._check_local_fields will
                # include the proper error.
                return errors
            to_fields[to_field] = {to_field.name, to_field.attname}
        # Build the product of names that can be used to reference a field to
        # support cases where a foreign key might be referenced either by its
        # name or _id attname.
        to_field_refs = list(map(set, product(*to_fields.values())))
        to_pk_field_names = [pk_field.name for pk_field in to_opts.pk_fields]
        if not (
            any(to_field.unique for to_field in to_fields)
            or any(
                any(
                    to_field_ref.issuperset(unique_together)
                    for to_field_ref in to_field_refs
                )
                for unique_together in to_opts.unique_together
            )
            or any(
                any(
                    to_field_ref.issuperset(constraint.fields)
                    for to_field_ref in to_field_refs
                )
                for constraint in to_opts.total_unique_constraints
            )
            or any(
                to_field_ref.issuperset(to_pk_field_names)
                for to_field_ref in to_field_refs
            )
        ):
            errors.append(
                Error(
                    f"Referenced fields {tuple(self.to_fields)!r} from model "
                    f"{to_opts.label} are not unique together.",
                    hint=(
                        "Referenced fields should be a superset of a `unique_together` member, "
                        "a superset of UniqueConstraint(fields), or part of a composite primary key."
                    ),
                    id="fk_constraint.E002",
                )
            )
        return errors

    # XXX: Kept for backward compatibility.
    def _check(
        self, model: type[models.Model], connection: BaseDatabaseWrapper
    ) -> list[CheckMessage]:
        return self.check(model, connection)
