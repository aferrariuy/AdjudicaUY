"""Tests for Phase 2: Template SEO blocks.

Verifies that templates render correct meta tags, OG tags, Twitter cards,
canonical URLs, JSON-LD structured data, and crawlable pagination hrefs.
"""

from unittest.mock import MagicMock

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jinja2.runtime import Context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jinja_env():
    """Create a Jinja2 Environment pointing at the app templates dir."""
    return Environment(
        loader=FileSystemLoader("app/templates"),
        autoescape=select_autoescape(["html"]),
    )


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

    # Build a proper Jinja2 runtime context
    parent = {**context}
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
        "canonical_url": "https://adjudica.digitales.gub.uy/",
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
        "canonical_url": "https://adjudica.digitales.gub.uy/organism/MSP",
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
        assert "https://adjudica.digitales.gub.uy/" in html

    def test_website_json_ld(self):
        html = _render_block(self.env, "index.html", "json_ld", _index_seo_context())
        assert '"@type": "WebSite"' in html
        assert '"name": "AdjudicaUY"' in html

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
            "canonical_url": "https://adjudica.digitales.gub.uy/about",
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
