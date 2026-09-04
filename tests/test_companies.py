from trellis.companies import REGISTRY, get_profile


def test_get_profile_returns_registered_company_with_its_overrides():
    profile = get_profile(320187)
    assert profile.name == "Nike, Inc."
    assert profile.ticker == "NKE"
    assert "interest_rate" in profile.overrides
    assert abs(profile.overrides["interest_rate"][0] - 0.0314) < 1e-9


def test_get_profile_returns_a_usable_default_for_an_unregistered_cik():
    """A company with no registry entry must still be fully forecastable -- this is
    the fallback that makes the registry additive, never required."""
    profile = get_profile(999999999)
    assert profile.cik == 999999999
    assert profile.overrides == {}  # no sourced overrides -- derive_drivers_from_history
    # will flag whatever it can't derive as an assumption instead, which is correct


def test_every_registered_override_has_a_nonempty_citation():
    """A sourced override with no source isn't sourced -- catches a registry entry
    added without actually doing the research."""
    for profile in REGISTRY.values():
        for driver_name, (value, source) in profile.overrides.items():
            assert source and len(source) > 20, (
                f"{profile.name}'s {driver_name} override has no real citation"
            )
