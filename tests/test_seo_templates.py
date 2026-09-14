"""Tests for Phase 2: Template SEO blocks.

Verifies that templates render correct meta tags, OG tags, Twitter cards,
canonical URLs, JSON-LD structured data, and crawlable pagination hrefs.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jinja2.runtime import Context

from app.presenters import DATA_ATTRIBUTION

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "app" / "templates"

# Absolute URLs that may legitimately appear literally in a template. Every
# other absolute URL must be built from the injected SEO context (the values
# ``app.presenters.build_seo_context`` derives from ``settings.site_url``) or
# from the ``site_url`` global installed by ``create_app``.
ALLOWED_ABSOLUTE_URLS = {"https://schema.org"}

_ABSOLUTE_URL = re.compile(r"https?://[^\s\"'<>)]+")

# Stand-in for the configured ``SITE_URL`` while rendering templates here. It
# deliberately differs from any deployed host, so a hardcoded domain in a
# template fallback fails these tests instead of passing unnoticed.
TEST_SITE_URL = "https://test.example"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jinja_env():
    """Create a Jinja2 Environment pointing at the app templates dir.

    The ``site_url`` and ``data_attribution`` globals mirror the ones
    ``create_app`` installs on ``app.state.templates.env``, so template
    fallbacks resolve here exactly as they do in production. A global that
    ``create_app`` installs and this harness forgets does not fall back to
    empty: reading an attribute off an undefined name raises, so every
    ``base.html`` render in this module would fail rather than degrade.
    """

    env = Environment(
        loader=FileSystemLoader("app/templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["site_url"] = TEST_SITE_URL
    env.globals["data_attribution"] = DATA_ATTRIBUTION
    return env


def _make_mock_filters():
    """Mock AdjudicationFilters for templates that include _filter_form.html."""
    f = MagicMock()
    f.article = ""
    f.article_id = ""
    f.company = ""
    f.organism = ""
    f.date_from = MagicMock()
    f.date_from.isoformat.return_value = "2024-01-01"
    f.date_to = MagicMock()
    f.date_to.isoformat.return_value = "2024-12-31"
    f.has_any.return_value = False
    return f


def _make_mock_request():
    """Mock Starlette request for templates that reference request.query_params."""
    r = MagicMock()
    r.query_params = {}
    return r


def _render_full(env, template_name, context=None):
    """Render a full template with safe defaults for common context vars."""
    if context is None:
        context = {}
    context.setdefault("request", _make_mock_request())
    context.setdefault("filters", _make_mock_filters())
    # Defaults for index-like templates
    context.setdefault("total", 0)
    context.setdefault("shown", 0)
    context.setdefault("results", [])
    context.setdefault("page", 1)
    context.setdefault("total_pages", 1)
    context.setdefault("page_numbers", [])
    context.setdefault("validation_error", None)
    context.setdefault("organisms", [])
    template = env.get_template(template_name)
    return template.render(**context)


def _render_block(env, template_name, block_name, context=None):
    """Render a single named block from a template (isolated from content block).

    This avoids pulling in heavy partial dependencies when we only need to
    verify SEO block output.
    """
    if context is None:
        context = {}
    context.setdefault("request", _make_mock_request())
    context.setdefault("filters", _make_mock_filters())

    template = env.get_template(template_name)
    # Jinja2 stores block callables keyed by block name.
    block_func = template.blocks.get(block_name)
    if block_func is None:
        raise ValueError(f"Block '{block_name}' not found in {template_name}")

    # Build a proper Jinja2 runtime context. ``Template.render`` merges the
    # environment globals into the parent context (``new_context`` does
    # ``parent = dict(globals).update(vars)``); constructing the Context by
    # hand has to mirror that, or globals such as ``site_url`` resolve as
    # undefined here while working in production.
    parent = {**env.globals, **context}
    ctx = Context(env, parent=parent, name=template.name, blocks=template.blocks)
    # Call the block function — it's a generator that yields strings
    return "".join(block_func(ctx))


def _base_context():
    """Minimal context for rendering base.html."""
    return {}


def _index_seo_context(**overrides):
    """SEO context for index page blocks."""
    ctx = {
        "meta_title": "AdjudicaUY",
        "meta_description": "Buscador de adjudicaciones del Estado uruguayo",
        "og_type": "website",
        "canonical_url": "https://test.example/",
        "json_ld": {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "AdjudicaUY",
        },
    }
    ctx.update(overrides)
    return ctx


def _organism_seo_context(**overrides):
    """SEO context for organism detail page blocks."""
    ctx = {
        "organism_name": "MSP",
        "meta_title": "MSP — AdjudicaUY",
        "meta_description": "Adjudicaciones del organismo MSP",
        "og_type": "GovernmentOrganization",
        "canonical_url": "https://test.example/organism/MSP",
        "json_ld": {
            "@context": "https://schema.org",
            "@type": "GovernmentOrganization",
            "name": "MSP",
        },
    }
    ctx.update(overrides)
    return ctx


def _pagination_context(**overrides):
    """Context for rendering _results_table.html with pagination."""
    row = MagicMock()
    row.amount = 1000.0
    row.currency = "UYU"
    row.organism = "MSP"
    row.winning_company = "Test Corp"
    row.article = "Test article"
    row.date = MagicMock()
    row.date.isoformat.return_value = "2024-01-15"
    row.license_type = "CD"
    row.company_document = "12345"
    row.company_document_type = "RUT"
    row.license_link = ""

    ctx = {
        "results": [row],
        "page": 2,
        "total_pages": 5,
        "page_numbers": [1, 2, 3, 4, 5],
    }
    ctx.update(overrides)
    return ctx


# ===========================================================================
# 2.1 — base.html SEO structure
# ===========================================================================


class TestBaseTemplateSEO:
    """base.html renders SEO tag scaffolding with safe defaults."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.env = _make_jinja_env()

    def test_has_meta_description_tag(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<meta name="description"' in html

    def test_has_og_title_meta_tag(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<meta property="og:title"' in html

    def test_has_og_description_meta_tag(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<meta property="og:description"' in html

    def test_has_og_type_meta_tag(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<meta property="og:type"' in html

    def test_has_og_url_meta_tag(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<meta property="og:url"' in html

    def test_has_og_site_name_meta_tag(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<meta property="og:site_name"' in html

    def test_has_og_image_meta_tag(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<meta property="og:image"' in html

    def test_has_twitter_card_meta_tag(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<meta name="twitter:card"' in html

    def test_has_twitter_title_meta_tag(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<meta name="twitter:title"' in html

    def test_has_twitter_description_meta_tag(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<meta name="twitter:description"' in html

    def test_has_canonical_link(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert '<link rel="canonical"' in html

    def test_has_json_ld_block(self):
        """The json_ld block exists; empty by default so no script tag."""
        html = _render_full(self.env, "base.html", _base_context())
        # With no json_ld data, the script tag should NOT appear
        assert "application/ld+json" not in html

    def test_json_ld_renders_when_provided(self):
        """When json_ld dict is provided, script tag renders with valid JSON."""
        ctx = {
            "json_ld": {
                "@context": "https://schema.org",
                "@type": "WebSite",
                "name": "Test",
            },
        }
        html = _render_full(self.env, "base.html", ctx)
        assert "application/ld+json" in html
        assert '"@type": "WebSite"' in html

    def test_meta_description_uses_default_when_not_provided(self):
        """Without explicit meta_description, the default filter provides a fallback."""
        html = _render_full(self.env, "base.html", _base_context())
        # Should still have the tag even with no context
        assert '<meta name="description"' in html
        assert 'content="' in html

    def test_canonical_url_uses_default_when_not_provided(self):
        html = _render_full(self.env, "base.html", _base_context())
        assert 'href="' in html


# ===========================================================================
# 2.2 — index.html SEO overrides
# ===========================================================================


class TestIndexTemplateSEO:
    """index.html overrides SEO blocks with index-specific values."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.env = _make_jinja_env()

    def test_title_is_adjudicauy(self):
        html = _render_block(self.env, "index.html", "title", _index_seo_context())
        assert "AdjudicaUY" in html

    def test_meta_description_mentions_adjudications(self):
        html = _render_block(
            self.env, "index.html", "meta_description", _index_seo_context()
        )
        assert "adjudicaciones" in html.lower()

    def test_og_type_is_website(self):
        html = _render_block(self.env, "index.html", "og_type", _index_seo_context())
        assert "website" in html

    def test_canonical_url_present(self):
        html = _render_block(
            self.env, "index.html", "canonical_url", _index_seo_context()
        )
        assert "https://test.example/" in html

    def test_website_json_ld(self):
        html = _render_block(self.env, "index.html", "json_ld", _index_seo_context())
        assert '"@type": "WebSite"' in html
        assert '"name": "AdjudicaUY"' in html

    def test_website_json_ld_has_search_action(self):
        html = _render_block(self.env, "index.html", "json_ld", _index_seo_context())
        assert '"@type": "SearchAction"' in html
        assert "?article={search_term_string}" in html
        assert '"query-input": "required name=search_term_string"' in html

    def test_twitter_card_present(self):
        # Twitter tags are in base.html using meta_title/meta_description vars
        html = _render_full(self.env, "base.html", _index_seo_context())
        assert "twitter:card" in html
        assert "twitter:title" in html


# ===========================================================================
# 2.3 — organism_detail.html SEO overrides
# ===========================================================================


class TestOrganismDetailTemplateSEO:
    """organism_detail.html overrides SEO blocks with organism-specific values."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.env = _make_jinja_env()

    def test_title_contains_organism_name(self):
        html = _render_block(
            self.env, "organism_detail.html", "title", _organism_seo_context()
        )
        assert "MSP" in html

    def test_meta_description_mentions_organism(self):
        html = _render_block(
            self.env,
            "organism_detail.html",
            "meta_description",
            _organism_seo_context(),
        )
        assert "MSP" in html

    def test_og_type_is_government_organization(self):
        html = _render_block(
            self.env, "organism_detail.html", "og_type", _organism_seo_context()
        )
        assert "GovernmentOrganization" in html

    def test_canonical_url_contains_organism(self):
        html = _render_block(
            self.env, "organism_detail.html", "canonical_url", _organism_seo_context()
        )
        assert "/organism/MSP" in html

    def test_government_organization_json_ld(self):
        html = _render_block(
            self.env, "organism_detail.html", "json_ld", _organism_seo_context()
        )
        assert '"@type": "GovernmentOrganization"' in html
        assert '"name": "MSP"' in html

    def test_json_ld_escapes_special_chars_in_organism_name(self):
        """Organism names with quotes/special chars produce valid JSON-LD."""
        ctx = _organism_seo_context(organism_name='Intendencia "Metro"')
        html = _render_block(self.env, "organism_detail.html", "json_ld", ctx)
        # |tojson should escape the quotes properly for valid JSON
        assert '"name": "Intendencia \\"Metro\\""' in html


# ===========================================================================
# 2.4 — pages/about.html
# ===========================================================================


class TestAboutPageSEO:
    """About page extends base.html with SEO blocks for the about content."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.env = _make_jinja_env()

    def _about_context(self):
        return {
            "meta_title": "Sobre AdjudicaUY",
            "meta_description": (
                "Plataforma de búsqueda de adjudicaciones estatales del Uruguay"
            ),
            "og_type": "website",
            "canonical_url": "https://test.example/about",
            "json_ld": {
                "@context": "https://schema.org",
                "@type": "WebSite",
                "name": "AdjudicaUY",
            },
        }

    def test_about_template_exists(self):
        template = self.env.get_template("pages/about.html")
        assert template is not None

    def test_about_title(self):
        html = _render_block(
            self.env, "pages/about.html", "title", self._about_context()
        )
        assert "Sobre AdjudicaUY" in html

    def test_about_meta_description(self):
        html = _render_block(
            self.env, "pages/about.html", "meta_description", self._about_context()
        )
        assert "adjudicaciones" in html.lower()

    def test_about_og_type_website(self):
        html = _render_block(
            self.env, "pages/about.html", "og_type", self._about_context()
        )
        assert "website" in html

    def test_about_website_json_ld(self):
        html = _render_block(
            self.env, "pages/about.html", "json_ld", self._about_context()
        )
        assert '"@type": "WebSite"' in html

    def test_about_has_informational_content(self):
        html = _render_full(self.env, "pages/about.html", self._about_context())
        assert "AdjudicaUY" in html


# ===========================================================================
# 2.5 — Pagination href attributes
# ===========================================================================


class TestPaginationHref:
    """Pagination links have real href attributes for crawlers."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.env = _make_jinja_env()

    def test_previous_link_has_href(self):
        html = _render_full(
            self.env, "partials/_results_table.html", _pagination_context()
        )
        assert 'href="/adjudications?page=1"' in html

    def test_next_link_has_href(self):
        html = _render_full(
            self.env, "partials/_results_table.html", _pagination_context()
        )
        assert 'href="/adjudications?page=3"' in html

    def test_page_number_links_have_href(self):
        html = _render_full(
            self.env, "partials/_results_table.html", _pagination_context()
        )
        assert 'href="/adjudications?page=1"' in html
        assert 'href="/adjudications?page=3"' in html

    def test_pagination_href_excludes_partial_param(self):
        html = _render_full(
            self.env, "partials/_results_table.html", _pagination_context()
        )
        import re

        hrefs = re.findall(r'href="[^"]*page=\d+[^"]*"', html)
        assert len(hrefs) > 0, "Expected at least one pagination href"
        for href in hrefs:
            assert "partial" not in href, f"href contains 'partial': {href}"

    def test_pagination_href_and_hx_get_coexist(self):
        html = _render_full(
            self.env, "partials/_results_table.html", _pagination_context()
        )
        assert 'hx-get="/adjudications?page=1&partial=table"' in html
        assert 'href="/adjudications?page=1"' in html


# ---------------------------------------------------------------------------
# Regression guard: templates must not hardcode the site's own domain
# ---------------------------------------------------------------------------


class TestTemplatesDoNotHardcodeTheSiteDomain:
    """Absolute site URLs must come from configuration, never from a literal.

    Every SEO block falls back to the injected ``canonical_url``/``og_image``
    and then to the ``site_url`` global installed by ``create_app``. A
    hardcoded domain anywhere in that chain means a route that forgets to
    inject the SEO context publishes a foreign canonical URL and can get the
    page deindexed.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.env = _make_jinja_env()

    def test_no_template_hardcodes_an_absolute_url_outside_the_allowlist(self):
        offenders = []
        for path in sorted(TEMPLATE_DIR.rglob("*.html")):
            relative = path.relative_to(TEMPLATE_DIR)
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                for match in _ABSOLUTE_URL.finditer(line):
                    if match.group(0) in ALLOWED_ABSOLUTE_URLS:
                        continue
                    offenders.append(f"{relative}:{lineno}: {match.group(0)}")

        assert offenders == [], (
            "Templates must build absolute URLs from the injected SEO context "
            "or the ``site_url`` global, never from a literal:\n" + "\n".join(offenders)
        )

    def test_base_canonical_fallback_uses_the_configured_site_url(self):
        html = _render_block(self.env, "base.html", "canonical_url", _base_context())

        assert html.strip() == f"{TEST_SITE_URL}/"

    def test_base_og_url_fallback_uses_the_configured_site_url(self):
        html = _render_block(self.env, "base.html", "og_url", _base_context())

        assert html.strip() == f"{TEST_SITE_URL}/"

    def test_base_og_image_fallback_uses_the_configured_site_url(self):
        html = _render_block(self.env, "base.html", "og_image", _base_context())

        assert html.strip() == f"{TEST_SITE_URL}/static/og-image.png"

    def test_index_canonical_fallback_uses_the_configured_site_url(self):
        html = _render_block(self.env, "index.html", "canonical_url", _base_context())

        assert html.strip() == f"{TEST_SITE_URL}/"

    def test_index_json_ld_target_fallback_uses_the_configured_site_url(self):
        html = _render_block(self.env, "index.html", "json_ld", _base_context())

        assert f"{TEST_SITE_URL}/?article={{search_term_string}}" in html

    def test_organism_canonical_fallback_uses_the_configured_site_url(self):
        html = _render_block(
            self.env, "organism_detail.html", "canonical_url", {"organism_name": "MSP"}
        )

        assert html.strip() == f"{TEST_SITE_URL}/organism/MSP"

    def test_about_canonical_fallback_uses_the_configured_site_url(self):
        html = _render_block(
            self.env, "pages/about.html", "canonical_url", _base_context()
        )

        assert html.strip() == f"{TEST_SITE_URL}/about"

    def test_company_canonical_fallback_uses_the_configured_site_url(self):
        html = _render_block(
            self.env,
            "company_detail.html",
            "canonical_url",
            {"company_type": "RUC", "company_number": "210000010017"},
        )

        assert html.strip() == f"{TEST_SITE_URL}/company/RUC/210000010017"
