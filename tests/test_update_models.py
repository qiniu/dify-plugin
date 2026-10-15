"""``scripts/update_models.py`` 下架处理逻辑的回归测试。

背景：市场 API 会为已下架的模型返回 ``retirement_at``，但脚本此前完全忽略
该字段，导致插件不断收录（并置顶）已经无法调用的模型。

这里守护三点：

1. ``retirement_at`` 为空表示**仍在服务中**，不能被误判为已下架
   （``"" <= "2026-10-15"`` 为 True，是一个很容易踩的坑）；
2. 已下架的模型会生成 ``deprecated: true``；
3. ``_position.yaml`` 把已下架模型排在末尾，且在售模型按创建时间倒序。

运行方式::

    python -m unittest discover -s tests -v
"""

import importlib.util
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "update_models.py"

spec = importlib.util.spec_from_file_location("update_models", SCRIPT_PATH)
update_models = importlib.util.module_from_spec(spec)
sys.modules["update_models"] = update_models
spec.loader.exec_module(update_models)


def make_model(model_id, retirement_at="", created_time="", suggested_model=""):
    return {
        "id": model_id,
        "name": model_id.upper(),
        "retirement_at": retirement_at,
        "created_time": created_time,
        "suggested_model": suggested_model,
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
        "model_constraints": {"context_length": 65536},
        "support_api_protocols": ["openai"],
    }


TODAY = date(2026, 11, 20)


class ParseDateTests(unittest.TestCase):
    def test_plain_date(self):
        self.assertEqual(update_models.parse_date("2026-07-08"), date(2026, 7, 8))

    def test_iso_datetime_is_truncated(self):
        self.assertEqual(
            update_models.parse_date("2025-08-05T15:57:22+08:00"), date(2025, 8, 5)
        )

    def test_empty_or_invalid(self):
        for value in ("", "   ", None, "not-a-date", 123):
            self.assertIsNone(update_models.parse_date(value), repr(value))


class IsRetiredTests(unittest.TestCase):
    def test_empty_retirement_means_active(self):
        """核心回归：空字符串绝不能被判为已下架。"""
        self.assertFalse(update_models.is_retired(make_model("m"), today=TODAY))

    def test_past_retirement_is_retired(self):
        model = make_model("m", retirement_at="2026-07-08")
        self.assertTrue(update_models.is_retired(model, today=TODAY))

    def test_retirement_today_is_retired(self):
        model = make_model("m", retirement_at=TODAY.isoformat())
        self.assertTrue(update_models.is_retired(model, today=TODAY))

    def test_future_retirement_is_not_retired_yet(self):
        future = (TODAY + timedelta(days=30)).isoformat()
        model = make_model("m", retirement_at=future)
        self.assertFalse(update_models.is_retired(model, today=TODAY))
        self.assertEqual(update_models.get_retirement_date(model), TODAY + timedelta(days=30))

    def test_missing_field_entirely(self):
        self.assertFalse(update_models.is_retired({"id": "m"}, today=TODAY))


class GetSuggestedModelsTests(unittest.TestCase):
    def test_multiple_suggestions(self):
        model = make_model("m", suggested_model="qwen/qwen3.7-plus,qwen/qwen3.7-max")
        self.assertEqual(
            update_models.get_suggested_models(model),
            ["qwen/qwen3.7-plus", "qwen/qwen3.7-max"],
        )

    def test_empty(self):
        self.assertEqual(update_models.get_suggested_models(make_model("m")), [])


class GenerateModelYamlTests(unittest.TestCase):
    def test_active_model_has_no_deprecated_key(self):
        """在售模型不应写入 deprecated 字段，避免无谓 diff。"""
        config = update_models.generate_model_yaml(make_model("m"))
        self.assertNotIn("deprecated", config)

    def test_future_retirement_has_no_deprecated_key(self):
        future = (date.today() + timedelta(days=365)).isoformat()
        config = update_models.generate_model_yaml(
            make_model("m", retirement_at=future)
        )
        self.assertNotIn("deprecated", config)

    def test_retired_model_is_marked_deprecated(self):
        config = update_models.generate_model_yaml(
            make_model("m", retirement_at="2000-01-01")
        )
        self.assertTrue(config["deprecated"])

    def test_deprecated_key_order_is_stable(self):
        config = update_models.generate_model_yaml(
            make_model("m", retirement_at="2000-01-01")
        )
        self.assertEqual(
            list(config.keys()),
            [
                "model",
                "label",
                "model_type",
                "features",
                "model_properties",
                "parameter_rules",
                "deprecated",
            ],
        )


class PositionOrderingTests(unittest.TestCase):
    """_position.yaml 排序：在售在前、下架在末尾（组内新模型在前）。"""

    def _order(self, models):
        ordered = sorted(models, key=update_models.get_model_created_at, reverse=True)
        active = [m for m in ordered if not update_models.is_retired(m)]
        retired = [m for m in ordered if update_models.is_retired(m)]
        return [m["id"] for m in active + retired]

    def test_retired_models_are_pushed_to_the_end(self):
        models = [
            make_model("old-retired", retirement_at="2020-01-01", created_time="2025-09-01"),
            make_model("new-active", created_time="2026-01-01"),
            make_model("newer-retired", retirement_at="2021-01-01", created_time="2026-05-01"),
            make_model("old-active", created_time="2024-01-01"),
        ]
        self.assertEqual(
            self._order(models),
            ["new-active", "old-active", "newer-retired", "old-retired"],
        )

    def test_active_models_sorted_newest_first(self):
        models = [
            make_model("a", created_time="2024-01-01"),
            make_model("b", created_time="2026-01-01"),
            make_model("c", created_time="2025-01-01"),
        ]
        self.assertEqual(self._order(models), ["b", "c", "a"])


class CreatedAtFieldTests(unittest.TestCase):
    def test_uses_real_api_field_name(self):
        """历史 bug：脚本查找 created_at/created/creation_time，API 从未提供过。"""
        self.assertEqual(update_models.CREATED_AT_FIELD, "created_time")
        model = make_model("m", created_time="2025-08-05T15:57:22+08:00")
        self.assertEqual(
            update_models.get_model_created_at(model), "2025-08-05T15:57:22+08:00"
        )

    def test_missing_created_time_sorts_last(self):
        self.assertEqual(update_models.get_model_created_at({"id": "m"}), "")


class BumpPatchVersionTests(unittest.TestCase):
    """守护：manifest.yaml 的 version 行带行尾注释，正则必须兼容。"""

    MANIFEST = """\
meta:
  version: 0.0.1 # 插件元数据格式版本，保持不变，不要修改
  runner:
    language: python
    version: '3.12'

version: 0.2.11 # 插件实际版本号，发布时更新此版本
"""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.manifest = Path(self.tmp.name) / "manifest.yaml"
        self.manifest.write_text(self.MANIFEST, encoding="utf-8")
        self._original = update_models.MANIFEST_FILE
        update_models.MANIFEST_FILE = self.manifest

    def tearDown(self):
        update_models.MANIFEST_FILE = self._original
        self.tmp.cleanup()

    def test_parses_version_with_trailing_comment(self):
        self.assertEqual(update_models.get_manifest_version(), "0.2.11")

    def test_bump_increments_patch_and_keeps_comment(self):
        new_version = update_models.bump_patch_version()
        self.assertEqual(new_version, "0.2.12")
        content = self.manifest.read_text(encoding="utf-8")
        self.assertIn("version: 0.2.12 # 插件实际版本号，发布时更新此版本", content)

    def test_bump_never_touches_meta_version(self):
        """meta.version 必须保持 0.0.1 —— 这是插件元数据格式版本。"""
        update_models.bump_patch_version()
        content = self.manifest.read_text(encoding="utf-8")
        self.assertIn(
            "version: 0.0.1 # 插件元数据格式版本，保持不变，不要修改", content
        )
        self.assertNotIn("version: 0.0.2", content)

    def test_bump_never_touches_runner_version(self):
        update_models.bump_patch_version()
        self.assertIn("version: '3.12'", self.manifest.read_text(encoding="utf-8"))

    def test_repeated_bumps_are_idempotent_in_structure(self):
        update_models.bump_patch_version()
        update_models.bump_patch_version()
        self.assertEqual(update_models.get_manifest_version(), "0.2.13")


if __name__ == "__main__":
    unittest.main(verbosity=2)
