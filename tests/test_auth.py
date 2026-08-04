"""Credential selection and the rclone credential source.

No network: these check that the right credential object is built from config, and that bad
config produces a message saying what to fix.
"""

import json

import pytest

from email_domain_scrubber import auth
from email_domain_scrubber.errors import RcloneConfigError

TOKEN = {
    'access_token': 'expired-access-token',
    'refresh_token': 'the-refresh-token',
    'token_type': 'Bearer',
    'expiry': '2020-01-01T00:00:00.000000-05:00',
}


def _write_config(tmp_path, **overrides):
    section = {
        'type': 'drive',
        'client_id': 'client-id.apps.googleusercontent.com',
        'client_secret': 'client-secret',
        'scope': 'drive',
        'token': json.dumps(TOKEN),
        'team_drive': '0APoqfDXp-2lcUk9PVA',
    }
    section.update(overrides)
    lines = ['[aso]'] + [f'{key} = {value}' for key, value in section.items() if value is not None]
    path = tmp_path / 'rclone.conf'
    path.write_text('\n'.join(lines) + '\n')
    return path


def test_builds_credentials_from_an_rclone_drive_remote(tmp_path):
    creds = auth.rclone_credentials('aso', _write_config(tmp_path))
    assert creds.refresh_token == 'the-refresh-token'
    assert creds.client_id == 'client-id.apps.googleusercontent.com'
    assert creds.token_uri == auth._TOKEN_URI
    # rclone's "drive" is the full auth/drive scope, which the Sheets API also accepts.
    assert creds.scopes == [auth.DRIVE_SCOPE]


def test_expired_stored_token_is_reported_expired_so_it_refreshes_proactively(tmp_path):
    """rclone's stored access token is usually stale; carrying its expiry over avoids a 401."""
    creds = auth.rclone_credentials('aso', _write_config(tmp_path))
    assert creds.token == 'expired-access-token'
    assert creds.expired
    assert not creds.valid
    assert creds.refresh_token


def test_expiry_is_converted_to_naive_utc():
    # 14:32-04:00 is 18:32Z; google-auth compares against naive UTC and would otherwise be 4h off.
    parsed = auth._parse_expiry('2026-07-19T14:32:42.604204-04:00')
    assert parsed is not None
    assert parsed.tzinfo is None
    assert (parsed.hour, parsed.minute) == (18, 32)


@pytest.mark.parametrize('value', [None, '', 'not a timestamp'])
def test_unparseable_expiry_degrades_to_reactive_refresh(value):
    assert auth._parse_expiry(value) is None


def test_missing_config_file_says_what_to_set(tmp_path):
    with pytest.raises(RcloneConfigError, match='RCLONE_CONFIG'):
        auth.rclone_credentials('aso', tmp_path / 'nope.conf')


def test_unknown_remote_lists_the_available_ones(tmp_path):
    with pytest.raises(RcloneConfigError, match='Available remotes: aso'):
        auth.rclone_credentials('missing', _write_config(tmp_path))


def test_non_drive_remote_is_rejected(tmp_path):
    with pytest.raises(RcloneConfigError, match='not "drive"'):
        auth.rclone_credentials('aso', _write_config(tmp_path, type='onedrive'))


def test_readonly_scope_is_rejected_because_redaction_writes(tmp_path):
    with pytest.raises(RcloneConfigError, match='full "drive" scope'):
        auth.rclone_credentials('aso', _write_config(tmp_path, scope='drive.readonly'))


def test_rclone_builtin_oauth_client_is_rejected(tmp_path):
    """Without client_id/secret rclone uses its own client, whose secret is not in the config."""
    with pytest.raises(RcloneConfigError, match='built-in OAuth client'):
        auth.rclone_credentials('aso', _write_config(tmp_path, client_id='', client_secret=''))


def test_token_without_a_refresh_token_is_rejected(tmp_path):
    token = json.dumps({'access_token': 'only-this'})
    with pytest.raises(RcloneConfigError, match='no refresh token'):
        auth.rclone_credentials('aso', _write_config(tmp_path, token=token))


def test_unparseable_token_is_rejected(tmp_path):
    with pytest.raises(RcloneConfigError, match='unreadable token'):
        auth.rclone_credentials('aso', _write_config(tmp_path, token='{not json'))


def test_credentials_prefers_rclone_when_the_remote_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv(auth.RCLONE_REMOTE_ENV, 'aso')
    monkeypatch.setenv('RCLONE_CONFIG', str(_write_config(tmp_path)))
    assert auth.credentials().refresh_token == 'the-refresh-token'


def test_credentials_falls_back_to_adc(monkeypatch):
    monkeypatch.delenv(auth.RCLONE_REMOTE_ENV, raising=False)
    monkeypatch.setattr(auth, 'adc_credentials', lambda: 'adc-sentinel')
    assert auth.credentials() == 'adc-sentinel'


def test_config_path_honours_rclones_own_override(monkeypatch):
    monkeypatch.setenv('RCLONE_CONFIG', '/somewhere/else.conf')
    assert str(auth.rclone_config_path()) == '/somewhere/else.conf'


def test_config_path_defaults_to_the_rclone_location(monkeypatch):
    monkeypatch.delenv('RCLONE_CONFIG', raising=False)
    assert auth.rclone_config_path().parts[-3:] == ('.config', 'rclone', 'rclone.conf')
