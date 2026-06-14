"""Lock the OpenAPI app version to the installed package metadata.

Regression guard (M-6/S-1.1): the FastAPI ``version`` was once a hardcoded
``"0.1.0"`` literal that drifted from the real release tag, so ``/docs`` reported
the wrong version even in a correctly-stamped bundle. It must derive from
``importlib.metadata`` — see ``_resolve_app_version`` in the search API.
"""
import msa_apps.search_api.app as appmod
from msa_apps.search_api.app import create_app, _resolve_app_version


def test_app_version_equals_package_metadata():
    app = create_app(reset_dependencies=True)
    assert app.version == _resolve_app_version()


def test_app_version_is_wired_to_metadata_not_a_literal(monkeypatch):
    # Force create_app to rebuild (it otherwise reuses the import-time global
    # app) so we observe the version it stamps. If someone re-hardcodes a
    # literal, app.version stops tracking _APP_VERSION and this fails.
    # monkeypatch restores the real global app at teardown.
    monkeypatch.setattr(appmod, "_APP_VERSION", "9.9.9-guard")
    monkeypatch.setattr(appmod, "app", None)
    app = create_app(reset_dependencies=True)
    assert app.version == "9.9.9-guard"


def test_resolve_app_version_returns_nonempty_string():
    v = _resolve_app_version()
    assert isinstance(v, str) and v
