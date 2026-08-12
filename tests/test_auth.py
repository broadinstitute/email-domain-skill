"""Reading the rclone credential source, and minting access tokens from it."""

import asyncio
import json

import pytest

from email_domain_scrubber import auth
from email_domain_scrubber.errors import CredentialsExpired, RcloneConfigError, ScrubberError

REFRESH_TOKEN = 'refresh-abc'


def write_config(tmp_path, **overrides):
    section = {
        'type': 'drive',
        'scope': 'drive,drive.readonly,drive.file',
        'client_id': 'client-123',
        'client_secret': 'secret-456',
        'token': json.dumps({'access_token': 'stale', 'refresh_token': REFRESH_TOKEN}),
    }
    section.update(overrides)
    lines = ['[aso]'] + [f'{key} = {value}' for key, value in section.items() if value is not None]
    path = tmp_path / 'rclone.conf'
    path.write_text('\n'.join(lines) + '\n')
    return path


# -- locating the credentials -------------------------------------------------------------------
def test_an_unset_remote_says_what_to_set(monkeypatch):
    monkeypatch.delenv(auth.RCLONE_REMOTE_ENV, raising=False)
    with pytest.raises(ScrubberError, match=auth.RCLONE_REMOTE_ENV):
        auth.configured_remote()


def test_the_login_hint_names_the_configured_remote(monkeypatch):
    monkeypatch.setenv(auth.RCLONE_REMOTE_ENV, 'aso')
    assert auth.login_hint() == 'rclone config reconnect aso:'


def test_the_login_hint_survives_an_unset_remote(monkeypatch):
    monkeypatch.delenv(auth.RCLONE_REMOTE_ENV, raising=False)
    assert auth.RCLONE_REMOTE_ENV in auth.login_hint()


def test_rclone_config_path_honours_rclones_own_override(monkeypatch):
    monkeypatch.setenv('RCLONE_CONFIG', '/somewhere/rclone.conf')
    assert str(auth.rclone_config_path()) == '/somewhere/rclone.conf'


# -- parsing ------------------------------------------------------------------------------------
def test_a_valid_remote_yields_the_oauth_client(tmp_path):
    client = auth.oauth_client('aso', write_config(tmp_path))

    assert client.client_id == 'client-123'
    assert client.client_secret == 'secret-456'
    assert client.refresh_token == REFRESH_TOKEN


def test_a_missing_config_file_says_where_it_looked(tmp_path):
    with pytest.raises(RcloneConfigError, match='No rclone config'):
        auth.oauth_client('aso', tmp_path / 'absent.conf')


def test_an_unknown_remote_lists_the_available_ones(tmp_path):
    with pytest.raises(RcloneConfigError, match='Available remotes: aso'):
        auth.oauth_client('other', write_config(tmp_path))


def test_a_non_drive_remote_is_rejected(tmp_path):
    with pytest.raises(RcloneConfigError, match='not "drive"'):
        auth.oauth_client('aso', write_config(tmp_path, type='s3'))


def test_the_connectors_scopes_are_accepted(tmp_path):
    config = write_config(tmp_path, scope='drive,drive.readonly,drive.file')
    assert auth.oauth_client('aso', config)


def test_the_connectors_scopes_alone_are_enough(tmp_path):
    config = write_config(tmp_path, scope='drive.readonly,drive.file')
    assert auth.oauth_client('aso', config)


def test_scope_lists_tolerate_whitespace(tmp_path):
    config = write_config(tmp_path, scope=' drive.readonly , drive.file ')
    assert auth.oauth_client('aso', config)


def test_the_broad_drive_scope_alone_is_rejected(tmp_path):
    """Verified against the live connector: it wants the exact names, not equivalent authority."""
    with pytest.raises(RcloneConfigError) as caught:
        auth.oauth_client('aso', write_config(tmp_path, scope='drive'))

    message = str(caught.value)
    assert 'drive.readonly' in message and 'drive.file' in message
    assert 'rclone config reconnect aso:' in message


def test_an_absent_scope_is_rejected_like_the_default(tmp_path):
    """rclone's default is plain `drive`, so saying nothing is the same failing configuration."""
    with pytest.raises(RcloneConfigError, match='drive.readonly'):
        auth.oauth_client('aso', write_config(tmp_path, scope=None))


def test_a_partial_scope_list_names_what_is_missing(tmp_path):
    with pytest.raises(RcloneConfigError, match='missing drive.file'):
        auth.oauth_client('aso', write_config(tmp_path, scope='drive,drive.readonly'))


def test_rclones_builtin_client_is_rejected(tmp_path):
    """Its secret is not in the config, so tokens could never be refreshed."""
    with pytest.raises(RcloneConfigError, match='built-in OAuth client'):
        auth.oauth_client('aso', write_config(tmp_path, client_id=None, client_secret=None))


def test_an_unreadable_token_is_reported(tmp_path):
    with pytest.raises(RcloneConfigError, match='unreadable token'):
        auth.oauth_client('aso', write_config(tmp_path, token='{not json'))


def test_a_missing_refresh_token_is_reported(tmp_path):
    config = write_config(tmp_path, token=json.dumps({'access_token': 'only'}))
    with pytest.raises(RcloneConfigError, match='no refresh token'):
        auth.oauth_client('aso', config)


def test_an_explicit_client_overrides_the_config(tmp_path, monkeypatch):
    """The connector is billed to the project owning the client, so it must be overridable."""
    monkeypatch.setenv(auth.CLIENT_ID_ENV, 'other-client')
    monkeypatch.setenv(auth.CLIENT_SECRET_ENV, 'other-secret')

    client = auth.oauth_client('aso', write_config(tmp_path))

    assert (client.client_id, client.client_secret) == ('other-client', 'other-secret')
    assert client.refresh_token == REFRESH_TOKEN


def test_an_explicit_client_rescues_rclones_builtin_client(tmp_path, monkeypatch):
    monkeypatch.setenv(auth.CLIENT_ID_ENV, 'other-client')
    monkeypatch.setenv(auth.CLIENT_SECRET_ENV, 'other-secret')
    config = write_config(tmp_path, client_id=None, client_secret=None)

    assert auth.oauth_client('aso', config).client_id == 'other-client'


# -- minting tokens -----------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


def patch_post(monkeypatch, *responses):
    """Replace httpx2's POST with a queue of canned responses, recording the form data."""
    import httpx2

    calls = []
    queued = list(responses)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, data=None):
            calls.append((url, data))
            return queued.pop(0)

    monkeypatch.setattr(httpx2, 'AsyncClient', FakeClient)
    return calls


def client():
    return auth.OAuthClient('client-123', 'secret-456', REFRESH_TOKEN)


def test_a_token_is_minted_from_the_refresh_token(monkeypatch):
    calls = patch_post(
        monkeypatch, FakeResponse(200, {'access_token': 'fresh', 'expires_in': 3600})
    )
    source = auth.TokenSource(client())

    assert asyncio.run(source.token()) == 'fresh'
    url, data = calls[0]
    assert url == auth.TOKEN_URI
    assert data == {
        'client_id': 'client-123',
        'client_secret': 'secret-456',
        'refresh_token': REFRESH_TOKEN,
        'grant_type': 'refresh_token',
    }


def test_a_valid_token_is_reused(monkeypatch):
    calls = patch_post(
        monkeypatch, FakeResponse(200, {'access_token': 'fresh', 'expires_in': 3600})
    )
    source = auth.TokenSource(client())

    assert asyncio.run(source.token()) == 'fresh'
    assert asyncio.run(source.token()) == 'fresh'
    assert len(calls) == 1


def test_an_expiring_token_is_refreshed(monkeypatch):
    """A short lifetime falls inside the safety margin, so it is never treated as valid."""
    calls = patch_post(
        monkeypatch,
        FakeResponse(200, {'access_token': 'first', 'expires_in': 10}),
        FakeResponse(200, {'access_token': 'second', 'expires_in': 3600}),
    )
    source = auth.TokenSource(client())

    assert asyncio.run(source.token()) == 'first'
    assert asyncio.run(source.token()) == 'second'
    assert len(calls) == 2


def test_a_revoked_refresh_token_says_how_to_sign_in_again(monkeypatch):
    monkeypatch.setenv(auth.RCLONE_REMOTE_ENV, 'aso')
    patch_post(
        monkeypatch,
        FakeResponse(400, {'error': 'invalid_grant', 'error_description': 'Bad Request'}),
    )
    source = auth.TokenSource(client())

    with pytest.raises(CredentialsExpired) as caught:
        asyncio.run(source.token())
    assert 'invalid_grant' in str(caught.value)
    assert 'rclone config reconnect aso:' in str(caught.value)


def test_a_non_json_refusal_still_raises_usefully(monkeypatch):
    patch_post(monkeypatch, FakeResponse(503, None))
    source = auth.TokenSource(client())

    with pytest.raises(CredentialsExpired, match='503'):
        asyncio.run(source.token())
