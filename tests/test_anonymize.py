import random

import pytest

from email_domain_scrubber.anonymize import generate_token, is_token
from email_domain_scrubber.errors import AnonymizationSpaceExhausted


def test_token_format():
    token = generate_token(set(), random.Random(0))
    assert is_token(token)
    assert token.startswith('anon')
    assert len(token) == len('anon0000')


def test_avoids_taken_tokens():
    rng = random.Random(1)
    taken: set[str] = set()
    for _ in range(200):
        token = generate_token(taken, rng)
        assert token not in taken
        taken.add(token)
    assert len(taken) == 200


def test_raises_when_the_space_is_exhausted():
    taken = {f'anon{n:04d}' for n in range(10_000)}
    with pytest.raises(AnonymizationSpaceExhausted):
        generate_token(taken, random.Random(0))


@pytest.mark.parametrize('value', ['anon3746', ' anon0000 '])
def test_is_token_accepts(value):
    assert is_token(value)


@pytest.mark.parametrize('value', ['', 'anon374', 'anon37460', 'anonabcd', 'smithlab.io'])
def test_is_token_rejects(value):
    assert not is_token(value)
