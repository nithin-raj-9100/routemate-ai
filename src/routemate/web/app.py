from __future__ import annotations

from flask import Flask, jsonify, render_template
from werkzeug.exceptions import HTTPException

from ..config import Settings, load_settings
from ..services import Services, build_services
from .api import api


def create_app(settings: Settings | None = None, services: Services | None = None) -> Flask:
    app = Flask(__name__)
    app.json.sort_keys = False
    app.json.ensure_ascii = False
    app.extensions["routemate"] = services or build_services(settings or load_settings())
    app.register_blueprint(api)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        return jsonify({"error": {"code": exc.name.upper().replace(" ", "_"), "message": exc.description}}), exc.code

    @app.errorhandler(ValueError)
    def value_error(exc: ValueError):
        return jsonify({"error": {"code": "BAD_REQUEST", "message": str(exc)}}), 400

    return app
