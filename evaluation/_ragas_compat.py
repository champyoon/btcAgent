"""ragas(0.3.0)와 이 환경에 설치된 최신 langchain-community(0.4.2) 간의 호환성 문제를 우회합니다.

langchain-community가 최근 버전에서 VertexAI 통합을 별도 패키지(langchain-google-vertexai)로
분리하면서 `langchain_community.chat_models.vertexai`/`langchain_community.llms.VertexAI`가
사라졌는데, ragas 0.3.0의 `ragas/llms/base.py`는 여전히 이 경로를 import합니다. 저희는 VertexAI를
전혀 쓰지 않고 Bedrock만 쓰므로, import만 성공하면 되는 더미 심볼을 미리 `sys.modules`에 등록해
우회합니다. 반드시 `import ragas`보다 먼저 이 모듈을 import해야 합니다.
"""

from __future__ import annotations

import sys
import types


def apply() -> None:
    if "langchain_community.chat_models.vertexai" in sys.modules:
        return  # 이미 적용됨

    class _StubVertexAI:
        pass

    vertexai_chat_mod = types.ModuleType("langchain_community.chat_models.vertexai")
    vertexai_chat_mod.ChatVertexAI = _StubVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = vertexai_chat_mod

    import langchain_community.llms as _llms_mod

    if not hasattr(_llms_mod, "VertexAI"):
        _llms_mod.VertexAI = _StubVertexAI


apply()
