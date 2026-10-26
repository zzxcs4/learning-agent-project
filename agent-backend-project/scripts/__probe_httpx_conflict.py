"""一次性排查：定位 `httpx2` 与 `httpx` 混用导致断言崩溃的确切位置（用完即删）。"""

from __future__ import annotations

import sys
import traceback

sys.dont_write_bytecode = True


def main() -> int:
    import httpx
    import httpx2
    import openai

    print("openai:", openai.__version__)
    print("openai._base_client 用的库:", openai._base_client.httpx2.__name__,
          getattr(openai._base_client.httpx2, "__version__", "?"))
    print("httpx:", httpx.__version__, "| httpx2:", httpx2.__version__)
    print()

    print("[A] 裸 OpenAI SDK（自己建客户端，走 httpx2）")
    client = openai.OpenAI(api_key="sk-" + "a" * 40, base_url="https://api.deepseek.com", max_retries=0)
    try:
        client.chat.completions.create(
            model="deepseek-chat", messages=[{"role": "user", "content": "ping"}], max_tokens=8
        )
        print("    成功（意外）")
    except Exception as exc:
        print(f"    {type(exc).__name__}: {str(exc)[:90]}  <- 能连到服务端，链路本身没问题")

    print()
    print("[B] 走 langchain-openai（项目主路径：agent loop / 出题 / 单轮聊天都用它）")
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model="deepseek-chat",
        api_key="sk-" + "a" * 40,
        base_url="https://api.deepseek.com",
        streaming=True,
        max_tokens=16,
        max_retries=0,
    )
    try:
        for _ in llm.stream("ping"):
            pass
        print("    成功（意外）")
    except Exception as exc:
        print(f"    {type(exc).__name__}: {str(exc)[:120]}")
        frames = traceback.format_exc().strip().splitlines()
        print("    关键帧：")
        for line in frames:
            if "site-packages" in line and ("langchain" in line or "httpx" in line or "openai" in line):
                print("      " + line.strip()[:150])
            if "assert" in line:
                print("      " + line.strip()[:150])

    print()
    print("[C] 两个库的 SyncByteStream 是**不同的类**（这就是断言失败的根因）")
    print("    httpx.SyncByteStream :", httpx.SyncByteStream, id(httpx.SyncByteStream))
    print("    httpx2.SyncByteStream:", httpx2.SyncByteStream, id(httpx2.SyncByteStream))
    print("    同一个类吗:", httpx.SyncByteStream is httpx2.SyncByteStream)
    return 0


if __name__ == "__main__":
    sys.exit(main())
