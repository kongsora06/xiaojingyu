from discord_dfy.sentences import chat_lines, sentences


def test_lines_are_cut_into_sentences():
    assert sentences("好呀。你呢？我刚吃完饭！") == ["好呀。", "你呢？", "我刚吃完饭！"]
    assert sentences("Really? 我不信") == ["Really?", "我不信"]
    assert chat_lines("嗯\n\n下班、") == ["嗯", "下班、"]


def test_word_less_bits_stay_with_a_sentence():
    assert sentences("真的吗？？～ 那太好了") == ["真的吗？？～", "那太好了"]
    assert sentences("？老板又发什么疯") == ["？老板又发什么疯"]
    assert sentences("……") == ["……"]
    assert sentences("晚安。(˶ᵔ ᵕ ᵔ˶)") == ["晚安。(˶ᵔ ᵕ ᵔ˶)"]


def test_links_quotes_and_prose_are_left_alone():
    assert sentences("看 https://a.com/?q=1 这个！好玩") == ["看 https://a.com/?q=1 这个！", "好玩"]
    assert sentences("他说：“走吧。”然后就走了") == ["他说：“走吧。”", "然后就走了"]
    long = "很长的一句话。" * 30
    assert sentences(long) == [long]
