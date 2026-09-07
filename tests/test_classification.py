from types import SimpleNamespace


def test_classification_facts_are_stable_and_normalized():
    from lunatvsource_test.classification import (
        CMS_CLASS_NAMES_FIELD,
        CMS_SOURCE_KEY_FIELD,
        CMS_TYPE_NAME_FIELD,
        extract_classification_facts,
        normalize_cms_class_names,
    )

    assert normalize_cms_class_names("动作, 科幻/动作；悬疑") == (
        "动作",
        "科幻",
        "悬疑",
    )
    result = SimpleNamespace(
        source_key="source-a",
        cms_type_name="电视剧",
        cms_class_names=("动作", "科幻"),
    )
    assert extract_classification_facts(result) == {
        CMS_SOURCE_KEY_FIELD: "source-a",
        CMS_TYPE_NAME_FIELD: "电视剧",
        CMS_CLASS_NAMES_FIELD: ["动作", "科幻"],
    }
