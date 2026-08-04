"""Generation of `anonNNNN` replacement tokens.

Tokens are random rather than sequential so that a published report leaks neither the order in
which domains were discovered nor how many exist, and they are not derived from the domain so
that the mapping cannot be recovered by hashing a guessed domain list.
"""

import random
import re

from .errors import AnonymizationSpaceExhausted

TOKEN_PATTERN = re.compile(r'^anon\d{4}$')
_SPACE = 10_000


def is_token(value: str) -> bool:
    return bool(TOKEN_PATTERN.match(value.strip()))


def generate_token(taken: set[str], rng: random.Random | None = None) -> str:
    """Return an `anonNNNN` token that is not in `taken`."""
    chooser = rng or random.SystemRandom()
    if len(taken & {f'anon{n:04d}' for n in range(_SPACE)}) >= _SPACE:
        raise AnonymizationSpaceExhausted(
            f'All {_SPACE} anonNNNN tokens are in use in this analysis workbook; '
            'widen the token format before analyzing more domains.'
        )
    while True:
        token = f'anon{chooser.randrange(_SPACE):04d}'
        if token not in taken:
            return token
