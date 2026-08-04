"""Translation of Google's HTTP failures into messages a caller can act on.

These exercise `GoogleBackend` — the only part of the package that talks to Google — against
stub request objects, so no network or credentials are involved.
"""

import httplib2
import pytest
from googleapiclient.errors import HttpError

from email_domain_scrubber import sheets
from email_domain_scrubber.errors import (
    AccessDenied,
    MissingScopes,
    ScrubberError,
    WorkbookNotFound,
)


def _http_error(status: int, message: str, reason: str = 'failed') -> HttpError:
    body = (
        f'{{"error": {{"code": {status}, "message": "{message}", '
        f'"errors": [{{"reason": "{reason}"}}]}}}}'
    )
    return HttpError(httplib2.Response({'status': status}), body.encode())


class _Request:
    """Stands in for a googleapiclient request; raises, or returns, on demand."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def execute(self):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else {'ok': True}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(sheets.time, 'sleep', lambda _seconds: None)


def test_missing_scope_403_explains_how_to_re_login():
    request = _Request(
        _http_error(
            403, 'Request had insufficient authentication scopes.', 'insufficientPermissions'
        )
    )
    with pytest.raises(MissingScopes, match='gcloud auth application-default login --scopes='):
        sheets._execute(request)


def test_a_403_on_an_unshared_file_is_not_mistaken_for_a_missing_scope():
    """Google sends `insufficientPermissions` for both, so only the message tells them apart."""
    request = _Request(
        _http_error(403, 'The caller does not have permission', 'insufficientPermissions')
    )
    with pytest.raises(AccessDenied, match='signed-in user'):
        sheets._execute(request)


def test_404_points_at_the_url_and_sharing():
    request = _Request(_http_error(404, 'File not found: abc'))
    with pytest.raises(WorkbookNotFound, match='shared with the signed-in account'):
        sheets._execute(request)


def test_rate_limit_is_retried_then_succeeds():
    request = _Request(_http_error(429, 'Quota exceeded'), {'spreadsheetId': 'abc'})
    assert sheets._execute(request) == {'spreadsheetId': 'abc'}
    assert request.calls == 2


def test_rate_limit_gives_up_after_the_retry_budget():
    request = _Request(*[_http_error(429, 'Quota exceeded')] * 5)
    with pytest.raises(ScrubberError, match='after retries'):
        sheets._execute(request)
    assert request.calls == sheets._RETRY_ATTEMPTS


def test_an_untranslated_error_is_re_raised_as_is():
    original = _http_error(400, 'Invalid range')
    request = _Request(original)
    with pytest.raises(HttpError) as caught:
        sheets._execute(request)
    assert caught.value is original


def test_a_400_is_not_retried():
    request = _Request(_http_error(400, 'Invalid range'))
    with pytest.raises(HttpError):
        sheets._execute(request)
    assert request.calls == 1
