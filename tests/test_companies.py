from trellis.companies import REGISTRY, check_industry_support, get_profile


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


# --- check_industry_support ---------------------------------------------------

def test_check_industry_support_refuses_a_bank():
    reason = check_industry_support(6022)  # State commercial banks
    assert reason is not None
    assert "inventory" in reason.lower() or "leverage" in reason.lower()


def test_check_industry_support_refuses_an_insurer():
    reason = check_industry_support(6311)  # Life insurance
    assert reason is not None


def test_check_industry_support_refuses_a_reit():
    reason = check_industry_support(6798)  # Real estate investment trusts
    assert reason is not None


def test_check_industry_support_allows_the_four_registered_companies_own_sic_codes():
    """Confirms the exclusion range doesn't accidentally catch any of the industries
    already validated this session -- footwear/apparel, warehouse retail,
    e-commerce/cloud, consumer electronics, pharma/healthcare."""
    assert check_industry_support(3021) is None   # Nike: rubber & plastics footwear
    assert check_industry_support(5331) is None   # Costco: variety stores
    assert check_industry_support(5961) is None   # Amazon: catalog & mail-order retail
    assert check_industry_support(3571) is None   # Apple: electronic computers
    assert check_industry_support(2834) is None   # J&J: pharmaceutical preparations


def test_check_industry_support_does_not_refuse_a_missing_sic():
    """An absent SIC is missing metadata, not evidence of being out of scope --
    refusing only makes sense on a positive match against a known-incompatible range."""
    assert check_industry_support(None) is None


def test_check_industry_support_boundary_values():
    assert check_industry_support(5999) is None   # just below the excluded range
    assert check_industry_support(6000) is not None  # first excluded code
    assert check_industry_support(6999) is not None  # last excluded code
    assert check_industry_support(7000) is None   # just above the excluded range
