from __future__ import annotations

from inspect import signature

from atlasrag.runtime.composition import ApplicationComposition, SharedResources
from atlasrag.runtime.production import build_default_business


def test_default_production_composition_is_available() -> None:
    parameter = signature(ApplicationComposition.start).parameters["build_business"]
    assert parameter.default is None
    assert callable(build_default_business)
    assert "guarded_mineru" in SharedResources.__dataclass_fields__
    assert "deepseek_http" in SharedResources.__dataclass_fields__
    assert "web_http" in SharedResources.__dataclass_fields__
