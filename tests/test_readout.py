import pytest

from azkims.readout import strip_name_runs


def strip(text: str, known: tuple[str, ...] = ()) -> str:
    return strip_name_runs(text, lambda clause: any(k in clause for k in known))


@pytest.mark.parametrize(
    "text",
    [
        "親方さん、マコールさん、宮本さん",
        "え、親方さん、え、マコールさん、宮本さん、ありがとうございます。",
        "親方さんマコールさん宮本さんネオンさんヒストさん",
    ],
)
def test_whole_utterance_is_a_readout(text):
    assert strip(text) == ""


def test_keeps_the_message_after_the_names():
    text = "え、高なさん、シャリーさん、し鉄ひろさん、ソロライブ絶対忘れない。"
    assert strip(text) == "え、ソロライブ絶対忘れない"


def test_keeps_the_talk_before_the_names():
    text = "ハンマーが不遇って言われてます、親方さん、マコールさん、宮本さん、ありがとう。"
    assert strip(text) == "ハンマーが不遇って言われてます"


@pytest.mark.parametrize(
    "text",
    [
        "親方さん、マコールさん",  # 2 人では読み上げと言えない
        "ピちゃん、ピちゃん、ピちゃん",  # さん ではない
        "すいちゃん、そらちゃん、ロボ子さん",
        "親方さん、親方さん、親方さん",  # 同じ名前の繰り返し
        "皆さん、みなさん、親方さん、お疲れ様でした",
        "ペコちゃんとコロさん冒険していいねいいね親方さんだよ",
    ],
)
def test_not_a_readout(text):
    assert strip(text) == text


def test_known_words_break_the_run():
    text = "そらさん、みこさん、すいせいさん"
    assert strip(text, known=("そら", "みこ")) == text
    assert strip(text) == ""


def test_unpunctuated_needs_five():
    text = "親方さんマコールさん宮本さん来てくれたよ"
    assert strip(text) == text
