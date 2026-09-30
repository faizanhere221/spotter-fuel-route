import logging

from django.test.runner import DiscoverRunner


def _blocked_request(*args, **kwargs):
    raise RuntimeError("Real ORS HTTP request attempted during tests; mock routing.services.ors._session.request")


class QuietTestRunner(DiscoverRunner):
    """Drop the `routing` logger's console handler and block real ORS HTTP during tests.

    `assertLogs` still works: it attaches its own handler to the logger under test.
    Tests that need ORS patch `routing.services.ors._session.request` (mock.patch.object).
    """

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        logging.getLogger("routing").handlers = [logging.NullHandler()]
        from routing.services import ors

        self._real_request = ors._session.request
        ors._session.request = _blocked_request

    def teardown_test_environment(self, **kwargs):
        from routing.services import ors

        ors._session.request = self._real_request
        super().teardown_test_environment(**kwargs)
