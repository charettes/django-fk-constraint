import uuid

from django.db import models
from fk_constraint import ForeignKeyConstraint


class Tenant(models.Model):
    name = models.CharField()


class TenantModel(models.Model):
    tenant = models.ForeignKey(
        Tenant,
        models.CASCADE,
    )
    uuid = models.UUIDField(default=uuid.uuid4)

    pk = models.CompositePrimaryKey("tenant", "uuid")

    class Meta:
        abstract = True


class Product(TenantModel):
    name = models.CharField()


class ProductPrice(TenantModel):
    product_uuid = models.UUIDField()
    product = models.ForeignObject(
        Product,
        models.CASCADE,
        from_fields=["tenant", "product_uuid"],
        to_fields=["tenant", "uuid"],
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        constraints = [
            ForeignKeyConstraint(
                Product,
                from_fields=["tenant", "product_uuid"],
                to_fields=["tenant", "uuid"],
                name="product_price_product_fk",
            )
        ]
