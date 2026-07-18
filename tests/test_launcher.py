from pathlib import Path


HTML = Path("launcher.html").read_text()


def test_manifest_values_are_not_inserted_with_inner_html():
    assert "btn.innerHTML" not in HTML
    assert "${app.label}" not in HTML
    assert ".textContent = app.label" in HTML


def test_local_ai_setup_has_privacy_profiles_and_confirmation():
    assert "Everything stays on this computer" in HTML
    assert "Quality" in HTML
    assert "Compatibility" in HTML
    assert "confirm(" in HTML
    assert "/api/local-ai/install-plan" in HTML
    assert "/api/local-ai/pull/" in HTML
    assert "/api/local-ai/verify/" in HTML
    assert "/api/local-ai/models/" in HTML
    assert "/api/local-ai/diagnostics" in HTML
    assert 'id="cancel-ai"' in HTML
    assert "pullController.abort()" in HTML


def test_chat_checks_readiness_before_enabling_input():
    assert "refreshChatReadiness" in HTML
    assert "setup_required" in HTML
    assert "Set up local AI" in HTML


def test_sse_parser_preserves_partial_network_lines():
    assert "streamBuffer" in HTML
    assert "streamBuffer.split('\\n')" in HTML


def test_mobile_layout_uses_drawer_and_collapsible_chat():
    assert "@media (max-width: 700px)" in HTML
    assert "#sidebar.open" in HTML
    assert ".chat.mobile-hidden" in HTML
    assert "aria-label=\"Open apps\"" in HTML


def test_icon_only_controls_have_accessible_labels():
    assert "aria-label=\"Local AI settings\"" in HTML
    assert "aria-label=\"Show or hide tutor chat\"" in HTML
