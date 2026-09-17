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


def _disable_nest_asyncio() -> None:
    """2026-09-18 발견: ragas.executor가 import 시점에 `nest_asyncio.apply()`를 호출하는데, 이
    패치가 Python 3.14의 강화된 `asyncio.timeout()`(3.12부터 `asyncio.wait_for` 내부 구현이 이걸
    쓴다) 태스크 컨텍스트 추적과 충돌해 `RuntimeError: Timeout should be used inside a task`를
    낸다(직접 재현: nest_asyncio.apply() 적용 후 `asyncio.wait_for(task, timeout=...)`를 부르면
    100% 재현됨, 미적용 시 정상).

    nest_asyncio는 "이미 실행 중인 이벤트 루프 안에서 또 asyncio.run()을 부를 수 있게" 해주는
    패치인데(Jupyter 등에서 필요), 우리는 evaluation/run_ragas.py를 평범한 동기 스크립트에서
    한 번만 실행하므로 애초에 재진입이 필요 없다 — 그래서 `nest_asyncio.apply`를 아무 일도 안 하는
    함수로 바꿔치기해, ragas.executor가 import 시점에 호출해도 실제로는 아무 것도 패치되지 않게
    한다. 반드시 `ragas.executor`가 import되기 전에 적용해야 한다(모듈 레벨에서 한 번만
    `nest_asyncio.apply()`를 부르므로).
    """
    import nest_asyncio

    nest_asyncio.apply = lambda *args, **kwargs: None


def apply() -> None:
    _disable_nest_asyncio()

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
