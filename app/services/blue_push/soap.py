"""SOAP payload builders and response parsers for Explorance Blue.

Copied from scripts/push_users_to_blue.py — do not re-derive. Payloads and
response parsing already handle Blue's inconsistent SOAP shapes.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

import requests


def escape_xml(text: str) -> str:
    """Escape special XML characters."""
    if text is None:
        return ""
    text = str(text)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def soap_get_datablock_info(api_key: str, datasource_id: str) -> str:
    """Build SOAP payload for GetDataBlockInformation()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
    <soapenv:Header>
        <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
        <tem:DatasourceId>{escape_xml(datasource_id)}</tem:DatasourceId>
    </soapenv:Header>
    <soapenv:Body>
        <tem:BasicRequestDataSourceId/>
    </soapenv:Body>
</soapenv:Envelope>"""


def soap_register_import(api_key: str, datasource_id: str) -> str:
    """Build SOAP payload for RegisterImport()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
    <soapenv:Header>
        <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
    </soapenv:Header>
    <soapenv:Body>
        <tem:RegisterImportRequest>
            <tem:AbortOnEmpty>true</tem:AbortOnEmpty>
            <tem:DataSourceID>{escape_xml(datasource_id)}</tem:DataSourceID>
            <tem:ReplaceBlueRole>false</tem:ReplaceBlueRole>
            <tem:ReplaceDataSourceAccessKey>false</tem:ReplaceDataSourceAccessKey>
            <tem:ReplaceLanguagePreferences>false</tem:ReplaceLanguagePreferences>
        </tem:RegisterImportRequest>
    </soapenv:Body>
</soapenv:Envelope>"""


def soap_push_data(
    api_key: str,
    transaction_id: str,
    block_name: str,
    columns: List[str],
    rows: List[List[str]],
) -> str:
    """Build SOAP payload for PushObjectDataV2()."""
    columns_xml = "\n".join([
        f'             <arr:string>{escape_xml(col)}</arr:string>'
        for col in columns
    ])

    rows_xml = ""
    for row in rows:
        row_values = "\n".join([
            f"""                  <blue:IDataObj>
                     <blue:IDataObjValue>{escape_xml(str(val) if val is not None else "")}</blue:IDataObjValue>
                  </blue:IDataObj>"""
            for val in row
        ])
        rows_xml += f"""            <blue:IDataRow>
               <blue:IDataRowValue>
{row_values}
               </blue:IDataRowValue>
            </blue:IDataRow>
"""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/" xmlns:arr="http://schemas.microsoft.com/2003/10/Serialization/Arrays" xmlns:blue="http://schemas.datacontract.org/2004/07/Blue.Integration">
   <soapenv:Header>
      <tem:TransactionId>{escape_xml(transaction_id)}</tem:TransactionId>
      <tem:DataBlockName>{escape_xml(block_name)}</tem:DataBlockName>
      <tem:ColumnNamesList>
{columns_xml}
      </tem:ColumnNamesList>
      <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:DataObjectTransferRequestV2>
         <tem:Data>
{rows_xml}
         </tem:Data>
      </tem:DataObjectTransferRequestV2>
   </soapenv:Body>
</soapenv:Envelope>"""


def soap_prepare_finalize(api_key: str, transaction_id: str) -> str:
    """Build SOAP payload for PrepareDataToFinalizeImportV2()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
   <soapenv:Header>
      <tem:TransactionID>{escape_xml(transaction_id)}</tem:TransactionID>
      <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:BasicRequest/>
   </soapenv:Body>
</soapenv:Envelope>"""


def soap_finalize_import(api_key: str, transaction_id: str) -> str:
    """Build SOAP payload for FinalizeImport()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
   <soapenv:Header>
      <tem:TransactionID>{escape_xml(transaction_id)}</tem:TransactionID>
      <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:FinalizeImportRequest/>
   </soapenv:Body>
</soapenv:Envelope>"""


def soap_cancel_import(api_key: str, transaction_id: str) -> str:
    """Build SOAP payload for CancelImport()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
   <soapenv:Header>
      <tem:TransactionID>{escape_xml(transaction_id)}</tem:TransactionID>
      <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:CancelImportRequest/>
    </soapenv:Body>
 </soapenv:Envelope>"""


def soap_get_current_process(api_key: str) -> str:
    """Build SOAP payload for GetCurrentImportingDataSourceProcess()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
   <soapenv:Header>
      <tem:TransactionID>0</tem:TransactionID>
      <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:BaseRequest/>
   </soapenv:Body>
</soapenv:Envelope>"""


def check_response(
    response: requests.Response, action: str
) -> Tuple[bool, str, bool]:
    """Parse SOAP response and check for success/failure.

    Returns: (is_success, message, has_warning)
    """
    if response.status_code != 200:
        return False, f"HTTP Error {response.status_code}", False

    text = response.text

    if "INVALID_APIKEY" in text or "Invalid API_KEY" in text:
        return False, "Invalid API Key", False

    is_success = False

    result_match = re.search(
        r'<[^>]*Result[^>]*>(true|false)</[^>]*Result>',
        text, re.IGNORECASE,
    )
    if result_match:
        is_success = result_match.group(1).lower() == "true"

    is_success_match = re.search(
        r'<[^>]*IsSuccess[^>]*>(true|false)</[^>]*IsSuccess>',
        text, re.IGNORECASE,
    )
    if is_success_match:
        is_success = is_success_match.group(1).lower() == "true"

    message_match = re.search(
        r'<(?![^>]*Warning)[^>]*Message[^>]*>([^<]*)</[^>]*Message>',
        text,
    )
    message = message_match.group(1) if message_match else ""
    message = message.replace("&#xD;", "\n").replace("&#xA;", "\n")

    warning_match = re.search(
        r'<[^>]*HasWarningMessage[^>]*>(true|false)</[^>]*HasWarningMessage>',
        text, re.IGNORECASE,
    )
    has_warning = warning_match and warning_match.group(1).lower() == "true"

    if result_match is None and is_success_match is None:
        if "VALID_APIKEY" in text:
            is_success = True

    return is_success, message, bool(has_warning)


def extract_value(response_text: str, tag_name: str) -> Optional[str]:
    """Extract a single value from SOAP response by tag name."""
    pattern = rf'<[^>]*{tag_name}[^>]*>([^<]*)</[^>]*{tag_name}>'
    match = re.search(pattern, response_text, re.IGNORECASE)
    return match.group(1) if match else None


def extract_block_name(response_text: str) -> Optional[str]:
    """Extract DataBlockName from GetDataBlockInformation response."""
    match = re.search(
        r'<[^>]*DataBlockName[^>]*>([^<]*)</[^>]*DataBlockName>',
        response_text,
    )
    return match.group(1) if match else None
