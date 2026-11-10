from django.apps import AppConfig


class ApitestingConfig(AppConfig):
    name = 'apitesting'

    def ready(self):
        # collection schedules run inside the web server process (not in tests or other manage.py commands)
        import os
        import sys
        if 'runserver' in sys.argv and (os.environ.get('RUN_MAIN') == 'true' or '--noreload' in sys.argv):
            from .collection_runner import start_scheduler
            start_scheduler()
