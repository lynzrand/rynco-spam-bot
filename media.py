"""Bounded local media extraction for the text-and-image classifier."""

import json
import math
import os
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from api import APIError, image_data

MEDIA_FORMATS = "mov,matroska,webm,avi,flv,mpeg,mpegts,ogg,mp3,wav,flac,aac,amr,gif,png_pipe,jpeg_pipe,webp_pipe,bmp_pipe,tiff_pipe"


@dataclass
class Inspection:
    images: list = field(default_factory=list)
    text: str = ""
    notes: list = field(default_factory=list)


def command(tool, args, timeout=30):
    """Never execute attachment text or log subprocess output containing user data."""
    executable = os.getenv(tool.upper().replace("-", "_") + "_PATH", tool)
    try:
        result = subprocess.run(
            [executable, *map(str, args)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
            env={**os.environ, "OMP_NUM_THREADS": "2", "LC_ALL": "C"},
        )
        return result.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        raise APIError(f"{tool} media extraction") from None


class Media:
    """Sample media instead of treating every non-photo attachment as unreadable."""

    def __init__(self, telegram):
        self.tg = telegram

    def inspect(self, kind, item):
        if kind == "sticker" and not item.get("is_video"):
            if item.get("is_animated"):
                thumbnail = item.get("thumbnail")
                if not thumbnail:
                    raise APIError("Animated sticker has no preview")
                return Inspection(
                    images=[
                        (
                            "Animated sticker preview",
                            self.tg.image(thumbnail["file_id"]),
                        )
                    ],
                    notes=[
                        "TGS sticker: only Telegram's preview is inspected, not every animation frame."
                    ],
                )
            return Inspection(
                images=[("Sticker image", self.tg.image(item["file_id"]))]
            )
        if item.get("file_size", 0) > 20 * 1024 * 1024:
            raise APIError("Media exceeds 20 MiB download limit")
        with tempfile.TemporaryDirectory(prefix="spam-media-") as directory:
            source = Path(directory) / "attachment"
            source.write_bytes(self.tg.download(item["file_id"]))
            if kind == "document":
                return self.document(source, item)
            return self.audiovisual(source, kind)

    def probe(self, source):
        try:
            info = json.loads(
                command(
                    "ffprobe",
                    [
                        "-v",
                        "error",
                        "-threads",
                        "2",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-format_whitelist",
                        MEDIA_FORMATS,
                        "-show_streams",
                        "-show_format",
                        "-of",
                        "json",
                        source,
                    ],
                )
            )
            duration = float(info.get("format", {}).get("duration", 0))
            if not math.isfinite(duration) or duration < 0:
                raise ValueError("Invalid duration")
            return info.get("streams", []), duration
        except (ValueError, TypeError, KeyError):
            raise APIError("Invalid media metadata") from None

    def ffmpeg(self, source, options, *, seek=None):
        command(
            "ffmpeg",
            [
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-threads",
                "2",
                "-filter_threads",
                "1",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                MEDIA_FORMATS,
                *(["-ss", str(seek)] if seek is not None else []),
                "-i",
                source,
                *options,
            ],
        )

    def audiovisual(self, source, kind):
        streams, duration = self.probe(source)
        result = Inspection()
        video = any(s.get("codec_type") == "video" for s in streams)
        audio = any(s.get("codec_type") == "audio" for s in streams)
        if any(s.get("width", 0) * s.get("height", 0) > 16000000 for s in streams):
            raise APIError("Media resolution exceeds extraction limit")
        if not video and not audio:
            raise APIError("Media has no decodable streams")
        if video:
            frames = 6 if duration > 1 else 1
            for index in range(frames):
                # Seek across the whole clip, rather than inspecting only its beginning.
                at = duration * index / frames
                target = source.parent / f"frame-{index}.jpg"
                self.ffmpeg(
                    source,
                    [
                        "-an",
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=1024:1024:force_original_aspect_ratio=decrease",
                        "-threads",
                        "2",
                        target,
                    ],
                    seek=at if duration else None,
                )
                if not target.exists():
                    raise APIError("Video frame unavailable")
                result.images.append(
                    (
                        f"{kind} sampled frame at {at:.2f}s",
                        image_data(target.read_bytes()),
                    )
                )
            result.notes.append(
                f"Visual sampling: {frames} frame(s) across {duration:.2f}s; intervening frames are not inspected."
            )
        if audio:
            result.text = self.transcribe(source)
            result.notes.append(
                "Speech transcription covers at most the first 120 seconds and may contain recognition errors."
            )
        return result

    def transcribe(self, source):
        model = Path(os.getenv("WHISPER_MODEL_PATH", "models/ggml-base.bin"))
        if not model.is_file():
            raise APIError("Local speech model unavailable")
        wav = source.parent / "speech.wav"
        self.ffmpeg(
            source,
            ["-vn", "-t", "120", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav],
        )
        output = source.parent / "transcript"
        command(
            "whisper-cli",
            [
                "-m",
                model.resolve(),
                "-f",
                wav,
                "-t",
                "2",
                "-p",
                "1",
                "-l",
                "auto",
                "-ng",
                "-otxt",
                "-of",
                output,
                "-nt",
                "-np",
            ],
            timeout=90,
        )
        transcript = output.with_suffix(".txt")
        if not transcript.exists():
            raise APIError("Speech transcript unavailable")
        return transcript.read_text(encoding="utf-8")[:16000]

    def document(self, source, item):
        mime = item.get("mime_type", "")
        suffix = Path(item.get("file_name", "")).suffix.lower()
        if mime == "application/pdf" or suffix == ".pdf":
            return self.pdf(source)
        if suffix in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
            return self.office(source)
        if mime.startswith("text/") or suffix in {
            ".txt",
            ".md",
            ".csv",
            ".json",
            ".xml",
            ".html",
            ".log",
        }:
            data = source.read_bytes()
            try:
                text = data.decode(
                    "utf-16"
                    if data.startswith((b"\xff\xfe", b"\xfe\xff"))
                    else "utf-8-sig"
                )
            except UnicodeError:
                raise APIError("Document text encoding unsupported") from None
            return Inspection(
                text=text[:16000],
                notes=[
                    "Document text: at most 16000 characters; markup is untrusted text."
                ],
            )
        if mime.startswith(("image/", "video/", "audio/")) or suffix in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
            ".mp4",
            ".webm",
            ".mp3",
            ".ogg",
            ".wav",
        }:
            return self.audiovisual(source, "document")
        raise APIError("Document format unsupported")

    def pdf(self, source):
        info = command("pdfinfo", [source])
        try:
            count = int(
                next(
                    line.split(":", 1)[1]
                    for line in info.splitlines()
                    if line.startswith("Pages:")
                )
            )
        except (StopIteration, ValueError):
            raise APIError("PDF page count unavailable") from None
        if count < 1:
            raise APIError("PDF has no readable pages")
        result = Inspection(
            notes=[f"PDF: first {min(count, 6)} of {count} pages inspected."]
        )
        for page in range(1, min(count, 6) + 1):
            prefix = source.parent / f"page-{page}"
            command(
                "pdftoppm",
                [
                    "-f",
                    page,
                    "-l",
                    page,
                    "-singlefile",
                    "-scale-to",
                    "1280",
                    "-png",
                    source,
                    prefix,
                ],
            )
            result.images.append(
                (
                    f"Document page {page}",
                    image_data(prefix.with_suffix(".png").read_bytes()),
                )
            )
        return result

    def office(self, source):
        result = Inspection(
            notes=[
                "Office document: extracted XML text and up to six embedded images; layout, macros, and external links are not executed."
            ]
        )
        try:
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > 2000
                    or sum(e.file_size for e in entries) > 32 * 1024 * 1024
                ):
                    raise APIError("Office document exceeds extraction limit")
                texts = []
                for entry in entries:
                    name = entry.filename
                    if name.endswith(".xml") and (
                        name.startswith(("word/", "xl/", "ppt/slides/"))
                        or name == "content.xml"
                    ):
                        root = ElementTree.fromstring(archive.read(entry))
                        texts.extend(text for text in root.itertext() if text.strip())
                    if len(result.images) < 6 and (
                        "/media/" in name or name.startswith("Pictures/")
                    ):
                        try:
                            result.images.append(
                                (
                                    "Embedded document image",
                                    image_data(archive.read(entry)),
                                )
                            )
                        except APIError:
                            result.notes.append(
                                "An embedded image has an unsupported format."
                            )
                result.text = " ".join(texts)[:16000]
                if not result.text and not result.images:
                    raise APIError("Office document has no readable content")
        except (zipfile.BadZipFile, ElementTree.ParseError, RuntimeError):
            raise APIError("Office document extraction") from None
        return result
