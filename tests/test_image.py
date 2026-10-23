"""이미지(PNG·JPG) 파서: 글자 읽기 함수를 등록해야 읽고, 없으면 사용자에게 보여 줄 안내를 낸다."""

import zipfile

import pytest

from amigo_rag import ParseError, parse_file, set_image_reader, supported_extensions


@pytest.fixture(autouse=True)
def _reset_reader():
    yield
    set_image_reader(None)


def test_image_extensions_are_supported():
    assert {".png", ".jpg", ".jpeg"} <= supported_extensions()


def test_image_without_reader_explains_why(tmp_path):
    path = tmp_path / "screen.png"
    path.write_bytes(b"\x89PNG fake")
    with pytest.raises(ParseError, match="읽을 수 없어요"):
        parse_file(path)


def test_image_text_becomes_blocks_and_reader_gets_media_type(tmp_path):
    seen = {}

    def reader(data, media_type):
        seen["args"] = (data, media_type)
        return "서버 점검 일정\n\n매월 둘째 주 화요일 02:00"

    set_image_reader(reader)
    path = tmp_path / "캡처.PNG"
    path.write_bytes(b"png-bytes")
    doc = parse_file(path)
    assert seen["args"] == (b"png-bytes", "image/png")
    assert doc.source_type == "image" and "둘째 주 화요일" in doc.text and doc.warnings == []


def test_image_without_text_warns_and_reader_errors_are_wrapped(tmp_path):
    path = tmp_path / "blank.jpg"
    path.write_bytes(b"jpg")
    set_image_reader(lambda data, media_type: "")
    assert parse_file(path).warnings == ["이미지에서 글자를 찾지 못했어요."]

    def broken(data, media_type):
        raise RuntimeError("Claude 호출에 실패했습니다(APIError).")

    set_image_reader(broken)
    with pytest.raises(ParseError, match="Claude 호출에 실패"):
        parse_file(path)


def test_oversized_image_is_rejected(tmp_path):
    set_image_reader(lambda data, media_type: "x")
    path = tmp_path / "big.png"
    path.write_bytes(b"0" * (5 * 1024 * 1024 + 1))
    with pytest.raises(ParseError, match="너무 커요"):
        parse_file(path)


def test_image_inside_zip_is_read(tmp_path):
    set_image_reader(lambda data, media_type: "압축 안의 화면 글자")
    archive = tmp_path / "자료.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("화면.png", b"png")
    doc = parse_file(archive)
    assert any("압축 안의 화면 글자" in item.text for item in doc.iter_all())
