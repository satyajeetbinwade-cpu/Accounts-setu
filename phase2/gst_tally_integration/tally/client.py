"""Tally API client.

Tally exposes data via:
1. **HTTP API** (Tally Prime / Tally ERP 9) — XML over HTTP on port 9000
2. **ODBC** — Direct database queries

This client implements the HTTP XML-based API which is the most common
and requires no additional Tally configuration beyond enabling the HTTP
server in Tally (Gateway of Tally > F12 > Configure > Accept XML requests).

Tally XML request format:
  <ENVELOPE>
    <HEADER><VERSION>1</VERSION></HEADER>
    <BODY>
      <DESC><STATICVARIABLES>...</STATICVARIABLES></DESC>
      <DATA>...</DATA>
    </BODY>
  </ENVELOPE>
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from typing import Any, Optional


class TallyClient:
    """Client for interacting with Tally via its HTTP XML API."""

    def __init__(self, host: str = "localhost", port: int = 9000, company_name: Optional[str] = None):
        self.base_url = f"http://{host}:{port}"
        self.company_name = company_name

    # -----------------------------------------------------------------------
    # Low-level request
    # -----------------------------------------------------------------------

    def _send_request(self, xml_body: str) -> tuple[bool, Any]:
        """Send an XML request to Tally and parse the response.

        Returns (success, response_xml_or_error_message).
        """
        envelope = f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION></HEADER>
<BODY>
{xml_body}
</BODY>
</ENVELOPE>"""

        try:
            req = urllib.request.Request(
                self.base_url,
                data=envelope.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                response_xml = resp.read().decode("utf-8")
                return True, response_xml
        except urllib.error.HTTPError as e:
            return False, f"Tally HTTP {e.code}: {e.read().decode()[:500]}"
        except urllib.error.URLError as e:
            return False, f"Cannot reach Tally at {self.base_url}: {e.reason}"
        except Exception as e:
            return False, str(e)

    # -----------------------------------------------------------------------
    # Chart of Accounts
    # -----------------------------------------------------------------------

    def fetch_chart_of_accounts(self) -> tuple[bool, Any]:
        """Fetch the Chart of Accounts from Tally.

        Returns (success, list_of_accounts_or_error).
        """
        xml = """<DESC><STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
</STATICVARIABLES></DESC>
<DATA>
<TALLYMESSAGE>
    <COLLECTION ISMODIFY="No" ISFIXEDLIST="Yes">
        <COLLECTIONS>ChartOfAccounts</COLLECTIONS>
        <NATIVEMETHOD>TypeInfo</NATIVEMETHOD>
    </COLLECTION>
</TALLYMESSAGE>
</DATA>"""
        success, result = self._send_request(xml)
        if not success:
            return False, result

        try:
            accounts = self._parse_coa_response(result)
            return True, accounts
        except Exception as e:
            return False, f"Failed to parse Tally COA response: {e}"

    def _parse_coa_response(self, xml: str) -> list[dict]:
        """Parse the Chart of Accounts XML response."""
        accounts = []
        root = ET.fromstring(xml)
        for tally_msg in root.findall(".//TALLYMESSAGE"):
            group = tally_msg.find("GROUP")
            ledger = tally_msg.find("LEDGER")
            element = group or ledger
            if element is None:
                continue

            name = element.get("NAME", "")
            parent = element.get("PARENT", "")
            acc_type = "Group" if group is not None else "Ledger"

            accounts.append({
                "name": name,
                "parent": parent,
                "type": acc_type,
            })
        return accounts

    # -----------------------------------------------------------------------
    # Ledgers / Vouchers
    # -----------------------------------------------------------------------

    def fetch_vouchers(self, from_date: str, to_date: str) -> tuple[bool, Any]:
        """Fetch vouchers from Tally for a date range.

        Args:
            from_date: "YYYYMM01" format
            to_date: "YYYYMMDD" format

        Returns (success, list_of_vouchers_or_error).
        """
        xml = f"""<DESC><STATICVARIABLES>
    <SVFROMDATE>{from_date}</SVFROMDATE>
    <SVTODATE>{to_date}</SVTODATE>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
</STATICVARIABLES></DESC>
<DATA>
<TALLYMESSAGE>
    <COLLECTION ISMODIFY="No" ISFIXEDLIST="Yes">
        <COLLECTIONS>VoucherType</COLLECTIONS>
        <NATIVEMETHOD>TypeInfo</NATIVEMETHOD>
    </COLLECTION>
</TALLYMESSAGE>
</DATA>"""
        success, result = self._send_request(xml)
        if not success:
            return False, result
        return True, self._simplify_xml(result)

    def fetch_ledgers(self) -> tuple[bool, Any]:
        """Fetch all ledgers from Tally."""
        xml = """<DESC><STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
</STATICVARIABLES></DESC>
<DATA>
<TALLYMESSAGE>
    <COLLECTION ISMODIFY="No" ISFIXEDLIST="Yes">
        <COLLECTIONS>Ledger</COLLECTIONS>
        <NATIVEMETHOD>TypeInfo</NATIVEMETHOD>
    </COLLECTION>
</TALLYMESSAGE>
</DATA>"""
        success, result = self._send_request(xml)
        if not success:
            return False, result
        return True, self._simplify_xml(result)

    # -----------------------------------------------------------------------
    # Push journal entries to Tally
    # -----------------------------------------------------------------------

    def push_journal_entry(self, entry: dict) -> tuple[bool, Optional[str]]:
        """Push a journal entry to Tally.

        Args:
            entry: {
                "date": "20260801",
                "voucher_number": "...",
                "narration": "...",
                "entries": [
                    {"ledger": "Ledger Name", "amount": 1000, "type": "Dr"},
                    {"ledger": "Ledger Name", "amount": 1000, "type": "Cr"},
                ]
            }

        Returns (success, error_message).
        """
        xml_entries = ""
        for e in entry.get("entries", []):
            xml_entries += f"""
        <ALLLEDGERENTRIES.LIST>
            <LEDGERNAME>{e['ledger']}</LEDGERNAME>
            <ISDEEMEDPOSITIVE>{"Yes" if e['type'] == "Dr" else "No"}</ISDEEMEDPOSITIVE>
            <AMOUNT>{abs(e['amount']):.2f}</AMOUNT>
        </ALLLEDGERENTRIES.LIST>"""

        xml = f"""<DESC><STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
</STATICVARIABLES></DESC>
<DATA>
<TALLYMESSAGE>
    <VOUCHER>
        <VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
        <DATE>{entry['date']}</DATE>
        <VOUCHERNUMBER>{entry.get('voucher_number', '')}</VOUCHERNUMBER>
        <NARRATION>{entry.get('narration', '')}</NARRATION>
        <PERSISTEDVIEW>Journal Voucher</PERSISTEDVIEW>
        <VCHGSTCLASS>0</VCHGSTCLASS>
        {xml_entries}
    </VOUCHER>
</TALLYMESSAGE>
</DATA>"""
        success, result = self._send_request(xml)
        if not success:
            return False, result
        # Check for success indicators in response
        if "LINEERROR" in result:
            return False, "Tally rejected the entry — check XML response for details."
        return True, None

    # -----------------------------------------------------------------------
    # Push reconciliation data to Tally
    # -----------------------------------------------------------------------

    def push_reconciliation_entry(
        self,
        period: str,
        gstin: str,
        entries: list[dict],
    ) -> tuple[bool, Optional[str]]:
        """Push GST reconciliation entries as journal vouchers to Tally.

        Creates adjustment entries for:
        - ITC claimed vs. eligible differences
        - Missing invoice provisions
        - Portal vs Books adjustments

        Args:
            period: "202608"
            gstin: GSTIN of the branch
            entries: List of adjustment entry dicts

        Returns (success, error_message).
        """
        for entry in entries:
            success, error = self.push_journal_entry({
                "date": period + "01",
                "voucher_number": f"GST-RECON-{period}-{gstin[:4]}",
                "narration": entry.get("narration", f"GST Recon adjustment for {period}"),
                "entries": entry.get("entries", []),
            })
            if not success:
                return False, error
        return True, None

    # -----------------------------------------------------------------------
    # Health check
    # -----------------------------------------------------------------------

    def ping(self) -> tuple[bool, Optional[str]]:
        """Check if Tally is reachable."""
        xml = """<DESC><STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
</STATICVARIABLES></DESC>
<DATA/>"""
        success, result = self._send_request(xml)
        if success:
            return True, None
        return False, result

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _simplify_xml(self, xml: str) -> list[dict]:
        """Convert Tally XML response to a list of simplified dicts."""
        result = []
        try:
            root = ET.fromstring(xml)
            for tally_msg in root.findall(".//TALLYMESSAGE"):
                for child in tally_msg:
                    if child.tag in ("COLLECTION",):
                        continue
                    item = {"_type": child.tag}
                    for attr in ("NAME", "PARENT", "GSTIN", "LEDGERNAME",
                                 "VOUCHERTYPENAME", "DATE", "NARRATION"):
                        val = child.get(attr)
                        if val:
                            item[attr.lower()] = val
                    # Get text values for sub-elements
                    for sub in child:
                        if sub.text and sub.text.strip():
                            item[sub.tag.lower()] = sub.text.strip()
                    result.append(item)
        except ET.ParseError:
            pass
        return result
