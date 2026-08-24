"""Tests for beagle.bridges.chat_model — OllamaCloudChatModel."""

from __future__ import annotations

from beagle.bridges.chat_model import (
    OllamaCloudChatModel,
)


class TestChatModelImport:
    def test_import(self):
        pass


class TestChatModelInstantiation:
    def test_create_with_defaults(self):
        model = OllamaCloudChatModel(model_name="qwen2.5-coder:32b")
        assert model._model_name == "qwen2.5-coder:32b"

    def test_create_with_repr(self):
        model = OllamaCloudChatModel(model_name="test-model")
        assert "test-model" in repr(model)
