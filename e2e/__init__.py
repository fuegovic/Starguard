"""Browser-driven checks for the two pages the OAuth server renders.

This package is deliberately outside ``testpaths``, so ``pytest`` with no
arguments never collects it: it needs a real Chromium, which the unit suite
does not. Run it with ``pytest e2e``. See e2e/README.md.
"""
