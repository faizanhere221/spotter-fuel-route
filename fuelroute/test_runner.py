import logging

from django.test.runner import DiscoverRunner


class QuietTestRunner(DiscoverRunner):
    """Drop the console handler of the `routing` logger during tests.

    `assertLogs` still works: it attaches its own handler to the logger under test.
    """

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        logging.getLogger("routing").handlers = [logging.NullHandler()]
