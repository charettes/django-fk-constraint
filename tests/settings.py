SECRET_KEY = "not-secret-anymore"

TIME_ZONE = "America/Montreal"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
    },
}

INSTALLED_APPS = [
    "tests",
]

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

USE_TZ = False
