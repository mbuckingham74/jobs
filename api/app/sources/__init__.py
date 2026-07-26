"""Source-layer packages for ATS, feed, and scrape adapters.

Task 004 introduces only the ``ats`` subpackage with the shared frozen
source-layer contract and one asynchronous Greenhouse adapter. The package
initialiser deliberately stays minimal so importing ``app.main:app`` and
calling ``GET /healthz`` continues to work without any source configuration.
"""
