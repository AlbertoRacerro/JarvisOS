from __future__ import annotations

from fastapi.testclient import TestClient

from app.modules.memory import pdf_text
from tests.test_literature_knowledge import _workspace_ids, client  # noqa: F401
from tests.test_literature_source_file import _source, _upload


def make_pdf(texts: list[str]) -> bytes:
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'']
    kids = []
    for text in texts:
        page = len(objects) + 1
        kids.append(f'{page} 0 R')
        stream = f'BT /F1 12 Tf 20 100 Td ({text}) Tj ET'.encode()
        objects.extend([f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents {page + 1} 0 R >>'.encode(), b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream'])
    objects[1] = f'<< /Type /Pages /Count {len(kids)} /Kids [{" ".join(kids)}] >>'.encode()
    result = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(result)
    result.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        result.extend(f'{offset:010} 00000 n \n'.encode())
    result.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(result)


def test_pdf_pages_and_cache(client: TestClient, monkeypatch) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    artifact = _upload(client, ws, 'paper.pdf', make_pdf(['Introduction', 'k = 1.2 x 10^-3 s^-1'])).json()['artifact_id']
    calls = []
    original = pdf_text.read_pdf_pages
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(pdf_text, 'read_pdf_pages', counted)
    source = _source(client, ws, artifact_id=artifact)
    assert source['backing']['page_text'] == 'available'
    url = f'/workspaces/{ws}/literature/sources/{source["id"]}/entries'
    base = {'entry_kind': 'datum', 'value_number': 0.0012, 'unit': '1/s', 'locator_kind': 'page'}
    for page, verdict in [(2, 'usable'), (1, 'not_usable')]:
        result = client.post(url, json={**base, 'locator_start': page, 'locator_end': page})
        assert result.status_code == 201 and result.json()['verdict'] == verdict
    assert client.post(url, json={**base, 'locator_start': 3}).status_code == 422
    assert client.get(f'/workspaces/{ws}/literature/sources/{source["id"]}').status_code == 200
    assert len(calls) == 1
    assert calls[0].with_suffix('.pages.json').exists()


def test_empty_and_corrupt_pdf(client: TestClient) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    for data, state in [(make_pdf(['']), 'no_text'), (b'%PDF-broken', 'unreadable')]:
        artifact = _upload(client, ws, 'paper.pdf', data).json()['artifact_id']
        source = _source(client, ws, artifact_id=artifact)
        assert source['backing']['page_text'] == state
        url = f'/workspaces/{ws}/literature/sources/{source["id"]}/entries'
        base = {'entry_kind': 'datum', 'value_number': 1}
        assert client.post(url, json={**base, 'locator_kind': 'page', 'locator_start': 1}).status_code == 422
        assert client.post(url, json=base).json()['verdict'] == 'unchecked'
