"""GST Portal & Tally API Integration module.

Fetches data from:
- GST Portal (GSTN APIs) — returns, invoices, filings
- Tally (HTTP/ODBC) — books data, chart of accounts

Pushes data to:
- GST Portal — file returns, update registrations
- Tally — push reconciled data, journal entries

All operations go through an admin preview → confirm workflow.
"""

__version__ = "0.1.0"
