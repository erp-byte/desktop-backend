"""Vendor management module.

CRUD across vendor_master / vendor_banking / vendor_document / vendor_contract,
plus a document-upload pipeline that pushes files to S3 and extracts
structured fields via the Claude API. S3 URLs are stored comma-separated
in the document/contract `s3_urls` text columns (jsonb migrated to text by
the DDL block bundled with this module's spec).
"""
