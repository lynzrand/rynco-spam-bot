"""Media regressions and small real-decoder integration tests; no Telegram calls."""

import base64
import io
import shutil
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

from api import APIError, image_data
from media import Media, command
from smoke_test import color_swatch


class Files:
    def __init__(self, data):
        self.data = data
        self.requested = []

    def download(self, file_id, limit=20 * 1024 * 1024):
        self.requested.append(file_id)
        return self.data

    def image(self, file_id):
        return image_data(self.download(file_id))


def pdf_bytes():
    stream = b"BT /F1 18 Tf 20 100 Td (Example document) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    return (
        data
        + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.png = base64.b64decode(color_swatch().split(",")[1])
        self.files = Files(self.png)
        self.media = Media(self.files)

    def test_static_sticker_is_inspected_instead_of_flagged_unreadable(self):
        result = self.media.inspect(
            "sticker", {"file_id": "sticker", "is_animated": False}
        )
        self.assertEqual(result.images[0][0], "Sticker image")
        self.assertEqual(self.files.requested, ["sticker"])

    def test_animated_sticker_uses_preview_and_discloses_coverage(self):
        result = self.media.inspect(
            "sticker",
            {
                "file_id": "tgs",
                "is_animated": True,
                "thumbnail": {"file_id": "preview"},
            },
        )
        self.assertEqual(self.files.requested, ["preview"])
        self.assertIn("not every animation frame", result.notes[0])

    def test_missing_animated_preview_is_explicit_failure(self):
        with self.assertRaisesRegex(APIError, "no preview"):
            self.media.inspect("sticker", {"file_id": "tgs", "is_animated": True})

    def test_plain_text_document(self):
        self.files.data = "Hello 中文".encode()
        result = self.media.inspect(
            "document", {"file_id": "text", "file_name": "notes.txt"}
        )
        self.assertEqual(result.text, "Hello 中文")

    def test_office_document_text_and_embedded_image(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as out:
            out.writestr(
                "word/document.xml", "<document><text>Example 中文</text></document>"
            )
            out.writestr("word/media/image.png", self.png)
        self.files.data = archive.getvalue()
        result = self.media.inspect(
            "document", {"file_id": "office", "file_name": "notes.docx"}
        )
        self.assertEqual(result.text, "Example 中文")
        self.assertEqual(len(result.images), 1)

    def test_oversize_attachment_is_rejected_before_download(self):
        with self.assertRaisesRegex(APIError, "download limit"):
            self.media.inspect(
                "video", {"file_id": "big", "file_size": 21 * 1024 * 1024}
            )
        self.assertFalse(self.files.requested)

    def test_unknown_binary_is_not_executed(self):
        self.files.data = b"binary data"
        with self.assertRaisesRegex(APIError, "format unsupported"):
            self.media.inspect(
                "document", {"file_id": "binary", "file_name": "program.exe"}
            )

    def test_office_expansion_limit(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as out:
            out.writestr("word/document.xml", b" " * (33 * 1024 * 1024))
        self.files.data = archive.getvalue()
        with self.assertRaisesRegex(APIError, "extraction limit"):
            self.media.inspect(
                "document", {"file_id": "office", "file_name": "bomb.docx"}
            )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools not installed"
)
class DecoderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def generate(self, suffix, codec):
        target = self.root / ("fixture" + suffix)
        command(
            "ffmpeg",
            [
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=64x64:r=6:d=2",
                "-threads",
                "2",
                *codec,
                target,
            ],
        )
        return target.read_bytes()

    def test_video_and_video_note_frames(self):
        data = self.generate(".mp4", ["-c:v", "libx264"])
        for kind in ("video", "video_note"):
            with self.subTest(kind=kind):
                result = Media(Files(data)).inspect(kind, {"file_id": "video"})
                self.assertEqual(len(result.images), 6)
                self.assertIn("1.67s", result.images[-1][0])

    def test_webm_video_sticker(self):
        data = self.generate(".webm", ["-c:v", "libvpx-vp9"])
        result = Media(Files(data)).inspect(
            "sticker", {"file_id": "video-sticker", "is_video": True}
        )
        self.assertEqual(len(result.images), 6)

    def test_gif_and_image_document(self):
        data = self.generate(".gif", [])
        result = Media(Files(data)).inspect("animation", {"file_id": "gif"})
        self.assertEqual(len(result.images), 6)
        png = base64.b64decode(color_swatch().split(",")[1])
        result = Media(Files(png)).inspect(
            "document", {"file_id": "png", "mime_type": "image/png"}
        )
        self.assertEqual(len(result.images), 1)

    def test_playlists_cannot_reference_other_host_files(self):
        # Attachment decoders must not follow a playlist to files outside its temp dir.
        data = b"ffconcat version 1.0\nfile '/etc/passwd'\n"
        with self.assertRaisesRegex(APIError, "ffprobe media extraction"):
            Media(Files(data)).inspect("video", {"file_id": "playlist"})

    @unittest.skipUnless(
        shutil.which("pdftoppm") and shutil.which("pdfinfo"), "Poppler not installed"
    )
    def test_pdf_page_rendering(self):
        result = Media(Files(pdf_bytes())).inspect(
            "document", {"file_id": "pdf", "mime_type": "application/pdf"}
        )
        self.assertEqual(len(result.images), 1)
        self.assertTrue(result.images[0][1].startswith("data:image/png;base64,"))

    @unittest.skipUnless(
        shutil.which("whisper-cli") and Path("models/ggml-base.bin").is_file(),
        "Whisper and model not installed",
    )
    def test_voice_and_audio_transcription_pipeline(self):
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setparams((1, 2, 16000, 16000, "NONE", "not compressed"))
            wav.writeframes(b"\0" * 32000)
        for kind in ("voice", "audio"):
            with self.subTest(kind=kind):
                result = Media(Files(output.getvalue())).inspect(
                    kind, {"file_id": "silent-wav"}
                )
                self.assertIsInstance(result.text, str)
                self.assertIn("120 seconds", result.notes[0])


if __name__ == "__main__":
    unittest.main()
