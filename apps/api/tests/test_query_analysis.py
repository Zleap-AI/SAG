"""Query analysis keeps Chinese lexical retrieval bounded and recoverable."""

import pytest

from sag_api.services.query_analysis import QueryAnalysis, analyze_query


def split_meat_soup(_text: str) -> list[str]:
    return ["肉类", "清汤"]


def test_contiguous_and_spaced_chinese_have_equivalent_core_analysis():
    contiguous = analyze_query("肉类清汤", segmenter=split_meat_soup)
    spaced = analyze_query("肉类 清汤", segmenter=lambda text: [text])

    expected = QueryAnalysis(
        normalized_phrase="肉类清汤",
        scoring_terms=("肉类", "清汤"),
        lookup_terms=("肉类", "清汤", "肉类清汤"),
        chinese_segmentation_used=True,
    )
    assert contiguous == expected
    assert spaced == expected


def test_analysis_filters_single_characters_numbers_duplicates_and_noise():
    result = analyze_query(
        "请问 A 123 肉类肉类是什么",
        segmenter=lambda _text: ["肉类", "肉类"],
    )

    assert result.normalized_phrase == "a123肉类肉类"
    assert result.scoring_terms == ("肉类",)
    assert result.lookup_terms == ("肉类", "a123肉类肉类")


def test_natural_question_prioritizes_topic_terms_within_lookup_budget():
    result = analyze_query(
        "如何制作肉类清汤",
        segmenter=lambda _text: ["如何", "制作", "肉类", "清汤"],
    )

    assert result.scoring_terms == ("制作", "肉类", "清汤")
    assert result.lookup_terms == ("制作", "肉类", "清汤", "如何制作肉类清汤")


def test_disabled_segmentation_uses_legacy_regex_terms():
    result = analyze_query("肉类清汤", segmentation_enabled=False)

    assert result.lookup_terms == ("肉类清汤",)
    assert result.scoring_terms == ("肉类清汤",)
    assert result.chinese_segmentation_used is False


def test_segmenter_failure_uses_legacy_regex_terms():
    def broken(_text: str) -> list[str]:
        raise RuntimeError("tokenizer unavailable")

    result = analyze_query("肉类清汤", segmenter=broken)

    assert result.lookup_terms == ("肉类清汤",)
    assert result.scoring_terms == ("肉类清汤",)
    assert result.chinese_segmentation_used is False


def test_lookup_terms_are_deduplicated_and_capped_at_four():
    result = analyze_query(
        "甲乙丙丁戊己庚辛",
        segmenter=lambda _text: ["甲乙", "丙丁", "戊己", "庚辛"],
    )

    assert result.lookup_terms == ("甲乙", "丙丁", "戊己", "庚辛")


def test_glued_english_digit_query_yields_separate_scoring_terms():
    result = analyze_query("AI2027", segmentation_enabled=False)

    assert result.scoring_terms == ("ai", "2027")
    assert result.lookup_terms == ("ai", "2027", "ai2027")


def test_glued_query_keeps_the_exact_token_as_a_lookup_term():
    result = analyze_query("iPhone15", segmentation_enabled=False)

    assert result.scoring_terms == ("iphone", "15")
    assert result.lookup_terms == ("iphone", "15", "iphone15")


def test_complex_model_identifier_is_not_split_into_numeric_fragments():
    segments = {
        "冰箱型号为": ["冰箱", "型号"],
        "的温度最低是": ["温度", "最低"],
        "吗": ["吗"],
    }

    result = analyze_query(
        "冰箱型号为dw-2412p30的温度最低是-200°吗？",
        segmenter=lambda text: segments[text],
    )

    assert "dw-2412p30" in result.scoring_terms
    assert "dw-2412p30" in result.lookup_terms
    assert "2412" not in result.lookup_terms
    assert "30" not in result.lookup_terms
    assert "200" not in result.lookup_terms


def test_punctuation_separated_english_term_is_not_split():
    result = analyze_query("foo-bar", segmentation_enabled=False)

    assert result.scoring_terms == ("foo-bar",)
    assert result.lookup_terms == ("foo-bar",)


def test_punctuation_separated_number_is_not_treated_as_alnum():
    result = analyze_query("2024.10", segmentation_enabled=False)

    assert result.scoring_terms == ()
    assert result.lookup_terms == ()


def test_mixed_chinese_and_glued_english_digit_splits_both():
    result = analyze_query(
        "大模型GPT4发布",
        segmenter=lambda _text: ["大模型", "发布"],
    )

    assert "gpt" in result.scoring_terms
    assert "gpt" in result.lookup_terms


def test_polite_prefix_does_not_spend_topic_budget():
    result = analyze_query("麻烦你可以帮忙帮我看看 DW-2412P30 不支持联网的温度说明")
    assert "dw-2412p30" in result.scoring_terms
    assert "不支持" in result.normalized_phrase
    assert not {"麻烦", "可以", "帮忙", "看看"}.intersection(result.scoring_terms)
    assert any("温度" in term for term in result.lookup_terms)


def test_english_polite_prefix_keeps_model_and_negation():
    result = analyze_query("Could you please help me find Qwen3 without networking temperature")
    assert "qwen" in result.lookup_terms
    assert "without" in result.scoring_terms
    assert not {"could", "you", "please", "help", "me"}.intersection(result.scoring_terms)


def test_politeness_removal_does_not_damage_topic_entities():
    result = analyze_query("请帮我看看申请流程与DW-2412P30不联网的说明")
    assert "申请流程" in result.normalized_phrase
    assert "dw2412p30不联网" in result.normalized_phrase


@pytest.mark.parametrize("query", ["请假流程", "请帮我看看请假流程", "麻烦你帮我查一下请假流程"])
def test_leave_request_entity_survives_direct_and_polite_queries(query):
    result = analyze_query(query, segmentation_enabled=False)
    assert result.normalized_phrase == "请假流程"
    assert "请假流程" in result.lookup_terms


@pytest.mark.parametrize("query", ["Linux find command", "Could you please help me find Linux find command"])
def test_find_command_survives_direct_and_polite_queries(query):
    result = analyze_query(query, segmentation_enabled=False)
    assert result.normalized_phrase == "linuxfindcommand"
    assert result.scoring_terms == ("linux", "find", "command")


@pytest.mark.parametrize("query", ["find 命令用法", "do not find secret files"])
def test_find_topic_and_explicit_negation_are_preserved(query):
    result = analyze_query(query, segmentation_enabled=False)
    assert "find" in result.scoring_terms
    if query.startswith("do not"):
        assert "not" in result.scoring_terms
        assert result.normalized_phrase.startswith("donotfind")
