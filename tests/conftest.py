from __future__ import annotations

import pytest

from routemate import db
from routemate.config import Settings
from routemate.nlp.assistant import Assistant
from routemate.planner.planner import RoutePlanner
from routemate.services import Services
from routemate.web.app import create_app

NINE_AM = 9 * 60


@pytest.fixture(scope="session")
def network():
    conn, net = db.open_network(":memory:")
    return net


@pytest.fixture(scope="session")
def planner(network):
    return RoutePlanner(network, now=lambda: NINE_AM)


def make_services(gemini=None) -> Services:
    conn, net = db.open_network(":memory:")
    planner = RoutePlanner(net, now=lambda: NINE_AM)
    settings = Settings(":memory:", None, "test-model", gemini is not None, "127.0.0.1", 0)
    return Services(settings, conn, net, planner, Assistant(planner, gemini), gemini)


@pytest.fixture
def client():
    app = create_app(services=make_services())
    app.testing = True
    return app.test_client()
