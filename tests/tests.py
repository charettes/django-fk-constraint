import uuid

from django.core.checks import Error
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import isolate_apps

from fk_constraint import ForeignKeyConstraint

from .models import Product, ProductPrice, Tenant


@isolate_apps("tests")
class SchemaTests(TransactionTestCase):
    def test_create_model_constraint(self):
        class Foo(models.Model):
            pass

        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(Foo)

    def test_add_constraint(self):
        with connection.cursor() as cursor:
            relations = connection.introspection.get_relations(
                cursor, ProductPrice._meta.db_table
            )
        self.assertEqual(
            relations,
            {
                "tenant_id": ("id", "tests_tenant"),
                "product_uuid": ("uuid", "tests_product"),
            },
        )

    def test_drop_constraint(self):
        with connection.cursor() as cursor:
            pass


class ForeignKeyConstraintTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create()
        cls.product = Product.objects.create(tenant=cls.tenant)
        cls.product_price = ProductPrice.objects.create(
            product=cls.product,
            price="13.37",
        )


class EnforcementTests(ForeignKeyConstraintTestCase):
    def test_integrity_error(self):
        with self.assertRaises(IntegrityError):
            ProductPrice.objects.create(
                tenant=self.tenant, product_uuid=uuid.uuid4(), price=42
            )


class ValidateTests(ForeignKeyConstraintTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.constraint = ForeignKeyConstraint(
            Product,
            from_fields=["tenant", "product_uuid"],
            to_fields=["tenant", "uuid"],
            name="product_price_product_fk",
        )
        cls.instance = ProductPrice(
            tenant=cls.tenant, product_uuid=uuid.uuid4(), price=42
        )

    def test_validation_error(self):
        expected_msg = (
            f"product instance with (tenant, uuid) (1, UUID('{self.instance.product_uuid}')) "
            "is not a valid choice."
        )
        with self.assertRaisesMessage(ValidationError, expected_msg) as ctx:
            self.constraint.validate(
                ProductPrice,
                self.instance,
            )
        self.assertEqual(ctx.exception.code, "invalid")
        self.assertEqual(
            ctx.exception.params,
            {
                "model": "product",
                "field": "(tenant, uuid)",
                "fields": ["tenant", "uuid"],
                "value": f"(1, UUID('{self.instance.product_uuid}'))",
                "values": [1, self.instance.product_uuid],
            },
        )

    def test_validation_exclude(self):
        instance = ProductPrice(tenant=self.tenant, product_uuid=uuid.uuid4(), price=42)
        excludes = [
            ["tenant"],
            ["product_uuid"],
            ["tenant", "product_uuid"],
        ]
        for exclude in excludes:
            with self.subTest(exclude=exclude):
                self.constraint.validate(ProductPrice, instance, exclude=exclude)
        with self.assertRaises(ValidationError):
            self.constraint.validate(ProductPrice, instance, exclude=["price"])

    def test_validation_error_code_message_overrides(self):
        constraint = ForeignKeyConstraint(
            Product,
            from_fields=["tenant", "product_uuid"],
            to_fields=["tenant", "uuid"],
            name="product_price_product_fk",
            violation_error_code="code",
            violation_error_message="message",
        )
        with self.assertRaisesMessage(ValidationError, "message") as ctx:
            constraint.validate(
                ProductPrice,
                self.instance,
            )
        self.assertEqual(ctx.exception.code, "code")
        self.assertEqual(
            ctx.exception.params,
            {
                "model": "product",
                "field": "(tenant, uuid)",
                "fields": ["tenant", "uuid"],
                "value": f"(1, UUID('{self.instance.product_uuid}'))",
                "values": [1, self.instance.product_uuid],
            },
        )


@isolate_apps("tests")
class CheckTests(SimpleTestCase):
    def test_referenced_model(self):
        class Foo(models.Model):
            foo = models.IntegerField()
            bar = models.IntegerField()

        constraint = ForeignKeyConstraint(
            to_model="app.Bar",
            from_fields=["foo", "bar"],
            to_fields=["foo", "bar"],
            name="name",
        )
        self.assertEqual(
            constraint.check(Foo, connection),
            [
                Error(
                    "Referenced model app.Bar is not registered.",
                    obj=constraint,
                    id="fk_constraint.E001",
                )
            ],
        )
