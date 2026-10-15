"""ModelRouter：v1 直通单 provider；v2 模型分档入口（设计 §5）。

codebuddy 路返回 CodebuddyRunner（SDK 自驱执行引擎，非 chat 协议）；
openai 路返回 OpenAICompatProvider（LLMProvider 协议，AgentLoop 自驱）。
"""
from __future__ import annotations


class ModelRouter:
    """按 config.model.active_provider 返回唯一 provider。

    v2 扩展位：按任务-模型分派表返回不同档位 provider（级联升级/降级链）。
    """

    def __init__(self, config):
        self.config = config
        self._cache: dict[str, object] = {}

    def get(self, gateway=None, registry=None, prompter=None, store=None,
            memory_store=None):
        name = self.config.model.active_provider
        if name in self._cache:
            return self._cache[name]
        if name == "codebuddy":
            from axon.providers.codebuddy import CodebuddyRunner
            prov = CodebuddyRunner.from_config(self.config, gateway, registry,
                                               prompter, store, memory_store)
        elif name == "openai":
            from axon.providers.openai_compat import OpenAICompatProvider
            prov = OpenAICompatProvider.from_config(self.config)
        else:
            raise ValueError(f"未知 provider: {name}（可用: codebuddy | openai）")
        self._cache[name] = prov
        return prov

    def fallback_chain(self) -> list[str]:
        """降级链：v1 不静默切换，仅返回候选供用户确认。"""
        return [p for p in self.config.model.providers if p != self.config.model.active_provider]
