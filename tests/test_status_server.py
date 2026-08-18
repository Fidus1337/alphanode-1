"""The status/health HTTP servers must bind WITHOUT a reverse-DNS lookup.

Production failure this file guards against: python's stock HTTPServer.server_bind()
calls socket.getfqdn() on the bind address. On hosts with broken PTR resolution —
every Windows GitHub runner, and plenty of customer Windows desktops — that lookup
hangs for MINUTES, so the node status page (which the GUI polls) and the signal
/health endpoint never come up. Caught live by the CI hang dump on v1.4.3:
    Thread 0x0000185c: socket.py:794 getfqdn <- http/server.py server_bind <- node.serve
Both servers now use a server_bind that never touches DNS; these tests fail if anyone
reverts to the stock class.
"""
import socket

import pytest

import signal_service as ss


def _forbid_getfqdn(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError('socket.getfqdn called at bind time (reverse DNS hang on Windows)')
    monkeypatch.setattr(socket, 'getfqdn', _boom)


def _assert_binds_instantly(server_cls, handler_cls):
    srv = server_cls(('127.0.0.1', 0), handler_cls)   # port 0: the OS picks a free one
    try:
        assert srv.server_port == srv.server_address[1] > 0
        assert srv.server_name                          # set, but never via DNS
    finally:
        srv.server_close()


def test_signal_health_server_binds_without_dns(monkeypatch):
    _forbid_getfqdn(monkeypatch)
    _assert_binds_instantly(ss._NoDNSHTTPServer, ss.Handler)


def test_node_status_server_binds_without_dns(monkeypatch):
    node = pytest.importorskip('node')                 # sandboxed env from conftest
    _forbid_getfqdn(monkeypatch)
    _assert_binds_instantly(node._NoDNSHTTPServer, node.Handler)
