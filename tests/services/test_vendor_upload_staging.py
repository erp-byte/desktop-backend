"""Vendor documents sent in pieces (app/modules/vendor/upload_staging.py).

The web app's API calls pass through Netlify (~6 MB request cap) and the web
server on EC2 (nginx, 1 MB by default); a batch of compliance documents posted
as one form came back 413. The browser now sends big files in pieces and the
vendor endpoints take the upload's id instead of the file.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.modules.vendor import router as VR
from app.modules.vendor import upload_staging as ST

PDF = b"%PDF-1.4\n" + b"x" * (2 * ST.CHUNK_BYTES + 1234) + b"\n%%EOF\n"
USER = 7


@pytest.fixture(autouse=True)
def _staging_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VENDOR_UPLOAD_STAGING_DIR", str(tmp_path))


def _send(data: bytes, user=USER, name="gst.pdf", ctype="application/pdf") -> str:
    up = ST.start(user, name, ctype, len(data))
    for i in range(up["chunks"]):
        ST.put_chunk(user, up["upload_id"], i, data[i * ST.CHUNK_BYTES:(i + 1) * ST.CHUNK_BYTES])
    return up["upload_id"]


def test_pieces_join_back_into_the_file():
    up = ST.start(USER, "gst.pdf", "application/pdf", len(PDF))
    assert up["chunks"] == 3 and up["chunk_bytes"] == ST.CHUNK_BYTES
    uid = _send(PDF)
    f = ST.StagedUpload(uid, USER)
    assert (f.filename, f.content_type) == ("gst.pdf", "application/pdf")
    assert asyncio.run(f.read()) == PDF


def test_an_upload_is_used_once():
    uid = _send(PDF)
    asyncio.run(ST.StagedUpload(uid, USER).read())
    with pytest.raises(HTTPException) as e:
        ST.StagedUpload(uid, USER)
    assert e.value.status_code == 404


def test_only_its_owner_can_add_to_or_use_it():
    uid = _send(PDF)
    for call in (lambda: ST.StagedUpload(uid, 8), lambda: ST.put_chunk(8, uid, 0, PDF[:ST.CHUNK_BYTES])):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 404


def test_a_missing_piece_is_refused():
    up = ST.start(USER, "gst.pdf", "application/pdf", len(PDF))
    ST.put_chunk(USER, up["upload_id"], 0, PDF[:ST.CHUNK_BYTES])
    with pytest.raises(HTTPException) as e:
        ST.StagedUpload(up["upload_id"], USER)
    assert e.value.detail["code"] == "upload_incomplete" and e.value.detail["missing_chunks"] == [1, 2]


@pytest.mark.parametrize("index,size", [(0, 10), (2, ST.CHUNK_BYTES), (3, 10), (-1, 10)])
def test_a_piece_of_the_wrong_size_or_place_is_refused(index, size):
    up = ST.start(USER, "gst.pdf", "application/pdf", len(PDF))
    with pytest.raises(HTTPException) as e:
        ST.put_chunk(USER, up["upload_id"], index, b"x" * size)
    assert e.value.status_code == 400


def test_a_piece_sent_again_replaces_itself():
    up = ST.start(USER, "a.pdf", "application/pdf", 5)
    ST.put_chunk(USER, up["upload_id"], 0, b"AAAAA")
    ST.put_chunk(USER, up["upload_id"], 0, b"BBBBB")
    assert asyncio.run(ST.StagedUpload(up["upload_id"], USER).read()) == b"BBBBB"


@pytest.mark.parametrize("size,status", [(0, 400), (25 * 1024 * 1024 + 1, 413)])
def test_start_checks_the_size(size, status):
    with pytest.raises(HTTPException) as e:
        ST.start(USER, "x.pdf", "application/pdf", size)
    assert e.value.status_code == status


@pytest.mark.parametrize("bad", ["../etc", "ABC", "x" * 32])
def test_an_upload_id_is_never_a_path(bad):
    with pytest.raises(HTTPException) as e:
        ST.StagedUpload(bad, USER)
    assert e.value.status_code == 400


def test_staged_files_keeps_the_order():
    a, b = _send(b"%PDF-a", name="a.pdf"), _send(b"%PDF-b", name="b.pdf")
    assert [f.filename for f in ST.staged_files(json.dumps([b, a]), USER)] == ["b.pdf", "a.pdf"]
    assert ST.staged_files("[]", USER) == []
    with pytest.raises(HTTPException):
        ST.staged_files("not json", USER)


def test_the_shared_reader_takes_a_staged_file():
    """The endpoints read every file through _sniff_and_read_upload, so a file
    sent in pieces gets the same size and type checks as a posted one."""
    uid = _send(PDF)
    content, mime, filename = asyncio.run(VR._sniff_and_read_upload(VR._one_file(None, uid, _User())))
    assert (content, mime, filename) == (PDF, "application/pdf", "gst.pdf")


def test_one_file_needs_a_file_or_an_upload():
    with pytest.raises(HTTPException) as e:
        VR._one_file(None, None, _User())
    assert e.value.status_code == 422


class _User:
    user_id = USER
    is_admin = True
    role_ids: list = []


def test_the_upload_routes_over_http():
    app = FastAPI()
    app.include_router(VR.router)
    app.dependency_overrides[VR._vendor_upload_user] = lambda: _User()
    c = TestClient(app)
    r = c.post("/api/v1/vendors/uploads", json={"filename": "gst.pdf", "content_type": "application/pdf",
                                                 "size": len(PDF)})
    assert r.status_code == 201, r.text
    up = r.json()
    for i in range(up["chunks"]):
        piece = PDF[i * up["chunk_bytes"]:(i + 1) * up["chunk_bytes"]]
        r = c.put(f"/api/v1/vendors/uploads/{up['upload_id']}/chunks/{i}", content=piece,
                  headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.text
    assert asyncio.run(ST.StagedUpload(up["upload_id"], USER).read()) == PDF


def test_every_file_endpoint_offers_the_upload_id():
    schema = FastAPI()
    schema.include_router(VR.router)
    paths = schema.openapi()["paths"]
    single = ["/api/v1/vendors/{vendor_id}/documents/extract",
              "/api/v1/vendors/{vendor_id}/documents/upload-and-save",
              "/api/v1/vendors/{vendor_id}/documents/{doc_id}/append-file",
              "/api/v1/vendors/{vendor_id}/contracts/extract",
              "/api/v1/vendors/{vendor_id}/contracts/upload-and-save",
              "/api/v1/vendors/{vendor_id}/contracts/{contract_id}/append-file"]
    multi = ["/api/v1/vendors/extract-bulk", "/api/v1/vendors/with-documents"]
    comps = schema.openapi()["components"]["schemas"]
    for path in single + multi:
        ref = paths[path]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
        props = comps[ref.rsplit("/", 1)[1]]["properties"]
        assert ("upload_id" if path in single else "upload_ids") in props, path
