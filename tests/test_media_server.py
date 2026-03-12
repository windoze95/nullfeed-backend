from pathlib import Path

from app.services.media_server import build_range_response


def test_build_range_response_supports_suffix_byte_range(tmp_path: Path):
    file_path = tmp_path / "sample.bin"
    content = b"0123456789"
    file_path.write_bytes(content)

    response = build_range_response(str(file_path), "bytes=-4")

    assert response.status_code == 206
    assert response.body == b"6789"
    assert response.headers["content-range"] == "bytes 6-9/10"
    assert response.headers["content-length"] == "4"


def test_build_range_response_keeps_standard_range_behavior(tmp_path: Path):
    file_path = tmp_path / "sample.bin"
    content = b"0123456789"
    file_path.write_bytes(content)

    response = build_range_response(str(file_path), "bytes=2-5")

    assert response.status_code == 206
    assert response.body == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["content-length"] == "4"
