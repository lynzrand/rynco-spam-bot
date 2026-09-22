"""Optional live classifier smoke tests using synthetic data only (uses API quota)."""

import base64
import json
import os
import struct
import zlib

from api import Classifier


def color_swatch():
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 64, 64, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00\x00\xff" * 64) * 64))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


def main():
    classifier = Classifier(
        os.environ["OPENAI_BASE_URL"],
        os.environ["OPENAI_API_KEY"],
        os.environ["OPENAI_MODEL"],
        "General discussion; unsolicited advertising is not permitted.",
    )
    cases = [
        (
            "Chinese question without avatar",
            "大家好，请问这个项目怎么部署？",
            "clean",
            [],
            {},
        ),
        (
            "Chinese task scam",
            "高佣金兼职，点赞做任务日赚2000，先充值500领取任务，私聊我马上返利！",
            "spam",
            [],
            {},
        ),
        (
            "Scam warning",
            "注意骗局：有人用刷单返利骗押金，千万不要充值。",
            "clean",
            [],
            {},
        ),
        (
            "Image input",
            "Testing this plain color swatch.",
            "clean",
            [("Message image", color_swatch())],
            {},
        ),
        # Regression: a profile funnel with no second signal is a member vote, while a
        # funnel plus a contact channel is the hit-and-run ad pattern the bot bans.
        ("Profile funnel without bio", "看我简介", "spam", [], {}),
        ("Profile funnel in name", "你好", "spam", [], {"first_name": "看我简介"}),
        (
            "Advertising account greeting",
            "大家好",
            "spam",
            [],
            {"description": "专业网站建设，承接企业官网和商城开发，需要的私聊报价。"},
        ),
        (
            "Account selling message",
            "出 ChatGPT Plus 账号 270 一个 需要的私聊",
            "spam",
            [],
            {},
        ),
        (
            "Profile funnel warning",
            "遇到名字写着看我简介的广告号，请举报。",
            "clean",
            [],
            {},
        ),
    ]
    failures = 0
    for label, text, expected, images, profile in cases:
        result = classifier.classify(
            {
                "event": "message",
                "profile": {
                    "first_name": "Example",
                    "description": None,
                    "photo_status": "no_visible_photo",
                    **profile,
                },
                "message": {"text": text},
            },
            images,
        )
        print(label + ": " + json.dumps(result, ensure_ascii=False), flush=True)
        if result["verdict"] != expected:
            failures += 1
    print(f"{len(cases) - failures}/{len(cases)} expected verdicts matched.")
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
