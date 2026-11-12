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
        "General technical discussion. Only gambling and adult advertising is acted on.",
    )
    # Scope lock (2026-11-10): gambling and adult advertising only. Every other spam
    # family must come back clean, so those cases are as important as the ad cases.
    cases = [
        (
            "Chinese question without avatar",
            "大家好，请问这个项目怎么部署？",
            "clean",
            [],
            {},
        ),
        (
            "Gambling deposit ad",
            "新会员首存100送100，真人荷官百家乐，注册送彩金 t.me/bet888",
            "spam",
            [],
            {},
        ),
        (
            "Adult escort ad",
            "同城约炮，学生妹上门服务，加V详聊",
            "spam",
            [],
            {},
        ),
        (
            "Scam warning about gambling",
            "注意骗局：有人用博彩计划群骗押金，千万不要转账。",
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
        (
            "Gambling caption over an image",
            "六合彩内部资料 三期必中 加V领料",
            "spam",
            [("Message image", color_swatch())],
            {},
        ),
        # Out of scope since the 2026-11-10 retarget: ordinary spam families pass.
        (
            "Account selling message",
            "出 ChatGPT Plus 账号 270 一个 需要的私聊",
            "clean",
            [],
            {},
        ),
        (
            "Task scam message",
            "高佣金兼职，点赞做任务日赚2000，先充值500领取任务，私聊我马上返利！",
            "clean",
            [],
            {},
        ),
        (
            "Commercial profile greeting",
            "大家好",
            "clean",
            [],
            {"description": "专业网站建设，承接企业官网和商城开发，需要的私聊报价。"},
        ),
        # In scope, profile-carried: two classes, one of them gambling wording.
        (
            "Gambling profile greeting",
            "大家好",
            "spam",
            [],
            {"description": "专业带计划，稳定盈利，加V详聊"},
        ),
        (
            "Redirect-only profile",
            "你好",
            "clean",
            [],
            {"first_name": "看我简介", "description": "喜欢旅行和摄影"},
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
