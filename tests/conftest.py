"""Suite-wide no-network guard.

Autouse fixture: any test that attempts a real outbound socket connection fails
with an AssertionError naming the address. This is the backstop for the S2 HTTP
seam migration — today's suite monkeypatches fg.requests.get in ~23 places, and
moving the seam silently un-patches every one of them, so an un-patched test
would reach for the real SteamGridDB. The guard makes that impossible to miss,
now and for every future change.

Must NOT import find_games (importing it creates directories under the real
HOME) and must NOT touch HOME.
"""

import socket

import pytest


@pytest.fixture(autouse=True)
def _no_network_guard(monkeypatch):
    """Make any real socket connect (and connect_ex) fail the test that caused
    it, naming the address it tried to reach."""

    def _blocked(self, address, *args, **kwargs):
        raise AssertionError(
            "no-network guard: test attempted a real socket connection to %r — "
            "patch the HTTP seam instead of reaching the network" % (address,))

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
