from django.core.management.base import BaseCommand

from apitesting import collection_runner


class Command(BaseCommand):
    help = ('Run API collection runs and schedules in this process. '
            'Start the web server with RUN_EXECUTION=worker so it only queues runs.')

    def add_arguments(self, parser):
        parser.add_argument('--poll', type=float, default=2.0, help='seconds to wait when the queue is empty')
        parser.add_argument('--once', action='store_true', help='process what is queued once, then exit')

    def handle(self, *args, **opts):
        self.stdout.write('Run worker started. Ctrl+C to stop.')
        try:
            collection_runner.worker_loop(poll=opts['poll'], once=opts['once'])
        except KeyboardInterrupt:
            self.stdout.write('Worker stopped.')
