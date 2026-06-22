"""
Explorance Blue API - Discovery Service
University of Kentucky TCE System

SOAP helpers for discovering available datasources, their schemas,
and block names from the Explorance Blue API.
"""
import requests
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional


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


def _soap_envelope(api_key: str, body: str, extra_headers: str = "") -> str:
    """Build a SOAP envelope with standard headers."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
    <soapenv:Header>
        <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
{extra_headers}
    </soapenv:Header>
    <soapenv:Body>
{body}
    </soapenv:Body>
</soapenv:Envelope>"""


def _call_soap(ws_url: str, payload: str, operation: str, timeout: int = 30) -> requests.Response:
    """Call a Blue SOAP endpoint with retry on transient errors."""
    headers = {
        'Content-Type': 'text/xml; charset=utf-8',
        'SOAPAction': f'http://tempuri.org/IBlueService/{operation}',
    }
    last_error = None
    for attempt in range(2):
        try:
            response = requests.post(ws_url, data=payload.encode('utf-8'),
                                     headers=headers, timeout=timeout)
            if response.status_code < 500:
                return response
            last_error = f"HTTP {response.status_code}"
        except requests.exceptions.Timeout:
            last_error = "Connection timeout"
        except requests.exceptions.ConnectionError:
            last_error = "Connection error"
        if attempt == 0:
            import time
            time.sleep(2)  # brief backoff before retry

    raise RuntimeError(f"SOAP call '{operation}' failed after 2 attempts: {last_error}")


def _extract_inner_xml(element: ET.Element) -> str:
    """Extract the inner XML (text plus children) of an element."""
    result = element.text or ""
    for child in element:
        result += ET.tostring(child, encoding='unicode')
        result += child.tail or ""
    return result


def get_datasource_list(api_key: str, ws_url: str) -> List[Dict[str, str]]:
    """
    Call GetDataSourceList() and return [{datasource_id, name}, ...].

    The SOAP response contains DataSources/IDataSource elements, each with
    an Id and Name field.
    """
    body = "        <tem:BasicRequest/>"
    payload = _soap_envelope(api_key, body)
    response = _call_soap(ws_url, payload, "GetDataSourceList", timeout=30)

    datasources = []
    try:
        # Parse the SOAP response
        root = ET.fromstring(response.text)
        # Blue returns namespace-agnostic; search for all IDataSource elements
        ns = {
            's': 'http://schemas.xmlsoap.org/soap/envelope/',
            'tem': 'http://tempuri.org/',
            'b': 'http://schemas.datacontract.org/2004/07/Blue.Integration',
            'a': 'http://schemas.microsoft.com/2003/10/Serialization/Arrays',
        }

        # Try to find DataSources container
        for elem in root.iter():
            if 'IDataSource' in elem.tag:
                ds_id = None
                ds_name = None
                for child in elem:
                    tag_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                    if tag_local == 'Id':
                        ds_id = child.text
                    elif tag_local == 'Name':
                        ds_name = child.text
                if ds_id:
                    datasources.append({
                        'datasource_id': ds_id,
                        'name': ds_name or ds_id,
                    })
    except ET.ParseError:
        pass

    return datasources


def get_datasource_schema(api_key: str, ws_url: str, datasource_id: str) -> List[str]:
    """
    Call GetDataSourceSchemaColumns() and return column name list.

    Returns a list of column name strings for the given datasource.
    """
    extra_headers = f"        <tem:DatasourceId>{escape_xml(datasource_id)}</tem:DatasourceId>"
    body = "        <tem:BasicRequestDataSourceId/>"
    payload = _soap_envelope(api_key, body, extra_headers)
    response = _call_soap(ws_url, payload, "GetDataSourceSchemaColumns", timeout=30)

    columns = []
    try:
        root = ET.fromstring(response.text)
        for elem in root.iter():
            tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            if tag_local in ('string', 'ColumnName', 'SchemaColumn'):
                if elem.text and elem.text.strip():
                    columns.append(elem.text.strip())
    except ET.ParseError:
        pass

    return columns


def get_datablock_name(api_key: str, ws_url: str, datasource_id: str) -> Optional[str]:
    """
    Call GetDataBlockInformation() and return the DataBlockName.

    Returns None if the block name cannot be determined.
    """
    extra_headers = f"        <tem:DatasourceId>{escape_xml(datasource_id)}</tem:DatasourceId>"
    body = "        <tem:BasicRequestDataSourceId/>"
    payload = _soap_envelope(api_key, body, extra_headers)
    response = _call_soap(ws_url, payload, "GetDataBlockInformation", timeout=30)

    try:
        root = ET.fromstring(response.text)
        for elem in root.iter():
            tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            if tag_local == 'DataBlockName':
                return elem.text
            if tag_local == 'IDataBlock' or 'DataBlock' in tag_local:
                for child in elem:
                    child_tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                    if child_tag == 'DataBlockName' or child_tag == 'Name':
                        return child.text
    except ET.ParseError:
        pass

    return None


def get_all_datasource_details(api_key: str, ws_url: str) -> List[Dict]:
    """
    Discover all Blue datasources with their IDs, names, and schemas.

    Returns a list of dicts with: datasource_id, name, columns, block_name.
    """
    ds_list = get_datasource_list(api_key, ws_url)
    for ds in ds_list:
        ds_id = ds['datasource_id']
        try:
            ds['columns'] = get_datasource_schema(api_key, ws_url, ds_id)
        except Exception:
            ds['columns'] = []
        try:
            ds['block_name'] = get_datablock_name(api_key, ws_url, ds_id)
        except Exception:
            ds['block_name'] = None
    return ds_list
