from django.apps import AppConfig


class MyappConfig(AppConfig):
    name = 'myapp'

    def ready(self):
        # Registers the project-wide one-device login handlers for both the
        # AI login form and Django's built-in admin login.
        from . import signals  # noqa: F401
        # Counts failed logins for the password-guessing brake.
        from . import login_guard  # noqa: F401
