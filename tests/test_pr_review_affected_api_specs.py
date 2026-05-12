"""P1.3: `pr_review.affected_api_specs` tests.

Pins the diff-aware API-spec matching rules:

  - An API flow on `/api/login` matches a spec whose URL path equals
    `/api/login` OR begins with `/api/login/...`.
  - It does NOT match `/api/login-history` (strict-prefix rule with
    a `/` delimiter).
  - URLs with env-substitution prefixes (`${BASE_URL}/api/...`) are
    parsed correctly.
  - Specs with no matching flow are dropped.
  - The result is deterministic (sorted by spec_id).
"""

from __future__ import annotations

from retrace.api_testing import APITestSpec
from retrace.pr_review import (
    AffectedAPISpec,
    AffectedFlow,
    ChangedFile,
    PRReviewAnalysis,
    affected_api_specs,
)


def _analysis(flows: list[AffectedFlow]) -> PRReviewAnalysis:
    return PRReviewAnalysis(
        changed_files=[ChangedFile(path="server/x.py", hunks=[])],
        affected_flows=flows,
        prior_failures=[],
        existing_tests=[],
        missing_tests=[],
    )


def _spec(spec_id: str, *, method: str = "GET", url: str = "/api/login") -> APITestSpec:
    return APITestSpec(
        schema_version=1,
        spec_id=spec_id,
        name=spec_id.replace("_", " "),
        method=method,
        url=url,
    )


def _loader(specs: list[APITestSpec]):
    return lambda: list(specs)


def test_returns_empty_when_no_api_flows():
    analysis = _analysis([AffectedFlow(kind="ui", name="/login", files=[], reason="r")])
    result = affected_api_specs(
        analysis=analysis,
        specs_dir=None,  # type: ignore[arg-type]
        spec_loader=_loader([_spec("s1")]),
    )
    assert result == []


def test_exact_path_match():
    analysis = _analysis([AffectedFlow(kind="api", name="/api/login", files=[], reason="r")])
    result = affected_api_specs(
        analysis=analysis,
        specs_dir=None,  # type: ignore[arg-type]
        spec_loader=_loader([_spec("s1", url="https://api.example.com/api/login")]),
    )
    assert [s.spec_id for s in result] == ["s1"]
    assert result[0].route_path == "/api/login"
    assert result[0].matched_flows == ["/api/login"]
    assert "retrace tester api-run s1" in result[0].command


def test_strict_prefix_match_with_subpath():
    """`/api/users` matches a spec on `/api/users/42`."""
    analysis = _analysis([AffectedFlow(kind="api", name="/api/users", files=[], reason="r")])
    result = affected_api_specs(
        analysis=analysis,
        specs_dir=None,  # type: ignore[arg-type]
        spec_loader=_loader([_spec("s1", url="/api/users/42")]),
    )
    assert [s.spec_id for s in result] == ["s1"]


def test_strict_prefix_does_not_match_similar_substring():
    """`/api/login` must NOT match `/api/login-history` (no
    delimiter). This is the load-bearing correctness test for the
    matching rule."""
    analysis = _analysis([AffectedFlow(kind="api", name="/api/login", files=[], reason="r")])
    result = affected_api_specs(
        analysis=analysis,
        specs_dir=None,  # type: ignore[arg-type]
        spec_loader=_loader([_spec("s1", url="/api/login-history")]),
    )
    assert result == []


def test_env_substitution_prefix_is_stripped():
    """A spec URL like `${BASE_URL}/api/login` should be parsed as
    `/api/login` and match accordingly."""
    analysis = _analysis([AffectedFlow(kind="api", name="/api/login", files=[], reason="r")])
    result = affected_api_specs(
        analysis=analysis,
        specs_dir=None,  # type: ignore[arg-type]
        spec_loader=_loader([_spec("s1", url="${BASE_URL}/api/login")]),
    )
    assert [s.spec_id for s in result] == ["s1"]
    assert result[0].route_path == "/api/login"


def test_trailing_slashes_normalized():
    analysis = _analysis([AffectedFlow(kind="api", name="/api/login/", files=[], reason="r")])
    result = affected_api_specs(
        analysis=analysis,
        specs_dir=None,  # type: ignore[arg-type]
        spec_loader=_loader([_spec("s1", url="https://api.example.com/api/login")]),
    )
    assert [s.spec_id for s in result] == ["s1"]


def test_specs_without_matching_flow_are_dropped():
    analysis = _analysis([AffectedFlow(kind="api", name="/api/login", files=[], reason="r")])
    result = affected_api_specs(
        analysis=analysis,
        specs_dir=None,  # type: ignore[arg-type]
        spec_loader=_loader([
            _spec("s_match", url="/api/login"),
            _spec("s_unrelated", url="/api/orders"),
        ]),
    )
    assert [s.spec_id for s in result] == ["s_match"]


def test_result_is_sorted_by_spec_id():
    analysis = _analysis([AffectedFlow(kind="api", name="/api/login", files=[], reason="r")])
    result = affected_api_specs(
        analysis=analysis,
        specs_dir=None,  # type: ignore[arg-type]
        spec_loader=_loader([
            _spec("zeta_spec", url="/api/login"),
            _spec("alpha_spec", url="/api/login"),
            _spec("middle_spec", url="/api/login"),
        ]),
    )
    assert [s.spec_id for s in result] == ["alpha_spec", "middle_spec", "zeta_spec"]


def test_method_preserved_on_result():
    analysis = _analysis([AffectedFlow(kind="api", name="/api/login", files=[], reason="r")])
    result = affected_api_specs(
        analysis=analysis,
        specs_dir=None,  # type: ignore[arg-type]
        spec_loader=_loader([_spec("s1", method="post", url="/api/login")]),
    )
    assert result[0].method == "POST"


def test_multiple_flows_aggregated_per_spec():
    analysis = _analysis([
        AffectedFlow(kind="api", name="/api/users", files=[], reason="r"),
        AffectedFlow(kind="api", name="/api/users/me", files=[], reason="r"),
    ])
    result = affected_api_specs(
        analysis=analysis,
        specs_dir=None,  # type: ignore[arg-type]
        spec_loader=_loader([_spec("s1", url="/api/users/me/profile")]),
    )
    # Spec matches under both flows — both names show up.
    assert [s.spec_id for s in result] == ["s1"]
    assert set(result[0].matched_flows) == {"/api/users", "/api/users/me"}


def test_to_dict_round_trip():
    spec = AffectedAPISpec(
        spec_id="s1",
        spec_name="login",
        method="GET",
        route_path="/api/login",
        matched_flows=["/api/login"],
        command="retrace tester api-run s1",
    )
    d = spec.to_dict()
    assert d["spec_id"] == "s1"
    assert d["matched_flows"] == ["/api/login"]
    assert d["command"].endswith("api-run s1")
