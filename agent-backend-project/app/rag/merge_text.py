"""把切好的片段拼回一份连续文本（知识库阅读器用）。

**为什么需要它**：`knowledge_chunks` 是按字数递归切的，块与块之间可能有 `OVERLAP` 字符的
重叠（见 `app/rag/core.py`）。直接 `"\\n\\n".join(chunks)` 会让接缝处的句子出现两遍。

**为什么不是简单的"最长后缀匹配"**：文档里本来就会重复（比如源码清单里同一行出现两次），
贪心算法会把这种**正文里的重复**误判成重叠、然后把正文吃掉。实测就撞上过：

    ...聚簇索引的叶子节点存整行数据。 | 聚簇索引的叶子节点存整行数据。二级索引叶子存主键值。...

`merged` 的后缀既能对上 28 字、也能对上 19 字。取最长（28）正好是真正的重叠；但如果反过来
只对上 19 字，就等于把"聚簇索引的叶子节点存整行数据。"这段正文删掉了一段。

所以策略是：**找最长重叠，但如果有多个长度相近的候选，就认定"歧义"、改按段落拼接**。
宁可留下重复（用户能看出来是原文重复），也不要悄悄删掉正文。

> 注意：这里只还原**文本**，不还原排版。入库时 `_normalize` 会把连续换行压成空格，
> 段落边界与 PDF 里的样子本身就不一致；要看原样得用原文预览（COS 临时链接）。
> 新上传的资料有 `resources.content_text`（入库时存的完整全文），不需要走这里。
"""

from __future__ import annotations

# 判定为"重叠"的最短长度。太小会把正文当重叠吃掉；中文实词密度高，16 字已足够可信
# （实测真实切片里的重叠是 18~48 字，取 24 会漏掉 18 字那一处、在正文里留下重复句子）。
MIN_OVERLAP = 16
# 只有比它更长的重叠才会被白名单直接接受，不再查歧义（消除长接缝的额外开销）。
CONFIDENT_OVERLAP = 200
# 短重叠的歧义判据：(最长 - 次长) < 最长 / 该分母 视为歧义。
AMBIGUITY_RATIO = 4


def join_chunks(chunks: list[str]) -> str:
    """按顺序拼接片段，消除相邻片段之间**确定**的重叠。"""
    merged = ""
    for chunk in chunks:
        merged = append_chunk(merged, chunk)
    return merged


def _overlap_lengths(merged: str, text: str) -> list[int]:
    """所有"merged 的后缀 == text 的前缀"的长度，从大到小。"""
    max_check = min(len(merged), len(text))
    if max_check < MIN_OVERLAP:
        return []
    return [
        size
        for size in range(max_check, MIN_OVERLAP - 1, -1)
        if merged.endswith(text[:size])
    ]


def append_chunk(merged: str, chunk: str) -> str:
    """把 `chunk` 接到 `merged` 后面；重叠确定就消掉，有歧义就按段落拼。"""
    text = (chunk or "").strip()
    if not text:
        return merged
    if not merged:
        return text

    sizes = _overlap_lengths(merged, text)
    if sizes:
        best = sizes[0]
        # 足够长 → 直接信（真实重叠通常就是几百字的一段）
        if best >= CONFIDENT_OVERLAP:
            return merged + text[best:]
        # 短重叠 → 只有当它明显比次长候选长，才认为没有歧义
        if len(sizes) == 1 or (best - sizes[1]) >= max(1, best // AMBIGUITY_RATIO):
            return merged + text[best:]
        # 歧义：正文里本来就有重复，保守处理——不删任何字
    return f"{merged}\n\n{text}"
